#!/bin/bash
# DVD ISO Creator - Stage 1 of Pipeline
# Creates ISO from DVD using ddrescue, then ejects disc immediately
# Encoding happens later via dvd-encoder.sh (cron job)

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Set logging stage for per-stage log routing
CURRENT_STAGE="iso"

# Configuration (overridden by config file)
DVD_DEVICE="${DVD_DEVICE:-/dev/sr0}"

# ============================================================================
# ISO Creation Function
# ============================================================================

create_dvd_iso() {
    local device="$1"
    local dvd_info title year main_title duration
    local iso_path state_file_creating state_file_ready

    log_info "[ISO] Starting ISO creation for device: $device"

    # Extract DVD information
    dvd_info=$(get_dvd_info "$device")
    if [[ $? -ne 0 ]]; then
        log_error "[ISO] Failed to extract DVD information"
        return 1
    fi

    # Parse DVD info (format: title|year|main_title|duration)
    IFS='|' read -r title year main_title duration <<< "$dvd_info"

    log_info "[ISO] DVD Info - Title: '$title', Year: '$year', Main Title: $main_title, Duration: $duration"

    # Check for duplicates (check NAS if configured)
    if check_duplicate "$title" "$year"; then
        log_warn "[ISO] Duplicate detected, aborting ISO creation"
        eject_disc "$device"
        return 2
    fi

    # Set umask so output files are world-readable
    umask 022

    # Generate paths
    local timestamp=$(date +%s)
    local sanitized_title=$(sanitize_filename "$title")
    iso_path="${STAGING_DIR}/${sanitized_title}-${timestamp}.mp4"

    # Build metadata for state file
    local metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path")

    # Create "creating" state file (for crash recovery)
    state_file_creating=$(create_pipeline_state "iso-creating" "$sanitized_title" "$timestamp" "$metadata")

    log_info "[ISO] Creating ISO: $iso_path"

    # Create the ISO
    if create_iso "$device" "$iso_path"; then
        log_info "[ISO] ISO created successfully: $iso_path"

        # Verify ISO size
        local iso_size=$(stat -c%s "$iso_path" 2>/dev/null || echo "0")
        local iso_size_mb=$((iso_size / 1024 / 1024))
        log_info "[ISO] ISO size: ${iso_size_mb}MB"

        # Transition state: creating -> ready
        remove_state_file "$state_file_creating"
        state_file_ready=$(create_pipeline_state "iso-ready" "$sanitized_title" "$timestamp" "$metadata")

        log_info "[ISO] ISO ready for encoding: $state_file_ready"

        # Trigger encoder if event-driven triggers enabled
        trigger_next_stage "iso-ready"

        # Package CSS keys for cluster distribution (must be before eject)
        local volume_label=$(blkid -o value -s LABEL "$device" 2>/dev/null)
        if [[ -n "$volume_label" ]] && package_dvdcss_keys "$iso_path" "$volume_label"; then
            log_info "[ISO] CSS keys packaged with ISO"
        else
            log_warn "[ISO] Could not package CSS keys (encoding may need to crack keys)"
        fi

        # Eject disc immediately - drive is now free for next disc
        eject_disc "$device"

        log_info "[ISO] Disc ejected, ISO creation complete"
        return 0
    else
        log_error "[ISO] ISO creation failed"

        # Cleanup
        cleanup_files "$iso_path" "${iso_path}.mapfile"
        remove_state_file "$state_file_creating"

        eject_disc "$device"
        return 1
    fi
}

# ============================================================================
# Recovery Function
# ============================================================================

check_iso_recovery() {
    log_info "[ISO] Checking for interrupted ISO operations..."

    # Skip recovery if other ISO processes are active
    # This prevents cleaning up ISOs being created by parallel drives
    if has_other_active_iso_locks "$CURRENT_DEVICE"; then
        log_info "[ISO] Other ISO processes active, skipping recovery check"
        return 0
    fi

    # Check for interrupted ISO creations
    local interrupted=$(find_state_files "iso-creating")
    if [[ -n "$interrupted" ]]; then
        log_warn "[ISO] Found interrupted ISO creation"
        while IFS= read -r state_file; do
            log_info "[ISO] Cleaning up interrupted ISO: $state_file"

            # Read metadata to find partial ISO
            local metadata=$(read_pipeline_state "$state_file")
            local iso_path=$(parse_json_field "$metadata" "iso_path")

            # Remove partial ISO and mapfile
            if [[ -n "$iso_path" ]]; then
                cleanup_files "$iso_path" "${iso_path}.mapfile"
            fi

            remove_state_file "$state_file"
        done <<< "$interrupted"
    fi
}

# ============================================================================
# Main Entry Point
# ============================================================================

main() {
    local device="${1:-$DVD_DEVICE}"
    local device_name="${device##*/}"  # Extract device name: /dev/sr0 -> sr0

    # Set device for per-drive logging (enables separate progress tracking)
    CURRENT_DEVICE="$device_name"

    # Clear device-specific log to remove stale progress from previous disc
    local device_log="${LOG_DIR}/iso-${device_name}.log"
    : > "$device_log" 2>/dev/null || true

    # Initialize
    init_logging || exit 1
    ensure_staging_dir

    log_info "[ISO] ==================== DVD ISO Creator Started ===================="
    log_info "[ISO] Device: $device"

    # Acquire per-device stage lock (allows parallel ripping from multiple drives)
    if ! acquire_stage_lock "iso" "$device_name"; then
        log_error "[ISO] Another ISO creation is already running on $device"
        exit 1
    fi

    # Set up cleanup trap with device-specific lock release
    trap 'release_stage_lock "iso" "'"$device_name"'"; log_info "[ISO] DVD ISO Creator stopped"' EXIT INT TERM

    # Check for recovery scenarios
    check_iso_recovery

    # Wait for device to be ready
    if ! wait_for_device "$device" 30; then
        log_error "[ISO] Device not ready or no disc present"
        exit 1
    fi

    # Verify it's a readable DVD
    if ! is_dvd_readable "$device"; then
        log_error "[ISO] Device is not a readable DVD"
        exit 1
    fi

    # Check disk space before creating ISO
    if ! check_disk_space "$STAGING_DIR"; then
        log_error "[ISO] Insufficient disk space, ejecting disc"
        eject_disc "$device"
        exit 1
    fi

    # Create the ISO
    create_dvd_iso "$device"
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        log_info "[ISO] ISO creation completed successfully"
        local pending=$(count_pending_state "iso-ready")
        log_info "[ISO] ISOs pending encoding: $pending"
    elif [[ $exit_code -eq 2 ]]; then
        log_info "[ISO] ISO creation skipped (duplicate)"
    else
        log_error "[ISO] ISO creation failed"
    fi

    exit $exit_code
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
