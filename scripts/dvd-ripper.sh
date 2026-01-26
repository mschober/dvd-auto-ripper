#!/bin/bash
# Main DVD Ripper Script
# Automated DVD ripping with HandBrake, duplicate detection, and NAS transfer

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Configuration (overridden by config file)
DVD_DEVICE="${DVD_DEVICE:-/dev/sr0}"
CREATE_ISO="${CREATE_ISO:-1}"
ENCODE_VIDEO="${ENCODE_VIDEO:-0}"
HANDBRAKE_QUALITY="${HANDBRAKE_QUALITY:-20}"
HANDBRAKE_ENCODER="${HANDBRAKE_ENCODER:-x265}"
HANDBRAKE_FORMAT="${HANDBRAKE_FORMAT:-mp4}"
MIN_FILE_SIZE_MB="${MIN_FILE_SIZE_MB:-100}"
NAS_ENABLED="${NAS_ENABLED:-0}"
# HANDBRAKE_EXTRA_OPTS="${HANDBRAKE_EXTRA_OPTS:---encoder-preset veryfast --vfr --no-dvdnav}"

# ============================================================================
# Main Ripping Function
# ============================================================================

rip_dvd() {
    local device="$1"
    local dvd_info title year main_title duration
    local output_filename output_path iso_path
    local state_file_ripping state_file_completed state_file_transferring
    local input_source="$device"

    log_info "Starting DVD rip process for device: $device"

    # Extract DVD information
    dvd_info=$(get_dvd_info "$device")
    if [[ $? -ne 0 ]]; then
        log_error "Failed to extract DVD information"
        return 1
    fi

    # Parse DVD info (format: title|year|main_title|duration)
    IFS='|' read -r title year main_title duration <<< "$dvd_info"

    log_info "DVD Info - Title: '$title', Year: '$year', Main Title: $main_title, Duration: $duration"

    # Check for duplicates
    if check_duplicate "$title" "$year"; then
        log_warn "Duplicate detected, aborting rip"
        eject_disc "$device"
        return 2
    fi

    # Set umask so output files are world-readable
    umask 022

    # Create state files
    local timestamp=$(date +%s)
    local sanitized_title=$(sanitize_filename "$title")

    # Step 1: Create ISO if enabled
    if [[ "$CREATE_ISO" == "1" ]]; then
        log_info "ISO creation enabled"
        iso_path="${STAGING_DIR}/${sanitized_title}-${timestamp}.iso"

        state_file_ripping=$(create_state_file "ripping" "$sanitized_title" "$timestamp")

        if create_iso "$device" "$iso_path"; then
            log_info "ISO created successfully: $iso_path"
            input_source="$iso_path"

            # Update state to completed ISO
            remove_state_file "$state_file_ripping"
            state_file_completed=$(create_state_file "completed" "$sanitized_title" "$timestamp")
        else
            log_error "ISO creation failed"
            cleanup_files "$iso_path" "$iso_path.mapfile" "$state_file_ripping"
            eject_disc "$device"
            return 1
        fi
    else
        log_info "ISO creation disabled, encoding directly from DVD"
    fi

    # Step 2: Encode video if enabled
    if [[ "$ENCODE_VIDEO" != "1" ]]; then
        log_info "Video encoding disabled, skipping HandBrake step"
        eject_disc "$device"
        return 0
    fi

    log_info "Video encoding enabled"

    # Generate output filename for encoded video
    output_filename=$(generate_filename "$title" "$year" "$HANDBRAKE_FORMAT")
    output_path="${STAGING_DIR}/${output_filename}"

    log_info "Output file: $output_path"

    # Create encoding state file
    if [[ "$CREATE_ISO" == "1" ]]; then
        remove_state_file "$state_file_completed"
    fi
    state_file_ripping=$(create_state_file "encoding" "$sanitized_title" "$timestamp")

    # Execute HandBrake encode
    log_info "Starting HandBrake encode (encoder: $HANDBRAKE_ENCODER, quality: $HANDBRAKE_QUALITY)"
    log_info "Input source: $input_source"

    local handbrake_cmd="HandBrakeCLI"
    handbrake_cmd+=" -i \"$input_source\""
    handbrake_cmd+=" -o \"$output_path\""
    handbrake_cmd+=" --format av_${HANDBRAKE_FORMAT}"
    handbrake_cmd+=" --encoder ${HANDBRAKE_ENCODER}"
    handbrake_cmd+=" --encoder-preset medium"
    handbrake_cmd+=" -q \"$HANDBRAKE_QUALITY\""

    # Add main title selection if available
    if [[ -n "$main_title" ]]; then
        handbrake_cmd+=" -t \"$main_title\""
    else
        handbrake_cmd+=" --main-feature"
    fi

    handbrake_cmd+=" --all-audio"
    handbrake_cmd+=" --all-subtitles"
    handbrake_cmd+=" --optimize"

    # Add extra options if specified
    if [[ -n "${HANDBRAKE_EXTRA_OPTS:-}" ]]; then
        handbrake_cmd+=" $HANDBRAKE_EXTRA_OPTS"
    fi

    # Execute with retry logic
    local rip_attempt=1
    local rip_success=false

    while [[ $rip_attempt -le $MAX_RETRIES ]]; do
        log_info "Rip attempt $rip_attempt/$MAX_RETRIES"

        # Run HandBrake (redirect output to log)
        if eval "$handbrake_cmd" >> "$LOG_FILE" 2>&1; then
            rip_success=true
            break
        else
            log_error "HandBrake failed on attempt $rip_attempt"
            rip_attempt=$((rip_attempt + 1))

            if [[ $rip_attempt -le $MAX_RETRIES ]]; then
                log_warn "Retrying in ${RETRY_DELAY}s..."
                sleep "$RETRY_DELAY"
            fi
        fi
    done

    if [[ "$rip_success" != "true" ]]; then
        log_error "Rip failed after $MAX_RETRIES attempts"
        cleanup_files "$output_path" "$state_file_ripping"
        eject_disc "$device"
        return 1
    fi

    # Verify output file
    if ! verify_file_size "$output_path" "$MIN_FILE_SIZE_MB"; then
        log_error "Output file verification failed"
        cleanup_files "$output_path" "$state_file_ripping"
        eject_disc "$device"
        return 1
    fi

    # Update state to completed
    remove_state_file "$state_file_ripping"
    state_file_completed=$(create_state_file "completed" "$sanitized_title" "$timestamp")

    log_info "Rip completed successfully"

    # Transfer to NAS
    if [[ "$NAS_ENABLED" == "1" ]] && [[ -n "$NAS_HOST" ]]; then
        log_info "Starting NAS transfer"

        # Update state to transferring
        remove_state_file "$state_file_completed"
        state_file_transferring=$(create_state_file "transferring" "$sanitized_title" "$timestamp")

        if transfer_to_nas "$output_path"; then
            log_info "NAS transfer completed successfully"

            # Cleanup local files
            cleanup_files "$output_path" "$state_file_transferring"
        else
            log_error "NAS transfer failed, keeping local file"
            remove_state_file "$state_file_transferring"
            # Don't cleanup output file - leave it for manual transfer
        fi
    else
        log_info "NAS transfer disabled or not configured, keeping file in staging directory"
        remove_state_file "$state_file_completed"
    fi

    # Eject disc
    eject_disc "$device"

    log_info "DVD rip process completed: $output_filename"
    return 0
}

# ============================================================================
# Recovery Function
# ============================================================================

# Check for and recover from interrupted operations
check_recovery() {
    log_info "Checking for interrupted operations..."

    # Check for interrupted rips
    local interrupted_rips=$(find_state_files "ripping")
    if [[ -n "$interrupted_rips" ]]; then
        log_warn "Found interrupted rip operations"
        while IFS= read -r state_file; do
            log_info "Cleaning up interrupted rip: $state_file"

            # Extract info from state filename
            local basename=$(basename "$state_file")
            # Format: .ripping-TITLE-TIMESTAMP

            # Find and remove partial output file
            # This is a simplified cleanup - in production might want to try resume
            local partial_file_pattern="${STAGING_DIR}/*.${HANDBRAKE_FORMAT}"
            find "$STAGING_DIR" -name "*.${HANDBRAKE_FORMAT}" -type f -mmin +60 -delete

            remove_state_file "$state_file"
        done <<< "$interrupted_rips"
    fi

    # Check for interrupted transfers
    local interrupted_transfers=$(find_state_files "transferring")
    if [[ -n "$interrupted_transfers" ]]; then
        log_warn "Found interrupted transfer operations"
        while IFS= read -r state_file; do
            log_info "Found interrupted transfer: $state_file"

            # Try to resume transfer if file still exists
            local basename=$(basename "$state_file")
            # Could implement retry logic here

            log_info "Manual intervention may be required for: $state_file"
        done <<< "$interrupted_transfers"
    fi
}

# ============================================================================
# Main Entry Point
# ============================================================================

main() {
    local device="${1:-$DVD_DEVICE}"

    # Initialize
    init_logging || exit 1
    ensure_staging_dir

    log_info "DVD Ripper started (device: $device)"

    # Acquire lock
    if ! acquire_lock; then
        log_error "Another instance is already running"
        exit 1
    fi

    # Set up cleanup trap
    trap 'release_lock; log_info "DVD Ripper stopped"' EXIT INT TERM

    # Check for recovery scenarios
    check_recovery

    # Wait for device to be ready
    if ! wait_for_device "$device" 30; then
        log_error "Device not ready or no disc present"
        exit 1
    fi

    # Verify it's a readable DVD
    if ! is_dvd_readable "$device"; then
        log_error "Device is not a readable DVD"
        exit 1
    fi

    # Check disk space before ripping
    if ! check_disk_space "$STAGING_DIR"; then
        log_error "Insufficient disk space, ejecting disc"
        eject_disc "$device"
        exit 1
    fi

    # Perform the rip
    rip_dvd "$device"
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        log_info "DVD rip completed successfully"
    elif [[ $exit_code -eq 2 ]]; then
        log_info "DVD rip skipped (duplicate)"
    else
        log_error "DVD rip failed"
    fi

    exit $exit_code
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
