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
LOG_LEVEL="${LOG_LEVEL:-INFO}"
LOCK_FILE="${LOCK_FILE:-/run/dvd-ripper/dvd-ripper.pid}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_DELAY="${RETRY_DELAY:-60}"
DISK_USAGE_THRESHOLD="${DISK_USAGE_THRESHOLD:-80}"

# Per-stage logging configuration
LOG_DIR="${LOG_DIR:-/var/log/dvd-ripper}"
LOG_FILE_ISO="${LOG_DIR}/iso.log"
LOG_FILE_ENCODER="${LOG_DIR}/encoder.log"
LOG_FILE_TRANSFER="${LOG_DIR}/transfer.log"
LOG_FILE_DISTRIBUTE="${LOG_DIR}/distribute.log"

# Current stage (set by each pipeline script)
# Valid values: iso, encoder, transfer, distribute
CURRENT_STAGE=""

# Current device (set by iso script for per-drive logging)
# E.g., "sr0", "sr1" - used for per-device ISO log files
CURRENT_DEVICE=""

# Log levels
declare -A LOG_LEVELS=([DEBUG]=0 [INFO]=1 [WARN]=2 [ERROR]=3)
CURRENT_LOG_LEVEL=${LOG_LEVELS[$LOG_LEVEL]:-1}

# ============================================================================
# Logging Functions
# ============================================================================

# Get log file path for current stage
# Returns: path to stage-specific log file
get_stage_log_file() {
    # Allow override for parallel encoding (per-slot log files)
    if [[ -n "${LOG_FILE_OVERRIDE:-}" ]]; then
        echo "$LOG_FILE_OVERRIDE"
        return
    fi

    case "$CURRENT_STAGE" in
        iso)        echo "$LOG_FILE_ISO" ;;
        encoder)    echo "$LOG_FILE_ENCODER" ;;
        transfer)   echo "$LOG_FILE_TRANSFER" ;;
        distribute) echo "$LOG_FILE_DISTRIBUTE" ;;
        archive)    echo "$LOG_FILE_ARCHIVE" ;;
        *)          echo "$LOG_FILE_ISO" ;;  # Default to iso.log
    esac
}

# Get per-device log file path for ISO operations
# Used for ddrescue output to enable per-drive progress tracking
# Returns: path to device-specific log file (e.g., iso-sr0.log)
get_device_log_file() {
    if [[ -n "$CURRENT_DEVICE" && "$CURRENT_STAGE" == "iso" ]]; then
        echo "${LOG_DIR}/iso-${CURRENT_DEVICE}.log"
    else
        get_stage_log_file
    fi
}

# Log message with timestamp and level
# Usage: log_message LEVEL "message"
log_message() {
    local level="$1"
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local level_num=${LOG_LEVELS[$level]:-1}
    local log_file=$(get_stage_log_file)

    # Only log if message level >= current log level
    if [[ $level_num -ge $CURRENT_LOG_LEVEL ]]; then
        echo "[$timestamp] [$level] $message" >> "$log_file"
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
    local output_file="$2"
    # local mapfile="${output_iso}.mapfile"

    log_info "Creating ISO from $device to $output_file"

    # Check if HandbrakeCLI is available
    if ! command -v HandBrakeCLI &>/dev/null; then
        log_error "HandBrakeCLI not found. Install with: sudo apt-get install handbrake-cli"
        return 1
    fi

    # Run HandBrakeCLI to create ISO HandBrakeCLI --input /dev/sr0 --output "Movie_HEVC.mkv" --format av_mkv --encoder x265 --quality 22 --main-feature
    if HandBrakeCLI --input "$device" --output "$output_file" --format av_mp4 --encoder x264 --quality 22 --main-feature --optimize >> "$(get_device_log_file)" 2>&1; then
        log_info "ISO creation completed successfully"

        # Verify ISO file exists and has reasonable size
        if [[ -f "$output_file" ]]; then
            local video_file_size=$(stat -c%s "$output_file" 2>/dev/null || echo "0")
            local video_file_size_mb=$((video_file_size / 1024 / 1024))
            log_info "ISO size: ${video_file_size_mb}MB"

            if [[ $video_file_size_mb -lt 100 ]]; then
                log_warn "ISO file seems too small (${video_file_size_mb}MB), may be incomplete"
            fi
        fi

        return 0
    else
        log_error "ISO creation failed"
        return 1
    fi

    # # Check if ddrescue is available
    # if ! command -v ddrescue &>/dev/null; then
    #     log_error "ddrescue not found. Install with: sudo apt-get install gddrescue"
    #     return 1
    # fi

    # # Run ddrescue with error recovery
    # # -n = no scraping (faster initial pass)
    # # -b 2048 = DVD sector size
    # if ddrescue -n -b 2048 "$device" "$output_iso" "$mapfile" >> "$(get_device_log_file)" 2>&1; then
    #     log_info "ISO creation completed successfully"

    #     # Verify ISO file exists and has reasonable size
    #     if [[ -f "$output_iso" ]]; then
    #         local iso_size=$(stat -c%s "$output_iso" 2>/dev/null || echo "0")
    #         local iso_size_mb=$((iso_size / 1024 / 1024))
    #         log_info "ISO size: ${iso_size_mb}MB"

    #         if [[ $iso_size_mb -lt 100 ]]; then
    #             log_warn "ISO file seems too small (${iso_size_mb}MB), may be incomplete"
    #         fi
    #     fi

    #     return 0
    # else
    #     log_error "ISO creation failed"
    #     return 1
    # fi
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
    # Use -t 0 to scan ALL titles so we can find the longest one
    scan_output=$(HandBrakeCLI --scan -t 0 -i "$device" 2>&1)

    # Check if we got valid output instead of relying on exit code
    # Note: Using 'grep > /dev/null' instead of 'grep -q' to avoid broken pipe with pipefail
    if [[ -z "$scan_output" ]] || ! echo "$scan_output" | grep "scan:" > /dev/null; then
        log_error "HandBrake scan failed - no valid output"
        return 1
    fi

    # Extract disc title from libdvdnav output (not track numbers)
    # Line format: [HH:MM:SS] libdvdnav: DVD Title: THE_MATRIX
    local title=$(echo "$scan_output" | grep -oP 'libdvdnav: DVD Title: \K.*' | head -1 | xargs)

    # Extract main title number (actually find the longest title by duration)
    # Parse "+ title N:" and "  + duration: HH:MM:SS" pairs
    local main_title duration
    read -r main_title duration < <(echo "$scan_output" | awk '
        /^\+ title [0-9]+:/ {
            title = $3
            gsub(/:/, "", title)
        }
        /^  \+ duration:/ && title {
            dur = $3
            # Convert duration to seconds for comparison
            split(dur, t, ":")
            secs = t[1]*3600 + t[2]*60 + t[3]
            if (secs > max_secs) {
                max_secs = secs
                max_title = title
                max_dur = dur
            }
            title = ""
        }
        END {
            if (max_title) print max_title, max_dur
        }
    ')

    # Fallback if parsing failed
    if [[ -z "$main_title" ]]; then
        main_title=$(echo "$scan_output" | grep "^+ title" | grep -oP '\+ title \K\d+' | head -1)
        duration=$(echo "$scan_output" | grep "duration:" | head -1 | grep -oP 'duration: \K[0-9:]+')
    fi

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
# Generic Title Detection
# ============================================================================

# Check if title appears to be a generic/fallback name
# Used to flag items that need user identification
# Usage: is_generic_title TITLE
# Returns: 0 if generic (needs identification), 1 if appears to be real title
is_generic_title() {
    local title="$1"
    local upper_title="${title^^}"

    # Pattern 1: Our fallback format DVD_YYYYMMDD_HHMMSS
    if [[ "$title" =~ ^DVD_[0-9]{8}_[0-9]{6}$ ]]; then
        return 0
    fi

    # Pattern 2: Common generic DVD volume labels (case-insensitive)
    if [[ "$upper_title" =~ ^(DVD|DVD_VIDEO|DVDVIDEO|DISC[0-9]*|DISK[0-9]*|VIDEO_TS|MYDVD)$ ]]; then
        return 0
    fi

    # Pattern 3: Very short titles (likely generic)
    if [[ ${#title} -le 3 ]]; then
        return 0
    fi

    # Not a generic title
    return 1
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
    # Note: Using 'grep > /dev/null' instead of 'grep -q' to avoid broken pipe with pipefail
    if ls "$STAGING_DIR"/$pattern 2>/dev/null | grep . > /dev/null; then
        log_warn "Duplicate found in staging directory: $pattern"
        return 0
    fi

    # Check NAS if configured
    if [[ -n "$NAS_HOST" ]] && [[ -n "$NAS_USER" ]] && [[ -n "$NAS_PATH" ]]; then
        log_debug "Checking for duplicates on NAS"
        if ssh "${NAS_USER}@${NAS_HOST}" "ls ${NAS_PATH}/${pattern}" 2>/dev/null | grep . > /dev/null; then
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

    # Build SSH options with identity file if configured
    local ssh_opts=""
    if [[ -n "${NAS_SSH_IDENTITY:-}" ]] && [[ -f "$NAS_SSH_IDENTITY" ]]; then
        ssh_opts="-i $NAS_SSH_IDENTITY"
        log_debug "Using SSH identity file: $NAS_SSH_IDENTITY"
    fi

    while [[ $attempt -le $MAX_RETRIES ]]; do
        log_info "Transfer attempt $attempt/$MAX_RETRIES: $filename to ${NAS_USER}@${NAS_HOST}:${NAS_PATH}"

        if [[ "$NAS_TRANSFER_METHOD" == "rsync" ]]; then
            if [[ -n "$ssh_opts" ]]; then
                rsync -avz --progress -e "ssh $ssh_opts" "$local_file" "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/" >> "$(get_stage_log_file)" 2>&1
            else
                rsync -avz --progress "$local_file" "${NAS_USER}@${NAS_HOST}:${NAS_PATH}/" >> "$(get_stage_log_file)" 2>&1
            fi
        else
            if [[ -n "$ssh_opts" ]]; then
                scp $ssh_opts "$local_file" "${NAS_USER}@${NAS_HOST}:${remote_path}" >> "$(get_stage_log_file)" 2>&1
            else
                scp "$local_file" "${NAS_USER}@${NAS_HOST}:${remote_path}" >> "$(get_stage_log_file)" 2>&1
            fi
        fi

        if [[ $? -eq 0 ]]; then
            log_info "Transfer successful"

            # Verify remote file size matches
            local local_size=$(stat -c%s "$local_file")
            local remote_size
            if [[ -n "$ssh_opts" ]]; then
                remote_size=$(ssh $ssh_opts "${NAS_USER}@${NAS_HOST}" "stat -c%s \"${remote_path}\"" 2>/dev/null)
            else
                remote_size=$(ssh "${NAS_USER}@${NAS_HOST}" "stat -c%s \"${remote_path}\"" 2>/dev/null)
            fi

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
#
# Handles desktop-automounted discs by using udisksctl for unmount (uses polkit)
# before ejecting. Falls back to direct eject if udisksctl unavailable.
eject_disc() {
    local device="$1"
    local block_device

    # Normalize device path (e.g., /dev/sr0)
    block_device=$(readlink -f "$device")

    log_info "Ejecting disc from $device"

    # First, try to unmount using udisksctl (works with polkit, no root needed)
    # This handles desktop-automounted discs at /media/username/...
    if command -v udisksctl &>/dev/null; then
        log_debug "Attempting unmount via udisksctl"
        if udisksctl unmount -b "$block_device" 2>&1 | tee -a "$(get_stage_log_file)"; then
            log_debug "udisksctl unmount successful"
        else
            # Not an error - disc might not be mounted
            log_debug "udisksctl unmount returned non-zero (disc may not be mounted)"
        fi
    fi

    # Now eject - retry a few times in case device is briefly busy
    local attempt
    for attempt in 1 2 3; do
        if eject "$block_device" 2>&1 | tee -a "$(get_stage_log_file)"; then
            log_info "Disc ejected successfully"
            return 0
        fi

        if [[ $attempt -lt 3 ]]; then
            log_debug "Eject attempt $attempt failed, retrying in 2s..."
            sleep 2
        fi
    done

    log_error "Failed to eject disc after 3 attempts"
    return 1
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
# CSS Key Management
# ============================================================================

# Get the dvdcss cache directory for a disc by volume label
# Usage: get_dvdcss_cache_dir VOLUME_LABEL
# Returns: Path to most recently modified cache dir, or empty if not found
get_dvdcss_cache_dir() {
    local volume_label="$1"
    local cache_dir="${DVDCSS_CACHE:-/var/cache/dvdcss}"

    if [[ -z "$volume_label" ]]; then
        return 1
    fi

    # Find matching cache directory (most recently modified, exclude -0000000000)
    find "$cache_dir" -maxdepth 1 -type d -name "${volume_label}-*" \
        ! -name "*-0000000000" \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-
}

# Package CSS keys alongside ISO file for cluster distribution
# Usage: package_dvdcss_keys ISO_PATH VOLUME_LABEL
# Creates: ISO_PATH.keys/ directory containing CSS decryption keys
package_dvdcss_keys() {
    local iso_path="$1"
    local volume_label="$2"
    local keys_dir="${iso_path}.keys"

    local cache_dir=$(get_dvdcss_cache_dir "$volume_label")
    if [[ -z "$cache_dir" || ! -d "$cache_dir" ]]; then
        log_warn "No dvdcss cache found for volume label: $volume_label"
        return 1
    fi

    mkdir -p "$keys_dir"
    cp -a "$cache_dir"/* "$keys_dir/" 2>/dev/null

    # Store the original cache dir name for reference (needed for import)
    basename "$cache_dir" > "$keys_dir/.disc_id"

    # Set ownership to match ISO file
    chown -R "$(stat -c '%U:%G' "$iso_path")" "$keys_dir" 2>/dev/null

    local key_count=$(find "$keys_dir" -maxdepth 1 -type f ! -name '.disc_id' | wc -l)
    log_info "Packaged $key_count CSS keys to $keys_dir"
    return 0
}

# Import CSS keys from ISO sidecar to local dvdcss cache
# Usage: import_dvdcss_keys ISO_PATH
# Copies keys from ISO_PATH.keys/ to local cache with -0000000000 suffix
import_dvdcss_keys() {
    local iso_path="$1"
    local keys_dir="${iso_path}.keys"
    local cache_dir="${DVDCSS_CACHE:-/var/cache/dvdcss}"

    if [[ ! -d "$keys_dir" ]]; then
        log_debug "No keys directory found at $keys_dir"
        return 1
    fi

    # Read the original disc ID
    local disc_id=""
    if [[ -f "$keys_dir/.disc_id" ]]; then
        disc_id=$(cat "$keys_dir/.disc_id")
    fi

    if [[ -z "$disc_id" ]]; then
        log_warn "No disc ID found in $keys_dir/.disc_id"
        return 1
    fi

    # Create cache directory for ISO access pattern (-0000000000 suffix)
    # Extract base name without suffix (e.g., DVD_VIDEO-xxx from DVD_VIDEO-xxx-1762a2987d)
    local base_name="${disc_id%-*}"
    local iso_cache_dir="$cache_dir/${base_name}-0000000000"

    mkdir -p "$iso_cache_dir"
    cp -n "$keys_dir"/* "$iso_cache_dir/" 2>/dev/null  # -n = don't overwrite
    rm -f "$iso_cache_dir/.disc_id"  # Don't keep the metadata file in cache

    local key_count=$(find "$iso_cache_dir" -maxdepth 1 -type f | wc -l)
    log_info "Imported $key_count CSS keys to $iso_cache_dir"
    return 0
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
# Creates log directory and ensures stage-specific log file is writable
init_logging() {
    # Create log directory if it doesn't exist
    if [[ ! -d "$LOG_DIR" ]]; then
        mkdir -p "$LOG_DIR"
        chmod 770 "$LOG_DIR"
    fi

    # Get the log file for the current stage
    local log_file=$(get_stage_log_file)

    # Ensure log file exists and is writable (use >> instead of touch for kernel 6.17+ compatibility)
    # touch fails on newer kernels when file is owned by different user, even with group write
    if [[ ! -f "$log_file" ]]; then
        touch "$log_file" 2>/dev/null || {
            echo "ERROR: Cannot create log file: $log_file" >&2
            return 1
        }
    else
        # Test write permission by appending nothing
        : >> "$log_file" 2>/dev/null || {
            echo "ERROR: Cannot write to log file: $log_file" >&2
            return 1
        }
    fi

    log_info "==================== DVD Ripper Started ===================="
    return 0
}

# ============================================================================
# Pipeline Mode: Stage-Specific Lock Management
# ============================================================================

# Default lock file paths for pipeline mode
ISO_LOCK_FILE="${ISO_LOCK_FILE:-/run/dvd-ripper/iso.lock}"
ENCODER_LOCK_FILE="${ENCODER_LOCK_FILE:-/run/dvd-ripper/encoder.lock}"
TRANSFER_LOCK_FILE="${TRANSFER_LOCK_FILE:-/run/dvd-ripper/transfer.lock}"
DISTRIBUTE_LOCK_FILE="${DISTRIBUTE_LOCK_FILE:-/run/dvd-ripper/distribute.lock}"
AUDIT_LOCK_FILE="${AUDIT_LOCK_FILE:-/run/dvd-ripper/audit.lock}"

# Acquire stage-specific lock (non-blocking)
# Usage: acquire_stage_lock STAGE [DEVICE]
# STAGE: iso, encoder, transfer, distribute
# DEVICE: Optional device identifier for per-device locks (e.g., "sr0" for ISO stage)
# Returns: 0 if acquired, 1 if already locked
acquire_stage_lock() {
    local stage="$1"
    local device="${2:-}"  # Optional device identifier for per-device locks
    local lock_file

    case "$stage" in
        iso)
            if [[ -n "$device" ]]; then
                # Per-device lock for parallel ISO ripping from multiple drives
                lock_file="/run/dvd-ripper/iso-${device}.lock"
            else
                lock_file="$ISO_LOCK_FILE"
            fi
            ;;
        encoder)    lock_file="$ENCODER_LOCK_FILE" ;;
        transfer)   lock_file="$TRANSFER_LOCK_FILE" ;;
        distribute) lock_file="$DISTRIBUTE_LOCK_FILE" ;;
        audit)      lock_file="$AUDIT_LOCK_FILE" ;;
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
    chmod 664 "$lock_file" 2>/dev/null  # Group-writable for multi-user access
    log_debug "Acquired $stage lock with PID $$"
    return 0
}

# Release stage-specific lock
# Usage: release_stage_lock STAGE [DEVICE]
# DEVICE: Optional device identifier for per-device locks (e.g., "sr0" for ISO stage)
release_stage_lock() {
    local stage="$1"
    local device="${2:-}"  # Optional device identifier for per-device locks
    local lock_file

    case "$stage" in
        iso)
            if [[ -n "$device" ]]; then
                # Per-device lock for parallel ISO ripping from multiple drives
                lock_file="/run/dvd-ripper/iso-${device}.lock"
            else
                lock_file="$ISO_LOCK_FILE"
            fi
            ;;
        encoder)    lock_file="$ENCODER_LOCK_FILE" ;;
        transfer)   lock_file="$TRANSFER_LOCK_FILE" ;;
        distribute) lock_file="$DISTRIBUTE_LOCK_FILE" ;;
        audit)      lock_file="$AUDIT_LOCK_FILE" ;;
        *)          return ;;
    esac

    if [[ -f "$lock_file" ]]; then
        rm -f "$lock_file"
        log_debug "Released $stage lock"
    fi
}

# Check if there are other active ISO locks (besides our own device)
# Usage: has_other_active_iso_locks [EXCLUDE_DEVICE]
# EXCLUDE_DEVICE: Device to exclude from check (e.g., "sr0")
# Returns: 0 if other active locks exist, 1 if none
has_other_active_iso_locks() {
    local exclude_device="${1:-}"
    local lock_dir="/run/dvd-ripper"

    for lock_file in "$lock_dir"/iso-*.lock; do
        [[ -f "$lock_file" ]] || continue

        # Skip our own device's lock
        if [[ -n "$exclude_device" ]]; then
            local lock_device="${lock_file##*/iso-}"
            lock_device="${lock_device%.lock}"
            [[ "$lock_device" == "$exclude_device" ]] && continue
        fi

        # Check if the lock is held by an active process
        local pid=$(cat "$lock_file" 2>/dev/null)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            return 0  # Found active lock
        fi
    done

    return 1  # No other active locks
}

# ============================================================================
# Pipeline Mode: Parallel Encoding Support
# ============================================================================

# Default parallel encoding configuration
MAX_PARALLEL_ENCODERS="${MAX_PARALLEL_ENCODERS:-2}"
ENCODER_LOAD_THRESHOLD="${ENCODER_LOAD_THRESHOLD:-0.8}"
ENABLE_PARALLEL_ENCODING="${ENABLE_PARALLEL_ENCODING:-0}"

# Get current 1-minute load average
# Usage: get_load_average
# Returns: load average as decimal (e.g., "2.15")
get_load_average() {
    cut -d' ' -f1 /proc/loadavg
}

# Get CPU count
# Usage: get_cpu_count
# Returns: number of CPU cores
get_cpu_count() {
    nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo "1"
}

# Check if system load is low enough to start an encoder
# Usage: should_start_encoder
# Returns: 0 if safe to start, 1 if system too busy
should_start_encoder() {
    local load_1m=$(get_load_average)
    local cpu_count=$(get_cpu_count)
    local threshold=$(awk "BEGIN {printf \"%.2f\", $cpu_count * $ENCODER_LOAD_THRESHOLD}")

    # Compare load to threshold using awk for decimal comparison
    local is_under_threshold=$(awk "BEGIN {print ($load_1m < $threshold) ? 1 : 0}")

    if [[ "$is_under_threshold" == "1" ]]; then
        log_debug "Load check passed: $load_1m < $threshold (${cpu_count} cores * ${ENCODER_LOAD_THRESHOLD})"
        return 0
    else
        log_info "Load too high: $load_1m >= $threshold, skipping encoder start"
        return 1
    fi
}

# Count currently active encoder slots
# Usage: count_active_encoders
# Returns: number of active encoder processes
count_active_encoders() {
    local count=0
    for i in $(seq 1 "$MAX_PARALLEL_ENCODERS"); do
        local lock_file="/run/dvd-ripper/encoder-${i}.lock"
        if [[ -f "$lock_file" ]]; then
            local pid=$(cat "$lock_file" 2>/dev/null)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                count=$((count + 1))
            fi
        fi
    done
    echo "$count"
}

# Acquire an encoder slot for parallel encoding
# Usage: acquire_encoder_slot
# Returns: slot number (1-N) on stdout, 0 exit code if acquired, 1 if none available
acquire_encoder_slot() {
    # If parallel encoding is disabled, use legacy single lock
    if [[ "$ENABLE_PARALLEL_ENCODING" != "1" ]]; then
        if acquire_stage_lock "encoder"; then
            echo "0"  # Slot 0 = legacy mode
            return 0
        fi
        return 1
    fi

    # Check load before acquiring
    if ! should_start_encoder; then
        return 1
    fi

    # Try to find an available slot
    for i in $(seq 1 "$MAX_PARALLEL_ENCODERS"); do
        local lock_file="/run/dvd-ripper/encoder-${i}.lock"

        if [[ -f "$lock_file" ]]; then
            local existing_pid=$(cat "$lock_file" 2>/dev/null)
            if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
                # Slot is busy
                continue
            else
                # Stale lock, clean up
                log_info "Stale encoder-${i} lock found, removing"
                rm -f "$lock_file"
            fi
        fi

        # Try to claim this slot
        echo "$$" > "$lock_file"
        chmod 664 "$lock_file" 2>/dev/null  # Group-writable for multi-user access
        log_info "Acquired encoder slot $i with PID $$"
        echo "$i"
        return 0
    done

    log_debug "All encoder slots ($MAX_PARALLEL_ENCODERS) are busy"
    return 1
}

# Release a specific encoder slot
# Usage: release_encoder_slot SLOT_NUMBER
release_encoder_slot() {
    local slot="$1"

    # If slot 0 (legacy mode), use legacy release
    if [[ "$slot" == "0" ]]; then
        release_stage_lock "encoder"
        return
    fi

    local lock_file="/run/dvd-ripper/encoder-${slot}.lock"
    if [[ -f "$lock_file" ]]; then
        rm -f "$lock_file"
        log_debug "Released encoder slot $slot"
    fi
}

# ============================================================================
# Pipeline Mode: Parallel Transfer Support
# ============================================================================

# Default parallel transfer configuration
MAX_PARALLEL_TRANSFERS="${MAX_PARALLEL_TRANSFERS:-5}"
ENABLE_PARALLEL_TRANSFERS="${ENABLE_PARALLEL_TRANSFERS:-1}"

# Count currently active transfer slots
# Usage: count_active_transfers
# Returns: number of active transfer processes
count_active_transfers() {
    local count=0
    for i in $(seq 1 "$MAX_PARALLEL_TRANSFERS"); do
        local lock_file="/run/dvd-ripper/transfer-${i}.lock"
        if [[ -f "$lock_file" ]]; then
            local pid=$(cat "$lock_file" 2>/dev/null)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                count=$((count + 1))
            fi
        fi
    done
    echo "$count"
}

# Acquire a transfer slot for parallel transfers
# Usage: acquire_transfer_slot
# Returns: slot number (1-N) on stdout, 0 exit code if acquired, 1 if none available
acquire_transfer_slot() {
    # If parallel transfers disabled, use legacy single lock
    if [[ "$ENABLE_PARALLEL_TRANSFERS" != "1" ]]; then
        if acquire_stage_lock "transfer"; then
            echo "0"  # Slot 0 = legacy mode
            return 0
        fi
        return 1
    fi

    # Try to find an available slot
    for i in $(seq 1 "$MAX_PARALLEL_TRANSFERS"); do
        local lock_file="/run/dvd-ripper/transfer-${i}.lock"

        if [[ -f "$lock_file" ]]; then
            local existing_pid=$(cat "$lock_file" 2>/dev/null)
            if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
                # Slot is busy
                continue
            else
                # Stale lock, clean up
                log_info "Stale transfer-${i} lock found, removing"
                rm -f "$lock_file"
            fi
        fi

        # Try to claim this slot
        echo "$$" > "$lock_file"
        chmod 664 "$lock_file" 2>/dev/null  # Group-writable for multi-user access
        log_info "Acquired transfer slot $i with PID $$"
        echo "$i"
        return 0
    done

    log_debug "All transfer slots ($MAX_PARALLEL_TRANSFERS) are busy"
    return 1
}

# Release a specific transfer slot
# Usage: release_transfer_slot SLOT_NUMBER
release_transfer_slot() {
    local slot="$1"

    # If slot 0 (legacy mode), use legacy release
    if [[ "$slot" == "0" ]]; then
        release_stage_lock "transfer"
        return
    fi

    local lock_file="/run/dvd-ripper/transfer-${slot}.lock"
    if [[ -f "$lock_file" ]]; then
        rm -f "$lock_file"
        log_debug "Released transfer slot $slot"
    fi
}

# Get log file for a specific transfer slot
# Usage: get_transfer_log_file SLOT_NUMBER
# Returns: path to slot-specific log file
get_transfer_log_file() {
    local slot="$1"
    if [[ "$slot" == "0" ]]; then
        echo "/var/log/dvd-ripper/transfer.log"
    else
        echo "/var/log/dvd-ripper/transfer.${slot}.log"
    fi
}

# ============================================================================
# Pipeline Mode: State File Management
# ============================================================================

# Claim a state file atomically for processing
# Used to prevent race conditions when multiple encoders run in parallel
# Usage: claim_state_file STATE_FILE NEW_STATE
# Returns: 0 if claimed successfully, 1 if another worker got it
claim_state_file() {
    local state_file="$1"
    local new_state="$2"

    # Extract components from state file name
    local basename=$(basename "$state_file")
    local old_state="${basename##*.}"
    local name_part="${basename%.${old_state}}"

    local new_file="${STAGING_DIR}/${name_part}.${new_state}"

    # Try atomic rename - if it fails, another worker got it
    if mv "$state_file" "$new_file" 2>/dev/null; then
        # Ensure file is group-writable for next pipeline stage
        chmod g+w "$new_file" 2>/dev/null || true
        log_info "Claimed: $basename -> ${name_part}.${new_state}"
        echo "$new_file"
        return 0
    else
        log_debug "Failed to claim $basename - another worker got it"
        return 1
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
    # Make group-writable so other pipeline stages can update the file
    chmod g+w "$state_file" 2>/dev/null || true
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
# Note: Uses || true to handle empty values without failing under set -e
parse_json_field() {
    local metadata="$1"
    local field="$2"
    echo "$metadata" | grep -oP "\"$field\":\s*\"?\K[^\",$}]+" 2>/dev/null | head -1 || true
}

# Trigger next pipeline stage if event-driven triggers are enabled
# Usage: trigger_next_stage CURRENT_STATE
# Returns: 0 on success or if disabled, 1 on failure
trigger_next_stage() {
    local current_state="$1"

    if [[ "${TRIGGER_NEXT_STAGE:-0}" != "1" ]]; then
        return 0
    fi

    local target_services=""
    case "$current_state" in
        iso-ready)
            # Trigger both encoder AND distributor in parallel
            target_services="dvd-encoder.service dvd-distribute.service"
            ;;
        encoded-ready)
            target_services="dvd-transfer.service"
            ;;
        *)
            return 0
            ;;
    esac

    for service in $target_services; do
        log_info "[TRIGGER] Starting $service"
        if systemctl start "$service" 2>/dev/null; then
            log_info "[TRIGGER] $service started"
        else
            log_warn "[TRIGGER] Failed to start $service (may already be running)"
        fi
    done
    return 0
}

# ============================================================================
# Cluster Mode: Distributed Encoding Support
# ============================================================================

# Default cluster configuration
CLUSTER_ENABLED="${CLUSTER_ENABLED:-0}"
CLUSTER_NODE_NAME="${CLUSTER_NODE_NAME:-}"
CLUSTER_PEERS="${CLUSTER_PEERS:-}"
CLUSTER_SSH_USER="${CLUSTER_SSH_USER:-}"
CLUSTER_SSH_IDENTITY="${CLUSTER_SSH_IDENTITY:-}"
CLUSTER_REMOTE_STAGING="${CLUSTER_REMOTE_STAGING:-/var/tmp/dvd-rips}"

# Check if we should distribute a job to a peer node
# Returns: 0 if should distribute, 1 if should encode locally
# Logic: Distribute if cluster enabled, peers configured, and multiple jobs queued
# Note: We no longer require high local load - the goal is to utilize idle peers proactively
should_distribute_job() {
    # Only consider distribution if cluster is enabled
    if [[ "$CLUSTER_ENABLED" != "1" ]]; then
        return 1
    fi

    # Check if we have peers configured
    if [[ -z "$CLUSTER_PEERS" ]]; then
        log_debug "[CLUSTER] No peers configured"
        return 1
    fi

    # Check queue depth - only distribute if multiple jobs queued
    # This ensures local machine still has work to do
    local queue_depth=$(count_pending_state "iso-ready")

    log_debug "[CLUSTER] Queue depth: $queue_depth"

    if [[ "$queue_depth" -gt 1 ]]; then
        log_info "[CLUSTER] Multiple jobs queued ($queue_depth), distribution candidate"
        return 0
    fi

    return 1
}

# Query a peer's capacity via API
# Usage: query_peer_capacity HOST PORT
# Returns: JSON capacity response on stdout, 0 if available, 1 if not
query_peer_capacity() {
    local host="$1"
    local port="$2"
    local timeout="${3:-5}"

    local response
    response=$(curl -s --connect-timeout "$timeout" "http://${host}:${port}/api/worker/capacity" 2>/dev/null)

    if [[ $? -ne 0 ]] || [[ -z "$response" ]]; then
        log_debug "[CLUSTER] Peer $host:$port unreachable"
        return 1
    fi

    # Check if peer is available
    local available=$(echo "$response" | grep -oP '"available":\s*\K(true|false)' | head -1)

    if [[ "$available" == "true" ]]; then
        echo "$response"
        return 0
    else
        log_debug "[CLUSTER] Peer $host:$port not available"
        return 1
    fi
}

# Find an available peer with encoding capacity
# Usage: find_available_peer
# Returns: "name:host:port" on stdout if found, 1 if none available
find_available_peer() {
    if [[ -z "$CLUSTER_PEERS" ]]; then
        return 1
    fi

    log_info "[CLUSTER] Checking peer availability..."

    for peer_entry in $CLUSTER_PEERS; do
        # Parse "name:host:port" format
        local name=$(echo "$peer_entry" | cut -d: -f1)
        local host=$(echo "$peer_entry" | cut -d: -f2)
        local port=$(echo "$peer_entry" | cut -d: -f3)

        if [[ -z "$host" ]] || [[ -z "$port" ]]; then
            log_warn "[CLUSTER] Invalid peer entry: $peer_entry"
            continue
        fi

        log_debug "[CLUSTER] Checking peer $name ($host:$port)..."

        if query_peer_capacity "$host" "$port" > /dev/null; then
            log_info "[CLUSTER] Found available peer: $name ($host:$port)"
            echo "$peer_entry"
            return 0
        fi
    done

    log_info "[CLUSTER] No peers with available capacity"
    return 1
}

# Distribute an ISO to a peer node for encoding
# Usage: distribute_to_peer STATE_FILE PEER_ENTRY
# PEER_ENTRY format: "name:host:port"
# Returns: 0 on success, 1 on failure
distribute_to_peer() {
    local state_file="$1"
    local peer_entry="$2"

    # Parse peer entry
    local peer_name=$(echo "$peer_entry" | cut -d: -f1)
    local peer_host=$(echo "$peer_entry" | cut -d: -f2)
    local peer_port=$(echo "$peer_entry" | cut -d: -f3)

    # Read state metadata
    local metadata=$(read_pipeline_state "$state_file")
    local title=$(parse_json_field "$metadata" "title")
    local timestamp=$(parse_json_field "$metadata" "timestamp")
    local iso_path=$(parse_json_field "$metadata" "iso_path")

    log_info "[CLUSTER] Distributing '$title' to peer $peer_name"

    # Verify ISO exists
    if [[ ! -f "$iso_path" ]]; then
        log_error "[CLUSTER] ISO file not found: $iso_path"
        return 1
    fi

    # Transition state to distributing
    remove_state_file "$state_file"
    local dist_state=$(create_pipeline_state "distributing" "$title" "$timestamp" "$metadata")

    # Transfer ISO to peer via rsync
    local remote_dest="${CLUSTER_SSH_USER}@${peer_host}:${CLUSTER_REMOTE_STAGING}/"
    log_info "[CLUSTER] Rsync ISO to $remote_dest"

    # Build SSH options with identity file if configured
    local ssh_opts=""
    if [[ -n "${CLUSTER_SSH_IDENTITY:-}" ]] && [[ -f "$CLUSTER_SSH_IDENTITY" ]]; then
        ssh_opts="-e ssh -i $CLUSTER_SSH_IDENTITY"
        log_debug "[CLUSTER] Using SSH identity file: $CLUSTER_SSH_IDENTITY"
    fi

    if ! rsync -avz --progress $ssh_opts "$iso_path" "$remote_dest" >> "$(get_stage_log_file)" 2>&1; then
        log_error "[CLUSTER] ISO transfer to $peer_name failed"
        # Revert state
        remove_state_file "$dist_state"
        create_pipeline_state "iso-ready" "$title" "$timestamp" "$metadata" > /dev/null
        return 1
    fi

    log_info "[CLUSTER] ISO transferred to $peer_name"

    # Also transfer CSS keys directory if it exists (for cluster decryption)
    if [[ -d "${iso_path}.keys" ]]; then
        if rsync -avz $ssh_opts "${iso_path}.keys" "$remote_dest" >> "$(get_stage_log_file)" 2>&1; then
            log_info "[CLUSTER] CSS keys transferred to $peer_name"
        else
            log_warn "[CLUSTER] Could not transfer CSS keys to $peer_name (encoding may need to crack keys)"
        fi
    fi

    # Update metadata with distribution info
    local new_metadata=$(update_metadata_for_distribution "$metadata" "$CLUSTER_NODE_NAME" "$peer_name")

    # Notify peer to start encoding via API
    local api_url="http://${peer_host}:${peer_port}/api/worker/accept-job"
    local api_response

    api_response=$(curl -s -X POST "$api_url" \
        -H "Content-Type: application/json" \
        -d "{\"metadata\": $new_metadata, \"origin\": \"$CLUSTER_NODE_NAME\"}" \
        2>/dev/null)

    # Note: Using 'grep > /dev/null' instead of 'grep -q' to avoid broken pipe with pipefail
    if [[ $? -ne 0 ]] || ! echo "$api_response" | grep '"status":\s*"accepted"' > /dev/null; then
        log_error "[CLUSTER] Peer $peer_name did not accept job"
        # Revert state
        remove_state_file "$dist_state"
        create_pipeline_state "iso-ready" "$title" "$timestamp" "$metadata" > /dev/null
        return 1
    fi

    log_info "[CLUSTER] Peer $peer_name accepted job"

    # Update state to distributed
    remove_state_file "$dist_state"
    create_pipeline_state "distributed-to-${peer_name}" "$title" "$timestamp" "$new_metadata" > /dev/null

    log_info "[CLUSTER] Job '$title' distributed to $peer_name successfully"
    return 0
}

# Update metadata JSON for distributed job
# Adds origin_node and is_remote_job fields
update_metadata_for_distribution() {
    local metadata="$1"
    local origin_node="$2"
    local dest_node="$3"

    # Add distribution fields to metadata
    # Simple string manipulation (metadata is valid JSON)
    echo "$metadata" | sed 's/}$/,\n  "origin_node": "'"$origin_node"'",\n  "dest_node": "'"$dest_node"'",\n  "is_remote_job": true\n}/'
}

# Build JSON metadata for state files
# Usage: build_state_metadata TITLE YEAR TIMESTAMP MAIN_TITLE ISO_PATH [MKV_PATH] [PREVIEW_PATH] [NAS_PATH]
# Returns: JSON string
build_state_metadata() {
    local title="$1"
    local year="$2"
    local timestamp="$3"
    local main_title="$4"
    local iso_path="$5"
    local mkv_path="${6:-}"
    local preview_path="${7:-}"
    local nas_path="${8:-}"
    local created_at=$(date -Iseconds)

    # Determine if item needs identification (generic title)
    local needs_id="false"
    if is_generic_title "$title"; then
        needs_id="true"
    fi

    cat <<EOF
{
  "title": "$title",
  "year": "$year",
  "timestamp": "$timestamp",
  "main_title": "$main_title",
  "iso_path": "$iso_path",
  "mkv_path": "$mkv_path",
  "preview_path": "$preview_path",
  "nas_path": "$nas_path",
  "needs_identification": $needs_id,
  "created_at": "$created_at"
}
EOF
}

# ============================================================================
# ISO Archival Compression Functions
# ============================================================================

# Default archival configuration
ENABLE_ISO_ARCHIVAL="${ENABLE_ISO_ARCHIVAL:-1}"
ENABLE_ISO_COMPRESS_FOR_ARCHIVAL="${ENABLE_ISO_COMPRESS_FOR_ARCHIVAL:-0}"
ISO_COMPRESSION_LEVEL="${ISO_COMPRESSION_LEVEL:-9}"
ISO_COMPRESSION_THREADS="${ISO_COMPRESSION_THREADS:-0}"
ENABLE_PAR2_RECOVERY="${ENABLE_PAR2_RECOVERY:-1}"
PAR2_REDUNDANCY_PERCENT="${PAR2_REDUNDANCY_PERCENT:-5}"
ISO_ARCHIVE_PATH="${ISO_ARCHIVE_PATH:-/var/lib/dvd/archives}"
NAS_ARCHIVE_PATH="${NAS_ARCHIVE_PATH:-}"
DELETE_ISO_AFTER_ARCHIVE="${DELETE_ISO_AFTER_ARCHIVE:-1}"
DELETE_LOCAL_XZ_AFTER_ARCHIVE="${DELETE_LOCAL_XZ_AFTER_ARCHIVE:-1}"

# Log file for archive stage
LOG_FILE_ARCHIVE="${LOG_DIR}/archive.log"

# Compress an ISO file using xz (LZMA2 compression)
# Usage: compress_iso ISO_PATH
# Returns: 0 on success, 1 on failure
# Creates: ISO_PATH.xz with CRC64 integrity check
compress_iso() {
    local iso_path="$1"
    local level="${ISO_COMPRESSION_LEVEL:-9}"
    local threads="${ISO_COMPRESSION_THREADS:-0}"

    if [[ ! -f "$iso_path" ]]; then
        log_error "ISO file not found: $iso_path"
        return 1
    fi

    # Check if xz is available
    if ! command -v xz &>/dev/null; then
        log_error "xz not found. Install with: sudo apt-get install xz-utils"
        return 1
    fi

    local iso_size=$(stat -c%s "$iso_path" 2>/dev/null)
    local iso_size_gb=$(awk "BEGIN {printf \"%.2f\", $iso_size / 1024 / 1024 / 1024}")
    log_info "Compressing ISO: $iso_path (${iso_size_gb}GB)"
    log_info "Compression settings: level=$level, threads=$threads"

    local start_time=$(date +%s)

    # xz compression with:
    # -${level}e = extreme compression at specified level
    # --threads=$threads = parallel compression (0=auto)
    # --keep = preserve original file
    # --check=crc64 = strong integrity verification
    if xz -${level}e --threads="$threads" --keep --check=crc64 "$iso_path" 2>> "$(get_stage_log_file)"; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        local xz_path="${iso_path}.xz"

        if [[ -f "$xz_path" ]]; then
            local xz_size=$(stat -c%s "$xz_path" 2>/dev/null)
            local xz_size_gb=$(awk "BEGIN {printf \"%.2f\", $xz_size / 1024 / 1024 / 1024}")
            local ratio=$(awk "BEGIN {printf \"%.2f\", $xz_size / $iso_size}")

            log_info "Compression complete in ${duration}s"
            log_info "Original: ${iso_size_gb}GB -> Compressed: ${xz_size_gb}GB (ratio: $ratio)"
            return 0
        else
            log_error "Compression completed but output file not found: $xz_path"
            return 1
        fi
    else
        log_error "xz compression failed for: $iso_path"
        return 1
    fi
}

# Verify a compressed ISO file integrity
# Usage: verify_compressed_iso XZ_PATH
# Returns: 0 if valid, 1 if corrupted or missing
verify_compressed_iso() {
    local xz_path="$1"

    if [[ ! -f "$xz_path" ]]; then
        log_error "Compressed file not found: $xz_path"
        return 1
    fi

    log_info "Verifying compressed file integrity: $xz_path"

    # xz -t tests the archive integrity without decompressing
    if xz -t "$xz_path" 2>> "$(get_stage_log_file)"; then
        log_info "Integrity verification passed: $xz_path"
        return 0
    else
        log_error "Integrity verification FAILED: $xz_path"
        return 1
    fi
}

# Generate PAR2 recovery files for a compressed ISO
# Usage: generate_recovery_files XZ_PATH
# Returns: 0 on success, 1 on failure
# Creates: XZ_PATH.par2 and XZ_PATH.volXXX+XXX.par2 files
generate_recovery_files() {
    local xz_path="$1"
    local redundancy="${PAR2_REDUNDANCY_PERCENT:-5}"

    if [[ ! -f "$xz_path" ]]; then
        log_error "Compressed file not found: $xz_path"
        return 1
    fi

    # Check if par2 is available
    if ! command -v par2 &>/dev/null; then
        log_error "par2 not found. Install with: sudo apt-get install par2"
        return 1
    fi

    log_info "Generating PAR2 recovery files for: $xz_path (${redundancy}% redundancy)"

    local start_time=$(date +%s)

    # par2 create with:
    # -r${redundancy} = percentage of recovery data
    # -n1 = single recovery file (simpler for archiving)
    if par2 create -r"$redundancy" -n1 "$xz_path" 2>> "$(get_stage_log_file)"; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))

        # Count created par2 files
        local par2_count=$(ls -1 "${xz_path}"*.par2 2>/dev/null | wc -l)
        log_info "PAR2 creation complete in ${duration}s ($par2_count files)"
        return 0
    else
        log_error "PAR2 creation failed for: $xz_path"
        return 1
    fi
}

# Find ISOs that are ready for archiving
# Looks for .archive-ready marker files created by the encoder
# Usage: find_archivable_isos
# Returns: list of ISO paths (one per line)
find_archivable_isos() {
    local staging_dir="${STAGING_DIR:-/var/tmp/dvd-rips}"

    # Find .archive-ready marker files
    find "$staging_dir" -maxdepth 1 -name "*.archive-ready" -type f 2>/dev/null | while read -r marker_file; do
        # Read ISO path from marker file metadata
        local iso_path=$(parse_json_field "$(cat "$marker_file" 2>/dev/null)" "iso_path")

        # Verify ISO still exists
        if [[ -z "$iso_path" ]] || [[ ! -f "$iso_path" ]]; then
            log_warn "ISO missing for marker: $marker_file"
            continue
        fi

        # Extract title-timestamp from ISO filename
        local basename=$(basename "$iso_path")
        local title_timestamp="${basename%.iso}"

        # Check if already archived (skip if .archived state exists)
        local archived_state="${staging_dir}/${title_timestamp}.archived"
        if [[ -f "$archived_state" ]]; then
            log_debug "Already archived, skipping: $iso_path"
            continue
        fi

        # Check if currently archiving (skip if .archiving state exists)
        local archiving_state="${staging_dir}/${title_timestamp}.archiving"
        if [[ -f "$archiving_state" ]]; then
            log_debug "Currently archiving, skipping: $iso_path"
            continue
        fi

        # Return the ISO path
        echo "$iso_path"
    done

    # Legacy support: also check for .iso.deletable files (migration path)
    find "$staging_dir" -maxdepth 1 -name "*.iso.deletable" -type f 2>/dev/null | while read -r deletable_file; do
        local basename=$(basename "$deletable_file")
        local title_timestamp="${basename%.iso.deletable}"

        # Skip if already archived or archiving
        if [[ -f "${staging_dir}/${title_timestamp}.archived" ]]; then
            continue
        fi
        if [[ -f "${staging_dir}/${title_timestamp}.archiving" ]]; then
            continue
        fi

        # Return the deletable file path (legacy - it IS the ISO)
        echo "$deletable_file"
    done
}

# Transfer archive files to NAS
# Usage: transfer_archive_to_nas XZ_PATH
# Returns: 0 on success, 1 on failure
# Transfers: .xz file and all .par2 files
transfer_archive_to_nas() {
    local xz_path="$1"
    local archive_path="${NAS_ARCHIVE_PATH:-}"

    if [[ -z "$archive_path" ]]; then
        log_error "NAS_ARCHIVE_PATH not configured"
        return 1
    fi

    if [[ -z "$NAS_HOST" ]] || [[ -z "$NAS_USER" ]]; then
        log_error "NAS configuration incomplete (NAS_HOST or NAS_USER missing)"
        return 1
    fi

    if [[ ! -f "$xz_path" ]]; then
        log_error "Compressed file not found: $xz_path"
        return 1
    fi

    log_info "Transferring archive to NAS: $xz_path -> ${NAS_HOST}:${archive_path}"

    # Build file list: .xz and all .par2 files
    local files_to_transfer=("$xz_path")
    for par2_file in "${xz_path}"*.par2; do
        [[ -f "$par2_file" ]] && files_to_transfer+=("$par2_file")
    done

    log_info "Transferring ${#files_to_transfer[@]} files to NAS archive"

    # Build SSH options with identity file if configured
    local ssh_opts=""
    if [[ -n "${NAS_SSH_IDENTITY:-}" ]] && [[ -f "$NAS_SSH_IDENTITY" ]]; then
        ssh_opts="-i $NAS_SSH_IDENTITY"
    fi

    local remote_dest="${NAS_USER}@${NAS_HOST}:${archive_path}/"

    # Transfer using rsync
    if [[ -n "$ssh_opts" ]]; then
        rsync -avz --progress -e "ssh $ssh_opts" "${files_to_transfer[@]}" "$remote_dest" >> "$(get_stage_log_file)" 2>&1
    else
        rsync -avz --progress "${files_to_transfer[@]}" "$remote_dest" >> "$(get_stage_log_file)" 2>&1
    fi

    if [[ $? -eq 0 ]]; then
        log_info "Archive transfer to NAS successful"

        # Verify remote files exist
        local xz_basename=$(basename "$xz_path")
        local remote_check
        if [[ -n "$ssh_opts" ]]; then
            remote_check=$(ssh $ssh_opts "${NAS_USER}@${NAS_HOST}" "ls -la \"${archive_path}/${xz_basename}\" 2>/dev/null")
        else
            remote_check=$(ssh "${NAS_USER}@${NAS_HOST}" "ls -la \"${archive_path}/${xz_basename}\" 2>/dev/null")
        fi

        if [[ -n "$remote_check" ]]; then
            log_info "Remote file verification passed"
            return 0
        else
            log_error "Remote file not found after transfer: ${archive_path}/${xz_basename}"
            return 1
        fi
    else
        log_error "Archive transfer to NAS failed"
        return 1
    fi
}

# Transfer raw ISO file to NAS (no compression)
# Usage: transfer_iso_to_nas ISO_PATH
# Returns: 0 on success, 1 on failure
transfer_iso_to_nas() {
    local iso_path="$1"
    local archive_path="${NAS_ARCHIVE_PATH:-}"

    if [[ -z "$archive_path" ]]; then
        log_error "NAS_ARCHIVE_PATH not configured"
        return 1
    fi

    if [[ -z "$NAS_HOST" ]] || [[ -z "$NAS_USER" ]]; then
        log_error "NAS configuration incomplete (NAS_HOST or NAS_USER missing)"
        return 1
    fi

    if [[ ! -f "$iso_path" ]]; then
        log_error "ISO file not found: $iso_path"
        return 1
    fi

    local iso_basename=$(basename "$iso_path")
    local iso_size_bytes=$(stat -c%s "$iso_path" 2>/dev/null || stat -f%z "$iso_path" 2>/dev/null)
    local iso_size_gb=$(echo "scale=2; $iso_size_bytes / 1073741824" | bc)

    log_info "Transferring ISO to NAS: $iso_basename (${iso_size_gb}GB) -> ${NAS_HOST}:${archive_path}"

    # Build SSH options with identity file if configured
    local ssh_opts=""
    if [[ -n "${NAS_SSH_IDENTITY:-}" ]] && [[ -f "$NAS_SSH_IDENTITY" ]]; then
        ssh_opts="-i $NAS_SSH_IDENTITY"
    fi

    local remote_dest="${NAS_USER}@${NAS_HOST}:${archive_path}/"

    # Transfer using rsync with progress
    local rsync_result
    if [[ -n "$ssh_opts" ]]; then
        rsync -avz --progress -e "ssh $ssh_opts" "$iso_path" "$remote_dest" >> "$(get_stage_log_file)" 2>&1
        rsync_result=$?
    else
        rsync -avz --progress "$iso_path" "$remote_dest" >> "$(get_stage_log_file)" 2>&1
        rsync_result=$?
    fi

    if [[ $rsync_result -eq 0 ]]; then
        log_info "ISO transfer to NAS successful"

        # Verify remote file exists and size matches
        local remote_size
        if [[ -n "$ssh_opts" ]]; then
            remote_size=$(ssh $ssh_opts "${NAS_USER}@${NAS_HOST}" "stat -c%s \"${archive_path}/${iso_basename}\" 2>/dev/null")
        else
            remote_size=$(ssh "${NAS_USER}@${NAS_HOST}" "stat -c%s \"${archive_path}/${iso_basename}\" 2>/dev/null")
        fi

        if [[ "$remote_size" == "$iso_size_bytes" ]]; then
            log_info "Remote ISO verification passed (${iso_size_gb}GB)"
            return 0
        elif [[ -n "$remote_size" ]]; then
            log_warn "Remote ISO size mismatch: local=$iso_size_bytes, remote=$remote_size"
            return 0  # Still consider success if file exists
        else
            log_error "Remote ISO not found after transfer: ${archive_path}/${iso_basename}"
            return 1
        fi
    else
        log_error "ISO transfer to NAS failed"
        return 1
    fi
}

# Build JSON metadata for archived state file
# Usage: build_archive_metadata TITLE TIMESTAMP ISO_PATH XZ_PATH NAS_PATH COMPRESSION_TIME
# Returns: JSON string
build_archive_metadata() {
    local title="$1"
    local timestamp="$2"
    local iso_path="$3"
    local xz_path="$4"
    local nas_path="$5"
    local compression_time="$6"
    local archived_at=$(date -Iseconds)

    # Calculate sizes
    local original_size=0
    local compressed_size=0
    local ratio="0.00"

    if [[ -f "$iso_path" ]]; then
        original_size=$(stat -c%s "$iso_path" 2>/dev/null || echo "0")
    fi

    if [[ -f "$xz_path" ]]; then
        compressed_size=$(stat -c%s "$xz_path" 2>/dev/null || echo "0")
    fi

    if [[ "$original_size" -gt 0 ]]; then
        ratio=$(awk "BEGIN {printf \"%.4f\", $compressed_size / $original_size}")
    fi

    # List par2 files
    local par2_files=""
    for par2_file in "${xz_path}"*.par2; do
        if [[ -f "$par2_file" ]]; then
            local par2_basename=$(basename "$par2_file")
            if [[ -n "$par2_files" ]]; then
                par2_files="${par2_files}, \"${par2_basename}\""
            else
                par2_files="\"${par2_basename}\""
            fi
        fi
    done

    cat <<EOF
{
  "title": "$title",
  "timestamp": "$timestamp",
  "original_iso": "$iso_path",
  "original_size_bytes": $original_size,
  "compressed_size_bytes": $compressed_size,
  "compression_ratio": $ratio,
  "compression_time_seconds": $compression_time,
  "archive_path": "$nas_path",
  "par2_files": [$par2_files],
  "archived_at": "$archived_at"
}
EOF
}
