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
NAS_FILE_OWNER="${NAS_FILE_OWNER:-plex:plex}"
CLEANUP_MKV_AFTER_TRANSFER="${CLEANUP_MKV_AFTER_TRANSFER:-1}"
CLEANUP_ISO_AFTER_TRANSFER="${CLEANUP_ISO_AFTER_TRANSFER:-1}"
CLEANUP_PREVIEW_AFTER_TRANSFER="${CLEANUP_PREVIEW_AFTER_TRANSFER:-0}"

# Transfer mode: "remote" (rsync to NAS) or "local" (mv to local path)
TRANSFER_MODE="${TRANSFER_MODE:-remote}"
LOCAL_LIBRARY_PATH="${LOCAL_LIBRARY_PATH:-}"

# Cluster configuration
CLUSTER_ENABLED="${CLUSTER_ENABLED:-0}"
CLUSTER_NODE_NAME="${CLUSTER_NODE_NAME:-}"
CLUSTER_SSH_USER="${CLUSTER_SSH_USER:-}"
CLUSTER_REMOTE_STAGING="${CLUSTER_REMOTE_STAGING:-/var/tmp/dvd-rips}"
CLUSTER_PEERS="${CLUSTER_PEERS:-}"

# ============================================================================
# Remote Job Return Function (Cluster Mode)
# ============================================================================

# Return a completed remote job to its origin node
# Usage: return_remote_job STATE_FILE METADATA
# Returns: 0 on success, 1 on failure
return_remote_job() {
    local state_file="$1"
    local metadata="$2"

    local title=$(parse_json_field "$metadata" "title")
    local timestamp=$(parse_json_field "$metadata" "timestamp")
    local mkv_path=$(parse_json_field "$metadata" "mkv_path")
    local iso_path=$(parse_json_field "$metadata" "iso_path")
    local origin_node=$(parse_json_field "$metadata" "origin_node")

    log_info "[TRANSFER] Remote job detected, returning to origin: $origin_node"

    # Find origin host from peers config
    local origin_host=""
    local origin_port=""
    for peer_entry in $CLUSTER_PEERS; do
        local name=$(echo "$peer_entry" | cut -d: -f1)
        if [[ "$name" == "$origin_node" ]]; then
            origin_host=$(echo "$peer_entry" | cut -d: -f2)
            origin_port=$(echo "$peer_entry" | cut -d: -f3)
            break
        fi
    done

    if [[ -z "$origin_host" ]]; then
        log_error "[TRANSFER] Could not find origin node $origin_node in peers"
        return 1
    fi

    log_info "[TRANSFER] Origin: $origin_node ($origin_host:$origin_port)"

    # Verify MKV exists
    if [[ ! -f "$mkv_path" ]]; then
        log_error "[TRANSFER] MKV file not found: $mkv_path"
        # Notify origin of failure
        curl -s -X POST "http://${origin_host}:${origin_port}/api/cluster/job-complete" \
            -H "Content-Type: application/json" \
            -d "{\"title\": \"$title\", \"timestamp\": \"$timestamp\", \"success\": false}" \
            2>/dev/null
        return 1
    fi

    # Transition state to transferring
    local state_file_transferring
    remove_state_file "$state_file"
    state_file_transferring=$(create_pipeline_state "transferring" "$title" "$timestamp" "$metadata")

    # Rsync MKV back to origin's staging directory
    local remote_dest="${CLUSTER_SSH_USER}@${origin_host}:${CLUSTER_REMOTE_STAGING}/"
    log_info "[TRANSFER] Rsync MKV to origin: $remote_dest"

    local mkv_size=$(stat -c%s "$mkv_path" 2>/dev/null || echo "0")
    local mkv_size_mb=$((mkv_size / 1024 / 1024))
    log_info "[TRANSFER] Transferring ${mkv_size_mb}MB to origin"

    if ! rsync -avz --progress "$mkv_path" "$remote_dest" >> "$LOG_FILE" 2>&1; then
        log_error "[TRANSFER] MKV transfer to origin failed"
        # Revert state
        remove_state_file "$state_file_transferring"
        create_pipeline_state "encoded-ready" "$title" "$timestamp" "$metadata" > /dev/null
        # Notify origin of failure
        curl -s -X POST "http://${origin_host}:${origin_port}/api/cluster/job-complete" \
            -H "Content-Type: application/json" \
            -d "{\"title\": \"$title\", \"timestamp\": \"$timestamp\", \"success\": false}" \
            2>/dev/null
        return 1
    fi

    log_info "[TRANSFER] MKV transferred to origin successfully"

    # Notify origin that job is complete
    local remote_mkv_path="${CLUSTER_REMOTE_STAGING}/$(basename "$mkv_path")"
    local api_response
    api_response=$(curl -s -X POST "http://${origin_host}:${origin_port}/api/cluster/job-complete" \
        -H "Content-Type: application/json" \
        -d "{\"title\": \"$title\", \"timestamp\": \"$timestamp\", \"mkv_path\": \"$remote_mkv_path\", \"success\": true}" \
        2>/dev/null)

    if [[ $? -ne 0 ]] || ! echo "$api_response" | grep -q '"status":\s*"ok"'; then
        log_warn "[TRANSFER] Could not notify origin (MKV was transferred though)"
    else
        log_info "[TRANSFER] Origin notified of completion"
    fi

    # Cleanup local files
    log_info "[TRANSFER] Cleaning up local files..."
    rm -f "$mkv_path"
    log_debug "[TRANSFER] Removed local MKV: $mkv_path"

    # Clean up ISO if it exists locally
    if [[ -f "$iso_path" ]]; then
        rm -f "$iso_path"
        log_debug "[TRANSFER] Removed local ISO: $iso_path"
    fi
    if [[ -f "${iso_path}.deletable" ]]; then
        rm -f "${iso_path}.deletable"
        log_debug "[TRANSFER] Removed local ISO.deletable"
    fi

    # Remove state file - job is done on this node
    remove_state_file "$state_file_transferring"

    log_info "[TRANSFER] Remote job '$title' returned to origin successfully"
    return 0
}

# ============================================================================
# Transfer Function
# ============================================================================

transfer_video() {
    local state_file="$1"
    local metadata mkv_path iso_path sanitized_title year timestamp main_title preview_path
    local state_file_transferring

    log_info "[TRANSFER] Processing: $state_file"

    # Read metadata from state file
    metadata=$(read_pipeline_state "$state_file")

    # Parse metadata
    sanitized_title=$(parse_json_field "$metadata" "title")
    year=$(parse_json_field "$metadata" "year")
    timestamp=$(parse_json_field "$metadata" "timestamp")
    main_title=$(parse_json_field "$metadata" "main_title")
    mkv_path=$(parse_json_field "$metadata" "mkv_path")
    iso_path=$(parse_json_field "$metadata" "iso_path")
    preview_path=$(parse_json_field "$metadata" "preview_path")

    log_info "[TRANSFER] Title: '$sanitized_title', Year: '$year'"
    log_info "[TRANSFER] MKV: $mkv_path"

    # Check if this is a remote job (encoded on this node, originated elsewhere)
    local is_remote_job=$(parse_json_field "$metadata" "is_remote_job")
    local origin_node=$(parse_json_field "$metadata" "origin_node")

    if [[ "$is_remote_job" == "true" ]] && [[ -n "$origin_node" ]]; then
        log_info "[TRANSFER] Remote job from '$origin_node', routing to return handler"
        return_remote_job "$state_file" "$metadata"
        return $?
    fi

    # Verify MKV exists
    if [[ ! -f "$mkv_path" ]]; then
        log_error "[TRANSFER] MKV file not found: $mkv_path"
        log_warn "[TRANSFER] Removing orphaned state file"
        remove_state_file "$state_file"
        return 1
    fi

    # Check configuration based on transfer mode
    if [[ "$TRANSFER_MODE" == "local" ]]; then
        if [[ -z "$LOCAL_LIBRARY_PATH" ]]; then
            log_error "[TRANSFER] LOCAL_LIBRARY_PATH not configured for local transfer mode"
            return 1
        fi
        if [[ ! -d "$LOCAL_LIBRARY_PATH" ]]; then
            log_error "[TRANSFER] LOCAL_LIBRARY_PATH does not exist: $LOCAL_LIBRARY_PATH"
            return 1
        fi
    else
        # Remote mode - check NAS configuration
        if [[ -z "$NAS_HOST" ]] || [[ -z "$NAS_USER" ]] || [[ -z "$NAS_PATH" ]]; then
            log_error "[TRANSFER] NAS configuration incomplete (NAS_HOST, NAS_USER, NAS_PATH required)"
            return 1
        fi
    fi

    # Transition state: encoded-ready -> transferring
    remove_state_file "$state_file"
    state_file_transferring=$(create_pipeline_state "transferring" "$sanitized_title" "$timestamp" "$metadata")

    local mkv_size=$(stat -c%s "$mkv_path" 2>/dev/null || echo "0")
    local mkv_size_mb=$((mkv_size / 1024 / 1024))

    local transfer_success=false
    local final_path=""

    if [[ "$TRANSFER_MODE" == "local" ]]; then
        # Local mode: move file to library path
        local dest_path="${LOCAL_LIBRARY_PATH}/$(basename "$mkv_path")"
        log_info "[TRANSFER] Moving (${mkv_size_mb}MB) to local library: $dest_path"

        if mv "$mkv_path" "$dest_path"; then
            log_info "[TRANSFER] Local move successful"
            transfer_success=true
            final_path="$dest_path"

            # Set ownership if configured
            if [[ -n "$NAS_FILE_OWNER" ]]; then
                log_info "[TRANSFER] Setting ownership to $NAS_FILE_OWNER"
                if chown "$NAS_FILE_OWNER" "$dest_path" 2>/dev/null; then
                    log_info "[TRANSFER] Ownership set successfully"
                else
                    log_warn "[TRANSFER] Failed to set ownership (continuing anyway)"
                fi
            fi
        else
            log_error "[TRANSFER] Local move failed"
        fi
    else
        # Remote mode: rsync to NAS
        log_info "[TRANSFER] Starting transfer (${mkv_size_mb}MB) to ${NAS_USER}@${NAS_HOST}:${NAS_PATH}"

        if transfer_to_nas "$mkv_path"; then
            log_info "[TRANSFER] Transfer successful"
            transfer_success=true
            final_path="${NAS_PATH}/$(basename "$mkv_path")"

            # Change ownership on remote if configured
            if [[ -n "$NAS_FILE_OWNER" ]]; then
                local remote_file="${NAS_PATH}/$(basename "$mkv_path")"
                log_info "[TRANSFER] Setting ownership to $NAS_FILE_OWNER on remote"
                if ssh "${NAS_USER}@${NAS_HOST}" "chown $NAS_FILE_OWNER \"$remote_file\"" 2>/dev/null; then
                    log_info "[TRANSFER] Ownership set successfully"
                else
                    log_warn "[TRANSFER] Failed to set ownership (continuing anyway)"
                fi
            fi

            # Cleanup local MKV if configured (only for remote mode - local mode already moved it)
            if [[ "$CLEANUP_MKV_AFTER_TRANSFER" == "1" ]]; then
                log_info "[TRANSFER] Removing local MKV: $mkv_path"
                rm -f "$mkv_path"
            fi
        else
            log_error "[TRANSFER] Transfer failed"
        fi
    fi

    if [[ "$transfer_success" == "true" ]]; then
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

        # Cleanup preview if configured (keep by default for identification)
        if [[ "$CLEANUP_PREVIEW_AFTER_TRANSFER" == "1" ]] && [[ -n "$preview_path" ]] && [[ -f "$preview_path" ]]; then
            log_info "[TRANSFER] Removing preview: $preview_path"
            rm -f "$preview_path"
            preview_path=""
        fi

        # Update metadata with final path and transition to "transferred" state
        # This allows the dashboard to track and rename files
        metadata=$(build_state_metadata "$sanitized_title" "$year" "$timestamp" "$main_title" "$iso_path" "$mkv_path" "$preview_path" "$final_path")
        remove_state_file "$state_file_transferring"
        create_pipeline_state "transferred" "$sanitized_title" "$timestamp" "$metadata" > /dev/null

        log_info "[TRANSFER] Transfer complete: $sanitized_title ($final_path)"
        return 0
    else
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
    log_info "[TRANSFER] Transfer mode: $TRANSFER_MODE"

    # Check if transfer is enabled based on mode
    if [[ "$TRANSFER_MODE" == "local" ]]; then
        if [[ -z "$LOCAL_LIBRARY_PATH" ]]; then
            log_info "[TRANSFER] Local transfer mode but LOCAL_LIBRARY_PATH not set, exiting"
            exit 0
        fi
        log_info "[TRANSFER] Local library path: $LOCAL_LIBRARY_PATH"
    else
        # Remote mode - check if NAS transfer is enabled
        if [[ "$NAS_ENABLED" != "1" ]]; then
            log_info "[TRANSFER] NAS transfer disabled (NAS_ENABLED != 1)"
            exit 0
        fi
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
