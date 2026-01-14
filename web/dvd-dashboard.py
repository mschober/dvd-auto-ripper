#!/usr/bin/env python3
"""
DVD Ripper Web Dashboard
A minimal Flask web UI for monitoring the DVD auto-ripper pipeline.

https://github.com/mschober/dvd-auto-ripper
"""

import os
import re
import json
import glob
import socket
import subprocess
from datetime import datetime
from flask import Flask, jsonify, render_template, request, redirect, url_for, send_file
from helpers.pipeline import (
    get_queue_items, count_by_state,
    STAGING_DIR, STATE_ORDER, QUEUE_ITEMS_PER_PAGE
)
from pages.archives import archives_bp

app = Flask(__name__,
            template_folder='templates',
            static_folder='static')
app.register_blueprint(archives_bp)

# Configuration - can be overridden via environment variables
# Note: STAGING_DIR and STATE_ORDER are imported from helpers.pipeline
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/dvd-ripper")
LOG_FILES = {
    "iso": f"{LOG_DIR}/iso.log",
    "encoder": f"{LOG_DIR}/encoder.log",
    "transfer": f"{LOG_DIR}/transfer.log",
    "distribute": f"{LOG_DIR}/distribute.log",
}
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/dvd-ripper.conf")
PIPELINE_VERSION_FILE = os.environ.get("PIPELINE_VERSION_FILE", "/usr/local/bin/VERSION")
DASHBOARD_VERSION = "1.9.0"
GITHUB_URL = "https://github.com/mschober/dvd-auto-ripper"
HOSTNAME = socket.gethostname().split('.')[0]

LOCK_DIR = "/run/dvd-ripper"
LOCK_FILES = {
    "encoder": f"{LOCK_DIR}/encoder.lock",
    "transfer": f"{LOCK_DIR}/transfer.lock",
    "distribute": f"{LOCK_DIR}/distribute.lock"
}
# ISO locks are now per-device (iso-sr0.lock, iso-sr1.lock) and detected dynamically

# State configuration for cancellation and reversion
STATE_CONFIG = {
    "iso-creating": {"lock": "iso", "revert_to": None},
    "iso-ready": {"lock": None, "revert_to": None},
    "distributing": {"lock": "distribute", "revert_to": "iso-ready"},
    "encoding": {"lock": "encoder", "revert_to": "iso-ready"},
    "encoded-ready": {"lock": None, "revert_to": None},
    "transferring": {"lock": "transfer", "revert_to": "encoded-ready"},
}

# Generic title detection patterns (items needing identification)
GENERIC_PATTERNS = [
    r'^DVD_\d{8}_\d{6}$',           # DVD_YYYYMMDD_HHMMSS (our fallback format)
    r'^DVD_VIDEO$',                  # Common generic
    r'^DVDVIDEO$',                   # Another variant
    r'^DISC\d*$',                    # DISC, DISC1, etc.
    r'^DISK\d*$',                    # DISK, DISK1, etc.
    r'^VIDEO_TS$',                   # Raw folder name
    r'^MYDVD$',                      # Generic authoring tool name
    r'^DVD$',                        # Plain DVD
]

# States that allow renaming (not actively being processed)
RENAMEABLE_STATES = ["iso-ready", "encoded-ready", "transferred"]


# ============================================================================
# Helper Functions
# ============================================================================

def get_pipeline_version():
    """Read pipeline version from VERSION file."""
    try:
        with open(PIPELINE_VERSION_FILE, 'r') as f:
            return f.read().strip()
    except Exception:
        return "unknown"


# Note: get_queue_items() and count_by_state() moved to helpers/pipeline.py


def get_disk_usage():
    """Get disk usage for staging directory."""
    try:
        result = subprocess.run(
            ["df", "-h", STAGING_DIR],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            return {
                "mount": parts[5] if len(parts) > 5 else parts[0],
                "total": parts[1],
                "used": parts[2],
                "available": parts[3],
                "percent": parts[4].rstrip("%"),
                "percent_num": int(parts[4].rstrip("%"))
            }
    except Exception:
        pass
    return {"mount": "N/A", "total": "N/A", "used": "N/A",
            "available": "N/A", "percent": "0", "percent_num": 0}


def get_stage_logs(stage, lines=100):
    """Read last N lines from a stage-specific log file.

    Also checks rotated log files in case a process is still writing
    to the old (rotated) file handle after logrotate runs.
    """
    log_file = LOG_FILES.get(stage)
    if not log_file:
        return f"(unknown stage: {stage})"

    try:
        if not os.path.exists(log_file):
            return "(no logs yet)"

        # Get main log mtime
        try:
            main_mtime = os.path.getmtime(log_file)
        except OSError:
            main_mtime = 0

        # Check for rotated log files that may be more recently modified
        log_base = os.path.basename(log_file)
        try:
            for f in os.listdir(LOG_DIR):
                if f.startswith(log_base) and f != log_base and not f.endswith('.gz'):
                    full_path = os.path.join(LOG_DIR, f)
                    try:
                        mtime = os.path.getmtime(full_path)
                        if mtime > main_mtime:
                            log_file = full_path
                            main_mtime = mtime
                    except OSError:
                        pass
        except OSError:
            pass

        result = subprocess.run(
            ["tail", "-n", str(lines), log_file],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout or "(no logs)"
    except Exception:
        return "(unable to read log file)"


def get_all_logs(lines=50):
    """Read last N lines from all stage log files combined.

    Returns logs from all stages merged and sorted by timestamp.
    """
    all_lines = []
    for stage, log_file in LOG_FILES.items():
        try:
            if os.path.exists(log_file):
                result = subprocess.run(
                    ["tail", "-n", str(lines), log_file],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout:
                    all_lines.extend(result.stdout.strip().split('\n'))
        except Exception:
            pass

    # Sort by timestamp (logs start with [YYYY-MM-DD HH:MM:SS])
    all_lines.sort()
    # Return last N lines
    return '\n'.join(all_lines[-lines:]) if all_lines else "(no logs)"


# Legacy alias for backwards compatibility
def get_recent_logs(lines=50):
    """Legacy function - returns combined logs from all stages."""
    return get_all_logs(lines)


def check_lock_file(lock_file):
    """Check if a lock file exists and has an active process."""
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                pid = f.read().strip()
            # Check if process is actually running via /proc (works across users)
            # os.kill(pid, 0) requires same-user or root permissions
            if os.path.exists(f"/proc/{pid}"):
                return {"active": True, "pid": pid}
            else:
                return {"active": False, "pid": None}
        except (ValueError, IOError):
            return {"active": False, "pid": None}
    return {"active": False, "pid": None}


def get_lock_status():
    """Check which stages are currently locked/running."""
    status = {}

    # Check distribute lock (single instance)
    status["distribute"] = check_lock_file(LOCK_FILES["distribute"])

    # Check parallel transfer locks (transfer-1.lock, transfer-2.lock, etc.)
    transfer_locks = glob.glob(os.path.join(LOCK_DIR, "transfer-*.lock"))
    transfer_slots = {}
    for lock_file in transfer_locks:
        # Extract slot number: transfer-1.lock -> 1
        slot = os.path.basename(lock_file).replace("transfer-", "").replace(".lock", "")
        transfer_slots[slot] = check_lock_file(lock_file)

    # Also check legacy transfer.lock for backwards compatibility
    legacy_transfer = LOCK_FILES["transfer"]
    legacy_status = check_lock_file(legacy_transfer)
    if legacy_status["active"]:
        transfer_slots["legacy"] = legacy_status

    # Provide combined "transfer" status (active if any slot is active)
    any_transfer_active = any(s.get("active") for s in transfer_slots.values())
    status["transfer"] = {"active": any_transfer_active, "pid": None, "slots": transfer_slots}

    # Check parallel encoder locks (encoder-1.lock, encoder-2.lock, etc.)
    encoder_locks = glob.glob(os.path.join(LOCK_DIR, "encoder-*.lock"))
    encoder_slots = {}
    for lock_file in encoder_locks:
        # Extract slot number: encoder-1.lock -> 1
        slot = os.path.basename(lock_file).replace("encoder-", "").replace(".lock", "")
        encoder_slots[slot] = check_lock_file(lock_file)

    # Also check legacy encoder.lock for backwards compatibility
    legacy_encoder = LOCK_FILES["encoder"]
    legacy_status = check_lock_file(legacy_encoder)
    if legacy_status["active"]:
        encoder_slots["legacy"] = legacy_status

    # Provide combined "encoder" status (active if any slot is active)
    any_encoder_active = any(s.get("active") for s in encoder_slots.values())
    status["encoder"] = {"active": any_encoder_active, "pid": None, "slots": encoder_slots}

    # Check per-device ISO locks (iso-sr0.lock, iso-sr1.lock, etc.)
    iso_locks = glob.glob(os.path.join(LOCK_DIR, "iso-*.lock"))
    iso_drives = {}
    for lock_file in iso_locks:
        # Extract device name: iso-sr0.lock -> sr0
        device = os.path.basename(lock_file).replace("iso-", "").replace(".lock", "")
        iso_drives[device] = check_lock_file(lock_file)

    # Also check legacy iso.lock for backwards compatibility
    legacy_iso = os.path.join(LOCK_DIR, "iso.lock")
    legacy_status = check_lock_file(legacy_iso)
    if legacy_status["active"]:
        iso_drives["default"] = legacy_status

    # Provide combined "iso" status for backwards compat (active if any drive is active)
    any_iso_active = any(d.get("active") for d in iso_drives.values())
    status["iso"] = {"active": any_iso_active, "pid": None, "drives": iso_drives}

    # Check archive lock (single instance - CPU intensive)
    archive_lock = os.path.join(LOCK_DIR, "archive.lock")
    status["archive"] = check_lock_file(archive_lock)

    return status


def get_receiving_transfers():
    """
    Detect incoming rsync transfers by looking for temp files.
    Rsync creates temp files like .FILENAME.XXXXXX while transferring.
    Returns list of receiving transfers with filename and current size.
    """
    receiving = []

    try:
        for entry in os.listdir(STAGING_DIR):
            # Rsync temp files start with . and have .iso. in the name
            # Example: .FAR_FROM_HEAVEN-1768079402.iso.rxYU4v
            if entry.startswith('.') and '.iso.' in entry:
                # Extract original filename: .NAME-123.iso.rxYU4v -> NAME-123.iso
                # Remove leading dot, then split on last dot to remove random suffix
                name_without_dot = entry[1:]
                parts = name_without_dot.rsplit('.', 1)
                if len(parts) == 2 and parts[0].endswith('.iso'):
                    original_name = parts[0]
                    temp_path = os.path.join(STAGING_DIR, entry)
                    try:
                        size = os.path.getsize(temp_path)
                        size_mb = size / (1024 * 1024)
                        receiving.append({
                            "filename": original_name,
                            "size_mb": round(size_mb, 1),
                            "temp_file": entry
                        })
                    except OSError:
                        pass
    except OSError:
        pass

    return receiving


def get_active_progress():
    """
    Parse recent logs to extract progress for active processes.
    Returns dict with progress info for iso, encoder, distributing, and transfer stages.
    """
    progress = {"iso": None, "encoder": None, "distributing": None, "transfer": None, "receiving": None}
    locks = get_lock_status()

    # Check for distributing state files (cluster distribution in progress)
    distributing_files = glob.glob(os.path.join(STAGING_DIR, "*.distributing"))
    is_distributing = len(distributing_files) > 0

    # Check for receiving transfers (rsync temp files) early so we don't skip them
    receiving = get_receiving_transfers()
    if receiving:
        progress["receiving"] = receiving

    # Only parse logs if something is actually running (but still return receiving if found)
    if not any(s["active"] for s in locks.values()) and not is_distributing:
        return progress

    # Parse HandBrake encoding progress (per-slot, like ISO per-drive)
    # Pattern: "Encoding: task X of Y, XX.XX % (XX.XX fps, avg XX.XX fps, ETA XXhXXmXXs)"
    encoder_status = locks.get("encoder", {})
    encoder_slots = encoder_status.get("slots", {})
    active_slots = [s for s, info in encoder_slots.items() if info.get("active")]

    if encoder_status.get("active") and active_slots:
        # Load all .encoding files to map slots to titles
        encoding_files = glob.glob(os.path.join(STAGING_DIR, "*.encoding"))
        slot_to_title = {}
        all_titles = []  # For fallback when encoder_slot not set
        for ef in encoding_files:
            try:
                with open(ef, 'r') as f:
                    meta = json.load(f)
                    slot = meta.get("encoder_slot", "")
                    title = meta.get("title", "").replace('_', ' ')
                    if title:
                        all_titles.append(title)
                    if slot and title:
                        slot_to_title[slot] = title
            except Exception:
                pass

        # Check if any per-slot logs exist (new behavior)
        has_per_slot_logs = any(
            os.path.exists(os.path.join(LOG_DIR, f"encoder-{slot}.log"))
            for slot in active_slots
        )

        # Fallback: read legacy encoder.log if no per-slot logs exist
        legacy_logs = ""
        if not has_per_slot_logs:
            encoder_log = LOG_FILES.get("encoder", "")
            if encoder_log and os.path.exists(encoder_log):
                try:
                    with open(encoder_log, 'r') as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 10240))
                        legacy_logs = f.read()
                except Exception:
                    pass

        encoder_progress_list = []
        for idx, slot in enumerate(active_slots):
            # Read per-slot log file
            slot_log_file = os.path.join(LOG_DIR, f"encoder-{slot}.log")
            slot_logs = ""
            if os.path.exists(slot_log_file):
                try:
                    with open(slot_log_file, 'r') as f:
                        f.seek(0, 2)  # End of file
                        size = f.tell()
                        f.seek(max(0, size - 10240))  # Last 10KB
                        slot_logs = f.read()
                except Exception:
                    pass

            # Find encoding progress in this slot's log
            encoder_matches = re.findall(
                r'Encoding:.*?(\d+\.?\d*)\s*%.*?(\d+\.?\d*)\s*fps.*?ETA\s*(\d+h\d+m\d+s|\d+m\d+s)',
                slot_logs
            )

            # Get title: prefer slot mapping, fallback to all_titles by index
            title = slot_to_title.get(slot)
            if not title and idx < len(all_titles):
                title = all_titles[idx]
            if not title:
                title = f"Slot {slot}"

            if encoder_matches:
                last_match = encoder_matches[-1]
                encoder_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": float(last_match[0]),
                    "speed": f"{last_match[1]} fps",
                    "eta": last_match[2]
                })
            elif legacy_logs:
                # Fallback: use legacy log progress (shared between encoders)
                legacy_matches = re.findall(
                    r'Encoding:.*?(\d+\.?\d*)\s*%.*?(\d+\.?\d*)\s*fps.*?ETA\s*(\d+h\d+m\d+s|\d+m\d+s)',
                    legacy_logs
                )
                if legacy_matches:
                    last_match = legacy_matches[-1]
                    encoder_progress_list.append({
                        "slot": slot,
                        "title": title,
                        "percent": float(last_match[0]),
                        "speed": f"{last_match[1]} fps",
                        "eta": last_match[2],
                        "shared_log": True  # Indicates progress from shared log
                    })
                else:
                    encoder_progress_list.append({
                        "slot": slot,
                        "title": title,
                        "percent": 0,
                        "speed": "starting",
                        "eta": "calculating"
                    })
            else:
                # Encoding active but no progress yet
                encoder_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": 0,
                    "speed": "starting",
                    "eta": "calculating"
                })

        if encoder_progress_list:
            progress["encoder"] = encoder_progress_list  # Now a list!

    # Parse ddrescue ISO creation progress (per-device)
    # Pattern: "pct rescued:  XX.XX%, read errors:        0,  remaining time:         Xm"
    iso_status = locks.get("iso", {})
    iso_drives = iso_status.get("drives", {})
    active_iso_drives = [d for d, info in iso_drives.items() if info.get("active")]

    if iso_status.get("active") and active_iso_drives:
        iso_progress_list = []
        for drive in active_iso_drives:
            # Read per-device log file for this drive
            device_log_file = os.path.join(LOG_DIR, f"iso-{drive}.log")
            drive_logs = ""
            if os.path.exists(device_log_file):
                try:
                    with open(device_log_file, 'r') as f:
                        # Read last 50 lines for recent progress
                        lines = f.readlines()
                        drive_logs = ''.join(lines[-50:])
                except Exception:
                    pass

            # Parse ddrescue progress from this drive's log
            iso_matches = re.findall(
                r'pct rescued:\s*(\d+\.?\d*)%.*?remaining time:\s*(\d+m|\d+s|n/a)',
                drive_logs
            )
            if iso_matches:
                last_match = iso_matches[-1]
                iso_progress_list.append({
                    "drive": drive,
                    "percent": float(last_match[0]),
                    "eta": last_match[1] if last_match[1] != "n/a" else "finishing..."
                })
            else:
                # Drive is active but no progress yet (just started)
                iso_progress_list.append({
                    "drive": drive,
                    "percent": 0.0,
                    "eta": "starting..."
                })

        if iso_progress_list:
            progress["iso"] = iso_progress_list

    # Parse rsync cluster distribution progress (during encoder lock with .distributing file)
    # Pattern: "XXX,XXX,XXX  XX%  XX.XXMB/s    X:XX:XX"
    if is_distributing:
        # Read distribute log directly
        dist_log = LOG_FILES.get("distribute", "")
        dist_logs = ""
        if dist_log and os.path.exists(dist_log):
            try:
                with open(dist_log, 'r') as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 10240))
                    dist_logs = f.read()
            except Exception:
                pass

        dist_matches = re.findall(
            r'(\d+)%\s+([\d.]+[KMG]?B/s)\s+(\d+:\d+:\d+)',
            dist_logs
        )
        if dist_matches:
            last_match = dist_matches[-1]
            progress["distributing"] = {
                "percent": float(last_match[0]),
                "speed": last_match[1],
                "eta": last_match[2]
            }

    # Parse rsync transfer progress (per-slot, like encoder)
    # Pattern: "XX% XX.XXMB/s X:XX:XX" or "XXX,XXX,XXX  XX%  XX.XXmB/s    X:XX:XX"
    transfer_status = locks.get("transfer", {})
    transfer_slots = transfer_status.get("slots", {})
    active_transfer_slots = [s for s, info in transfer_slots.items() if info.get("active")]

    if transfer_status.get("active") and active_transfer_slots:
        # Load all .transferring files to map slots to titles
        transferring_files = glob.glob(os.path.join(STAGING_DIR, "*.transferring"))
        all_transfer_titles = []
        for tf in transferring_files:
            try:
                with open(tf, 'r') as f:
                    meta = json.load(f)
                    title = meta.get("title", "").replace('_', ' ')
                    if title:
                        all_transfer_titles.append(title)
            except Exception:
                pass

        transfer_progress_list = []
        for idx, slot in enumerate(active_transfer_slots):
            # Read per-slot log file
            slot_log_file = os.path.join(LOG_DIR, f"transfer.{slot}.log")
            slot_logs = ""
            if os.path.exists(slot_log_file):
                try:
                    with open(slot_log_file, 'r') as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 10240))
                        slot_logs = f.read()
                except Exception:
                    pass

            # Fallback to legacy log if no per-slot log
            if not slot_logs:
                transfer_log = LOG_FILES.get("transfer", "")
                if transfer_log and os.path.exists(transfer_log):
                    try:
                        with open(transfer_log, 'r') as f:
                            f.seek(0, 2)
                            size = f.tell()
                            f.seek(max(0, size - 10240))
                            slot_logs = f.read()
                    except Exception:
                        pass

            # Get title from transferring files
            title = all_transfer_titles[idx] if idx < len(all_transfer_titles) else f"Slot {slot}"

            transfer_matches = re.findall(
                r'(\d+)%\s+([\d.]+[KMG]?B/s)\s+(\d+:\d+:\d+)',
                slot_logs
            )
            if transfer_matches:
                last_match = transfer_matches[-1]
                transfer_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": float(last_match[0]),
                    "speed": last_match[1],
                    "eta": last_match[2]
                })
            else:
                transfer_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": 0,
                    "speed": "starting",
                    "eta": "calculating"
                })

        if transfer_progress_list:
            progress["transfer"] = transfer_progress_list  # Now a list!

    # Parse archive progress (xz compression)
    archive_status = locks.get("archive", {})
    if archive_status.get("active"):
        archive_progress_list = []

        # Find .archiving state files
        archiving_files = glob.glob(os.path.join(STAGING_DIR, "*.archiving"))
        for state_file in archiving_files:
            try:
                with open(state_file, 'r') as f:
                    meta = json.load(f)

                iso_path = meta.get("iso_path", "")
                title = meta.get("title", "Unknown").replace('_', ' ')
                iso_size = meta.get("iso_size_bytes", 0)
                started_at = meta.get("started_at", "")
                started_time = ""
                if started_at:
                    try:
                        from datetime import datetime
                        st = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                        started_time = st.strftime("%-I:%M %p")
                    except Exception:
                        started_time = started_at[:16] if len(started_at) > 16 else started_at

                # Calculate progress from xz output file size
                xz_path = f"{iso_path}.xz"
                percent = 0
                speed = "compressing"
                eta = "calculating"

                if os.path.exists(xz_path) and iso_size > 0:
                    try:
                        xz_size = os.path.getsize(xz_path)
                        # xz typically achieves 40-60% compression, so estimate based on that
                        # Max at 95% since we can't know exactly when it will finish
                        estimated_final = iso_size * 0.5  # Assume 50% compression ratio
                        percent = min(95, (xz_size / estimated_final) * 100)

                        # Calculate elapsed time and estimate ETA
                        if started_at:
                            try:
                                from datetime import datetime
                                start_time = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                                elapsed = (datetime.now(start_time.tzinfo) - start_time).total_seconds()
                                if percent > 5 and elapsed > 60:
                                    total_estimated = elapsed / (percent / 100)
                                    remaining = total_estimated - elapsed
                                    if remaining > 0:
                                        hours = int(remaining // 3600)
                                        minutes = int((remaining % 3600) // 60)
                                        if hours > 0:
                                            eta = f"{hours}h{minutes}m"
                                        else:
                                            eta = f"{minutes}m"
                                    speed = f"{xz_size / 1024 / 1024 / elapsed:.1f} MB/s" if elapsed > 0 else "starting"
                            except Exception:
                                pass
                    except OSError:
                        pass

                archive_progress_list.append({
                    "title": title,
                    "percent": round(percent, 1),
                    "speed": speed,
                    "eta": eta,
                    "started_time": started_time
                })
            except Exception:
                pass

        if archive_progress_list:
            progress["archive"] = archive_progress_list

    return progress


# ============================================================================
# System Health Functions
# ============================================================================

def get_cpu_usage():
    """
    Read /proc/stat and calculate CPU usage.
    Returns dict with total and per-core usage percentages.
    """
    try:
        with open('/proc/stat', 'r') as f:
            lines = f.readlines()

        cpu_data = {}
        for line in lines:
            if line.startswith('cpu'):
                parts = line.split()
                cpu_name = parts[0]
                # user, nice, system, idle, iowait, irq, softirq, steal
                values = [int(x) for x in parts[1:8]]
                total = sum(values)
                idle = values[3] + values[4]  # idle + iowait
                usage = ((total - idle) / total * 100) if total > 0 else 0
                cpu_data[cpu_name] = {
                    "usage": round(usage, 1),
                    "total": total,
                    "idle": idle
                }

        return cpu_data
    except Exception as e:
        return {"cpu": {"usage": 0, "error": str(e)}}


def get_memory_usage():
    """
    Read /proc/meminfo and return memory statistics.
    Returns dict with total, used, available, cached, and percent used.
    """
    try:
        meminfo = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(':')
                    # Convert kB to bytes
                    value = int(parts[1]) * 1024
                    meminfo[key] = value

        total = meminfo.get('MemTotal', 0)
        available = meminfo.get('MemAvailable', 0)
        cached = meminfo.get('Cached', 0) + meminfo.get('Buffers', 0)
        used = total - available

        return {
            "total": total,
            "used": used,
            "available": available,
            "cached": cached,
            "percent": round((used / total * 100) if total > 0 else 0, 1),
            "total_human": _format_bytes(total),
            "used_human": _format_bytes(used),
            "available_human": _format_bytes(available)
        }
    except Exception as e:
        return {"total": 0, "used": 0, "available": 0, "percent": 0, "error": str(e)}


def _format_bytes(bytes_val):
    """Format bytes to human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"


def get_load_average():
    """
    Read /proc/loadavg and return load averages.
    Returns dict with 1m, 5m, 15m load averages and CPU count.
    """
    try:
        with open('/proc/loadavg', 'r') as f:
            parts = f.read().split()

        cpu_count = os.cpu_count() or 1
        load_1m = float(parts[0])
        load_5m = float(parts[1])
        load_15m = float(parts[2])

        return {
            "load_1m": load_1m,
            "load_5m": load_5m,
            "load_15m": load_15m,
            "cpu_count": cpu_count,
            "load_per_core": round(load_1m / cpu_count, 2),
            "status": "high" if load_1m > cpu_count else "normal"
        }
    except Exception as e:
        return {"load_1m": 0, "load_5m": 0, "load_15m": 0, "cpu_count": 1, "error": str(e)}


def get_temperatures():
    """
    Run sensors command and parse temperature/fan output.
    Returns dict with temperature readings and fan speeds.
    """
    result = {
        "temperatures": [],
        "fans": [],
        "available": False
    }

    try:
        proc = subprocess.run(
            ["sensors", "-j"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode != 0:
            return result

        data = json.loads(proc.stdout)
        result["available"] = True

        for chip_name, chip_data in data.items():
            if not isinstance(chip_data, dict):
                continue

            for sensor_name, sensor_data in chip_data.items():
                if not isinstance(sensor_data, dict):
                    continue

                for reading_name, reading_val in sensor_data.items():
                    if 'temp' in reading_name.lower() and '_input' in reading_name:
                        temp_c = reading_val
                        result["temperatures"].append({
                            "chip": chip_name,
                            "sensor": sensor_name,
                            "temp_c": round(temp_c, 1),
                            "temp_f": round(temp_c * 9/5 + 32, 1),
                            "status": "critical" if temp_c > 85 else "warning" if temp_c > 70 else "ok"
                        })
                    elif 'fan' in reading_name.lower() and '_input' in reading_name:
                        result["fans"].append({
                            "chip": chip_name,
                            "sensor": sensor_name,
                            "rpm": int(reading_val)
                        })

    except FileNotFoundError:
        result["error"] = "lm-sensors not installed"
    except json.JSONDecodeError:
        # Fall back to text parsing
        try:
            proc = subprocess.run(
                ["sensors"],
                capture_output=True, text=True, timeout=5
            )
            if proc.returncode == 0:
                result["available"] = True
                result["raw_output"] = proc.stdout
                # Parse text output for temperatures
                for line in proc.stdout.split('\n'):
                    if '°C' in line:
                        match = re.search(r'([+-]?\d+\.?\d*)\s*°C', line)
                        if match:
                            temp_c = float(match.group(1))
                            label = line.split(':')[0].strip() if ':' in line else "CPU"
                            result["temperatures"].append({
                                "sensor": label,
                                "temp_c": round(temp_c, 1),
                                "status": "critical" if temp_c > 85 else "warning" if temp_c > 70 else "ok"
                            })
                    if 'RPM' in line:
                        match = re.search(r'(\d+)\s*RPM', line)
                        if match:
                            rpm = int(match.group(1))
                            label = line.split(':')[0].strip() if ':' in line else "Fan"
                            result["fans"].append({
                                "sensor": label,
                                "rpm": rpm
                            })
        except Exception:
            pass
    except subprocess.TimeoutExpired:
        result["error"] = "sensors command timed out"
    except Exception as e:
        result["error"] = str(e)

    return result


# Module-level cache for I/O stats delta calculation
_io_stats_prev = {}
_io_stats_time = 0


def get_io_stats():
    """
    Get disk I/O statistics and device information.
    Returns device list with throughput and I/O wait percentage.
    """
    global _io_stats_prev, _io_stats_time
    import time

    result = {
        "devices": [],
        "iowait_percent": 0.0,
        "total_read_mb_s": 0.0,
        "total_write_mb_s": 0.0,
        "available": False,
        "error": None
    }

    current_time = time.time()

    try:
        # Get I/O wait from /proc/stat
        try:
            with open('/proc/stat', 'r') as f:
                for line in f:
                    if line.startswith('cpu '):
                        parts = line.split()
                        # cpu user nice system idle iowait irq softirq
                        if len(parts) >= 6:
                            total = sum(int(p) for p in parts[1:8] if p.isdigit())
                            iowait = int(parts[5]) if len(parts) > 5 else 0
                            if total > 0:
                                result["iowait_percent"] = round((iowait / total) * 100, 1)
                        break
        except Exception:
            pass

        # Get block devices with lsblk
        try:
            lsblk_result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,MODEL,TRAN,ROTA"],
                capture_output=True, text=True, timeout=5
            )
            if lsblk_result.returncode == 0:
                lsblk_data = json.loads(lsblk_result.stdout)
                devices_info = {}

                def process_device(dev, parent_tran=None):
                    """Process a device and its children recursively."""
                    name = dev.get("name", "")
                    dev_type = dev.get("type", "")

                    # Only process disks and partitions, skip loops/roms
                    if dev_type not in ("disk", "part"):
                        return

                    tran = dev.get("tran") or parent_tran
                    rota = dev.get("rota")  # 1=rotational (HDD), 0=SSD

                    # Determine device type label
                    if tran == "usb":
                        type_label = "usb"
                    elif tran == "nvme":
                        type_label = "nvme"
                    elif rota == "0" or rota == 0 or rota is False:
                        type_label = "ssd"
                    elif rota == "1" or rota == 1 or rota is True:
                        type_label = "hdd"
                    else:
                        type_label = "disk"

                    # For disks, store the info
                    if dev_type == "disk":
                        devices_info[name] = {
                            "name": name,
                            "model": (dev.get("model") or "").strip(),
                            "size": dev.get("size", ""),
                            "type": type_label,
                            "mountpoint": dev.get("mountpoint") or "",
                            "read_mb_s": 0.0,
                            "write_mb_s": 0.0
                        }

                    # Process children (partitions)
                    for child in dev.get("children", []):
                        # If partition has mountpoint, update parent disk's mountpoint
                        if child.get("mountpoint") and name in devices_info:
                            if not devices_info[name]["mountpoint"]:
                                devices_info[name]["mountpoint"] = child.get("mountpoint")
                        process_device(child, tran)

                for dev in lsblk_data.get("blockdevices", []):
                    process_device(dev)

                # Read /proc/diskstats for I/O counters
                diskstats = {}
                try:
                    with open('/proc/diskstats', 'r') as f:
                        for line in f:
                            parts = line.split()
                            if len(parts) >= 14:
                                dev_name = parts[2]
                                # Fields: reads_completed, reads_merged, sectors_read, ms_reading,
                                #         writes_completed, writes_merged, sectors_written, ms_writing
                                diskstats[dev_name] = {
                                    "sectors_read": int(parts[5]),
                                    "sectors_written": int(parts[9]),
                                    "time": current_time
                                }
                except Exception:
                    pass

                # Calculate throughput using delta from previous reading
                time_delta = current_time - _io_stats_time if _io_stats_time > 0 else 1.0
                if time_delta < 0.1:
                    time_delta = 0.1  # Minimum delta to avoid division issues

                for dev_name, dev_info in devices_info.items():
                    if dev_name in diskstats:
                        current = diskstats[dev_name]
                        if dev_name in _io_stats_prev and _io_stats_time > 0:
                            prev = _io_stats_prev[dev_name]
                            read_sectors = current["sectors_read"] - prev["sectors_read"]
                            write_sectors = current["sectors_written"] - prev["sectors_written"]
                            # Sectors are typically 512 bytes
                            dev_info["read_mb_s"] = round((read_sectors * 512 / 1024 / 1024) / time_delta, 1)
                            dev_info["write_mb_s"] = round((write_sectors * 512 / 1024 / 1024) / time_delta, 1)
                            # Clamp negative values (can happen on counter wrap)
                            if dev_info["read_mb_s"] < 0:
                                dev_info["read_mb_s"] = 0.0
                            if dev_info["write_mb_s"] < 0:
                                dev_info["write_mb_s"] = 0.0

                # Store current stats for next delta calculation
                _io_stats_prev = diskstats
                _io_stats_time = current_time

                # Build device list and calculate totals
                result["devices"] = list(devices_info.values())
                result["total_read_mb_s"] = round(sum(d["read_mb_s"] for d in result["devices"]), 1)
                result["total_write_mb_s"] = round(sum(d["write_mb_s"] for d in result["devices"]), 1)
                result["available"] = True

        except subprocess.TimeoutExpired:
            result["error"] = "lsblk timed out"
        except json.JSONDecodeError:
            result["error"] = "Failed to parse lsblk output"

    except Exception as e:
        result["error"] = str(e)

    return result


def get_dvd_processes():
    """
    Get list of DVD ripper related processes.
    Returns list of process info dicts.
    """
    # Processes we care about
    target_commands = ['HandBrakeCLI', 'ddrescue', 'rsync', 'ffmpeg', 'dvd-iso', 'dvd-encoder', 'dvd-transfer']

    processes = []
    try:
        proc = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5
        )

        for line in proc.stdout.split('\n')[1:]:  # Skip header
            if not line.strip():
                continue

            # Check if line contains any target command
            matched_cmd = None
            for cmd in target_commands:
                if cmd in line:
                    matched_cmd = cmd
                    break

            if matched_cmd:
                parts = line.split(None, 10)  # Split into max 11 parts
                if len(parts) >= 11:
                    pid = parts[1]
                    cpu_pct = float(parts[2])
                    mem_pct = float(parts[3])
                    start_time = parts[8]
                    elapsed = parts[9]
                    command = parts[10]

                    # Determine process type based on command
                    process_type = "unknown"
                    if 'HandBrakeCLI' in command:
                        process_type = "encoder"
                    elif 'ddrescue' in command:
                        process_type = "iso"
                    elif 'rsync' in command or 'scp' in command:
                        process_type = "transfer"
                    elif 'ffmpeg' in command:
                        process_type = "preview"

                    processes.append({
                        "pid": int(pid),
                        "cpu_percent": cpu_pct,
                        "mem_percent": mem_pct,
                        "start_time": start_time,
                        "elapsed": elapsed,
                        "command": command[:100],  # Truncate long commands
                        "command_full": command,
                        "type": process_type,
                        "matched": matched_cmd
                    })

    except Exception as e:
        processes.append({"error": str(e)})

    # Sort by CPU usage descending
    return sorted(processes, key=lambda x: x.get("cpu_percent", 0), reverse=True)


def kill_process_with_cleanup(pid):
    """
    Kill a DVD ripper process and clean up associated state.
    Returns tuple of (success, message).
    """
    import time
    pid = int(pid)

    # Map process type to state name
    PROCESS_TO_STATE = {
        "encoder": "encoding",
        "iso": "iso-creating",
        "transfer": "transferring",
        "distribute": "distributing"
    }

    # First verify this is one of our processes
    processes = get_dvd_processes()
    target_process = None
    for proc in processes:
        if proc.get("pid") == pid:
            target_process = proc
            break

    if not target_process:
        return False, f"PID {pid} is not a DVD ripper process"

    process_type = target_process.get("type", "unknown")
    state_name = PROCESS_TO_STATE.get(process_type)
    config = STATE_CONFIG.get(state_name, {}) if state_name else {}

    # Get lock file from STATE_CONFIG
    lock_stage = config.get("lock")
    lock_file = LOCK_FILES.get(lock_stage) if lock_stage else None

    try:
        # Send SIGTERM first
        os.kill(pid, 15)  # SIGTERM

        # Wait a moment for graceful shutdown
        time.sleep(2)

        # Check if still running, send SIGKILL if needed
        try:
            os.kill(pid, 0)  # Check if process exists
            os.kill(pid, 9)  # SIGKILL
        except ProcessLookupError:
            pass  # Process already terminated

        # Clean up lock file
        if lock_file and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass

        # Revert state files using STATE_CONFIG
        cleanup_msg = ""
        if state_name:
            revert_to = config.get("revert_to")
            pattern = os.path.join(STAGING_DIR, f"*.{state_name}")
            for state_file in glob.glob(pattern):
                try:
                    _, msg = revert_state_file(state_file, revert_to)
                    cleanup_msg += f" {msg}."
                except Exception as e:
                    cleanup_msg += f" Failed to clean up {os.path.basename(state_file)}: {e}"

        return True, f"Killed PID {pid} ({process_type}).{cleanup_msg}"

    except ProcessLookupError:
        return False, f"Process {pid} not found"
    except PermissionError:
        return False, f"Permission denied to kill PID {pid}"
    except Exception as e:
        return False, f"Failed to kill PID {pid}: {e}"


def revert_state_file(state_file_path, new_state):
    """
    Revert a state file to a previous state, or remove it.
    Returns (new_path or None, message).
    """
    basename = os.path.basename(state_file_path)
    if new_state is None:
        os.remove(state_file_path)
        return None, f"Removed {basename}"
    else:
        base = state_file_path.rsplit('.', 1)[0]
        new_state_file = f"{base}.{new_state}"
        os.rename(state_file_path, new_state_file)
        return new_state_file, f"Reverted {basename} to {new_state}"


def find_process_for_lock(lock_stage):
    """Find PID of process from lock file if it's still running."""
    # Handle per-device ISO locks
    if lock_stage == "iso":
        # Check all per-device ISO locks (iso-sr0.lock, iso-sr1.lock, etc.)
        iso_locks = glob.glob(os.path.join(LOCK_DIR, "iso-*.lock"))
        # Also check legacy iso.lock
        legacy_iso = os.path.join(LOCK_DIR, "iso.lock")
        if os.path.exists(legacy_iso):
            iso_locks.append(legacy_iso)
        for lock_file in iso_locks:
            try:
                with open(lock_file, 'r') as f:
                    pid = int(f.read().strip())
                if os.path.exists(f"/proc/{pid}"):
                    return pid
            except (ValueError, IOError):
                pass
        return None

    if lock_stage not in LOCK_FILES:
        return None
    lock_file = LOCK_FILES[lock_stage]
    if not os.path.exists(lock_file):
        return None
    try:
        with open(lock_file, 'r') as f:
            pid = int(f.read().strip())
        if os.path.exists(f"/proc/{pid}"):
            return pid
    except (ValueError, IOError):
        pass
    return None


def cancel_queue_item(state_file_name, delete_files=False):
    """
    Cancel a queue item by state file name.
    Handles process killing and state reversion based on current state.
    Returns (success, message).
    """
    import time

    state_file_path = os.path.join(STAGING_DIR, state_file_name)

    if not os.path.exists(state_file_path):
        return False, "State file not found"

    # Extract state from filename
    state = state_file_name.rsplit('.', 1)[-1]

    if state not in STATE_CONFIG:
        return False, f"Unknown or non-cancellable state: {state}"

    config = STATE_CONFIG[state]

    # Read metadata for file paths
    try:
        with open(state_file_path, 'r') as f:
            metadata = json.load(f)
    except (json.JSONDecodeError, IOError):
        metadata = {}

    messages = []

    # Handle active processes
    if config["lock"]:
        pid = find_process_for_lock(config["lock"])
        if pid:
            try:
                os.kill(pid, 15)  # SIGTERM
                time.sleep(2)
                try:
                    os.kill(pid, 0)
                    os.kill(pid, 9)  # SIGKILL if still running
                except ProcessLookupError:
                    pass
                messages.append(f"Killed process {pid}")
            except PermissionError:
                messages.append(f"Permission denied killing PID {pid}")
            except Exception as e:
                messages.append(f"Could not kill process: {e}")

        # Clean up lock file
        if config["lock"] == "iso":
            # For ISO, clean up all per-device lock files (process trap should handle this,
            # but clean up any stale locks just in case)
            for lock_file in glob.glob(os.path.join(LOCK_DIR, "iso-*.lock")):
                try:
                    with open(lock_file, 'r') as f:
                        lock_pid = int(f.read().strip())
                    # Only remove if this was the process we killed
                    if lock_pid == pid:
                        os.remove(lock_file)
                except Exception:
                    pass
            # Also check legacy lock
            legacy_iso = os.path.join(LOCK_DIR, "iso.lock")
            if os.path.exists(legacy_iso):
                try:
                    os.remove(legacy_iso)
                except Exception:
                    pass
        elif config["lock"] in LOCK_FILES:
            lock_file = LOCK_FILES[config["lock"]]
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass

    # Clean up partial files for active states
    if state == "iso-creating":
        iso_path = metadata.get("iso_path")
        if iso_path and os.path.exists(iso_path):
            try:
                os.remove(iso_path)
                messages.append("Removed partial ISO")
            except Exception:
                pass
    elif state == "encoding":
        mkv_path = metadata.get("mkv_path")
        if mkv_path and os.path.exists(mkv_path):
            try:
                os.remove(mkv_path)
                messages.append("Removed partial MKV")
            except Exception:
                pass

    # Handle optional file deletion for queued states
    if delete_files:
        if state == "iso-ready":
            iso_path = metadata.get("iso_path")
            if iso_path and os.path.exists(iso_path):
                try:
                    os.remove(iso_path)
                    messages.append("Deleted ISO file")
                except Exception as e:
                    messages.append(f"Could not delete ISO: {e}")
        elif state == "encoded-ready":
            mkv_path = metadata.get("mkv_path")
            if mkv_path and os.path.exists(mkv_path):
                try:
                    os.remove(mkv_path)
                    messages.append("Deleted MKV file")
                except Exception as e:
                    messages.append(f"Could not delete MKV: {e}")

    # Revert or remove state file
    try:
        _, msg = revert_state_file(state_file_path, config["revert_to"])
        messages.append(msg)
    except Exception as e:
        return False, f"Failed to update state file: {e}"

    return True, ". ".join(messages)


def read_config():
    """Read and parse config file, hiding sensitive values."""
    config = {}
    sensitive_keys = {"NAS_USER", "NAS_HOST", "NAS_PATH"}
    try:
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key in sensitive_keys and value:
                        config[key] = value[:3] + "***"
                    else:
                        config[key] = value
    except Exception:
        pass
    return config


def read_config_full():
    """Read and parse config file without masking - for editing."""
    config = {}
    try:
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    config[key] = value
    except Exception:
        pass
    return config


def write_config(new_settings):
    """Write config file, preserving comments and structure.

    Args:
        new_settings: dict of key-value pairs to update

    Returns:
        tuple: (success: bool, changed_keys: list, message: str)
    """
    try:
        # Read existing file to preserve structure and comments
        with open(CONFIG_FILE, 'r') as f:
            lines = f.readlines()

        # Track which keys we've updated and original values
        old_config = read_config_full()
        changed_keys = []
        updated_keys = set()

        # Update values in-place
        new_lines = []
        for line in lines:
            stripped = line.strip()

            # Check if this is a config line (not comment, not empty, has =)
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _, _ = stripped.partition("=")
                key = key.strip()

                if key in new_settings:
                    new_value = new_settings[key]
                    old_value = old_config.get(key, "")

                    # Validate: no newlines allowed in values
                    if "\n" in str(new_value) or "\r" in str(new_value):
                        return False, [], f"Invalid value for {key}: newlines not allowed"

                    # Check if value actually changed
                    if str(new_value) != str(old_value):
                        changed_keys.append(key)

                    # Write the updated line with proper quoting
                    new_lines.append(f'{key}="{new_value}"\n')
                    updated_keys.add(key)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Add any new keys that weren't in the file
        for key, value in new_settings.items():
            if key not in updated_keys:
                # Validate
                if "\n" in str(value) or "\r" in str(value):
                    return False, [], f"Invalid value for {key}: newlines not allowed"
                new_lines.append(f'{key}="{value}"\n')
                if key not in old_config or str(value) != str(old_config.get(key, "")):
                    changed_keys.append(key)

        # Write back to file
        with open(CONFIG_FILE, 'w') as f:
            f.writelines(new_lines)

        return True, changed_keys, f"Saved {len(changed_keys)} change(s)"

    except PermissionError:
        return False, [], "Permission denied - config file requires root access"
    except Exception as e:
        return False, [], f"Failed to save config: {str(e)}"


def get_restart_recommendations(changed_keys):
    """Determine which services should be restarted based on changed settings.

    Returns:
        list of dicts with service info: [{"name": "dvd-encoder", "type": "timer"}, ...]
    """
    recommendations = []
    seen = set()

    # Mapping of config key patterns to affected services
    patterns = {
        # Encoder-related settings
        "HANDBRAKE_": [{"name": "dvd-encoder", "type": "timer"}],
        "ENABLE_PARALLEL_": [{"name": "dvd-encoder", "type": "timer"}],
        "MAX_PARALLEL_": [{"name": "dvd-encoder", "type": "timer"}],
        "ENCODER_LOAD_": [{"name": "dvd-encoder", "type": "timer"}],
        "PREVIEW_": [{"name": "dvd-encoder", "type": "timer"}],
        "GENERATE_PREVIEWS": [{"name": "dvd-encoder", "type": "timer"}],
        "MIN_FILE_SIZE": [{"name": "dvd-encoder", "type": "timer"}],

        # Transfer-related settings
        "NAS_": [{"name": "dvd-transfer", "type": "timer"}],
        "TRANSFER_MODE": [{"name": "dvd-transfer", "type": "timer"}],
        "LOCAL_LIBRARY_": [{"name": "dvd-transfer", "type": "timer"}],
        "CLEANUP_": [{"name": "dvd-transfer", "type": "timer"}],

        # Cluster settings affect both
        "CLUSTER_": [
            {"name": "dvd-encoder", "type": "timer"},
            {"name": "dvd-transfer", "type": "timer"}
        ],

        # Core settings affect everything
        "STAGING_DIR": [
            {"name": "dvd-encoder", "type": "timer"},
            {"name": "dvd-transfer", "type": "timer"},
            {"name": "dvd-dashboard", "type": "service"}
        ],
        "LOG_": [
            {"name": "dvd-encoder", "type": "timer"},
            {"name": "dvd-transfer", "type": "timer"}
        ],
    }

    for key in changed_keys:
        for pattern, services in patterns.items():
            if key.startswith(pattern) or key == pattern:
                for svc in services:
                    svc_key = f"{svc['name']}:{svc['type']}"
                    if svc_key not in seen:
                        recommendations.append(svc)
                        seen.add(svc_key)

    return recommendations


# Config sections for grouped display
CONFIG_SECTIONS = [
    {
        "id": "storage",
        "title": "Storage & Logging",
        "keys": ["STAGING_DIR", "LOG_FILE", "LOG_LEVEL", "DISK_USAGE_THRESHOLD"]
    },
    {
        "id": "device",
        "title": "DVD Device",
        "keys": ["DVD_DEVICE", "DEVICE_TIMEOUT"]
    },
    {
        "id": "pipeline",
        "title": "Pipeline Mode",
        "keys": ["PIPELINE_MODE", "CREATE_ISO", "ENCODE_VIDEO"]
    },
    {
        "id": "handbrake",
        "title": "HandBrake Encoding",
        "keys": ["HANDBRAKE_PRESET", "HANDBRAKE_QUALITY", "HANDBRAKE_FORMAT", "HANDBRAKE_EXTRA_OPTS", "MIN_FILE_SIZE_MB"]
    },
    {
        "id": "parallel",
        "title": "Parallel Encoding",
        "keys": ["ENABLE_PARALLEL_ENCODING", "MAX_PARALLEL_ENCODERS", "ENCODER_LOAD_THRESHOLD"]
    },
    {
        "id": "preview",
        "title": "Preview Generation",
        "keys": ["GENERATE_PREVIEWS", "PREVIEW_DURATION", "PREVIEW_START_PERCENT", "PREVIEW_RESOLUTION"]
    },
    {
        "id": "nas",
        "title": "NAS Transfer",
        "keys": ["NAS_ENABLED", "NAS_HOST", "NAS_USER", "NAS_PATH", "NAS_TRANSFER_METHOD", "NAS_FILE_OWNER"]
    },
    {
        "id": "transfer",
        "title": "Transfer Mode",
        "keys": ["TRANSFER_MODE", "LOCAL_LIBRARY_PATH"]
    },
    {
        "id": "cluster",
        "title": "Cluster Mode",
        "keys": ["CLUSTER_ENABLED", "CLUSTER_NODE_NAME", "CLUSTER_PEERS", "CLUSTER_SSH_USER", "CLUSTER_REMOTE_STAGING"]
    },
    {
        "id": "cleanup",
        "title": "Cleanup Settings",
        "keys": ["CLEANUP_MKV_AFTER_TRANSFER", "CLEANUP_ISO_AFTER_TRANSFER", "CLEANUP_PREVIEW_AFTER_TRANSFER"]
    },
    {
        "id": "retry",
        "title": "Retry & Locks",
        "keys": ["MAX_RETRIES", "RETRY_DELAY", "LOCK_FILE", "ISO_LOCK_FILE", "ENCODER_LOCK_FILE", "TRANSFER_LOCK_FILE"]
    }
]

# Settings that use toggle switches (0/1 values)
BOOLEAN_SETTINGS = {
    "NAS_ENABLED", "PIPELINE_MODE", "CREATE_ISO", "ENCODE_VIDEO",
    "ENABLE_PARALLEL_ENCODING", "GENERATE_PREVIEWS", "CLUSTER_ENABLED",
    "CLEANUP_MKV_AFTER_TRANSFER", "CLEANUP_ISO_AFTER_TRANSFER", "CLEANUP_PREVIEW_AFTER_TRANSFER"
}

# Settings with dropdown options
DROPDOWN_SETTINGS = {
    "LOG_LEVEL": ["DEBUG", "INFO", "WARN", "ERROR"],
    "NAS_TRANSFER_METHOD": ["rsync", "scp"],
    "TRANSFER_MODE": ["remote", "local"],
    "HANDBRAKE_FORMAT": ["mkv", "mp4"]
}


def trigger_service(stage):
    """Trigger a systemd service."""
    if stage not in ["encoder", "transfer", "distribute"]:
        return False, "Invalid stage"

    service_name = f"dvd-{stage}.service"
    try:
        result = subprocess.run(
            ["systemctl", "start", "--no-block", service_name],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, result.stderr.strip() or "OK"
    except Exception as e:
        return False, str(e)


# ============================================================================
# Service & Timer Status Functions
# ============================================================================

# Services and timers managed by the DVD ripper
MANAGED_SERVICES = [
    {"name": "dvd-dashboard", "description": "Web Dashboard"},
    {"name": "dvd-encoder", "description": "Video Encoder (Stage 2)"},
    {"name": "dvd-transfer", "description": "NAS Transfer (Stage 3)"},
]

MANAGED_TIMERS = [
    {"name": "dvd-encoder", "description": "Encoder Timer (15 min)"},
    {"name": "dvd-transfer", "description": "Transfer Timer (15 min)"},
]


def get_service_status(service_name):
    """Get detailed status of a systemd service."""
    try:
        # Check if service is active
        result = subprocess.run(
            ["systemctl", "is-active", f"{service_name}.service"],
            capture_output=True, text=True, timeout=5
        )
        is_active = result.stdout.strip() == "active"

        # Check if service is enabled
        result = subprocess.run(
            ["systemctl", "is-enabled", f"{service_name}.service"],
            capture_output=True, text=True, timeout=5
        )
        is_enabled = result.stdout.strip() == "enabled"

        # Get more details
        result = subprocess.run(
            ["systemctl", "show", f"{service_name}.service",
             "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        props = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                key, _, value = line.partition("=")
                props[key] = value

        return {
            "active": is_active,
            "enabled": is_enabled,
            "state": props.get("ActiveState", "unknown"),
            "substate": props.get("SubState", "unknown"),
            "pid": props.get("MainPID", "0"),
            "started": props.get("ExecMainStartTimestamp", ""),
        }
    except Exception as e:
        return {
            "active": False,
            "enabled": False,
            "state": "error",
            "substate": str(e),
            "pid": "0",
            "started": "",
        }


def get_timer_status(timer_name):
    """Get detailed status of a systemd timer."""
    try:
        # Check if timer is active
        result = subprocess.run(
            ["systemctl", "is-active", f"{timer_name}.timer"],
            capture_output=True, text=True, timeout=5
        )
        is_active = result.stdout.strip() == "active"

        # Check if timer is enabled
        result = subprocess.run(
            ["systemctl", "is-enabled", f"{timer_name}.timer"],
            capture_output=True, text=True, timeout=5
        )
        is_enabled = result.stdout.strip() == "enabled"

        # Get timer details
        result = subprocess.run(
            ["systemctl", "show", f"{timer_name}.timer",
             "--property=NextElapseUSecRealtime,LastTriggerUSec"],
            capture_output=True, text=True, timeout=5
        )
        props = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                key, _, value = line.partition("=")
                props[key] = value

        return {
            "active": is_active,
            "enabled": is_enabled,
            "next_trigger": props.get("NextElapseUSecRealtime", ""),
            "last_trigger": props.get("LastTriggerUSec", ""),
        }
    except Exception as e:
        return {
            "active": False,
            "enabled": False,
            "next_trigger": "",
            "last_trigger": "",
            "error": str(e),
        }


def get_all_service_status():
    """Get status of all managed services."""
    services = []
    for svc in MANAGED_SERVICES:
        status = get_service_status(svc["name"])
        services.append({
            "name": svc["name"],
            "description": svc["description"],
            **status
        })
    return services


def get_all_timer_status():
    """Get status of all managed timers."""
    timers = []
    for tmr in MANAGED_TIMERS:
        status = get_timer_status(tmr["name"])
        timers.append({
            "name": tmr["name"],
            "description": tmr["description"],
            **status
        })
    return timers


def get_udev_trigger_status():
    """Get status of the udev disc detection trigger."""
    udev_rule = "/etc/udev/rules.d/99-dvd-ripper.rules"
    udev_disabled = f"{udev_rule}.disabled"

    if os.path.exists(udev_rule):
        return {
            "enabled": True,
            "status": "active",
            "message": "Disc insertion triggers ISO creation"
        }
    elif os.path.exists(udev_disabled):
        return {
            "enabled": False,
            "status": "paused",
            "message": "Disc detection paused (rule disabled)"
        }
    else:
        return {
            "enabled": False,
            "status": "missing",
            "message": "Udev rule not installed"
        }


def control_service(service_name, action):
    """Start, stop, or restart a systemd service."""
    if action not in ["start", "stop", "restart"]:
        return False, "Invalid action"

    # Validate service name
    valid_services = [s["name"] for s in MANAGED_SERVICES]
    if service_name not in valid_services:
        return False, "Invalid service"

    # Don't allow stopping the dashboard from itself
    if service_name == "dvd-dashboard" and action == "stop":
        return False, "Cannot stop dashboard from web UI"

    try:
        result = subprocess.run(
            ["systemctl", action, f"{service_name}.service"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, result.stderr.strip() or "OK"
    except Exception as e:
        return False, str(e)


def control_timer(timer_name, action):
    """Start (unpause), stop (pause), enable, or disable a systemd timer."""
    if action not in ["start", "stop", "enable", "disable"]:
        return False, "Invalid action"

    # Validate timer name
    valid_timers = [t["name"] for t in MANAGED_TIMERS]
    if timer_name not in valid_timers:
        return False, "Invalid timer"

    try:
        result = subprocess.run(
            ["systemctl", action, f"{timer_name}.timer"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, result.stderr.strip() or "OK"
    except Exception as e:
        return False, str(e)


# ============================================================================
# Identification Helper Functions
# ============================================================================

def is_generic_title(title):
    """Check if title appears to be generic/fallback and needs identification."""
    if not title:
        return True
    upper_title = title.upper()
    for pattern in GENERIC_PATTERNS:
        if re.match(pattern, upper_title):
            return True
    # Also flag very short titles
    if len(title) <= 3:
        return True
    return False


def get_audit_flags():
    """Get audit flags for suspicious MKVs (created by dvd-audit.sh)."""
    flags = []
    pattern = os.path.join(STAGING_DIR, ".audit-*")
    for audit_file in glob.glob(pattern):
        try:
            with open(audit_file, 'r') as f:
                data = json.load(f)
                data['audit_file'] = os.path.basename(audit_file)
                data['mtime'] = os.path.getmtime(audit_file)
                flags.append(data)
        except (json.JSONDecodeError, IOError):
            continue
    return sorted(flags, key=lambda x: x.get('mtime', 0))


def get_pending_identification():
    """Get items that need identification (generic names in renameable states)."""
    pending = []
    for state in RENAMEABLE_STATES:
        pattern = os.path.join(STAGING_DIR, f"*.{state}")
        for state_file in glob.glob(pattern):
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            title = metadata.get('title', '')
            # Check explicit flag first, fall back to pattern matching
            needs_id = metadata.get('needs_identification', is_generic_title(title))

            if needs_id:
                pending.append({
                    "state_file": os.path.basename(state_file),
                    "state": state,
                    "metadata": metadata,
                    "mtime": os.path.getmtime(state_file)
                })

    return sorted(pending, key=lambda x: x["mtime"])


def sanitize_filename(name):
    """Sanitize string for use in filenames."""
    # Replace special characters with underscore
    sanitized = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    # Collapse multiple underscores
    sanitized = re.sub(r'__+', '_', sanitized)
    # Remove leading/trailing underscores
    return sanitized.strip('_')


def generate_plex_filename(title, year, extension):
    """Generate Plex-compatible filename like 'The Matrix (1999).mkv'."""
    # Clean title (replace underscores with spaces, title case)
    clean = title.replace('_', ' ')
    clean = ' '.join(word.capitalize() for word in clean.split())

    if year and re.match(r'^\d{4}$', str(year)):
        return f"{clean} ({year}).{extension}"
    return f"{clean}.{extension}"


def read_nas_config():
    """Read NAS configuration from config file."""
    config = read_config()
    return {
        "host": config.get("NAS_HOST", "").replace("***", ""),  # Config may be masked
        "user": config.get("NAS_USER", "").replace("***", ""),
        "path": config.get("NAS_PATH", "").replace("***", "")
    }


def rename_remote_file(nas_host, nas_user, old_path, new_path):
    """Rename a file on the NAS via SSH."""
    try:
        cmd = ["ssh", f"{nas_user}@{nas_host}", f'mv "{old_path}" "{new_path}"']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stderr.strip() or "OK"
    except Exception as e:
        return False, str(e)


def rename_item(state_file_path, new_title, new_year):
    """
    Rename an item's files and update metadata.

    Steps:
    1. Read current metadata
    2. Generate new filenames
    3. Rename MKV, ISO, preview files (local or remote)
    4. Update metadata with new paths
    5. Create new state file with updated metadata
    6. Remove old state file
    """
    # Read current metadata
    with open(state_file_path, 'r') as f:
        metadata = json.load(f)

    old_title = metadata.get('title', '')
    timestamp = metadata.get('timestamp', '')
    year = metadata.get('year', '')
    main_title = metadata.get('main_title', '')
    state = os.path.basename(state_file_path).rsplit('.', 1)[-1]

    # Generate new sanitized title for state files
    new_sanitized = sanitize_filename(new_title)

    # Current paths
    old_mkv = metadata.get('mkv_path', '')
    old_iso = metadata.get('iso_path', '')
    old_preview = metadata.get('preview_path', '')
    old_nas = metadata.get('nas_path', '')

    # Initialize new paths
    new_mkv = old_mkv
    new_iso = old_iso
    new_preview = old_preview
    new_nas = old_nas

    # Rename local MKV if exists
    if old_mkv and os.path.exists(old_mkv):
        new_mkv_name = generate_plex_filename(new_title, new_year, 'mkv')
        new_mkv = os.path.join(STAGING_DIR, new_mkv_name)
        os.rename(old_mkv, new_mkv)

    # Rename ISO if exists (could be .iso or .iso.deletable)
    if old_iso:
        iso_deletable = old_iso + '.deletable'
        if os.path.exists(iso_deletable):
            new_iso_name = f"{new_sanitized}-{timestamp}.iso.deletable"
            new_iso = os.path.join(STAGING_DIR, new_iso_name).replace('.deletable', '')
            os.rename(iso_deletable, new_iso + '.deletable')
        elif os.path.exists(old_iso):
            new_iso_name = f"{new_sanitized}-{timestamp}.iso"
            new_iso = os.path.join(STAGING_DIR, new_iso_name)
            os.rename(old_iso, new_iso)

    # Rename preview if exists
    if old_preview and os.path.exists(old_preview):
        new_preview = os.path.join(STAGING_DIR, f"{new_sanitized}-{timestamp}.preview.mp4")
        os.rename(old_preview, new_preview)

    # Rename NAS file if transferred
    if state == "transferred" and old_nas:
        nas_config = read_nas_config()
        if nas_config["host"] and nas_config["user"]:
            new_nas_name = generate_plex_filename(new_title, new_year, 'mkv')
            nas_dir = os.path.dirname(old_nas)
            new_nas = os.path.join(nas_dir, new_nas_name)
            success, msg = rename_remote_file(
                nas_config["host"], nas_config["user"], old_nas, new_nas
            )
            if not success:
                raise Exception(f"Failed to rename on NAS: {msg}")

    # Build updated metadata
    new_metadata = {
        "title": new_sanitized,
        "year": new_year or year,
        "timestamp": timestamp,
        "main_title": main_title,
        "iso_path": new_iso,
        "mkv_path": new_mkv,
        "preview_path": new_preview,
        "nas_path": new_nas,
        "needs_identification": False,
        "identified_at": datetime.now().isoformat(),
        "original_title": old_title,
        "created_at": metadata.get("created_at", "")
    }

    # Create new state file
    new_state_file = os.path.join(STAGING_DIR, f"{new_sanitized}-{timestamp}.{state}")

    with open(new_state_file, 'w') as f:
        json.dump(new_metadata, f, indent=2)

    # Remove old state file (if different)
    if state_file_path != new_state_file:
        os.remove(state_file_path)

    return os.path.basename(new_state_file)




# ============================================================================
# Routes
# ============================================================================

@app.route("/")
def dashboard():
    """Main dashboard page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")
    page = request.args.get("page", 1, type=int)

    cluster_config = get_cluster_config()
    queue_data = get_queue_items(page=page)
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        counts=count_by_state(),
        queue=queue_data["items"],
        queue_total=queue_data["total"],
        queue_page=queue_data["page"],
        queue_total_pages=queue_data["total_pages"],
        disk=get_disk_usage(),
        locks=get_lock_status(),
        progress=get_active_progress(),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        message=message,
        message_type=message_type,
        pending_identification=len(get_pending_identification()),
        audit_flag_count=len(get_audit_flags()),
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        cluster_enabled=cluster_config.get("cluster_enabled", False),
        hostname=HOSTNAME
    )


@app.route("/logs")
def logs_page():
    """Per-stage logs overview page."""
    lines = request.args.get("lines", 50, type=int)
    logs = {stage: get_stage_logs(stage, lines) for stage in LOG_FILES.keys()}
    return render_template(
        "logs.html",
        active_page="logs",
        version=DASHBOARD_VERSION,
        logs=logs,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@app.route("/log/<stage>")
def stage_log_page(stage):
    """Individual stage log page."""
    if stage not in LOG_FILES:
        return f"Unknown stage: {stage}", 404
    lines = request.args.get("lines", 200, type=int)
    return render_template(
        "stage_log.html",
        stage=stage,
        logs=get_stage_logs(stage, lines),
        version=DASHBOARD_VERSION,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@app.route("/config")
def config_page():
    """Configuration edit page with collapsible sections."""
    return render_template(
        "config.html",
        active_page="config",
        config=read_config_full(),
        sections=CONFIG_SECTIONS,
        boolean_settings=BOOLEAN_SETTINGS,
        dropdown_settings=DROPDOWN_SETTINGS,
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@app.route("/architecture")
def architecture_page():
    """Architecture documentation page."""
    return render_template(
        "architecture.html",
        active_page="architecture",
        version=DASHBOARD_VERSION,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@app.route("/issues")
@app.route("/identify")  # Keep old route for backwards compatibility
def issues_page():
    """Issues page for items needing attention (identification, audit flags)."""
    return render_template(
        "identify.html",
        active_page="issues",
        pending=get_pending_identification(),
        audit_flags=get_audit_flags(),
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@app.route("/status")
def status_page():
    """Service and timer status page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template(
        "status.html",
        active_page="status",
        services=get_all_service_status(),
        timers=get_all_timer_status(),
        udev_trigger=get_udev_trigger_status(),
        message=message,
        message_type=message_type,
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@app.route("/health")
def health_page():
    """System health monitoring page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template(
        "health.html",
        cpu=get_cpu_usage(),
        memory=get_memory_usage(),
        load=get_load_average(),
        temps=get_temperatures(),
        io=get_io_stats(),
        processes=get_dvd_processes(),
        message=message,
        message_type=message_type,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


def get_distributed_jobs():
    """Get jobs that have been distributed to peer nodes."""
    jobs = []
    # Find distributing and distributed-to-* state files
    for pattern in ["*.distributing", "*.distributed-to-*"]:
        for state_file in glob.glob(os.path.join(STAGING_DIR, pattern)):
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
                basename = os.path.basename(state_file)
                state = basename.split('.')[-1]  # Get state from extension
                jobs.append({
                    "title": metadata.get("title", "Unknown"),
                    "timestamp": metadata.get("timestamp", ""),
                    "dest_node": metadata.get("dest_node", state.replace("distributed-to-", "")),
                    "state": state,
                    "file": basename
                })
            except:
                pass
    return jobs


def get_received_jobs():
    """Get jobs received from peer nodes (is_remote_job=true)."""
    jobs = []
    # Check iso-ready, encoding, and encoded-ready for remote jobs
    for state in ["iso-ready", "encoding", "encoded-ready"]:
        for state_file in glob.glob(os.path.join(STAGING_DIR, f"*.{state}")):
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
                if metadata.get("is_remote_job"):
                    basename = os.path.basename(state_file)
                    jobs.append({
                        "title": metadata.get("title", "Unknown"),
                        "timestamp": metadata.get("timestamp", ""),
                        "origin_node": metadata.get("origin_node", "Unknown"),
                        "received_at": metadata.get("received_at", ""),
                        "state": state,
                        "file": basename
                    })
            except:
                pass
    return jobs


@app.route("/cluster")
def cluster_page():
    """Cluster status page showing all nodes and distributed jobs."""
    config = get_cluster_config()

    # Get this node's status
    this_node = {
        "node_name": config["node_name"],
        "transfer_mode": config["transfer_mode"],
        "capacity": get_worker_capacity()
    }

    # Get peer status (only if cluster enabled)
    peers = []
    if config["cluster_enabled"]:
        peers = get_all_peer_status()

    # Get hostname for quick-enable form
    try:
        hostname = socket.gethostname().split('.')[0]  # Short hostname
    except Exception:
        hostname = "node1"

    return render_template(
        "cluster.html",
        active_page="cluster",
        cluster_enabled=config["cluster_enabled"],
        this_node=this_node,
        peers=peers,
        peers_raw=config["peers_raw"],
        distributed_jobs=get_distributed_jobs(),
        received_jobs=get_received_jobs(),
        io=get_io_stats(),
        hostname=hostname,
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


# ============================================================================
# API Routes
# ============================================================================

@app.route("/api/status")
def api_status():
    """API: Get pipeline status counts."""
    return jsonify({
        "counts": count_by_state(),
        "locks": get_lock_status(),
        "pipeline_version": get_pipeline_version(),
        "dashboard_version": DASHBOARD_VERSION
    })


@app.route("/api/queue")
def api_queue():
    """API: Get queue items with optional pagination."""
    page = request.args.get("page", type=int)
    per_page = request.args.get("per_page", type=int)

    if page is not None:
        return jsonify(get_queue_items(page=page, per_page=per_page))
    return jsonify(get_queue_items())  # All items for backward compat


@app.route("/api/logs")
def api_logs():
    """API: Get recent logs (combined from all stages)."""
    lines = request.args.get("lines", 100, type=int)
    return jsonify({"logs": get_all_logs(lines)})


@app.route("/api/logs/<stage>")
def api_stage_logs(stage):
    """API: Get logs for a specific stage."""
    if stage not in LOG_FILES:
        return jsonify({"error": f"Unknown stage: {stage}"}), 404
    lines = request.args.get("lines", 100, type=int)
    return jsonify({"stage": stage, "logs": get_stage_logs(stage, lines)})


@app.route("/api/disk")
def api_disk():
    """API: Get disk usage."""
    return jsonify(get_disk_usage())


@app.route("/api/config")
def api_config():
    """API: Get configuration."""
    return jsonify(read_config())


@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    """API: Save configuration changes."""
    data = request.get_json() or {}
    settings = data.get("settings", {})

    if not settings:
        return jsonify({"success": False, "message": "No settings provided"}), 400

    # Write config and get results
    success, changed_keys, message = write_config(settings)

    if success:
        # Get restart recommendations for changed settings
        restart_recs = get_restart_recommendations(changed_keys)
        return jsonify({
            "success": True,
            "message": message,
            "changed_keys": changed_keys,
            "restart_recommendations": restart_recs
        })
    else:
        return jsonify({
            "success": False,
            "message": message
        }), 500


@app.route("/api/locks")
def api_locks():
    """API: Get lock status."""
    return jsonify(get_lock_status())


@app.route("/api/progress")
def api_progress():
    """API: Get real-time progress for active processes."""
    return jsonify(get_active_progress())


@app.route("/api/health")
def api_health():
    """API: Get system health metrics."""
    return jsonify({
        "cpu": get_cpu_usage(),
        "memory": get_memory_usage(),
        "load": get_load_average(),
        "temps": get_temperatures(),
        "io": get_io_stats()
    })


@app.route("/api/processes")
def api_processes():
    """API: Get list of DVD ripper processes."""
    return jsonify(get_dvd_processes())


@app.route("/api/kill/<int:pid>", methods=["POST"])
def api_kill_process(pid):
    """API: Kill a DVD ripper process with cleanup."""
    success, message = kill_process_with_cleanup(pid)

    # If called from form, redirect back to health page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("health_page",
                                    message=message,
                                    type="success"))
        else:
            return redirect(url_for("health_page",
                                    message=message,
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"error": message}), 500


@app.route("/api/trigger/<stage>", methods=["POST"])
def api_trigger(stage):
    """API: Trigger encoder or transfer stage."""
    success, message = trigger_service(stage)

    # If called from form, redirect back to dashboard
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard", message=f"{stage.title()} triggered successfully", type="success"))
        else:
            return redirect(url_for("dashboard", message=f"Failed to trigger {stage}: {message}", type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "triggered", "stage": stage})
    else:
        return jsonify({"error": message}), 500


@app.route("/api/queue/<path:state_file>/cancel", methods=["POST"])
def api_cancel_queue_item(state_file):
    """API: Cancel/remove a queue item by state file name."""
    data = request.get_json() or {}
    delete_files = data.get('delete_files', False)

    success, message = cancel_queue_item(state_file, delete_files)

    # If called from form, redirect back to dashboard
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard", message=message, type="success"))
        else:
            return redirect(url_for("dashboard", message=f"Cancel failed: {message}", type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"error": message}), 500


# ============================================================================
# Identification API Routes
# ============================================================================

@app.route("/api/identify/pending")
def api_identify_pending():
    """API: Get items pending identification."""
    return jsonify(get_pending_identification())


@app.route("/api/audit/flags")
def api_audit_flags():
    """API: Get audit flags for suspicious MKVs."""
    return jsonify(get_audit_flags())


@app.route("/api/audit/clear/<path:title>", methods=["POST"])
def api_audit_clear(title):
    """API: Clear an audit flag after issue is resolved."""
    sanitized = title.replace(' ', '_')
    audit_file = os.path.join(STAGING_DIR, f".audit-{sanitized}")
    if os.path.exists(audit_file):
        try:
            os.remove(audit_file)
            return jsonify({"status": "ok", "message": f"Cleared audit flag for {title}"})
        except OSError as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Audit flag not found"}), 404


@app.route("/api/identify/<path:state_file>/rename", methods=["POST"])
def api_identify_rename(state_file):
    """API: Rename an item with new title/year."""
    data = request.get_json() or {}
    new_title = data.get('title', '').strip()
    new_year = data.get('year', '').strip()

    if not new_title:
        return jsonify({"error": "Title is required"}), 400

    if new_year and not re.match(r'^\d{4}$', new_year):
        return jsonify({"error": "Year must be 4 digits"}), 400

    # Build full path and verify state file exists
    full_path = os.path.join(STAGING_DIR, state_file)
    if not os.path.exists(full_path):
        return jsonify({"error": "Item not found"}), 404

    # Verify state is renameable
    state = state_file.rsplit('.', 1)[-1] if '.' in state_file else ''
    if state not in RENAMEABLE_STATES:
        return jsonify({"error": f"Cannot rename items in '{state}' state"}), 400

    try:
        new_state_file = rename_item(full_path, new_title, new_year)
        return jsonify({
            "status": "renamed",
            "new_state_file": new_state_file,
            "new_title": new_title,
            "new_year": new_year
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/preview/<filename>")
def api_serve_preview(filename):
    """API: Serve preview video file."""
    # Security: only allow .preview.mp4 files
    if not filename.endswith('.preview.mp4'):
        return jsonify({"error": "Invalid preview file"}), 400

    preview_path = os.path.join(STAGING_DIR, filename)
    if not os.path.exists(preview_path):
        return jsonify({"error": "Preview not found"}), 404

    return send_file(preview_path, mimetype='video/mp4')


# ============================================================================
# Service & Timer Control API Routes
# ============================================================================

@app.route("/api/service/<name>", methods=["POST"])
def api_control_service(name):
    """API: Start, stop, or restart a service."""
    action = request.form.get("action") or (request.get_json() or {}).get("action")

    if not action:
        return jsonify({"error": "Action required"}), 400

    success, message = control_service(name, action)

    # If called from form, redirect back to status page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("status_page",
                                    message=f"Service {name} {action}ed successfully",
                                    type="success"))
        else:
            return redirect(url_for("status_page",
                                    message=f"Failed to {action} {name}: {message}",
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "service": name, "action": action})
    else:
        return jsonify({"error": message}), 500


@app.route("/api/timer/<name>", methods=["POST"])
def api_control_timer(name):
    """API: Start (unpause), stop (pause), enable, or disable a timer."""
    action = request.form.get("action") or (request.get_json() or {}).get("action")

    if not action:
        return jsonify({"error": "Action required"}), 400

    success, message = control_timer(name, action)

    # Human-readable action descriptions
    action_desc = {
        "start": "unpaused",
        "stop": "paused",
        "enable": "enabled",
        "disable": "disabled"
    }.get(action, action + "ed")

    # If called from form, redirect back to status page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("status_page",
                                    message=f"Timer {name} {action_desc} successfully",
                                    type="success"))
        else:
            return redirect(url_for("status_page",
                                    message=f"Failed to {action} timer {name}: {message}",
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "timer": name, "action": action})
    else:
        return jsonify({"error": message}), 500


@app.route("/api/udev/<action>", methods=["POST"])
def api_control_udev(action):
    """API: Pause or resume the udev disc detection trigger."""
    if action not in ["pause", "resume"]:
        return jsonify({"error": "Invalid action. Use 'pause' or 'resume'"}), 400

    # Use systemctl to trigger the udev control service
    # This runs via polkit which allows dvd-web to manage dvd-udev-control@*.service
    service = f"dvd-udev-control@{action}.service"

    try:
        result = subprocess.run(
            ["systemctl", "start", service],
            capture_output=True, text=True, timeout=10
        )
        success = result.returncode == 0
        if success:
            message = f"Disc detection {action}d"
        else:
            message = result.stderr.strip() or result.stdout.strip() or "Unknown error"
    except subprocess.TimeoutExpired:
        success = False
        message = "Command timed out"
    except Exception as e:
        success = False
        message = str(e)

    # Human-readable action descriptions
    action_desc = "paused" if action == "pause" else "resumed"

    # If called from form, redirect back to status page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("status_page",
                                    message=f"Disc detection {action_desc}",
                                    type="success"))
        else:
            return redirect(url_for("status_page",
                                    message=f"Failed to {action} disc detection: {message}",
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "action": action, "message": message})
    else:
        return jsonify({"error": message}), 500


# ============================================================================
# Cluster API Routes
# ============================================================================

def get_cluster_config():
    """Read cluster-related configuration from config file."""
    config = read_config()
    return {
        "cluster_enabled": config.get("CLUSTER_ENABLED", "0") == "1",
        "node_name": config.get("CLUSTER_NODE_NAME", ""),
        "peers_raw": config.get("CLUSTER_PEERS", ""),
        "ssh_user": config.get("CLUSTER_SSH_USER", ""),
        "remote_staging": config.get("CLUSTER_REMOTE_STAGING", "/var/tmp/dvd-rips"),
        "transfer_mode": config.get("TRANSFER_MODE", "remote"),
        "local_library_path": config.get("LOCAL_LIBRARY_PATH", ""),
        "enable_parallel": config.get("ENABLE_PARALLEL_ENCODING", "0") == "1",
        "max_parallel": int(config.get("MAX_PARALLEL_ENCODERS", "2")),
        "load_threshold": float(config.get("ENCODER_LOAD_THRESHOLD", "0.8"))
    }


def parse_cluster_peers(peers_raw):
    """Parse peer string into list of peer dicts.

    Format: "name:host:port name2:host2:port2"
    Example: "plex:192.168.1.50:5000 cart:192.168.1.34:5000"
    """
    peers = []
    if not peers_raw:
        return peers

    for entry in peers_raw.split():
        parts = entry.split(":")
        if len(parts) >= 3:
            peers.append({
                "name": parts[0],
                "host": parts[1],
                "port": int(parts[2])
            })
    return peers


def count_active_encoders():
    """Count currently running HandBrakeCLI processes."""
    count = 0
    try:
        proc = subprocess.run(
            ["pgrep", "-c", "HandBrakeCLI"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            count = int(proc.stdout.strip())
    except Exception:
        pass
    return count


def get_worker_capacity():
    """Calculate available encoding capacity for this node."""
    config = get_cluster_config()
    load = get_load_average()
    cpu_count = os.cpu_count() or 1
    max_load = cpu_count * config["load_threshold"]
    slots_used = count_active_encoders()
    max_slots = config["max_parallel"] if config["enable_parallel"] else 1
    slots_free = max(0, max_slots - slots_used)

    # Count pending ISOs
    pattern = os.path.join(STAGING_DIR, "*.iso-ready")
    queue_depth = len(glob.glob(pattern))

    # Available if: load is acceptable AND we have free slots
    available = load["load_1m"] < max_load and slots_free > 0

    return {
        "available": available,
        "load_1m": load["load_1m"],
        "load_5m": load["load_5m"],
        "max_load": round(max_load, 2),
        "cpu_count": cpu_count,
        "slots_total": max_slots,
        "slots_used": slots_used,
        "slots_free": slots_free,
        "queue_depth": queue_depth,
        "transfer_mode": config["transfer_mode"]
    }


def ping_peer(host, port, timeout=5):
    """Check if a peer is alive and get its capacity.

    Returns capacity dict on success, None on failure.
    """
    import urllib.request
    import urllib.error

    url = f"http://{host}:{port}/api/worker/capacity"
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
    except Exception:
        return None


def get_all_peer_status():
    """Get status of all configured peers."""
    config = get_cluster_config()
    peers = parse_cluster_peers(config["peers_raw"])

    results = []
    for peer in peers:
        capacity = ping_peer(peer["host"], peer["port"])
        results.append({
            "name": peer["name"],
            "host": peer["host"],
            "port": peer["port"],
            "online": capacity is not None,
            "capacity": capacity
        })
    return results


@app.route("/api/cluster/status")
def api_cluster_status():
    """API: Get this node's cluster configuration and status."""
    config = get_cluster_config()
    load = get_load_average()

    return jsonify({
        "node_name": config["node_name"],
        "cluster_enabled": config["cluster_enabled"],
        "transfer_mode": config["transfer_mode"],
        "local_library_path": config["local_library_path"] if config["transfer_mode"] == "local" else None,
        "peers": parse_cluster_peers(config["peers_raw"]),
        "load": load,
        "capacity": get_worker_capacity()
    })


@app.route("/api/cluster/peers")
def api_cluster_peers():
    """API: List all configured peers and their current status."""
    config = get_cluster_config()

    if not config["cluster_enabled"]:
        return jsonify({
            "cluster_enabled": False,
            "peers": [],
            "message": "Cluster mode is not enabled"
        })

    return jsonify({
        "cluster_enabled": True,
        "this_node": config["node_name"],
        "peers": get_all_peer_status()
    })


@app.route("/api/worker/capacity")
def api_worker_capacity():
    """API: Get this node's current encoding capacity.

    Called by peer nodes to check if we can accept work.
    """
    config = get_cluster_config()
    capacity = get_worker_capacity()

    return jsonify({
        "node_name": config["node_name"],
        **capacity
    })


@app.route("/api/cluster/ping", methods=["POST"])
def api_cluster_ping():
    """API: Health check endpoint for peer nodes.

    Peers call this to verify connectivity and get basic status.
    """
    config = get_cluster_config()

    return jsonify({
        "status": "ok",
        "node_name": config["node_name"],
        "cluster_enabled": config["cluster_enabled"],
        "timestamp": datetime.now().isoformat()
    })


@app.route("/api/worker/accept-job", methods=["POST"])
def api_accept_job():
    """API: Accept an encoding job from a peer node.

    Expected JSON body:
    {
        "metadata": {...},  # State file metadata
        "origin": "node_name"  # Originating node
    }

    Creates a local iso-ready state file for the encoder to pick up.
    """
    config = get_cluster_config()

    if not config["cluster_enabled"]:
        return jsonify({"error": "Cluster mode is not enabled"}), 400

    # Note: No capacity check here. Capacity is checked BEFORE rsync transfer
    # in find_available_peer(). Once ISO is transferred, we always accept the
    # job so it gets queued for encoding.

    try:
        data = request.json
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        metadata = data.get("metadata")
        origin = data.get("origin", "unknown")

        if not metadata:
            return jsonify({"error": "Missing metadata in request"}), 400

        # Extract required fields from metadata
        title = metadata.get("title")
        timestamp = metadata.get("timestamp")
        iso_path = metadata.get("iso_path")

        if not title or not timestamp:
            return jsonify({"error": "Missing title or timestamp in metadata"}), 400

        # Update ISO path to local staging directory
        # The ISO should have been rsync'd to our staging dir
        iso_filename = os.path.basename(iso_path) if iso_path else ""
        local_iso_path = os.path.join(STAGING_DIR, iso_filename)

        # Verify ISO exists locally
        if not os.path.exists(local_iso_path):
            return jsonify({
                "error": f"ISO not found: {local_iso_path}",
                "expected_path": local_iso_path
            }), 404

        # Update metadata with local paths and remote job markers
        metadata["iso_path"] = local_iso_path
        metadata["origin_node"] = origin
        metadata["is_remote_job"] = True
        metadata["received_at"] = datetime.now().isoformat()

        # Create state file for encoder to pick up
        state_file = f"{STAGING_DIR}/{title}-{timestamp}.iso-ready"

        with open(state_file, 'w') as f:
            json.dump(metadata, f, indent=2)

        # Calculate queue position (number of iso-ready files including this one)
        queue_depth = len(glob.glob(os.path.join(STAGING_DIR, "*.iso-ready")))

        return jsonify({
            "status": "accepted",
            "state_file": os.path.basename(state_file),
            "node_name": config["node_name"],
            "queue_position": queue_depth
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cluster/job-complete", methods=["POST"])
def api_job_complete():
    """API: Notification that a distributed job has completed on a peer.

    Expected JSON body:
    {
        "title": "Movie Title",
        "timestamp": "123456789",
        "mkv_path": "/path/to/encoded.mkv",
        "success": true
    }

    Updates local state for the distributed job.
    """
    config = get_cluster_config()

    try:
        data = request.json
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        title = data.get("title")
        timestamp = data.get("timestamp")
        success = data.get("success", True)
        mkv_path = data.get("mkv_path")

        if not title or not timestamp:
            return jsonify({"error": "Missing title or timestamp"}), 400

        # Find the distributed state file
        pattern = f"{title}-{timestamp}.distributed-to-*"
        matches = glob.glob(os.path.join(STAGING_DIR, pattern))

        if not matches:
            return jsonify({
                "status": "ok",
                "message": "No matching distributed state file found (may have been cleaned up)"
            })

        state_file = matches[0]

        if success:
            # Read existing metadata and update
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
            except:
                metadata = {}

            metadata["mkv_path"] = mkv_path
            metadata["remote_completed_at"] = datetime.now().isoformat()

            # Transition to encoded-ready
            new_state_file = f"{STAGING_DIR}/{title}-{timestamp}.encoded-ready"
            with open(new_state_file, 'w') as f:
                json.dump(metadata, f, indent=2)

            os.remove(state_file)

            return jsonify({
                "status": "ok",
                "message": "State updated to encoded-ready",
                "state_file": os.path.basename(new_state_file)
            })
        else:
            # Job failed, return to iso-ready for local retry
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
            except:
                metadata = {}

            metadata["remote_failed_at"] = datetime.now().isoformat()
            metadata.pop("is_remote_job", None)
            metadata.pop("origin_node", None)

            new_state_file = f"{STAGING_DIR}/{title}-{timestamp}.iso-ready"
            with open(new_state_file, 'w') as f:
                json.dump(metadata, f, indent=2)

            os.remove(state_file)

            return jsonify({
                "status": "ok",
                "message": "State reverted to iso-ready for local retry",
                "state_file": os.path.basename(new_state_file)
            })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cluster/confirm-files", methods=["POST"])
def api_confirm_files():
    """API: Confirm that specified files exist in staging directory.

    Used by peers to verify that transferred files arrived successfully.

    Expected JSON:
    {
        "files": ["TITLE-TIMESTAMP.iso", "TITLE-TIMESTAMP.iso.mapfile", ...]
    }

    Returns:
    {
        "confirmed": ["files", "that", "exist"],
        "missing": ["files", "that", "dont"]
    }
    """
    data = request.json or {}
    files = data.get("files", [])

    if not isinstance(files, list):
        return jsonify({"error": "files must be a list"}), 400

    confirmed = []
    missing = []

    for filename in files:
        # Security: only allow checking files in staging dir, no path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            continue

        path = os.path.join(STAGING_DIR, filename)
        if os.path.exists(path):
            confirmed.append(filename)
        else:
            missing.append(filename)

    return jsonify({
        "confirmed": confirmed,
        "missing": missing
    })


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print(f"DVD Ripper Dashboard v{DASHBOARD_VERSION}")
    print(f"Pipeline version: {get_pipeline_version()}")
    print(f"Starting on http://{host}:{port}")
    print(f"Project: {GITHUB_URL}")
    app.run(host=host, port=port, debug=debug)
