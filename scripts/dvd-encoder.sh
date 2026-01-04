#!/bin/bash
# DVD Encoder - Stage 2 of Pipeline
# Processes ONE pending ISO per run, encodes to MKV using HandBrake
# Run via cron/systemd timer every 15 minutes

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Configuration (overridden by config file)
HANDBRAKE_PRESET="${HANDBRAKE_PRESET:-Fast 1080p30}"
HANDBRAKE_QUALITY="${HANDBRAKE_QUALITY:-20}"
HANDBRAKE_FORMAT="${HANDBRAKE_FORMAT:-mkv}"
MIN_FILE_SIZE_MB="${MIN_FILE_SIZE_MB:-100}"

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

    log_info "[ENCODER] Title: '$sanitized_title', Year: '$year', ISO: $iso_path"

    # Verify ISO exists
    if [[ ! -f "$iso_path" ]]; then
        log_error "[ENCODER] ISO file not found: $iso_path"
        log_warn "[ENCODER] Removing orphaned state file"
        remove_state_file "$state_file"
        return 1
    fi

    # Generate Plex-friendly output filename (e.g., "The Matrix (1999).mkv")
    output_filename=$(generate_plex_filename "$sanitized_title" "$year" "$HANDBRAKE_FORMAT")
    output_path="${STAGING_DIR}/${output_filename}"

    # Update metadata with MKV path
    metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path" "$output_path")

    # Transition state: iso-ready -> encoding
    remove_state_file "$state_file"
    state_file_encoding=$(create_pipeline_state "encoding" "$sanitized_title" "$timestamp" "$metadata")

    log_info "[ENCODER] Starting HandBrake encode"
    log_info "[ENCODER] Input: $iso_path"
    log_info "[ENCODER] Output: $output_path"
    log_info "[ENCODER] Preset: $HANDBRAKE_PRESET, Quality: $HANDBRAKE_QUALITY"

    # Build HandBrake command
    local handbrake_cmd="HandBrakeCLI"
    handbrake_cmd+=" -i \"$iso_path\""
    handbrake_cmd+=" -o \"$output_path\""
    handbrake_cmd+=" --preset \"$HANDBRAKE_PRESET\""
    handbrake_cmd+=" -q \"$HANDBRAKE_QUALITY\""

    # Add main title selection if available
    if [[ -n "$main_title" ]]; then
        handbrake_cmd+=" -t \"$main_title\""
    fi

    # Add extra options if specified
    if [[ -n "${HANDBRAKE_EXTRA_OPTS:-}" ]]; then
        handbrake_cmd+=" $HANDBRAKE_EXTRA_OPTS"
    fi

    # Execute with retry logic
    local attempt=1
    local success=false

    while [[ $attempt -le $MAX_RETRIES ]]; do
        log_info "[ENCODER] Encode attempt $attempt/$MAX_RETRIES"

        if eval "$handbrake_cmd" >> "$LOG_FILE" 2>&1; then
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

    # Mark ISO as deletable (robust - handle missing file gracefully)
    local iso_deletable="${iso_path}.deletable"
    if [[ -f "$iso_path" ]]; then
        if mv "$iso_path" "$iso_deletable" 2>/dev/null; then
            log_info "[ENCODER] Marked ISO for cleanup: $iso_deletable"
        else
            log_warn "[ENCODER] Could not rename ISO to .deletable (may already be renamed)"
        fi
        # Also remove mapfile if it exists
        rm -f "${iso_path}.mapfile" 2>/dev/null
    else
        log_warn "[ENCODER] ISO file not found for cleanup (already deleted?): $iso_path"
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

main() {
    # Initialize
    init_logging || exit 1
    ensure_staging_dir

    log_info "[ENCODER] ==================== DVD Encoder Started ===================="

    # Try to acquire stage lock (non-blocking)
    if ! acquire_stage_lock "encoder"; then
        log_info "[ENCODER] Another encoder is already running, exiting"
        exit 0
    fi

    # Set up cleanup trap
    trap 'release_stage_lock "encoder"; log_info "[ENCODER] DVD Encoder stopped"' EXIT INT TERM

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
    log_info "[ENCODER] Processing oldest: $state_file"

    # Check disk space before encoding
    if ! check_disk_space "$STAGING_DIR"; then
        log_error "[ENCODER] Insufficient disk space, skipping encode"
        exit 1
    fi

    # Encode the ISO
    encode_iso "$state_file"
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        log_info "[ENCODER] Encoding completed successfully"
        local remaining=$(count_pending_state "iso-ready")
        local pending_transfer=$(count_pending_state "encoded-ready")
        log_info "[ENCODER] ISOs remaining: $remaining, Videos pending transfer: $pending_transfer"
    else
        log_error "[ENCODER] Encoding failed"
    fi

    exit $exit_code
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
