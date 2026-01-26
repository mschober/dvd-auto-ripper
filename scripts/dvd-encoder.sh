#!/bin/bash
# DVD Encoder - Stage 2 of Pipeline
# Processes ONE pending ISO per run, encodes to MKV using HandBrake
# Run via cron/systemd timer every 15 minutes

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Set logging stage for per-stage log routing
CURRENT_STAGE="encoder"

# Configuration (overridden by config file)
HANDBRAKE_PRESET="${HANDBRAKE_PRESET:-Fast 1080p30}"
HANDBRAKE_QUALITY="${HANDBRAKE_QUALITY:-20}"
HANDBRAKE_FORMAT="${HANDBRAKE_FORMAT:-mkv}"
MIN_FILE_SIZE_MB="${MIN_FILE_SIZE_MB:-100}"

# Preview generation settings
GENERATE_PREVIEWS="${GENERATE_PREVIEWS:-1}"
PREVIEW_DURATION="${PREVIEW_DURATION:-120}"
PREVIEW_START_PERCENT="${PREVIEW_START_PERCENT:-25}"
PREVIEW_RESOLUTION="${PREVIEW_RESOLUTION:-640:360}"

# ============================================================================
# DVD CSS Cache Cleanup
# ============================================================================

# Clear partial dvdcss cache directories to force fresh key cracking
#
# libdvdcss computes different disc IDs when reading from physical disc vs ISO:
#   - Physical disc: DVD_VIDEO-xxx-1762a2987d (random suffix)
#   - ISO file:      DVD_VIDEO-xxx-0000000000 (zeros suffix)
#
# This causes cache misses because Stage 1 (ISO creation) caches keys under
# the physical disc ID, but Stage 2 (encoding) looks under the ISO disc ID.
#
# Solution: Remove -0000000000 directories so libdvdcss cracks keys fresh.
# See docs/troubleshooting-dvdcss-cache.md for details.
clear_partial_dvdcss_cache() {
    local cache_dir="${DVDCSS_CACHE:-/var/cache/dvdcss}"

    if [[ ! -d "$cache_dir" ]]; then
        return 0
    fi

    # Find and remove directories ending with -0000000000 (ISO-derived, often incomplete)
    local removed=0
    while IFS= read -r -d '' dir; do
        log_debug "[ENCODER] Removing partial dvdcss cache: $dir"
        rm -rf "$dir"
        ((removed++)) || true
    done < <(find "$cache_dir" -maxdepth 1 -type d -name '*-0000000000' -print0 2>/dev/null)

    if [[ $removed -gt 0 ]]; then
        log_info "[ENCODER] Cleared $removed partial dvdcss cache director(ies)"
    fi
}

# Prepare dvdcss cache for encoding an ISO
# Imports packaged CSS keys if available, otherwise clears partial cache
# Usage: prepare_dvdcss_cache ISO_PATH
prepare_dvdcss_cache() {
    local iso_path="$1"

    # Try to import packaged keys first
    if import_dvdcss_keys "$iso_path"; then
        log_info "[ENCODER] Imported packaged CSS keys"
        return 0
    fi

    # Fall back to clearing partial cache (forces fresh key cracking)
    log_info "[ENCODER] No packaged keys, clearing partial cache"
    clear_partial_dvdcss_cache
}

# ============================================================================
# Preview Generation Function
# ============================================================================

# Generate a preview clip from the encoded MKV for identification
# Usage: generate_preview INPUT_MKV PREVIEW_PATH
# Returns: 0 on success, 1 on failure
generate_preview() {
    local input_mkv="$1"
    local preview_path="$2"

    # Check if ffmpeg/ffprobe are available
    if ! command -v ffmpeg &>/dev/null || ! command -v ffprobe &>/dev/null; then
        log_warn "[ENCODER] ffmpeg/ffprobe not found, skipping preview generation"
        return 1
    fi

    log_info "[ENCODER] Generating preview clip..."

    # Get video duration using ffprobe
    local duration
    duration=$(ffprobe -v quiet -show_entries format=duration \
        -of csv=p=0 "$input_mkv" 2>/dev/null | cut -d. -f1)

    if [[ -z "$duration" ]] || [[ "$duration" -eq 0 ]]; then
        log_warn "[ENCODER] Could not determine video duration, skipping preview"
        return 1
    fi

    # Calculate start position (past intro/commercials)
    local start_time=$((duration * PREVIEW_START_PERCENT / 100))

    log_debug "[ENCODER] Video duration: ${duration}s, preview start: ${start_time}s"

    # Generate preview clip at low resolution
    if ffmpeg -ss "$start_time" -i "$input_mkv" \
        -t "$PREVIEW_DURATION" \
        -vf "scale=${PREVIEW_RESOLUTION}" \
        -c:v libx264 -preset veryfast -crf 28 \
        -c:a aac -b:a 64k \
        -movflags +faststart \
        -y "$preview_path" >> "$(get_stage_log_file)" 2>&1; then

        local preview_size=$(stat -c%s "$preview_path" 2>/dev/null || echo "0")
        local preview_size_mb=$((preview_size / 1024 / 1024))
        log_info "[ENCODER] Preview generated: ${preview_size_mb}MB at $preview_path"
        return 0
    else
        log_warn "[ENCODER] Preview generation failed"
        rm -f "$preview_path" 2>/dev/null
        return 1
    fi
}

# ============================================================================
# Encoding Function
# ============================================================================

encode_iso() {
    local state_file="$1"
    local metadata iso_path sanitized_title year timestamp main_title
    local output_filename output_path state_file_encoding

    log_info "[ENCODER] Processing: $state_file"

    # Read metadata from state file
    metadata=$(read_pipeline_state "$state_file")

    # Parse metadata
    sanitized_title=$(parse_json_field "$metadata" "title")
    year=$(parse_json_field "$metadata" "year")
    timestamp=$(parse_json_field "$metadata" "timestamp")
    main_title=$(parse_json_field "$metadata" "main_title")
    iso_path=$(parse_json_field "$metadata" "iso_path")

    local rip_method
    rip_method=$(parse_json_field "$metadata" "rip_method")

    log_info "[ENCODER] Title: '$sanitized_title', Year: '$year', ISO: $iso_path"

    # Verify rip exists (file for ddrescue, directory for dvdbackup)
    if ! verify_rip_exists "$iso_path"; then
        log_error "[ENCODER] Rip not found: $iso_path"
        log_warn "[ENCODER] Removing orphaned state file"
        remove_state_file "$state_file"
        return 1
    fi

    # Prepare CSS cache (only needed for ddrescue ISOs; dvdbackup handles CSS during rip)
    if [[ "$rip_method" != "dvdbackup" ]]; then
        prepare_dvdcss_cache "$iso_path"
    fi

    # Generate Plex-friendly output filename (e.g., "The Matrix (1999).mkv")
    output_filename=$(generate_plex_filename "$sanitized_title" "$year" "$HANDBRAKE_FORMAT")
    output_path="${STAGING_DIR}/${output_filename}"

    # Update metadata with MKV path
    metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path" "$output_path")

    # Add encoder slot to metadata for dashboard progress tracking
    if [[ -n "$ENCODER_SLOT" && "$ENCODER_SLOT" != "0" ]]; then
        metadata=$(echo "$metadata" | sed 's/}$/,\n  "encoder_slot": "'"$ENCODER_SLOT"'"\n}/')
    fi

    # Transition state: iso-ready -> encoding
    remove_state_file "$state_file"
    state_file_encoding=$(create_pipeline_state "encoding" "$sanitized_title" "$timestamp" "$metadata")

    log_info "[ENCODER] Starting HandBrake encode"
    log_info "[ENCODER] Input: $iso_path"
    log_info "[ENCODER] Output: $output_path"
    log_info "[ENCODER] Preset: $HANDBRAKE_PRESET, Quality: $HANDBRAKE_QUALITY"

    # Build HandBrake command
    # -i accepts both ISO files (ddrescue) and directories (dvdbackup)
    local handbrake_cmd="HandBrakeCLI"
    handbrake_cmd+=" -i \"$iso_path\""
    handbrake_cmd+=" -o \"$output_path\""
    handbrake_cmd+=" --preset \"$HANDBRAKE_PRESET\""
    handbrake_cmd+=" -q \"$HANDBRAKE_QUALITY\""

    if [[ -n "$main_title" ]]; then
        handbrake_cmd+=" -t \"$main_title\""
    else
        handbrake_cmd+=" --main-feature"
    fi

    if [[ -n "${HANDBRAKE_EXTRA_OPTS:-}" ]]; then
        handbrake_cmd+=" $HANDBRAKE_EXTRA_OPTS"
    fi

    # Execute with retry logic
    local attempt=1
    local success=false

    while [[ $attempt -le $MAX_RETRIES ]]; do
        log_info "[ENCODER] Encode attempt $attempt/$MAX_RETRIES"

        if eval "$handbrake_cmd" >> "$(get_stage_log_file)" 2>&1; then
            success=true
            break
        else
            log_error "[ENCODER] HandBrake failed on attempt $attempt"
            attempt=$((attempt + 1))

            if [[ $attempt -le $MAX_RETRIES ]]; then
                log_warn "[ENCODER] Retrying in ${RETRY_DELAY}s..."
                sleep "$RETRY_DELAY"
            fi
        fi
    done

    if [[ "$success" != "true" ]]; then
        log_error "[ENCODER] Encoding failed after $MAX_RETRIES attempts"

        # Cleanup partial output
        cleanup_files "$output_path"

        # Revert state back to iso-ready for future retry
        remove_state_file "$state_file_encoding"
        create_pipeline_state "iso-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null

        log_warn "[ENCODER] ISO returned to queue for retry"
        return 1
    fi

    # Verify output file size
    if ! verify_file_size "$output_path" "$MIN_FILE_SIZE_MB"; then
        log_error "[ENCODER] Output file verification failed"

        # Cleanup and revert
        cleanup_files "$output_path"
        remove_state_file "$state_file_encoding"
        create_pipeline_state "iso-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null

        log_warn "[ENCODER] ISO returned to queue for retry"
        return 1
    fi

    log_info "[ENCODER] Encoding successful"

    # Generate preview clip for identification (if enabled)
    local preview_path=""
    if [[ "${GENERATE_PREVIEWS}" == "1" ]]; then
        preview_path="${STAGING_DIR}/${sanitized_title}-${timestamp}.preview.mp4"
        if ! generate_preview "$output_path" "$preview_path"; then
            # Preview generation failed, continue without preview
            preview_path=""
        fi
    fi

    # Update metadata with preview path
    metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path" "$output_path" "$preview_path")

    # Create archive-ready marker (ISO remains unchanged for archival)
    local archive_marker="${iso_path}.archive-ready"
    if verify_rip_exists "$iso_path"; then
        local marker_content="{\"iso_path\": \"$iso_path\", \"marked_at\": \"$(date -Iseconds)\", \"title\": \"$sanitized_title\", \"timestamp\": \"$timestamp\"}"
        if echo "$marker_content" > "$archive_marker" 2>/dev/null; then
            chmod 664 "$archive_marker" 2>/dev/null || true
            log_info "[ENCODER] Marked ISO for archival: $archive_marker"
        else
            log_warn "[ENCODER] Could not create archive marker: $archive_marker"
        fi
    else
        log_warn "[ENCODER] ISO file not found for archive marking (already deleted?): $iso_path"
    fi

    # Transition state: encoding -> encoded-ready
    remove_state_file "$state_file_encoding"
    create_pipeline_state "encoded-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null

    local output_size=$(stat -c%s "$output_path" 2>/dev/null || echo "0")
    local output_size_mb=$((output_size / 1024 / 1024))
    log_info "[ENCODER] Encode complete: ${output_size_mb}MB"

    return 0
}

# ============================================================================
# Recovery Function
# ============================================================================

check_encoder_recovery() {
    log_info "[ENCODER] Checking for interrupted encoding operations..."

    # Check for interrupted encodes
    local interrupted=$(find_state_files "encoding")
    if [[ -n "$interrupted" ]]; then
        log_warn "[ENCODER] Found interrupted encoding"
        while IFS= read -r state_file; do
            log_info "[ENCODER] Checking interrupted encode: $state_file"

            # Read metadata
            local metadata=$(read_pipeline_state "$state_file")
            local mkv_path=$(parse_json_field "$metadata" "mkv_path")
            local sanitized_title=$(parse_json_field "$metadata" "title")
            local timestamp=$(parse_json_field "$metadata" "timestamp")

            # Check if MKV exists and is valid size
            if [[ -f "$mkv_path" ]] && verify_file_size "$mkv_path" "$MIN_FILE_SIZE_MB" 2>/dev/null; then
                log_info "[ENCODER] Found valid MKV from interrupted encode, transitioning to encoded-ready"
                remove_state_file "$state_file"
                create_pipeline_state "encoded-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null
            else
                log_info "[ENCODER] MKV invalid or missing, returning ISO to queue"
                # Clean up partial MKV
                [[ -f "$mkv_path" ]] && rm -f "$mkv_path"
                # Revert to iso-ready
                remove_state_file "$state_file"
                create_pipeline_state "iso-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null
            fi
        done <<< "$interrupted"
    fi
}

# ============================================================================
# Main Entry Point
# ============================================================================

# Global variable to track encoder slot (used in cleanup trap)
ENCODER_SLOT=""

cleanup_and_exit() {
    if [[ -n "$ENCODER_SLOT" ]]; then
        release_encoder_slot "$ENCODER_SLOT"
    fi
    log_info "[ENCODER] DVD Encoder stopped"
}

main() {
    # Initialize
    init_logging || exit 1
    ensure_staging_dir

    log_info "[ENCODER] ==================== DVD Encoder Started ===================="

    # Log parallel encoding status
    if [[ "${ENABLE_PARALLEL_ENCODING:-0}" == "1" ]]; then
        local active_encoders=$(count_active_encoders)
        log_info "[ENCODER] Parallel encoding enabled: max=$MAX_PARALLEL_ENCODERS, active=$active_encoders"
    fi

    # Try to acquire an encoder slot (supports both legacy and parallel modes)
    ENCODER_SLOT=$(acquire_encoder_slot)
    if [[ $? -ne 0 ]]; then
        if [[ "${ENABLE_PARALLEL_ENCODING:-0}" == "1" ]]; then
            log_info "[ENCODER] No encoder slots available or load too high, exiting"
        else
            log_info "[ENCODER] Another encoder is already running, exiting"
        fi
        exit 0
    fi

    # Set up cleanup trap
    trap cleanup_and_exit EXIT INT TERM

    if [[ "$ENCODER_SLOT" == "0" ]]; then
        log_debug "[ENCODER] Running in legacy single-encoder mode"
    else
        # Use per-slot log file for parallel encoding progress tracking
        export LOG_FILE_OVERRIDE="${LOG_DIR}/encoder-${ENCODER_SLOT}.log"
        log_info "[ENCODER] Acquired encoder slot $ENCODER_SLOT"
    fi

    # Check for recovery scenarios
    check_encoder_recovery

    # Find oldest iso-ready state file
    local state_file=$(find_oldest_state "iso-ready")

    if [[ -z "$state_file" ]]; then
        log_info "[ENCODER] No ISOs pending encoding"
        exit 0
    fi

    local pending=$(count_pending_state "iso-ready")
    log_info "[ENCODER] Found $pending ISO(s) pending encoding"

    # In parallel mode, try to claim the state file atomically
    if [[ "${ENABLE_PARALLEL_ENCODING:-0}" == "1" ]]; then
        # Try to claim the state file - another worker might have grabbed it
        local claimed_file
        claimed_file=$(claim_state_file "$state_file" "encoding")
        if [[ $? -ne 0 ]]; then
            log_info "[ENCODER] State file claimed by another worker, trying next..."
            # Try to find another available state file
            state_file=$(find_oldest_state "iso-ready")
            if [[ -z "$state_file" ]]; then
                log_info "[ENCODER] No more ISOs available"
                exit 0
            fi
            claimed_file=$(claim_state_file "$state_file" "encoding")
            if [[ $? -ne 0 ]]; then
                log_info "[ENCODER] Could not claim any state file, exiting"
                exit 0
            fi
        fi
        # Use the claimed file path for encoding
        log_info "[ENCODER] Processing claimed file: $claimed_file"
        # Note: encode_iso expects iso-ready file, but we've already transitioned
        # So we need to pass the .encoding file and modify the flow
        encode_iso_from_encoding "$claimed_file"
    else
        log_info "[ENCODER] Processing oldest: $state_file"
        # Check disk space before encoding
        if ! check_disk_space "$STAGING_DIR"; then
            log_error "[ENCODER] Insufficient disk space, skipping encode"
            exit 1
        fi
        # Encode the ISO (legacy mode - handles state transition itself)
        encode_iso "$state_file"
    fi

    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        log_info "[ENCODER] Encoding completed successfully"
        local remaining=$(count_pending_state "iso-ready")
        local pending_transfer=$(count_pending_state "encoded-ready")
        log_info "[ENCODER] ISOs remaining: $remaining, Videos pending transfer: $pending_transfer"

        # Trigger transfer if event-driven triggers enabled
        trigger_next_stage "encoded-ready"
    else
        log_error "[ENCODER] Encoding failed"
    fi

    exit $exit_code
}

# Encode from an already-claimed .encoding state file (parallel mode)
# Usage: encode_iso_from_encoding ENCODING_STATE_FILE
encode_iso_from_encoding() {
    local state_file="$1"
    local metadata iso_path sanitized_title year timestamp main_title
    local output_filename output_path

    log_info "[ENCODER] Processing claimed file: $state_file"

    # Check disk space before encoding
    if ! check_disk_space "$STAGING_DIR"; then
        log_error "[ENCODER] Insufficient disk space, skipping encode"
        # Revert state back to iso-ready
        local basename=$(basename "$state_file")
        local name_part="${basename%.encoding}"
        local old_metadata=$(read_pipeline_state "$state_file")
        remove_state_file "$state_file"
        local title=$(parse_json_field "$old_metadata" "title")
        local ts=$(parse_json_field "$old_metadata" "timestamp")
        create_pipeline_state "iso-ready" "$title" "$ts" "$old_metadata" > /dev/null
        return 1
    fi

    # Read metadata from state file
    metadata=$(read_pipeline_state "$state_file")

    # Parse metadata
    sanitized_title=$(parse_json_field "$metadata" "title")
    year=$(parse_json_field "$metadata" "year")
    timestamp=$(parse_json_field "$metadata" "timestamp")
    main_title=$(parse_json_field "$metadata" "main_title")
    iso_path=$(parse_json_field "$metadata" "iso_path")

    local rip_method
    rip_method=$(parse_json_field "$metadata" "rip_method")

    log_info "[ENCODER] Title: '$sanitized_title', Year: '$year', ISO: $iso_path"

    # Verify rip exists (file for ddrescue, directory for dvdbackup)
    if ! verify_rip_exists "$iso_path"; then
        log_error "[ENCODER] Rip not found: $iso_path"
        log_warn "[ENCODER] Removing orphaned state file"
        remove_state_file "$state_file"
        return 1
    fi

    # Prepare CSS cache (only needed for ddrescue ISOs; dvdbackup handles CSS during rip)
    if [[ "$rip_method" != "dvdbackup" ]]; then
        prepare_dvdcss_cache "$iso_path"
    fi

    # Generate Plex-friendly output filename
    output_filename=$(generate_plex_filename "$sanitized_title" "$year" "$HANDBRAKE_FORMAT")
    output_path="${STAGING_DIR}/${output_filename}"

    # Update metadata with MKV path
    metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path" "$output_path")

    # Add encoder slot to metadata for dashboard progress tracking
    if [[ -n "$ENCODER_SLOT" && "$ENCODER_SLOT" != "0" ]]; then
        metadata=$(echo "$metadata" | sed 's/}$/,\n  "encoder_slot": "'"$ENCODER_SLOT"'"\n}/')
    fi

    # Update the .encoding state file with new metadata
    echo "$metadata" > "$state_file"

    log_info "[ENCODER] Starting HandBrake encode"
    log_info "[ENCODER] Input: $iso_path"
    log_info "[ENCODER] Output: $output_path"
    log_info "[ENCODER] Preset: $HANDBRAKE_PRESET, Quality: $HANDBRAKE_QUALITY"

    # Build HandBrake command
    # -i accepts both ISO files (ddrescue) and directories (dvdbackup)
    local handbrake_cmd="HandBrakeCLI"
    handbrake_cmd+=" -i \"$iso_path\""
    handbrake_cmd+=" -o \"$output_path\""
    handbrake_cmd+=" --preset \"$HANDBRAKE_PRESET\""
    handbrake_cmd+=" -q \"$HANDBRAKE_QUALITY\""

    if [[ -n "$main_title" ]]; then
        handbrake_cmd+=" -t \"$main_title\""
    else
        handbrake_cmd+=" --main-feature"
    fi

    if [[ -n "${HANDBRAKE_EXTRA_OPTS:-}" ]]; then
        handbrake_cmd+=" $HANDBRAKE_EXTRA_OPTS"
    fi

    # Execute with retry logic
    local attempt=1
    local success=false

    while [[ $attempt -le $MAX_RETRIES ]]; do
        log_info "[ENCODER] Encode attempt $attempt/$MAX_RETRIES"

        if eval "$handbrake_cmd" >> "$(get_stage_log_file)" 2>&1; then
            success=true
            break
        else
            log_error "[ENCODER] HandBrake failed on attempt $attempt"
            attempt=$((attempt + 1))

            if [[ $attempt -le $MAX_RETRIES ]]; then
                log_warn "[ENCODER] Retrying in ${RETRY_DELAY}s..."
                sleep "$RETRY_DELAY"
            fi
        fi
    done

    if [[ "$success" != "true" ]]; then
        log_error "[ENCODER] Encoding failed after $MAX_RETRIES attempts"
        cleanup_files "$output_path"
        # Revert to iso-ready
        remove_state_file "$state_file"
        create_pipeline_state "iso-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null
        log_warn "[ENCODER] ISO returned to queue for retry"
        return 1
    fi

    # Verify output file size
    if ! verify_file_size "$output_path" "$MIN_FILE_SIZE_MB"; then
        log_error "[ENCODER] Output file verification failed"
        cleanup_files "$output_path"
        remove_state_file "$state_file"
        create_pipeline_state "iso-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null
        log_warn "[ENCODER] ISO returned to queue for retry"
        return 1
    fi

    log_info "[ENCODER] Encoding successful"

    # Generate preview clip
    local preview_path=""
    if [[ "${GENERATE_PREVIEWS}" == "1" ]]; then
        preview_path="${STAGING_DIR}/${sanitized_title}-${timestamp}.preview.mp4"
        if ! generate_preview "$output_path" "$preview_path"; then
            preview_path=""
        fi
    fi

    # Update metadata with preview path
    metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path" "$output_path" "$preview_path")

    # Create archive-ready marker (ISO remains unchanged for archival)
    local archive_marker="${iso_path}.archive-ready"
    if verify_rip_exists "$iso_path"; then
        local marker_content="{\"iso_path\": \"$iso_path\", \"marked_at\": \"$(date -Iseconds)\", \"title\": \"$sanitized_title\", \"timestamp\": \"$timestamp\"}"
        if echo "$marker_content" > "$archive_marker" 2>/dev/null; then
            chmod 664 "$archive_marker" 2>/dev/null || true
            log_info "[ENCODER] Marked ISO for archival: $archive_marker"
        else
            log_warn "[ENCODER] Could not create archive marker: $archive_marker"
        fi
    fi

    # Transition state: encoding -> encoded-ready
    remove_state_file "$state_file"
    create_pipeline_state "encoded-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null

    local output_size=$(stat -c%s "$output_path" 2>/dev/null || echo "0")
    local output_size_mb=$((output_size / 1024 / 1024))
    log_info "[ENCODER] Encode complete: ${output_size_mb}MB"

    return 0
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
