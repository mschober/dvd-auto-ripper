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

    log_info "[ARCHIVE] Processing: $iso_path"

    # Extract title and timestamp from filename
    # Format: TITLE-TIMESTAMP.iso
    local basename=$(basename "$iso_path" .iso)
    local title="${basename%-*}"
    local timestamp="${basename##*-}"

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
    local xz_path="${iso_path}.xz"

    # Step 1: Compress ISO
    log_info "[ARCHIVE] Step 1/4: Compressing ISO..."
    if ! compress_iso "$iso_path"; then
        log_error "[ARCHIVE] Compression failed"
        remove_state_file "$state_file_archiving"
        return 1
    fi

    local compression_end=$(date +%s)
    local compression_time=$((compression_end - compression_start))

    # Step 2: Generate PAR2 recovery files (if enabled)
    if [[ "$ENABLE_PAR2_RECOVERY" == "1" ]]; then
        log_info "[ARCHIVE] Step 2/4: Generating PAR2 recovery files..."
        if ! generate_recovery_files "$xz_path"; then
            log_warn "[ARCHIVE] PAR2 generation failed (continuing without recovery files)"
        fi
    else
        log_info "[ARCHIVE] Step 2/4: PAR2 recovery disabled, skipping..."
    fi

    # Step 3: Verify compressed file integrity
    log_info "[ARCHIVE] Step 3/4: Verifying compressed file integrity..."
    if ! verify_compressed_iso "$xz_path"; then
        log_error "[ARCHIVE] Integrity verification failed - compressed file may be corrupt"
        # Clean up corrupt archive
        rm -f "$xz_path" "${xz_path}"*.par2
        remove_state_file "$state_file_archiving"
        return 1
    fi

    # Step 4: Transfer to NAS (if configured)
    local nas_path=""
    if [[ -n "$NAS_ARCHIVE_PATH" ]]; then
        log_info "[ARCHIVE] Step 4/4: Transferring archive to NAS..."
        if transfer_archive_to_nas "$xz_path"; then
            nas_path="${NAS_ARCHIVE_PATH}/$(basename "$xz_path")"
            log_info "[ARCHIVE] Archive transferred to: $nas_path"

            # Clean up local .xz and par2 files if configured
            if [[ "$DELETE_LOCAL_XZ_AFTER_ARCHIVE" == "1" ]]; then
                log_info "[ARCHIVE] Removing local archive files..."
                rm -f "$xz_path"
                rm -f "${xz_path}"*.par2
            fi
        else
            log_error "[ARCHIVE] Transfer to NAS failed"
            # Keep local files for retry
            nas_path="(transfer failed - local files retained)"
        fi
    else
        log_info "[ARCHIVE] Step 4/4: No NAS_ARCHIVE_PATH configured, keeping local archive"
        nas_path="local:$xz_path"
    fi

    # Create .archived state file with complete metadata
    local archived_metadata
    archived_metadata=$(build_archive_metadata "$title" "$timestamp" "$iso_path" "$xz_path" "$nas_path" "$compression_time")

    remove_state_file "$state_file_archiving"
    create_pipeline_state "archived" "$title" "$timestamp" "$archived_metadata" > /dev/null

    # Delete original ISO if configured and transfer was successful
    if [[ "$DELETE_ISO_AFTER_ARCHIVE" == "1" ]] && [[ "$nas_path" != "(transfer failed - local files retained)" ]]; then
        log_info "[ARCHIVE] Removing original ISO: $iso_path"
        rm -f "$iso_path"

        # Also remove the .deletable marker
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
    if [[ -n "$NAS_ARCHIVE_PATH" ]]; then
        log_info "[ARCHIVE] NAS archive path: $NAS_ARCHIVE_PATH"
    else
        log_info "[ARCHIVE] NAS archive path: (not configured - local only)"
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
