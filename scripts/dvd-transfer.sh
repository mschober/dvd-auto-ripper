#!/bin/bash
# DVD Transfer - Stage 3 of Pipeline
# Transfers ONE encoded video to NAS/Plex server per run
# Run via cron/systemd timer every 15 minutes

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Configuration (overridden by config file)
NAS_ENABLED="${NAS_ENABLED:-0}"
CLEANUP_MKV_AFTER_TRANSFER="${CLEANUP_MKV_AFTER_TRANSFER:-1}"
CLEANUP_ISO_AFTER_TRANSFER="${CLEANUP_ISO_AFTER_TRANSFER:-1}"

# ============================================================================
# Transfer Function
# ============================================================================

transfer_video() {
    local state_file="$1"
    local metadata mkv_path iso_path sanitized_title year timestamp
    local state_file_transferring

    log_info "[TRANSFER] Processing: $state_file"

    # Read metadata from state file
    metadata=$(read_pipeline_state "$state_file")

    # Parse metadata
    sanitized_title=$(parse_json_field "$metadata" "title")
    year=$(parse_json_field "$metadata" "year")
    timestamp=$(parse_json_field "$metadata" "timestamp")
    mkv_path=$(parse_json_field "$metadata" "mkv_path")
    iso_path=$(parse_json_field "$metadata" "iso_path")

    log_info "[TRANSFER] Title: '$sanitized_title', Year: '$year'"
    log_info "[TRANSFER] MKV: $mkv_path"

    # Verify MKV exists
    if [[ ! -f "$mkv_path" ]]; then
        log_error "[TRANSFER] MKV file not found: $mkv_path"
        log_warn "[TRANSFER] Removing orphaned state file"
        remove_state_file "$state_file"
        return 1
    fi

    # Check NAS configuration
    if [[ -z "$NAS_HOST" ]] || [[ -z "$NAS_USER" ]] || [[ -z "$NAS_PATH" ]]; then
        log_error "[TRANSFER] NAS configuration incomplete (NAS_HOST, NAS_USER, NAS_PATH required)"
        return 1
    fi

    # Transition state: encoded-ready -> transferring
    remove_state_file "$state_file"
    state_file_transferring=$(create_pipeline_state "transferring" "$sanitized_title" "$timestamp" "$metadata")

    local mkv_size=$(stat -c%s "$mkv_path" 2>/dev/null || echo "0")
    local mkv_size_mb=$((mkv_size / 1024 / 1024))
    log_info "[TRANSFER] Starting transfer (${mkv_size_mb}MB) to ${NAS_USER}@${NAS_HOST}:${NAS_PATH}"

    # Perform transfer
    if transfer_to_nas "$mkv_path"; then
        log_info "[TRANSFER] Transfer successful"

        # Cleanup local MKV if configured
        if [[ "$CLEANUP_MKV_AFTER_TRANSFER" == "1" ]]; then
            log_info "[TRANSFER] Removing local MKV: $mkv_path"
            rm -f "$mkv_path"
        fi

        # Cleanup ISO.deletable if configured and exists
        if [[ "$CLEANUP_ISO_AFTER_TRANSFER" == "1" ]]; then
            local iso_deletable="${iso_path}.deletable"
            if [[ -f "$iso_deletable" ]]; then
                log_info "[TRANSFER] Removing ISO: $iso_deletable"
                rm -f "$iso_deletable"
            fi
            # Also try without .deletable suffix (in case it wasn't renamed)
            if [[ -f "$iso_path" ]]; then
                log_info "[TRANSFER] Removing ISO: $iso_path"
                rm -f "$iso_path"
            fi
        fi

        # Remove state file - transfer complete
        remove_state_file "$state_file_transferring"

        log_info "[TRANSFER] Transfer complete: $sanitized_title"
        return 0
    else
        log_error "[TRANSFER] Transfer failed"

        # Revert state back to encoded-ready for future retry
        remove_state_file "$state_file_transferring"
        create_pipeline_state "encoded-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null

        log_warn "[TRANSFER] Video returned to queue for retry"
        return 1
    fi
}

# ============================================================================
# Recovery Function
# ============================================================================

check_transfer_recovery() {
    log_info "[TRANSFER] Checking for interrupted transfer operations..."

    # Check for interrupted transfers
    local interrupted=$(find_state_files "transferring")
    if [[ -n "$interrupted" ]]; then
        log_warn "[TRANSFER] Found interrupted transfer"
        while IFS= read -r state_file; do
            log_info "[TRANSFER] Checking interrupted transfer: $state_file"

            # Read metadata
            local metadata=$(read_pipeline_state "$state_file")
            local mkv_path=$(parse_json_field "$metadata" "mkv_path")
            local sanitized_title=$(parse_json_field "$metadata" "title")
            local timestamp=$(parse_json_field "$metadata" "timestamp")

            # Check if local file still exists
            if [[ -f "$mkv_path" ]]; then
                log_info "[TRANSFER] Local file exists, returning to queue for retry"
                remove_state_file "$state_file"
                create_pipeline_state "encoded-ready" "$sanitized_title" "$timestamp" "$metadata" > /dev/null
            else
                # File was likely transferred and cleaned up, but state wasn't updated
                log_info "[TRANSFER] Local file gone (transfer likely completed), removing state"
                remove_state_file "$state_file"
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

    log_info "[TRANSFER] ==================== DVD Transfer Started ===================="

    # Check if NAS transfer is enabled
    if [[ "$NAS_ENABLED" != "1" ]]; then
        log_info "[TRANSFER] NAS transfer disabled (NAS_ENABLED != 1)"
        exit 0
    fi

    # Try to acquire stage lock (non-blocking)
    if ! acquire_stage_lock "transfer"; then
        log_info "[TRANSFER] Another transfer is already running, exiting"
        exit 0
    fi

    # Set up cleanup trap
    trap 'release_stage_lock "transfer"; log_info "[TRANSFER] DVD Transfer stopped"' EXIT INT TERM

    # Check for recovery scenarios
    check_transfer_recovery

    # Find oldest encoded-ready state file
    local state_file=$(find_oldest_state "encoded-ready")

    if [[ -z "$state_file" ]]; then
        log_info "[TRANSFER] No videos pending transfer"
        exit 0
    fi

    local pending=$(count_pending_state "encoded-ready")
    log_info "[TRANSFER] Found $pending video(s) pending transfer"
    log_info "[TRANSFER] Processing oldest: $state_file"

    # Transfer the video
    transfer_video "$state_file"
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        log_info "[TRANSFER] Transfer completed successfully"
        local remaining=$(count_pending_state "encoded-ready")
        log_info "[TRANSFER] Videos remaining: $remaining"
    else
        log_error "[TRANSFER] Transfer failed"
    fi

    exit $exit_code
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
