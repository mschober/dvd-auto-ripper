#!/bin/bash
# DVD Archive - Stage 4 of Pipeline
# Compresses ISOs for long-term archival storage
# Creates .xz compressed files with par2 recovery data
# Transfers archives to NAS and cleans up local files
# Run via cron/systemd timer every 30 minutes

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Set logging stage for per-stage log routing
CURRENT_STAGE="archive"

# Configuration (overridden by config file)
ENABLE_ISO_ARCHIVAL="${ENABLE_ISO_ARCHIVAL:-0}"
ISO_COMPRESSION_LEVEL="${ISO_COMPRESSION_LEVEL:-9}"
ISO_COMPRESSION_THREADS="${ISO_COMPRESSION_THREADS:-0}"
ENABLE_PAR2_RECOVERY="${ENABLE_PAR2_RECOVERY:-1}"
PAR2_REDUNDANCY_PERCENT="${PAR2_REDUNDANCY_PERCENT:-5}"
ISO_ARCHIVE_PATH="${ISO_ARCHIVE_PATH:-/var/lib/dvd/archives}"
NAS_ARCHIVE_PATH="${NAS_ARCHIVE_PATH:-}"
DELETE_ISO_AFTER_ARCHIVE="${DELETE_ISO_AFTER_ARCHIVE:-1}"
DELETE_LOCAL_XZ_AFTER_ARCHIVE="${DELETE_LOCAL_XZ_AFTER_ARCHIVE:-1}"

# Archive lock file
ARCHIVE_LOCK_FILE="${ARCHIVE_LOCK_FILE:-/run/dvd-ripper/archive.lock}"

# ============================================================================
# Archive Function
# ============================================================================

archive_iso() {
    local iso_path="$1"

    # Skip archival for dvdbackup directories (archival only applies to ISO files)
    if [[ -d "$iso_path" ]]; then
        log_info "[ARCHIVE] Skipping directory (dvdbackup rip, not an ISO file): $iso_path"
        return 0
    fi

    log_info "[ARCHIVE] Processing: $iso_path"

    # Handle both new marker files and legacy .iso.deletable files
    local filename=$(basename "$iso_path")
    local basename
    local title
    local timestamp

    # Check if this is a legacy .iso.deletable file
    if [[ "$filename" == *.iso.deletable ]]; then
        basename="${filename%.iso.deletable}"
        title="${basename%-*}"
        timestamp="${basename##*-}"
        log_info "[ARCHIVE] Legacy mode: processing .iso.deletable file"
    else
        # New mode: iso_path is the actual ISO, extract from filename
        basename="${filename%.iso}"
        title="${basename%-*}"
        timestamp="${basename##*-}"
    fi

    log_info "[ARCHIVE] Title: '$title', Timestamp: '$timestamp'"

    # Verify ISO exists
    if [[ ! -f "$iso_path" ]]; then
        log_error "[ARCHIVE] ISO file not found: $iso_path"
        return 1
    fi

    # Get ISO size for metadata
    local iso_size=$(stat -c%s "$iso_path" 2>/dev/null || echo "0")
    local iso_size_gb=$(awk "BEGIN {printf \"%.2f\", $iso_size / 1024 / 1024 / 1024}")
    log_info "[ARCHIVE] ISO size: ${iso_size_gb}GB"

    # Create archiving state file
    local archive_metadata=$(cat <<EOF
{
  "title": "$title",
  "timestamp": "$timestamp",
  "iso_path": "$iso_path",
  "iso_size_bytes": $iso_size,
  "started_at": "$(date -Iseconds)"
}
EOF
)
    local state_file_archiving
    state_file_archiving=$(create_pipeline_state "archiving" "$title" "$timestamp" "$archive_metadata")

    local compression_start=$(date +%s)
    local staging_xz_path="${iso_path}.xz"  # Initial compression in staging dir
    local xz_basename=$(basename "${iso_path%.iso*}.iso.xz")  # e.g., TITLE-1234567890.iso.xz
    local archive_xz_path="${ISO_ARCHIVE_PATH}/${xz_basename}"  # Final location

    # Ensure archive directory exists (for local archives when compression enabled)
    if [[ "${ENABLE_ISO_COMPRESS_FOR_ARCHIVAL:-0}" == "1" ]] && [[ ! -d "$ISO_ARCHIVE_PATH" ]]; then
        log_info "[ARCHIVE] Creating archive directory: $ISO_ARCHIVE_PATH"
        mkdir -p "$ISO_ARCHIVE_PATH"
        chmod 2775 "$ISO_ARCHIVE_PATH" 2>/dev/null || true
    fi

    # Branch based on compression setting
    local nas_path=""
    local xz_path=""
    local compression_time=0

    if [[ "${ENABLE_ISO_COMPRESS_FOR_ARCHIVAL:-0}" == "1" ]]; then
        # ====================================================================
        # COMPRESSED ARCHIVE PATH: Compress ISO, then transfer .xz to NAS
        # ====================================================================
        log_info "[ARCHIVE] Compression enabled - compressing before transfer"

        # Step 1: Compress ISO (in staging directory)
        log_info "[ARCHIVE] Step 1/5: Compressing ISO..."
        if ! compress_iso "$iso_path"; then
            log_error "[ARCHIVE] Compression failed"
            remove_state_file "$state_file_archiving"
            return 1
        fi

        local compression_end=$(date +%s)
        compression_time=$((compression_end - compression_start))

        # Step 2: Generate PAR2 recovery files (if enabled)
        if [[ "$ENABLE_PAR2_RECOVERY" == "1" ]]; then
            log_info "[ARCHIVE] Step 2/5: Generating PAR2 recovery files..."
            if ! generate_recovery_files "$staging_xz_path"; then
                log_warn "[ARCHIVE] PAR2 generation failed (continuing without recovery files)"
            fi
        else
            log_info "[ARCHIVE] Step 2/5: PAR2 recovery disabled, skipping..."
        fi

        # Step 3: Verify compressed file integrity
        log_info "[ARCHIVE] Step 3/5: Verifying compressed file integrity..."
        if ! verify_compressed_iso "$staging_xz_path"; then
            log_error "[ARCHIVE] Integrity verification failed - compressed file may be corrupt"
            # Clean up corrupt archive
            rm -f "$staging_xz_path" "${staging_xz_path}"*.par2
            remove_state_file "$state_file_archiving"
            return 1
        fi

        # Step 4: Move to local archive path (ISO_ARCHIVE_PATH)
        log_info "[ARCHIVE] Step 4/5: Moving to archive location..."
        if ! mv "$staging_xz_path" "$archive_xz_path" 2>/dev/null; then
            log_error "[ARCHIVE] Failed to move archive to: $archive_xz_path"
            rm -f "$staging_xz_path" "${staging_xz_path}"*.par2
            remove_state_file "$state_file_archiving"
            return 1
        fi
        log_info "[ARCHIVE] Archive moved to: $archive_xz_path"

        # Move PAR2 files to archive location too
        local par2_files
        par2_files=$(find "$(dirname "$staging_xz_path")" -maxdepth 1 -name "$(basename "$staging_xz_path")*.par2" 2>/dev/null || true)
        if [[ -n "$par2_files" ]]; then
            while IFS= read -r par2_file; do
                [[ -z "$par2_file" ]] && continue
                mv "$par2_file" "$ISO_ARCHIVE_PATH/" 2>/dev/null || log_warn "[ARCHIVE] Could not move PAR2 file: $par2_file"
            done <<< "$par2_files"
        fi

        # Step 5: Transfer compressed archive to remote NAS (if configured)
        xz_path="$archive_xz_path"
        if [[ -n "$NAS_ARCHIVE_PATH" ]]; then
            log_info "[ARCHIVE] Step 5/5: Transferring compressed archive to remote NAS..."
            if transfer_archive_to_nas "$archive_xz_path"; then
                nas_path="${NAS_ARCHIVE_PATH}/$(basename "$archive_xz_path")"
                log_info "[ARCHIVE] Archive transferred to: $nas_path"

                # Clean up local archive if configured (since remote has it)
                if [[ "$DELETE_LOCAL_XZ_AFTER_ARCHIVE" == "1" ]]; then
                    log_info "[ARCHIVE] Removing local archive files (remote copy exists)..."
                    rm -f "$archive_xz_path"
                    rm -f "${archive_xz_path}"*.par2
                fi
            else
                log_error "[ARCHIVE] Transfer to remote NAS failed"
                # Keep local files for retry
                nas_path="(transfer failed - local files retained at $archive_xz_path)"
            fi
        else
            log_info "[ARCHIVE] Step 5/5: No remote NAS configured, archive stored locally"
            nas_path="local:$archive_xz_path"
        fi
    else
        # ====================================================================
        # RAW ISO TRANSFER PATH: Transfer or move uncompressed ISO
        # ====================================================================
        log_info "[ARCHIVE] Compression disabled - archiving raw ISO"

        if [[ -n "$NAS_ARCHIVE_PATH" ]]; then
            # Remote NAS: Transfer raw ISO directly
            log_info "[ARCHIVE] Transferring ISO (${iso_size_gb}GB) to remote NAS..."
            if transfer_iso_to_nas "$iso_path"; then
                nas_path="${NAS_ARCHIVE_PATH}/$(basename "$iso_path")"
                log_info "[ARCHIVE] ISO transferred to: $nas_path"
            else
                log_error "[ARCHIVE] ISO transfer to NAS failed"
                nas_path="(transfer failed - ISO retained locally)"
                remove_state_file "$state_file_archiving"
                return 1
            fi
        elif [[ -n "$ISO_ARCHIVE_PATH" ]]; then
            # Local archive: Move raw ISO to archive directory
            log_info "[ARCHIVE] Moving ISO (${iso_size_gb}GB) to local archive..."

            # Ensure archive directory exists
            if [[ ! -d "$ISO_ARCHIVE_PATH" ]]; then
                log_info "[ARCHIVE] Creating archive directory: $ISO_ARCHIVE_PATH"
                mkdir -p "$ISO_ARCHIVE_PATH"
                chmod 2775 "$ISO_ARCHIVE_PATH" 2>/dev/null || true
            fi

            local archive_iso_path="${ISO_ARCHIVE_PATH}/$(basename "$iso_path")"
            if mv "$iso_path" "$archive_iso_path" 2>/dev/null; then
                nas_path="local:$archive_iso_path"
                log_info "[ARCHIVE] ISO moved to: $archive_iso_path"
            else
                log_error "[ARCHIVE] Failed to move ISO to archive"
                remove_state_file "$state_file_archiving"
                return 1
            fi
        else
            log_error "[ARCHIVE] No archive destination configured (set NAS_ARCHIVE_PATH or ISO_ARCHIVE_PATH)"
            remove_state_file "$state_file_archiving"
            return 1
        fi
    fi

    # Create .archived state file with complete metadata
    local archived_metadata
    archived_metadata=$(build_archive_metadata "$title" "$timestamp" "$iso_path" "$xz_path" "$nas_path" "$compression_time")

    remove_state_file "$state_file_archiving"
    create_pipeline_state "archived" "$title" "$timestamp" "$archived_metadata" > /dev/null

    # Delete original ISO if configured and archive was successful (local or remote)
    # Only delete if we have a valid archive location (not a transfer failure)
    local archive_successful=false
    if [[ "$nas_path" == "local:"* ]] || [[ "$nas_path" == "${NAS_ARCHIVE_PATH}/"* ]]; then
        archive_successful=true
    fi

    if [[ "$DELETE_ISO_AFTER_ARCHIVE" == "1" ]] && [[ "$archive_successful" == "true" ]]; then
        log_info "[ARCHIVE] Removing original ISO: $iso_path"
        rm -f "$iso_path"

        # Remove .archive-ready marker (new approach)
        local archive_marker="${iso_path}.archive-ready"
        if [[ -f "$archive_marker" ]]; then
            log_debug "[ARCHIVE] Removing archive marker: $archive_marker"
            rm -f "$archive_marker"
        fi

        # Legacy: remove .deletable marker if exists
        if [[ -f "${iso_path}.deletable" ]]; then
            rm -f "${iso_path}.deletable"
        fi

        # Remove mapfile if exists
        if [[ -f "${iso_path}.mapfile" ]]; then
            rm -f "${iso_path}.mapfile"
        fi

        # Remove keys directory if exists
        if [[ -d "${iso_path}.keys" ]]; then
            rm -rf "${iso_path}.keys"
        fi
    elif [[ "$archive_successful" != "true" ]]; then
        log_warn "[ARCHIVE] ISO not deleted - archive transfer failed, will retry later"
    fi

    log_info "[ARCHIVE] Archive complete: $title"
    log_info "[ARCHIVE] Location: $nas_path"
    log_info "[ARCHIVE] Compression time: ${compression_time}s"

    return 0
}

# ============================================================================
# Recovery Function
# ============================================================================

check_archive_recovery() {
    log_info "[ARCHIVE] Checking for interrupted archive operations..."

    # Check for interrupted archives
    local interrupted=$(find_state_files "archiving")
    if [[ -n "$interrupted" ]]; then
        log_warn "[ARCHIVE] Found interrupted archive operation(s)"
        while IFS= read -r state_file; do
            [[ -z "$state_file" ]] && continue
            log_info "[ARCHIVE] Checking interrupted archive: $state_file"

            # Read metadata
            local metadata=$(read_pipeline_state "$state_file")
            local iso_path=$(parse_json_field "$metadata" "iso_path")
            local title=$(parse_json_field "$metadata" "title")
            local timestamp=$(parse_json_field "$metadata" "timestamp")
            local xz_path="${iso_path}.xz"

            # Check what was completed
            if [[ -f "$xz_path" ]]; then
                # Compression completed - verify and try to continue
                log_info "[ARCHIVE] Found compressed file, verifying..."
                if verify_compressed_iso "$xz_path"; then
                    log_info "[ARCHIVE] Compressed file valid, attempting to complete archive"
                    # Remove archiving state and re-queue
                    remove_state_file "$state_file"
                    # Mark ISO as archivable again (it will be processed normally)
                else
                    log_warn "[ARCHIVE] Compressed file invalid, removing and re-queuing"
                    rm -f "$xz_path" "${xz_path}"*.par2
                    remove_state_file "$state_file"
                fi
            else
                # Compression was interrupted
                log_info "[ARCHIVE] Compression incomplete, re-queuing"
                remove_state_file "$state_file"
            fi
        done <<< "$interrupted"
    fi
}

# ============================================================================
# Lock Management
# ============================================================================

acquire_archive_lock() {
    if [[ -f "$ARCHIVE_LOCK_FILE" ]]; then
        local existing_pid=$(cat "$ARCHIVE_LOCK_FILE" 2>/dev/null)
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            log_debug "[ARCHIVE] Already locked by PID $existing_pid"
            return 1
        else
            log_info "[ARCHIVE] Stale lock file found, removing"
            rm -f "$ARCHIVE_LOCK_FILE"
        fi
    fi

    echo "$$" > "$ARCHIVE_LOCK_FILE"
    chmod 664 "$ARCHIVE_LOCK_FILE" 2>/dev/null || true
    log_debug "[ARCHIVE] Lock acquired with PID $$"
    return 0
}

release_archive_lock() {
    if [[ -f "$ARCHIVE_LOCK_FILE" ]]; then
        rm -f "$ARCHIVE_LOCK_FILE"
        log_debug "[ARCHIVE] Lock released"
    fi
}

# ============================================================================
# Main Entry Point
# ============================================================================

main() {
    # Initialize
    init_logging || exit 1
    ensure_staging_dir

    log_info "[ARCHIVE] ==================== DVD Archive Started ===================="

    # Check if archival is enabled
    if [[ "$ENABLE_ISO_ARCHIVAL" != "1" ]]; then
        log_info "[ARCHIVE] ISO archival disabled (ENABLE_ISO_ARCHIVAL != 1)"
        exit 0
    fi

    log_info "[ARCHIVE] Compression level: $ISO_COMPRESSION_LEVEL"
    log_info "[ARCHIVE] Threads: $ISO_COMPRESSION_THREADS (0=auto)"
    log_info "[ARCHIVE] PAR2 recovery: $ENABLE_PAR2_RECOVERY"
    log_info "[ARCHIVE] Local archive path: $ISO_ARCHIVE_PATH"
    if [[ -n "$NAS_ARCHIVE_PATH" ]]; then
        log_info "[ARCHIVE] Remote NAS archive path: $NAS_ARCHIVE_PATH"
    else
        log_info "[ARCHIVE] Remote NAS archive path: (not configured - local only)"
    fi

    # Acquire lock (only one archive process at a time due to high CPU usage)
    if ! acquire_archive_lock; then
        log_info "[ARCHIVE] Another archive process is running, exiting"
        exit 0
    fi

    # Set up trap to release lock on exit
    trap release_archive_lock EXIT

    # Check for recovery scenarios
    check_archive_recovery

    # Find archivable ISOs
    local archivable_isos
    archivable_isos=$(find_archivable_isos)

    if [[ -z "$archivable_isos" ]]; then
        log_info "[ARCHIVE] No ISOs pending archival"
        exit 0
    fi

    # Count pending
    local pending=$(echo "$archivable_isos" | wc -l)
    log_info "[ARCHIVE] Found $pending ISO(s) pending archival"

    # Process one ISO at a time (archival is CPU-intensive)
    # Use FIFO order - oldest first
    local iso_path
    iso_path=$(echo "$archivable_isos" | head -1)

    if [[ -n "$iso_path" ]]; then
        archive_iso "$iso_path"
        local exit_code=$?

        if [[ $exit_code -eq 0 ]]; then
            log_info "[ARCHIVE] Archive completed successfully"
        else
            log_error "[ARCHIVE] Archive failed"
        fi
    fi

    local remaining=$(find_archivable_isos | wc -l)
    log_info "[ARCHIVE] Archive run complete. ISOs remaining: $remaining"

    exit 0
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
