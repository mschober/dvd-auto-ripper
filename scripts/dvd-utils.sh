#!/bin/bash
# DVD Ripper Utility Functions Library
# Provides helper functions for logging, state management, and DVD operations

# Source configuration file
CONFIG_FILE="${CONFIG_FILE:-/etc/dvd-ripper.conf}"
if [[ -f "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
fi

# Default configuration values (overridden by config file)
STAGING_DIR="${STAGING_DIR:-/var/tmp/dvd-rips}"
LOG_FILE="${LOG_FILE:-/var/log/dvd-ripper.log}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
LOCK_FILE="${LOCK_FILE:-/var/run/dvd-ripper.pid}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_DELAY="${RETRY_DELAY:-60}"
DISK_USAGE_THRESHOLD="${DISK_USAGE_THRESHOLD:-80}"

# Log levels
declare -A LOG_LEVELS=([DEBUG]=0 [INFO]=1 [WARN]=2 [ERROR]=3)
CURRENT_LOG_LEVEL=${LOG_LEVELS[$LOG_LEVEL]:-1}

# ============================================================================
# Logging Functions
# ============================================================================

# Log message with timestamp and level
# Usage: log_message LEVEL "message"
log_message() {
    local level="$1"
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local level_num=${LOG_LEVELS[$level]:-1}

    # Only log if message level >= current log level
    if [[ $level_num -ge $CURRENT_LOG_LEVEL ]]; then
        echo "[$timestamp] [$level] $message" >> "$LOG_FILE"
    fi
}

log_debug() { log_message "DEBUG" "$@"; }
log_info() { log_message "INFO" "$@"; }
log_warn() { log_message "WARN" "$@"; }
log_error() { log_message "ERROR" "$@"; }

# ============================================================================
# Lock/PID Management
# ============================================================================

# Create lock file with current PID
# Returns: 0 if lock acquired, 1 if already locked
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local existing_pid=$(cat "$LOCK_FILE" 2>/dev/null)
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            log_warn "Lock file exists and process $existing_pid is running"
            return 1
        else
            log_info "Stale lock file found, removing"
            rm -f "$LOCK_FILE"
        fi
    fi

    echo "$$" > "$LOCK_FILE"
    log_debug "Lock acquired with PID $$"
    return 0
}

# Release lock file
release_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        rm -f "$LOCK_FILE"
        log_debug "Lock released"
    fi
}

# ============================================================================
# State File Management
# ============================================================================

# Create state file to track operation progress
# Usage: create_state_file STATE TITLE TIMESTAMP
# STATE: ripping, completed, transferring
# File format: TITLE-TIMESTAMP.STATE (visible, not hidden)
create_state_file() {
    local state="$1"
    local title="$2"
    local timestamp="$3"
    local state_file="${STAGING_DIR}/${title}-${timestamp}.${state}"

    touch "$state_file"
    log_debug "Created state file: $state_file"
    echo "$state_file"
}

# Remove state file
# Usage: remove_state_file PATH
remove_state_file() {
    local state_file="$1"
    if [[ -f "$state_file" ]]; then
        rm -f "$state_file"
        log_debug "Removed state file: $state_file"
    fi
}

# Find existing state files for recovery
# Usage: find_state_files STATE
find_state_files() {
    local state="$1"
    find "$STAGING_DIR" -maxdepth 1 -name "*.${state}" -type f 2>/dev/null
}

# ============================================================================
# Filename Utilities
# ============================================================================

# Sanitize string for use in filenames
# Removes special characters, replaces spaces with underscores
# Usage: sanitize_filename "string"
sanitize_filename() {
    local input="$1"
    # Remove/replace problematic characters
    echo "$input" | sed 's/[^a-zA-Z0-9._-]/_/g' | sed 's/__*/_/g' | sed 's/^_//;s/_$//'
}

# Generate output filename (internal use with timestamp for dedup)
# Usage: generate_filename TITLE YEAR EXTENSION
generate_filename() {
    local title="$1"
    local year="$2"
    local extension="$3"
    local timestamp=$(date +%s)
    local sanitized_title=$(sanitize_filename "$title")

    if [[ -n "$year" ]]; then
        echo "${sanitized_title}-${year}-${timestamp}.${extension}"
    else
        echo "${sanitized_title}-${timestamp}.${extension}"
    fi
}

# Clean title for Plex-friendly display (preserve spaces, remove bad chars)
# Usage: clean_title_for_plex "THE_MATRIX" -> "The Matrix"
clean_title_for_plex() {
    local input="$1"

    # Replace underscores with spaces
    local cleaned="${input//_/ }"

    # Remove characters that are problematic in filenames but keep spaces
    cleaned=$(echo "$cleaned" | sed 's/[<>:"/\\|?*]//g')

    # Collapse multiple spaces to single space
    cleaned=$(echo "$cleaned" | sed 's/  */ /g')

    # Trim leading/trailing spaces
    cleaned=$(echo "$cleaned" | sed 's/^ *//;s/ *$//')

    # Title case (capitalize first letter of each word)
    # Handle common articles/prepositions that should stay lowercase
    cleaned=$(echo "$cleaned" | sed 's/.*/\L&/' | sed 's/\b\(.\)/\u\1/g')

    echo "$cleaned"
}

# Generate Plex-compatible filename
# Usage: generate_plex_filename TITLE YEAR EXTENSION
# Output: "The Matrix (1999).mkv" or "The Matrix.mkv" if no year
generate_plex_filename() {
    local title="$1"
    local year="$2"
    local extension="$3"

    # Clean title for Plex (spaces, proper case)
    local clean_title=$(clean_title_for_plex "$title")

    if [[ -n "$year" ]] && [[ "$year" =~ ^[0-9]{4}$ ]]; then
        echo "${clean_title} (${year}).${extension}"
    else
        echo "${clean_title}.${extension}"
    fi
}

# ============================================================================
# DVD Detection and Information
# ============================================================================

# Check if disc is readable DVD
# Usage: is_dvd_readable DEVICE
is_dvd_readable() {
    local device="$1"

    # Check if device exists
    if [[ ! -b "$device" ]]; then
        log_error "Device $device does not exist"
        return 1
    fi

    # Check if media is actually present using udevadm
    local media_present=$(udevadm info --query=property --name="$device" | grep "ID_CDROM_MEDIA=" | cut -d= -f2)
    if [[ "$media_present" != "1" ]]; then
        log_info "No media present in $device"
        return 1
    fi

    # Try to detect if it's a DVD using lsblk
    if ! lsblk "$device" &>/dev/null; then
        log_error "Cannot read device $device"
        return 1
    fi

    return 0
}

# Create ISO image from DVD using ddrescue
# Usage: create_iso DEVICE OUTPUT_ISO
# Returns: 0 on success, 1 on failure
create_iso() {
    local device="$1"
    local output_iso="$2"
    local mapfile="${output_iso}.mapfile"

    log_info "Creating ISO from $device to $output_iso"

    # Check if ddrescue is available
    if ! command -v ddrescue &>/dev/null; then
        log_error "ddrescue not found. Install with: sudo apt-get install gddrescue"
        return 1
    fi

    # Run ddrescue with error recovery
    # -n = no scraping (faster initial pass)
    # -b 2048 = DVD sector size
    if ddrescue -n -b 2048 "$device" "$output_iso" "$mapfile" >> "$LOG_FILE" 2>&1; then
        log_info "ISO creation completed successfully"

        # Verify ISO file exists and has reasonable size
        if [[ -f "$output_iso" ]]; then
            local iso_size=$(stat -c%s "$output_iso" 2>/dev/null || echo "0")
            local iso_size_mb=$((iso_size / 1024 / 1024))
            log_info "ISO size: ${iso_size_mb}MB"

            if [[ $iso_size_mb -lt 100 ]]; then
                log_warn "ISO file seems too small (${iso_size_mb}MB), may be incomplete"
            fi
        fi

        return 0
    else
        log_error "ISO creation failed"
        return 1
    fi
}

# Extract DVD metadata using handbrake
# Usage: get_dvd_info DEVICE
# Returns: title|year|main_title_num|duration (pipe-separated)
get_dvd_info() {
    local device="$1"
    local scan_output

    log_info "Scanning DVD in $device"

    # Run handbrake scan and capture output
    # Note: HandBrake --scan returns non-zero exit code even on success
    scan_output=$(HandBrakeCLI --scan -i "$device" 2>&1)

    # Check if we got valid output instead of relying on exit code
    if [[ -z "$scan_output" ]] || ! echo "$scan_output" | grep -q "scan:"; then
        log_error "HandBrake scan failed - no valid output"
        return 1
    fi

    # Extract disc title from libdvdnav output (not track numbers)
    # Line format: [HH:MM:SS] libdvdnav: DVD Title: THE_MATRIX
    local title=$(echo "$scan_output" | grep -oP 'libdvdnav: DVD Title: \K.*' | head -1 | xargs)

    # Extract main title number (longest title)
    local main_title=$(echo "$scan_output" | grep "^+ title" | \
        grep -oP '\+ title \K\d+' | head -1)

    # Extract duration
    local duration=$(echo "$scan_output" | grep "duration:" | head -1 | \
        grep -oP 'duration: \K[0-9:]+')

    # Extract year from title if present
    local year=""

    # Pattern 1: Year in parentheses like "(1999)"
    if [[ "$title" =~ \(([12][0-9]{3})\) ]]; then
        year="${BASH_REMATCH[1]}"
    # Pattern 2: Year with underscore like "_1999" at end
    elif [[ "$title" =~ _([12][0-9]{3})$ ]]; then
        year="${BASH_REMATCH[1]}"
    # Pattern 3: Standalone year at end like "MOVIE1999"
    elif [[ "$title" =~ ([12][0-9]{3})$ ]]; then
        year="${BASH_REMATCH[1]}"
    fi

    # Remove year from title to avoid duplication in filename
    if [[ -n "$year" ]]; then
        title=$(echo "$title" | sed -E "s/[_ ]?\($year\)//g; s/_$year$//; s/$year$//" | xargs)
    fi

    # Use generic name if title is empty or generic
    if [[ -z "$title" ]] || [[ "${title^^}" =~ ^(DVD|DVD_VIDEO|DISC|DISK|VIDEO_TS|DVDVIDEO|MYDVD)$ ]]; then
        title="DVD_$(date +%Y%m%d_%H%M%S)"
    fi

    echo "${title}|${year}|${main_title}|${duration}"
    return 0
}

# ============================================================================
# Duplicate Detection
# ============================================================================

# Check if file with same title-year pattern exists
# Usage: check_duplicate TITLE YEAR
# Returns: 0 if duplicate found, 1 if not
check_duplicate() {
    local title="$1"
    local year="$2"
    local sanitized_title=$(sanitize_filename "$title")
    local pattern

    if [[ -n "$year" ]]; then
        pattern="${sanitized_title}-${year}-*"
    else
        pattern="${sanitized_title}-*"
    fi

    # Check local staging directory
    if ls "$STAGING_DIR"/$pattern 2>/dev/null | grep -q .; then
        log_warn "Duplicate found in staging directory: $pattern"
        return 0
    fi

    # Check NAS if configured
    if [[ -n "$NAS_HOST" ]] && [[ -n "$NAS_USER" ]] && [[ -n "$NAS_PATH" ]]; then
        log_debug "Checking for duplicates on NAS"
        if ssh "${NAS_USER}@${NAS_HOST}" "ls ${NAS_PATH}/${pattern}" 2>/dev/null | grep -q .; then
            log_warn "Duplicate found on NAS: $pattern"
            return 0
        fi
    fi

    log_debug "No duplicates found for pattern: $pattern"
    return 1
}

# ============================================================================
# Disk Space Management
# ============================================================================

# Check if disk usage is below threshold
# Usage: check_disk_space [PATH]
# Returns: 0 if space available, 1 if disk is too full
check_disk_space() {
    local path="${1:-$STAGING_DIR}"
    local threshold="${DISK_USAGE_THRESHOLD:-80}"

    # Get disk usage percentage for the filesystem containing path
    local usage=$(df "$path" 2>/dev/null | awk 'NR==2 {gsub(/%/,""); print $5}')

    if [[ -z "$usage" ]]; then
        log_error "Could not determine disk usage for $path"
        return 1
    fi

    log_info "Disk usage for $path: ${usage}% (threshold: ${threshold}%)"

    if [[ $usage -ge $threshold ]]; then
        log_error "Disk usage ${usage}% exceeds threshold ${threshold}%, skipping rip"
        return 1
    fi

    log_debug "Disk space check passed: ${usage}% < ${threshold}%"
    return 0
}

# ============================================================================
# File Operations
# ============================================================================

# Verify file exists and has reasonable size
# Usage: verify_file_size FILE MIN_SIZE_MB
verify_file_size() {
    local file="$1"
    local min_size_mb="${2:-100}"

    if [[ ! -f "$file" ]]; then
        log_error "File does not exist: $file"
        return 1
    fi

    local size_bytes=$(stat -c%s "$file" 2>/dev/null)
    local size_mb=$((size_bytes / 1024 / 1024))

    if [[ $size_mb -lt $min_size_mb ]]; then
        log_error "File too small: ${size_mb}MB (minimum: ${min_size_mb}MB)"
        return 1
    fi

    log_info "File size verification passed: ${size_mb}MB"
    return 0
}

# Transfer file to NAS
# Usage: transfer_to_nas LOCAL_FILE
transfer_to_nas() {
    local local_file="$1"
    local filename=$(basename "$local_file")
    local remote_path="${NAS_PATH}/${filename}"
    local attempt=1

    if [[ -z "$NAS_HOST" ]] || [[ -z "$NAS_USER" ]] || [[ -z "$NAS_PATH" ]]; then
        log_error "NAS configuration incomplete"
        return 1
    fi

    while [[ $attempt -le $MAX_RETRIES ]]; do
        log_info "Transfer attempt $attempt/$MAX_RETRIES: $filename to ${NAS_USER}@${NAS_HOST}:${NAS_PATH}"

        if [[ "$NAS_TRANSFER_METHOD" == "rsync" ]]; then
            rsync -avz --progress "$local_file" "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/"
        else
            scp "$local_file" "${NAS_USER}@${NAS_HOST}:${remote_path}"
        fi

        if [[ $? -eq 0 ]]; then
            log_info "Transfer successful"

            # Verify remote file size matches
            local local_size=$(stat -c%s "$local_file")
            local remote_size=$(ssh "${NAS_USER}@${NAS_HOST}" "stat -c%s ${remote_path}" 2>/dev/null)

            if [[ "$local_size" == "$remote_size" ]]; then
                log_info "Transfer verification passed"
                return 0
            else
                log_warn "Size mismatch: local=$local_size remote=$remote_size"
            fi
        fi

        attempt=$((attempt + 1))
        if [[ $attempt -le $MAX_RETRIES ]]; then
            log_warn "Transfer failed, retrying in ${RETRY_DELAY}s..."
            sleep "$RETRY_DELAY"
        fi
    done

    log_error "Transfer failed after $MAX_RETRIES attempts"
    return 1
}

# ============================================================================
# Device Operations
# ============================================================================

# Eject disc from device
# Usage: eject_disc DEVICE
# TODO: Fix eject to handle waiting/retrying while device becomes available.
#       After ddrescue completes, the device may still be busy. Need to:
#       1. Wait for device to become available (not busy)
#       2. Retry eject with backoff if it fails
#       3. Handle "device busy" errors gracefully
eject_disc() {
    local device="$1"
    log_info "Ejecting disc from $device"
    eject "$device" 2>&1 | tee -a "$LOG_FILE"
    return ${PIPESTATUS[0]}
}

# Wait for device to be ready
# Usage: wait_for_device DEVICE TIMEOUT_SECONDS
wait_for_device() {
    local device="$1"
    local timeout="${2:-30}"
    local elapsed=0

    log_info "Waiting for device $device to be ready (timeout: ${timeout}s)"

    while [[ $elapsed -lt $timeout ]]; do
        if [[ -b "$device" ]] && is_dvd_readable "$device"; then
            log_info "Device ready after ${elapsed}s"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    log_error "Device not ready after ${timeout}s"
    return 1
}

# ============================================================================
# Cleanup Functions
# ============================================================================

# Clean up temporary files and state
# Usage: cleanup_files FILE [STATE_FILE...]
cleanup_files() {
    local file="$1"
    shift

    if [[ -f "$file" ]]; then
        log_info "Removing local file: $file"
        rm -f "$file"
    fi

    # Remove any state files passed as additional arguments
    for state_file in "$@"; do
        remove_state_file "$state_file"
    done
}

# ============================================================================
# Initialization
# ============================================================================

# Ensure staging directory exists
ensure_staging_dir() {
    if [[ ! -d "$STAGING_DIR" ]]; then
        log_info "Creating staging directory: $STAGING_DIR"
        mkdir -p "$STAGING_DIR"
        chmod 750 "$STAGING_DIR"
    fi
}

# Initialize logging
init_logging() {
    local log_dir=$(dirname "$LOG_FILE")
    if [[ ! -d "$log_dir" ]]; then
        mkdir -p "$log_dir"
    fi

    # Ensure log file is writable
    touch "$LOG_FILE" 2>/dev/null || {
        echo "ERROR: Cannot write to log file: $LOG_FILE" >&2
        return 1
    }

    log_info "==================== DVD Ripper Started ===================="
    return 0
}

# ============================================================================
# Pipeline Mode: Stage-Specific Lock Management
# ============================================================================

# Default lock file paths for pipeline mode
ISO_LOCK_FILE="${ISO_LOCK_FILE:-/var/run/dvd-ripper-iso.lock}"
ENCODER_LOCK_FILE="${ENCODER_LOCK_FILE:-/var/run/dvd-ripper-encoder.lock}"
TRANSFER_LOCK_FILE="${TRANSFER_LOCK_FILE:-/var/run/dvd-ripper-transfer.lock}"

# Acquire stage-specific lock (non-blocking)
# Usage: acquire_stage_lock STAGE
# STAGE: iso, encoder, transfer
# Returns: 0 if acquired, 1 if already locked
acquire_stage_lock() {
    local stage="$1"
    local lock_file

    case "$stage" in
        iso)      lock_file="$ISO_LOCK_FILE" ;;
        encoder)  lock_file="$ENCODER_LOCK_FILE" ;;
        transfer) lock_file="$TRANSFER_LOCK_FILE" ;;
        *)
            log_error "Unknown stage: $stage"
            return 1
            ;;
    esac

    if [[ -f "$lock_file" ]]; then
        local existing_pid=$(cat "$lock_file" 2>/dev/null)
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            log_debug "Stage $stage already locked by PID $existing_pid"
            return 1
        else
            log_info "Stale $stage lock file found, removing"
            rm -f "$lock_file"
        fi
    fi

    echo "$$" > "$lock_file"
    log_debug "Acquired $stage lock with PID $$"
    return 0
}

# Release stage-specific lock
# Usage: release_stage_lock STAGE
release_stage_lock() {
    local stage="$1"
    local lock_file

    case "$stage" in
        iso)      lock_file="$ISO_LOCK_FILE" ;;
        encoder)  lock_file="$ENCODER_LOCK_FILE" ;;
        transfer) lock_file="$TRANSFER_LOCK_FILE" ;;
        *)        return ;;
    esac

    if [[ -f "$lock_file" ]]; then
        rm -f "$lock_file"
        log_debug "Released $stage lock"
    fi
}

# ============================================================================
# Pipeline Mode: JSON State File Management
# ============================================================================

# Create pipeline state file with JSON metadata
# Usage: create_pipeline_state STATE TITLE TIMESTAMP METADATA_JSON
# Returns: path to created state file
# File format: TITLE-TIMESTAMP.STATE (visible, not hidden)
create_pipeline_state() {
    local state="$1"
    local title="$2"
    local timestamp="$3"
    local metadata="$4"
    local state_file="${STAGING_DIR}/${title}-${timestamp}.${state}"

    echo "$metadata" > "$state_file"
    log_debug "Created pipeline state file: $state_file"
    echo "$state_file"
}

# Read metadata from pipeline state file
# Usage: read_pipeline_state STATE_FILE
# Returns: JSON metadata or empty object if file doesn't exist
read_pipeline_state() {
    local state_file="$1"
    if [[ -f "$state_file" ]]; then
        cat "$state_file"
    else
        echo "{}"
    fi
}

# Find oldest state file of given type (for queue processing)
# Usage: find_oldest_state STATE
# Returns: path to oldest state file, or empty if none found
find_oldest_state() {
    local state="$1"
    find "$STAGING_DIR" -maxdepth 1 -name "*.${state}" -type f -printf '%T@ %p\n' 2>/dev/null | \
        sort -n | head -1 | cut -d' ' -f2-
}

# Count pending state files of given type
# Usage: count_pending_state STATE
# Returns: count of matching state files
count_pending_state() {
    local state="$1"
    find "$STAGING_DIR" -maxdepth 1 -name "*.${state}" -type f 2>/dev/null | wc -l
}

# Transition state: remove old state file, create new one with same metadata
# Usage: transition_state OLD_STATE_FILE NEW_STATE
# Returns: path to new state file
transition_state() {
    local old_state_file="$1"
    local new_state="$2"

    # Extract title and timestamp from old state file name
    local basename=$(basename "$old_state_file")
    # Format: TITLE-TIMESTAMP.STATE
    local old_state="${basename##*.}"         # Get STATE (extension)
    local name_part="${basename%.${old_state}}"  # Remove .STATE suffix
    local title="${name_part%-*}"             # Remove -TIMESTAMP
    local timestamp="${name_part##*-}"        # Get TIMESTAMP

    # Read existing metadata
    local metadata=$(read_pipeline_state "$old_state_file")

    # Create new state file
    local new_state_file=$(create_pipeline_state "$new_state" "$title" "$timestamp" "$metadata")

    # Remove old state file
    remove_state_file "$old_state_file"

    echo "$new_state_file"
}

# Parse JSON field from state metadata (simple bash parsing)
# Usage: parse_json_field METADATA FIELD
# Returns: field value or empty string
parse_json_field() {
    local metadata="$1"
    local field="$2"
    echo "$metadata" | grep -oP "\"$field\":\s*\"?\K[^\",$}]+" | head -1
}

# Build JSON metadata for state files
# Usage: build_state_metadata TITLE YEAR TIMESTAMP MAIN_TITLE ISO_PATH
# Returns: JSON string
build_state_metadata() {
    local title="$1"
    local year="$2"
    local timestamp="$3"
    local main_title="$4"
    local iso_path="$5"
    local mkv_path="${6:-}"
    local created_at=$(date -Iseconds)

    cat <<EOF
{
  "title": "$title",
  "year": "$year",
  "timestamp": "$timestamp",
  "main_title": "$main_title",
  "iso_path": "$iso_path",
  "mkv_path": "$mkv_path",
  "created_at": "$created_at"
}
EOF
}
