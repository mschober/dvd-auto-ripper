#!/bin/bash
# DVD Audit - Detects suspicious MKVs and missing archives
# Runs hourly via systemd timer to flag videos that need attention
#
# Checks for:
# 1. Gibberish/scrambled titles (e.g., "Mi2", "Wap2 Natures Calling")
# 2. Suspiciously small files (< 500MB, likely incomplete encodes)
# 3. MKVs without source ISO archives (missing for re-encode)

set -euo pipefail

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source utility functions
source "${SCRIPT_DIR}/dvd-utils.sh"

# Set logging stage for per-stage log routing
CURRENT_STAGE="audit"

# Configuration
MIN_MKV_SIZE_MB="${MIN_MKV_SIZE_MB:-500}"  # MKVs smaller than this are suspicious
PLEX_VIDEOS_DIR="${PLEX_VIDEOS_DIR:-/var/lib/plexmediaserver/videos}"
AUDIT_STATE_PREFIX=".audit"

# Gibberish patterns - titles with random-looking tokens
# Good titles: "Couples Retreat", "The Artist" (dictionary words, title case)
# Bad titles: "Mi2", "Wap2 Natures Calling", "YA1-0N-NW1.2_DES"
GIBBERISH_PATTERNS=(
    '[A-Z][a-z]+[0-9]'          # Word + number: Mi2, Wap2
    '^[A-Z]{2,4}[0-9]'          # Short abbrev + number: MI2
    '[0-9]+\.[0-9]+'            # Version numbers: 1.2
    '-[A-Z0-9]{2,}-'            # Dashes with caps/nums: -NW1-
    '^[A-Z0-9_-]{8,}$'          # All caps/numbers/symbols (long)
    '_[0-9]{4,}'                # Underscore + many digits
)

# ============================================================================
# Title Analysis Functions
# ============================================================================

# Check if a title looks like gibberish (non-dictionary random tokens)
# Returns: 0 if gibberish, 1 if OK
is_gibberish_title() {
    local title="$1"

    for pattern in "${GIBBERISH_PATTERNS[@]}"; do
        if echo "$title" | grep -qE "$pattern"; then
            return 0  # Matches gibberish pattern
        fi
    done

    return 1  # Looks like a valid title
}

# Find the .transferred metadata file for a given MKV title
# Returns: path to .transferred file or empty string
find_transferred_metadata() {
    local title="$1"
    local sanitized=$(echo "$title" | tr ' ' '_' | tr '[:lower:]' '[:upper:]')

    # Search for matching .transferred files
    for tf in "$STAGING_DIR"/*.transferred; do
        [[ -f "$tf" ]] || continue
        local tf_title=$(parse_json_field "$(cat "$tf")" "title" 2>/dev/null || echo "")
        local tf_sanitized=$(echo "$tf_title" | tr '[:lower:]' '[:upper:]')
        if [[ "$tf_sanitized" == "$sanitized" ]] || [[ "$tf" == *"$sanitized"* ]]; then
            echo "$tf"
            return 0
        fi
    done

    echo ""
    return 1
}

# Check if source ISO exists for a given MKV
# Returns: 0 if ISO exists, 1 if missing
has_source_iso() {
    local title="$1"
    local transferred_file

    transferred_file=$(find_transferred_metadata "$title") || true

    if [[ -z "$transferred_file" ]]; then
        return 1  # No metadata, can't verify
    fi

    local iso_path=$(parse_json_field "$(cat "$transferred_file")" "iso_path" 2>/dev/null || echo "")

    if [[ -n "$iso_path" ]] && [[ -f "$iso_path" ]]; then
        return 0  # ISO exists
    fi

    return 1  # ISO missing
}

# ============================================================================
# Audit State Management
# ============================================================================

# Create or update an audit flag file for a suspicious MKV
flag_for_audit() {
    local title="$1"
    local mkv_path="$2"
    local issues="$3"
    local size_mb="$4"

    local sanitized_title=$(echo "$title" | tr ' ' '_')
    local audit_file="${STAGING_DIR}/${AUDIT_STATE_PREFIX}-${sanitized_title}"

    # Build JSON metadata
    cat > "$audit_file" << EOF
{
  "title": "$title",
  "mkv_path": "$mkv_path",
  "issues": "$issues",
  "size_mb": $size_mb,
  "flagged_at": "$(date -Iseconds)",
  "hostname": "$(hostname)"
}
EOF

    chmod 664 "$audit_file" 2>/dev/null || true
    chgrp dvd-ripper "$audit_file" 2>/dev/null || true

    log_debug "[AUDIT] Created flag: $audit_file"
}

# Remove audit flag for a file (e.g., after it's been fixed)
clear_audit_flag() {
    local title="$1"
    local sanitized_title=$(echo "$title" | tr ' ' '_')
    local audit_file="${STAGING_DIR}/${AUDIT_STATE_PREFIX}-${sanitized_title}"

    if [[ -f "$audit_file" ]]; then
        rm -f "$audit_file"
        log_debug "[AUDIT] Cleared flag: $audit_file"
    fi
}

# Get all current audit flags
get_audit_flags() {
    local flags=()
    for f in "$STAGING_DIR"/${AUDIT_STATE_PREFIX}-*; do
        [[ -f "$f" ]] || continue
        flags+=("$f")
    done
    echo "${flags[@]}"
}

# ============================================================================
# Main Audit Function
# ============================================================================

audit_mkv_files() {
    local suspicious_count=0
    local checked_count=0

    log_info "[AUDIT] Starting MKV audit in $PLEX_VIDEOS_DIR"

    # Check if directory exists
    if [[ ! -d "$PLEX_VIDEOS_DIR" ]]; then
        log_warn "[AUDIT] Plex directory not found: $PLEX_VIDEOS_DIR"
        return 0
    fi

    for mkv in "$PLEX_VIDEOS_DIR"/*.mkv; do
        [[ -f "$mkv" ]] || continue

        local name=$(basename "$mkv" .mkv)
        local size=$(stat -c%s "$mkv" 2>/dev/null || echo 0)
        local size_mb=$((size / 1024 / 1024))
        local issues=""

        checked_count=$((checked_count + 1))

        # Check 1: Gibberish title
        if is_gibberish_title "$name"; then
            issues="${issues}gibberish_title,"
        fi

        # Check 2: Suspiciously small
        if [[ $size_mb -lt $MIN_MKV_SIZE_MB ]]; then
            issues="${issues}small_file,"
        fi

        # Check 3: Missing source ISO
        if ! has_source_iso "$name"; then
            issues="${issues}missing_archive,"
        fi

        # Report findings
        if [[ -n "$issues" ]]; then
            issues="${issues%,}"  # Remove trailing comma
            log_warn "[AUDIT] Suspicious: $name (${size_mb}MB) - $issues"
            flag_for_audit "$name" "$mkv" "$issues" "$size_mb"
            suspicious_count=$((suspicious_count + 1))
        else
            # Clear any existing flag if file is now OK
            clear_audit_flag "$name"
        fi
    done

    log_info "[AUDIT] Complete. Checked $checked_count files, found $suspicious_count suspicious."

    # Log summary to audit log
    echo "$(date -Iseconds)|checked=$checked_count|suspicious=$suspicious_count" >> "${LOG_DIR}/audit.log"
}

# ============================================================================
# Main Entry Point
# ============================================================================

main() {
    # Initialize
    init_logging || exit 1
    ensure_staging_dir

    log_info "[AUDIT] ==================== DVD Audit Started ===================="

    # Acquire lock to prevent concurrent audits
    if ! acquire_stage_lock "audit"; then
        log_info "[AUDIT] Another audit is already running"
        exit 0
    fi

    # Set up cleanup trap
    trap 'release_stage_lock "audit"; log_info "[AUDIT] DVD Audit stopped"' EXIT INT TERM

    # Run the audit
    audit_mkv_files

    log_info "[AUDIT] ==================== DVD Audit Complete ===================="
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
