#!/bin/bash
# DVD Cluster Distributor - Sends ISOs to idle cluster peers
# Runs independently of encoder, allowing parallel operation

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# ============================================================================
# Distribution Functions
# ============================================================================

distribute_pending_iso() {
    # Find available peer
    local peer
    peer=$(find_available_peer)
    if [[ -z "$peer" ]]; then
        log_info "[DISTRIBUTE] No peers available"
        return 0
    fi

    # Find oldest iso-ready (skip if <2 pending - keep one for local)
    local pending
    pending=$(count_pending_state "iso-ready")
    if [[ "$pending" -lt 2 ]]; then
        log_debug "[DISTRIBUTE] Only $pending job(s) pending, keeping for local encoder"
        return 0
    fi

    local state_file
    state_file=$(find_oldest_state "iso-ready")
    if [[ -z "$state_file" ]]; then
        log_info "[DISTRIBUTE] No ISOs available for distribution"
        return 0
    fi

    # Distribute
    log_info "[DISTRIBUTE] Distributing to peer: $peer"
    if distribute_to_peer "$state_file" "$peer"; then
        log_info "[DISTRIBUTE] Distribution successful"
        return 0
    else
        log_warn "[DISTRIBUTE] Distribution failed"
        return 1
    fi
}

# ============================================================================
# Recovery Function
# ============================================================================

check_distribute_recovery() {
    log_info "[DISTRIBUTE] Checking for interrupted distributions..."

    # Check for interrupted distributions
    local interrupted
    interrupted=$(find_state_files "distributing")
    if [[ -n "$interrupted" ]]; then
        log_warn "[DISTRIBUTE] Found interrupted distribution(s)"
        while IFS= read -r state_file; do
            log_info "[DISTRIBUTE] Recovering interrupted distribution: $state_file"

            # Read metadata
            local metadata
            metadata=$(read_pipeline_state "$state_file")
            local title
            title=$(parse_json_field "$metadata" "title")
            local timestamp
            timestamp=$(parse_json_field "$metadata" "timestamp")

            # Revert state back to iso-ready for retry
            remove_state_file "$state_file"
            create_pipeline_state "iso-ready" "$title" "$timestamp" "$metadata" > /dev/null

            log_info "[DISTRIBUTE] Reverted '$title' to iso-ready for retry"
        done <<< "$interrupted"
    fi
}

# ============================================================================
# Main Entry Point
# ============================================================================

main() {
    # Initialize
    init_logging || exit 1

    log_info "[DISTRIBUTE] ==================== DVD Distributor Started ===================="

    # Check if cluster mode enabled
    if [[ "${CLUSTER_ENABLED:-0}" != "1" ]]; then
        log_info "[DISTRIBUTE] Cluster mode disabled, exiting"
        exit 0
    fi

    # Acquire distribution lock
    if ! acquire_stage_lock "distribute"; then
        log_info "[DISTRIBUTE] Another distribution in progress"
        exit 0
    fi

    # Set up cleanup trap
    trap 'release_stage_lock "distribute"; log_info "[DISTRIBUTE] DVD Distributor stopped"' EXIT INT TERM

    # Check for recovery scenarios
    check_distribute_recovery

    # Distribute one ISO
    distribute_pending_iso
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        local pending
        pending=$(count_pending_state "iso-ready")
        local distributed
        distributed=$(find_state_files "distributed-to-*" | wc -l)
        log_info "[DISTRIBUTE] ISOs pending: $pending, Distributed to peers: $distributed"
    fi

    exit $exit_code
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
