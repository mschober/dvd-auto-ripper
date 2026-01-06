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
from flask import Flask, jsonify, render_template_string, request, redirect, url_for, send_file

app = Flask(__name__)

# Configuration - can be overridden via environment variables
STAGING_DIR = os.environ.get("STAGING_DIR", "/var/tmp/dvd-rips")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/dvd-ripper.log")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/dvd-ripper.conf")
PIPELINE_VERSION_FILE = os.environ.get("PIPELINE_VERSION_FILE", "/usr/local/bin/VERSION")
DASHBOARD_VERSION = "1.6.0"
GITHUB_URL = "https://github.com/mschober/dvd-auto-ripper"

LOCK_FILES = {
    "iso": "/run/dvd-ripper/iso.lock",
    "encoder": "/run/dvd-ripper/encoder.lock",
    "transfer": "/run/dvd-ripper/transfer.lock"
}
STATE_ORDER = ["iso-creating", "iso-ready", "encoding", "encoded-ready", "transferring", "transferred"]

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


def get_queue_items():
    """Read all state files and return sorted list of queue items."""
    items = []
    for state in STATE_ORDER:
        pattern = os.path.join(STAGING_DIR, f"*.{state}")
        for state_file in glob.glob(pattern):
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
            except (json.JSONDecodeError, IOError):
                metadata = {}

            items.append({
                "state": state,
                "file": os.path.basename(state_file),
                "metadata": metadata,
                "mtime": os.path.getmtime(state_file)
            })

    return sorted(items, key=lambda x: x["mtime"])


def count_by_state():
    """Return dict of counts by state."""
    counts = {}
    for state in STATE_ORDER:
        pattern = os.path.join(STAGING_DIR, f"*.{state}")
        counts[state] = len(glob.glob(pattern))
    return counts


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


def get_recent_logs(lines=50):
    """Read last N lines from log file.

    Also checks rotated log files in case a process is still writing
    to the old (rotated) file handle after logrotate runs.
    Uses whichever log file has the most recent modification time.
    """
    try:
        log_file = LOG_FILE

        # Get main log mtime
        try:
            main_mtime = os.path.getmtime(LOG_FILE) if os.path.exists(LOG_FILE) else 0
        except OSError:
            main_mtime = 0

        # Check for rotated log files that may be more recently modified
        # This happens when encoder is still writing to old file handle
        log_dir = os.path.dirname(LOG_FILE)
        log_base = os.path.basename(LOG_FILE)

        try:
            for f in os.listdir(log_dir):
                if f.startswith(log_base) and f != log_base and not f.endswith('.gz'):
                    full_path = os.path.join(log_dir, f)
                    try:
                        mtime = os.path.getmtime(full_path)
                        # Use rotated log if it's more recently modified
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


def get_lock_status():
    """Check which stages are currently locked/running."""
    status = {}
    for stage, lock_file in LOCK_FILES.items():
        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r') as f:
                    pid = f.read().strip()
                # Check if process is actually running via /proc (works across users)
                # os.kill(pid, 0) requires same-user or root permissions
                if os.path.exists(f"/proc/{pid}"):
                    status[stage] = {"active": True, "pid": pid}
                else:
                    status[stage] = {"active": False, "pid": None}
            except (ValueError, IOError):
                status[stage] = {"active": False, "pid": None}
        else:
            status[stage] = {"active": False, "pid": None}
    return status


def get_active_progress():
    """
    Parse recent logs to extract progress for active processes.
    Returns dict with progress info for iso, encoder, and transfer stages.
    """
    progress = {"iso": None, "encoder": None, "transfer": None}
    locks = get_lock_status()

    # Only parse if something is actually running
    if not any(s["active"] for s in locks.values()):
        return progress

    # Read more lines to catch progress updates
    logs = get_recent_logs(200)

    # Parse HandBrake encoding progress
    # Pattern: "Encoding: task X of Y, XX.XX % (XX.XX fps, avg XX.XX fps, ETA XXhXXmXXs)"
    if locks.get("encoder", {}).get("active"):
        # Find all encoding lines and get the most recent one
        encoder_matches = re.findall(
            r'Encoding:.*?(\d+\.?\d*)\s*%.*?(\d+\.?\d*)\s*fps.*?ETA\s*(\d+h\d+m\d+s|\d+m\d+s)',
            logs
        )
        if encoder_matches:
            last_match = encoder_matches[-1]
            progress["encoder"] = {
                "percent": float(last_match[0]),
                "speed": f"{last_match[1]} fps",
                "eta": last_match[2]
            }

    # Parse ddrescue ISO creation progress
    # Pattern: "pct rescued:  XX.XX%, read errors:        0,  remaining time:         Xm"
    if locks.get("iso", {}).get("active"):
        iso_matches = re.findall(
            r'pct rescued:\s*(\d+\.?\d*)%.*?remaining time:\s*(\d+m|\d+s|n/a)',
            logs
        )
        if iso_matches:
            last_match = iso_matches[-1]
            progress["iso"] = {
                "percent": float(last_match[0]),
                "eta": last_match[1] if last_match[1] != "n/a" else "finishing..."
            }

    # Parse rsync transfer progress
    # Pattern: "XX% XX.XXMB/s X:XX:XX" or "XXX,XXX,XXX  XX%  XX.XXmB/s    X:XX:XX"
    if locks.get("transfer", {}).get("active"):
        transfer_matches = re.findall(
            r'(\d+)%\s+([\d.]+[KMG]?B/s)\s+(\d+:\d+:\d+)',
            logs
        )
        if transfer_matches:
            last_match = transfer_matches[-1]
            progress["transfer"] = {
                "percent": float(last_match[0]),
                "speed": last_match[1],
                "eta": last_match[2]
            }

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
    pid = int(pid)

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

    # Try to find associated lock file
    lock_file = None
    if process_type == "encoder":
        lock_file = LOCK_FILES.get("encoder")
    elif process_type == "iso":
        lock_file = LOCK_FILES.get("iso")
    elif process_type == "transfer":
        lock_file = LOCK_FILES.get("transfer")

    # Find associated state file for cleanup
    state_to_revert = None
    revert_to = None

    if process_type == "encoder":
        state_to_revert = "encoding"
        revert_to = "iso-ready"
    elif process_type == "iso":
        state_to_revert = "iso-creating"
        revert_to = None  # Just remove, disc can be re-inserted
    elif process_type == "transfer":
        state_to_revert = "transferring"
        revert_to = "encoded-ready"

    try:
        # Send SIGTERM first
        os.kill(pid, 15)  # SIGTERM

        # Wait a moment for graceful shutdown
        import time
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

        # Revert state files
        cleanup_msg = ""
        if state_to_revert:
            pattern = os.path.join(STAGING_DIR, f"*.{state_to_revert}")
            for state_file in glob.glob(pattern):
                try:
                    if revert_to:
                        # Rename to previous state
                        base = state_file.rsplit('.', 1)[0]
                        new_state_file = f"{base}.{revert_to}"
                        os.rename(state_file, new_state_file)
                        cleanup_msg += f" Reverted {os.path.basename(state_file)} to {revert_to}."
                    else:
                        # Just remove the state file
                        os.remove(state_file)
                        cleanup_msg += f" Removed {os.path.basename(state_file)}."
                except Exception as e:
                    cleanup_msg += f" Failed to clean up {os.path.basename(state_file)}: {e}"

        return True, f"Killed PID {pid} ({process_type}).{cleanup_msg}"

    except ProcessLookupError:
        return False, f"Process {pid} not found"
    except PermissionError:
        return False, f"Permission denied to kill PID {pid}"
    except Exception as e:
        return False, f"Failed to kill PID {pid}: {e}"


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
    if stage not in ["encoder", "transfer"]:
        return False, "Invalid stage"

    service_name = f"dvd-{stage}.service"
    try:
        result = subprocess.run(
            ["systemctl", "start", service_name],
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
# HTML Templates
# ============================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>DVD Ripper Dashboard</title>
    <meta http-equiv="refresh" content="30">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f0f2f5; color: #1a1a1a;
            min-height: 100vh;
        }
        h1 { margin: 0 0 20px 0; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
        .card {
            background: white; border-radius: 8px; padding: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .card h2 { margin: 0 0 12px 0; font-size: 16px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
        .status-badge {
            display: inline-block; padding: 4px 10px; border-radius: 12px;
            font-size: 11px; font-weight: 600;
        }
        .state-iso-creating { background: #fed7aa; color: #9a3412; }
        .state-iso-ready { background: #fef3c7; color: #92400e; }
        .state-encoding { background: #dbeafe; color: #1e40af; }
        .state-encoded-ready { background: #d1fae5; color: #065f46; }
        .state-transferring { background: #ede9fe; color: #5b21b6; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th { text-align: left; padding: 8px; border-bottom: 2px solid #eee; color: #666; font-weight: 600; }
        td { padding: 8px; border-bottom: 1px solid #eee; }
        .btn {
            padding: 6px 12px; border: none; border-radius: 4px;
            cursor: pointer; font-size: 12px; font-weight: 500;
            text-decoration: none; display: inline-block;
        }
        .btn-primary { background: #3b82f6; color: white; }
        .btn-primary:hover { background: #2563eb; }
        .btn-primary:disabled { background: #9ca3af; cursor: not-allowed; }
        pre {
            background: #1e1e1e; color: #d4d4d4; padding: 12px;
            overflow-x: auto; border-radius: 4px; font-size: 11px;
            line-height: 1.4; max-height: 300px; overflow-y: auto;
            margin: 0;
        }
        .disk-bar { background: #e5e7eb; height: 24px; border-radius: 12px; overflow: hidden; }
        .disk-fill { height: 100%; transition: width 0.3s; display: flex; align-items: center;
                     justify-content: center; color: white; font-size: 12px; font-weight: 600;
                     min-width: 40px; }
        .disk-ok { background: #10b981; }
        .disk-warn { background: #f59e0b; }
        .disk-danger { background: #ef4444; }
        .lock-status { display: flex; gap: 20px; flex-wrap: wrap; }
        .lock-item { display: flex; align-items: center; gap: 8px; }
        .lock-dot { width: 12px; height: 12px; border-radius: 50%; }
        .lock-active { background: #10b981; animation: pulse 2s infinite; }
        .lock-idle { background: #d1d5db; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .progress-section { margin-top: 16px; }
        .progress-item { margin-bottom: 12px; }
        .progress-header { display: flex; justify-content: space-between; margin-bottom: 4px; font-size: 13px; }
        .progress-label { font-weight: 500; }
        .progress-stats { color: #666; }
        .progress-bar { background: #e5e7eb; height: 8px; border-radius: 4px; overflow: hidden; }
        .progress-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
        .progress-iso { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
        .progress-encoder { background: linear-gradient(90deg, #3b82f6, #60a5fa); }
        .progress-transfer { background: linear-gradient(90deg, #8b5cf6, #a78bfa); }
        .queue-empty { color: #666; font-style: italic; padding: 20px 0; }
        a { color: #3b82f6; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .flash { padding: 12px 16px; border-radius: 4px; margin-bottom: 16px; }
        .flash-success { background: #d1fae5; color: #065f46; border: 1px solid #a7f3d0; }
        .flash-error { background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
        .footer {
            margin-top: 24px; padding-top: 16px; border-top: 1px solid #e5e7eb;
            font-size: 12px; color: #666; text-align: center;
        }
        .footer a { color: #3b82f6; }
    </style>
</head>
<body>
    <h1>DVD Ripper Dashboard</h1>

    {% if message %}
    <div class="flash flash-{{ message_type }}">{{ message }}</div>
    {% endif %}

    <div class="grid">
        <div class="card">
            <h2>Pipeline Status</h2>
            <table>
                <tr><th>Stage</th><th>Count</th><th>Action</th></tr>
                {% for state, count in counts.items() %}
                <tr>
                    <td><span class="status-badge state-{{ state }}">{{ state }}</span></td>
                    <td><strong>{{ count }}</strong></td>
                    <td>
                        {% if state == 'iso-ready' and count > 0 %}
                        <form method="POST" action="/api/trigger/encoder" style="display:inline">
                            <button class="btn btn-primary" type="submit"
                                    {% if locks.encoder.active %}disabled title="Encoder already running"{% endif %}>
                                Run Encoder
                            </button>
                        </form>
                        {% elif state == 'encoded-ready' and count > 0 %}
                        <form method="POST" action="/api/trigger/transfer" style="display:inline">
                            <button class="btn btn-primary" type="submit"
                                    {% if locks.transfer.active %}disabled title="Transfer already running"{% endif %}>
                                Run Transfer
                            </button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>Disk Usage</h2>
            <div class="disk-bar">
                <div class="disk-fill {% if disk.percent_num >= 80 %}disk-danger{% elif disk.percent_num >= 60 %}disk-warn{% else %}disk-ok{% endif %}"
                     style="width: {{ disk.percent }}%">{{ disk.percent }}%</div>
            </div>
            <p style="margin: 12px 0 0 0; font-size: 14px;">
                <strong>{{ disk.used }}</strong> used of <strong>{{ disk.total }}</strong>
                (<strong>{{ disk.available }}</strong> free)
            </p>
            <p style="margin: 4px 0 0 0; font-size: 12px; color: #666;">
                Mount: {{ disk.mount }}
            </p>
        </div>

        <div class="card">
            <h2>Active Processes</h2>
            <div class="lock-status">
                {% for stage, status in locks.items() %}
                <div class="lock-item">
                    <div class="lock-dot {% if status.active %}lock-active{% else %}lock-idle{% endif %}"></div>
                    <span>
                        <strong>{{ stage }}</strong>
                        {% if status.active %}<span style="color: #666;">(PID {{ status.pid }})</span>{% endif %}
                    </span>
                </div>
                {% endfor %}
            </div>
            <div class="progress-section" id="progress-section">
                {% if progress.iso %}
                <div class="progress-item">
                    <div class="progress-header">
                        <span class="progress-label">ISO Creation</span>
                        <span class="progress-stats">{{ "%.1f"|format(progress.iso.percent) }}% | ETA: {{ progress.iso.eta }}</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill progress-iso" style="width: {{ progress.iso.percent }}%"></div>
                    </div>
                </div>
                {% endif %}
                {% if progress.encoder %}
                <div class="progress-item">
                    <div class="progress-header">
                        <span class="progress-label">Encoding</span>
                        <span class="progress-stats">{{ "%.1f"|format(progress.encoder.percent) }}% | {{ progress.encoder.speed }} | ETA: {{ progress.encoder.eta }}</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill progress-encoder" style="width: {{ progress.encoder.percent }}%"></div>
                    </div>
                </div>
                {% endif %}
                {% if progress.transfer %}
                <div class="progress-item">
                    <div class="progress-header">
                        <span class="progress-label">Transfer</span>
                        <span class="progress-stats">{{ "%.1f"|format(progress.transfer.percent) }}% | {{ progress.transfer.speed }} | ETA: {{ progress.transfer.eta }}</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill progress-transfer" style="width: {{ progress.transfer.percent }}%"></div>
                    </div>
                </div>
                {% endif %}
                {% if not progress.iso and not progress.encoder and not progress.transfer %}
                <p style="color: #666; font-size: 13px; margin: 12px 0 0 0;">No active operations</p>
                {% endif %}
            </div>
        </div>
    </div>

    <div class="card" style="margin-top: 16px;">
        <h2>Queue ({{ queue|length }} items)</h2>
        {% if queue %}
        <table>
            <tr><th>Title</th><th>Year</th><th>State</th><th>Created</th></tr>
            {% for item in queue %}
            <tr>
                <td>{{ item.metadata.get('title', 'Unknown') | replace('_', ' ') }}</td>
                <td>{{ item.metadata.get('year', '-') or '-' }}</td>
                <td><span class="status-badge state-{{ item.state }}">{{ item.state }}</span></td>
                <td style="font-size: 12px; color: #666;">{{ item.metadata.get('created_at', 'N/A')[:19] }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <p class="queue-empty">No items in queue. Insert a DVD to start ripping.</p>
        {% endif %}
    </div>

    <div class="card" style="margin-top: 16px;">
        <h2>Recent Logs <a href="/logs" style="font-size: 12px; font-weight: normal;">(view all)</a></h2>
        <pre>{{ logs }}</pre>
    </div>

    <div class="footer">
        <p>
            Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
            <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a> |
            <a href="/architecture">Architecture</a> |
            <a href="/logs">Logs</a> |
            <a href="/config">Config</a> |
            <a href="/status">Status</a> |
            <a href="/health">Health</a> |
            <a href="/cluster">Cluster</a> |
            <a href="/identify">
                Pending ID
                {% if pending_identification > 0 %}
                <span style="background: #f59e0b; color: white; padding: 2px 6px; border-radius: 10px; font-size: 10px; font-weight: 600;">{{ pending_identification }}</span>
                {% endif %}
            </a>
        </p>
        <p style="margin-top: 4px;">
            Auto-refreshes every 30 seconds. Progress updates every 10 seconds. Last update: {{ now }}
        </p>
    </div>

    <script>
    // Auto-refresh progress bars every 10 seconds
    function updateProgress() {
        fetch('/api/progress')
            .then(response => response.json())
            .then(data => {
                const section = document.getElementById('progress-section');
                if (!section) return;

                let html = '';

                if (data.iso) {
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">ISO Creation</span>
                                <span class="progress-stats">${data.iso.percent.toFixed(1)}% | ETA: ${data.iso.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-iso" style="width: ${data.iso.percent}%"></div>
                            </div>
                        </div>`;
                }

                if (data.encoder) {
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">Encoding</span>
                                <span class="progress-stats">${data.encoder.percent.toFixed(1)}% | ${data.encoder.speed} | ETA: ${data.encoder.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-encoder" style="width: ${data.encoder.percent}%"></div>
                            </div>
                        </div>`;
                }

                if (data.transfer) {
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">Transfer</span>
                                <span class="progress-stats">${data.transfer.percent.toFixed(1)}% | ${data.transfer.speed} | ETA: ${data.transfer.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-transfer" style="width: ${data.transfer.percent}%"></div>
                            </div>
                        </div>`;
                }

                if (!data.iso && !data.encoder && !data.transfer) {
                    html = '<p style="color: #666; font-size: 13px; margin: 12px 0 0 0;">No active operations</p>';
                }

                section.innerHTML = html;
            })
            .catch(err => console.log('Progress update failed:', err));
    }

    // Update progress every 10 seconds
    setInterval(updateProgress, 10000);
    </script>
</body>
</html>
"""

ARCHITECTURE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>DVD Ripper Architecture</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f0f2f5; color: #1a1a1a;
        }
        h1 { margin: 0 0 8px 0; }
        h2 { margin: 24px 0 12px 0; color: #333; }
        a { color: #3b82f6; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .card {
            background: white; border-radius: 8px; padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 16px;
        }
        pre {
            background: #1e293b; color: #e2e8f0; padding: 20px;
            border-radius: 8px; overflow-x: auto; font-size: 13px;
            line-height: 1.4; margin: 0;
        }
        table { width: 100%; border-collapse: collapse; margin: 16px 0; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #eee; }
        th { background: #f8fafc; font-weight: 600; color: #475569; }
        .stage-badge {
            display: inline-block; padding: 4px 12px; border-radius: 12px;
            font-size: 12px; font-weight: 600;
        }
        .stage-1 { background: #fef3c7; color: #92400e; }
        .stage-2 { background: #dbeafe; color: #1e40af; }
        .stage-3 { background: #d1fae5; color: #065f46; }
        .state-file { font-family: monospace; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
        .footer { margin-top: 24px; font-size: 12px; color: #666; text-align: center; }
        ul { margin: 8px 0; padding-left: 24px; }
        li { margin: 4px 0; }
        code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 13px; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Architecture</h1>
    <p style="color: #666; margin-top: 0;">Understanding the DVD auto-ripper pipeline</p>

    <div class="card">
        <h2 style="margin-top: 0;">Pipeline Overview</h2>
        <pre>
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DVD AUTO-RIPPER PIPELINE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌───────────┐  │
│  │   DVD    │     │   STAGE 1    │     │   STAGE 2    │     │  STAGE 3  │  │
│  │  INSERT  │────▶│  ISO Create  │────▶│   Encoder    │────▶│ Transfer  │  │
│  │          │     │  (udev)      │     │  (15 min)    │     │ (15 min)  │  │
│  └──────────┘     └──────────────┘     └──────────────┘     └───────────┘  │
│                          │                    │                    │        │
│                          ▼                    ▼                    ▼        │
│                   ┌────────────┐       ┌────────────┐       ┌──────────┐   │
│                   │ .iso file  │       │ .mkv file  │       │   NAS    │   │
│                   │ + eject    │       │ Plex-ready │       │  Plex    │   │
│                   └────────────┘       └────────────┘       └──────────┘   │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  State Files: *.iso-ready → *.encoding → *.encoded-ready → (cleanup)       │
│  Lock Files:  /run/dvd-ripper/{iso,encoder,transfer}.lock                  │
└─────────────────────────────────────────────────────────────────────────────┘
        </pre>
    </div>

    <div class="card">
        <h2 style="margin-top: 0;">Pipeline Stages</h2>
        <table>
            <tr>
                <th>Stage</th>
                <th>Script</th>
                <th>Trigger</th>
                <th>Purpose</th>
            </tr>
            <tr>
                <td><span class="stage-badge stage-1">Stage 1</span></td>
                <td><code>dvd-iso.sh</code></td>
                <td>udev (disc insert)</td>
                <td>Create ISO with ddrescue, eject disc immediately</td>
            </tr>
            <tr>
                <td><span class="stage-badge stage-2">Stage 2</span></td>
                <td><code>dvd-encoder.sh</code></td>
                <td>systemd timer (15 min)</td>
                <td>Encode ONE ISO to Plex-ready MKV per run</td>
            </tr>
            <tr>
                <td><span class="stage-badge stage-3">Stage 3</span></td>
                <td><code>dvd-transfer.sh</code></td>
                <td>systemd timer (15 min)</td>
                <td>Transfer ONE MKV to NAS per run</td>
            </tr>
        </table>
    </div>

    <div class="card">
        <h2 style="margin-top: 0;">Benefits of Pipeline Architecture</h2>
        <ul>
            <li><strong>Drive freed immediately:</strong> Disc ejects right after ISO creation, ready for next DVD</li>
            <li><strong>Background processing:</strong> Encoding happens via timer, doesn't block new rips</li>
            <li><strong>Resilient:</strong> Each stage can fail and retry independently</li>
            <li><strong>Queue-based:</strong> Multiple ISOs can queue up, processed one at a time</li>
            <li><strong>Resource efficient:</strong> Only one encode/transfer runs at a time</li>
        </ul>
    </div>

    <div class="card">
        <h2 style="margin-top: 0;">State File Flow</h2>
        <p>State files track progress through the pipeline. Each file contains JSON metadata.</p>
        <table>
            <tr>
                <th>State</th>
                <th>Meaning</th>
                <th>Next Action</th>
            </tr>
            <tr>
                <td><span class="state-file">*.iso-creating</span></td>
                <td>ISO creation in progress (ddrescue running)</td>
                <td>Wait for completion</td>
            </tr>
            <tr>
                <td><span class="state-file">*.iso-ready</span></td>
                <td>ISO complete, waiting for encoder</td>
                <td>Encoder picks up oldest</td>
            </tr>
            <tr>
                <td><span class="state-file">*.encoding</span></td>
                <td>HandBrake encoding in progress</td>
                <td>Wait for completion</td>
            </tr>
            <tr>
                <td><span class="state-file">*.encoded-ready</span></td>
                <td>MKV ready, waiting for transfer</td>
                <td>Transfer picks up oldest</td>
            </tr>
            <tr>
                <td><span class="state-file">*.transferring</span></td>
                <td>rsync/scp transfer in progress</td>
                <td>Wait for completion</td>
            </tr>
        </table>
    </div>

    <div class="card">
        <h2 style="margin-top: 0;">File Locations</h2>
        <table>
            <tr><th>Path</th><th>Purpose</th></tr>
            <tr><td><code>/var/tmp/dvd-rips/</code></td><td>Staging directory (ISOs, MKVs, state files)</td></tr>
            <tr><td><code>/var/log/dvd-ripper.log</code></td><td>Application log file</td></tr>
            <tr><td><code>/etc/dvd-ripper.conf</code></td><td>Configuration file</td></tr>
            <tr><td><code>/run/dvd-ripper/*.lock</code></td><td>Stage lock files (prevent concurrent runs)</td></tr>
            <tr><td><code>/usr/local/bin/dvd-*.sh</code></td><td>Pipeline scripts</td></tr>
        </table>
    </div>

    <div class="card">
        <h2 style="margin-top: 0;">Manual Commands</h2>
        <table>
            <tr><th>Command</th><th>Purpose</th></tr>
            <tr><td><code>systemctl start dvd-encoder.service</code></td><td>Trigger encoder immediately</td></tr>
            <tr><td><code>systemctl start dvd-transfer.service</code></td><td>Trigger transfer immediately</td></tr>
            <tr><td><code>systemctl list-timers | grep dvd</code></td><td>Check timer status</td></tr>
            <tr><td><code>journalctl -u dvd-encoder -f</code></td><td>Watch encoder logs</td></tr>
            <tr><td><code>ls /var/tmp/dvd-rips/.*</code></td><td>View state files</td></tr>
        </table>
    </div>

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a>
    </div>
</body>
</html>
"""

LOGS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>DVD Ripper Logs</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: monospace; margin: 0; padding: 20px;
            background: #1e1e1e; color: #d4d4d4;
        }
        h1 { color: white; margin: 0 0 16px 0; font-family: sans-serif; }
        a { color: #3b82f6; text-decoration: none; }
        a:hover { text-decoration: underline; }
        pre {
            background: #2d2d2d; padding: 16px; border-radius: 4px;
            overflow-x: auto; font-size: 12px; line-height: 1.5;
        }
        .controls { margin-bottom: 16px; }
        .btn {
            padding: 8px 16px; border: none; border-radius: 4px;
            cursor: pointer; background: #3b82f6; color: white;
            font-size: 14px; margin-right: 8px; text-decoration: none;
            display: inline-block;
        }
        .btn:hover { background: #2563eb; }
        .footer { margin-top: 16px; font-size: 12px; color: #666; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Logs</h1>
    <div class="controls">
        <a href="?lines=100" class="btn">Last 100</a>
        <a href="?lines=500" class="btn">Last 500</a>
        <a href="?lines=1000" class="btn">Last 1000</a>
    </div>
    <pre>{{ logs }}</pre>
    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a>
    </div>
</body>
</html>
"""

CONFIG_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Configuration - DVD Ripper</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f0f2f5; color: #1a1a1a;
            min-height: 100vh;
        }
        h1 { margin: 0 0 8px 0; }
        h1 a { color: #3b82f6; text-decoration: none; }
        h1 a:hover { text-decoration: underline; }
        .subtitle { color: #666; margin-bottom: 20px; }

        .section {
            background: white;
            border-radius: 8px;
            margin-bottom: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .section-header {
            padding: 14px 16px;
            background: #f9fafb;
            border-bottom: 1px solid #e5e7eb;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            user-select: none;
        }
        .section-header:hover { background: #f3f4f6; }
        .section-title {
            font-weight: 600;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #374151;
        }
        .section-toggle {
            font-size: 12px;
            color: #6b7280;
            transition: transform 0.2s;
        }
        .section.collapsed .section-toggle { transform: rotate(-90deg); }
        .section.collapsed .section-content { display: none; }
        .section-content { padding: 16px; }

        .setting-row {
            display: flex;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid #f3f4f6;
        }
        .setting-row:last-child { border-bottom: none; }
        .setting-label {
            flex: 0 0 220px;
            font-family: monospace;
            font-size: 13px;
            color: #4b5563;
        }
        .setting-input {
            flex: 1;
        }
        .setting-input input[type="text"],
        .setting-input input[type="number"],
        .setting-input select {
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            font-size: 14px;
            font-family: inherit;
        }
        .setting-input input:focus,
        .setting-input select:focus {
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }

        /* Toggle switch for booleans */
        .toggle-switch {
            position: relative;
            width: 50px;
            height: 26px;
        }
        .toggle-switch input { opacity: 0; width: 0; height: 0; }
        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #d1d5db;
            border-radius: 26px;
            transition: 0.3s;
        }
        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 20px; width: 20px;
            left: 3px; bottom: 3px;
            background: white;
            border-radius: 50%;
            transition: 0.3s;
        }
        .toggle-switch input:checked + .toggle-slider { background: #10b981; }
        .toggle-switch input:checked + .toggle-slider:before { transform: translateX(24px); }
        .toggle-label {
            margin-left: 12px;
            font-size: 13px;
            color: #6b7280;
        }

        .actions {
            position: sticky;
            bottom: 0;
            background: white;
            padding: 16px 20px;
            margin: 20px -20px -20px;
            border-top: 1px solid #e5e7eb;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 -2px 10px rgba(0,0,0,0.1);
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }
        .btn-primary { background: #3b82f6; color: white; }
        .btn-primary:hover { background: #2563eb; }
        .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; }
        .btn-secondary { background: #f3f4f6; color: #374151; }
        .btn-secondary:hover { background: #e5e7eb; }

        .status-msg {
            padding: 10px 16px;
            border-radius: 6px;
            font-size: 14px;
            display: none;
        }
        .status-msg.show { display: block; }
        .status-msg.success { background: #d1fae5; color: #065f46; }
        .status-msg.error { background: #fee2e2; color: #991b1b; }

        /* Restart modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal-overlay.show { display: flex; }
        .modal {
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 450px;
            width: 90%;
            box-shadow: 0 20px 40px rgba(0,0,0,0.2);
        }
        .modal h3 { margin: 0 0 16px 0; }
        .modal-body { margin-bottom: 20px; }
        .restart-item {
            display: flex;
            align-items: center;
            padding: 10px 12px;
            background: #f9fafb;
            border-radius: 6px;
            margin-bottom: 8px;
        }
        .restart-item input { margin-right: 12px; }
        .restart-item label { flex: 1; cursor: pointer; }
        .modal-actions { display: flex; gap: 12px; justify-content: flex-end; }

        .footer {
            margin-top: 20px;
            padding-top: 20px;
            font-size: 12px;
            color: #666;
            text-align: center;
        }
        .footer a { color: #3b82f6; text-decoration: none; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Configuration</h1>
    <p class="subtitle">Edit settings and save to /etc/dvd-ripper.conf</p>

    <div id="status-msg" class="status-msg"></div>

    <form id="config-form">
        {% for section in sections %}
        <div class="section" id="section-{{ section.id }}">
            <div class="section-header" onclick="toggleSection('{{ section.id }}')">
                <span class="section-title">{{ section.title }}</span>
                <span class="section-toggle">▼</span>
            </div>
            <div class="section-content">
                {% for key in section.keys %}
                <div class="setting-row">
                    <div class="setting-label">{{ key }}</div>
                    <div class="setting-input">
                        {% if key in boolean_settings %}
                        <label class="toggle-switch">
                            <input type="checkbox" name="{{ key }}" value="1" {% if config.get(key) == '1' %}checked{% endif %}>
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="toggle-label">{% if config.get(key) == '1' %}Enabled{% else %}Disabled{% endif %}</span>
                        {% elif key in dropdown_settings %}
                        <select name="{{ key }}">
                            {% for opt in dropdown_settings[key] %}
                            <option value="{{ opt }}" {% if config.get(key) == opt %}selected{% endif %}>{{ opt }}</option>
                            {% endfor %}
                        </select>
                        {% else %}
                        <input type="text" name="{{ key }}" value="{{ config.get(key, '') }}">
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endfor %}
    </form>

    <div class="actions">
        <span id="change-count" style="color: #6b7280; font-size: 13px;"></span>
        <div>
            <button type="button" class="btn btn-secondary" onclick="resetForm()">Reset</button>
            <button type="button" class="btn btn-primary" onclick="saveConfig()" id="save-btn">Save Changes</button>
        </div>
    </div>

    <!-- Restart Modal -->
    <div class="modal-overlay" id="restart-modal">
        <div class="modal">
            <h3>Restart Services?</h3>
            <div class="modal-body">
                <p style="margin: 0 0 16px 0; color: #6b7280;">
                    The following services may need to be restarted for changes to take effect:
                </p>
                <div id="restart-services"></div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeModal()">Skip</button>
                <button class="btn btn-primary" onclick="restartSelected()">Restart Selected</button>
            </div>
        </div>
    </div>

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a> |
        <a href="/">Back to Dashboard</a>
    </div>

    <script>
        const originalConfig = {{ config | tojson }};
        let pendingRestarts = [];

        function toggleSection(id) {
            document.getElementById('section-' + id).classList.toggle('collapsed');
        }

        function showStatus(msg, type) {
            const el = document.getElementById('status-msg');
            el.textContent = msg;
            el.className = 'status-msg show ' + type;
            setTimeout(() => el.classList.remove('show'), 5000);
        }

        function getFormData() {
            const form = document.getElementById('config-form');
            const data = {};

            // Get all inputs
            form.querySelectorAll('input[type="text"], input[type="number"], select').forEach(el => {
                data[el.name] = el.value;
            });

            // Get checkboxes (booleans)
            form.querySelectorAll('input[type="checkbox"]').forEach(el => {
                data[el.name] = el.checked ? '1' : '0';
            });

            return data;
        }

        function resetForm() {
            const form = document.getElementById('config-form');
            form.querySelectorAll('input[type="text"], input[type="number"]').forEach(el => {
                el.value = originalConfig[el.name] || '';
            });
            form.querySelectorAll('select').forEach(el => {
                el.value = originalConfig[el.name] || el.options[0].value;
            });
            form.querySelectorAll('input[type="checkbox"]').forEach(el => {
                el.checked = originalConfig[el.name] === '1';
                updateToggleLabel(el);
            });
            showStatus('Form reset to saved values', 'success');
        }

        function updateToggleLabel(checkbox) {
            const label = checkbox.parentElement.nextElementSibling;
            if (label) label.textContent = checkbox.checked ? 'Enabled' : 'Disabled';
        }

        // Update toggle labels on change
        document.querySelectorAll('.toggle-switch input').forEach(el => {
            el.addEventListener('change', () => updateToggleLabel(el));
        });

        async function saveConfig() {
            const btn = document.getElementById('save-btn');
            btn.disabled = true;
            btn.textContent = 'Saving...';

            try {
                const response = await fetch('/api/config/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: getFormData() })
                });

                const result = await response.json();

                if (result.success) {
                    showStatus(result.message, 'success');

                    // Update original config with new values
                    Object.assign(originalConfig, getFormData());

                    // Show restart modal if needed
                    if (result.restart_recommendations && result.restart_recommendations.length > 0) {
                        pendingRestarts = result.restart_recommendations;
                        showRestartModal(result.restart_recommendations);
                    }
                } else {
                    showStatus(result.message || 'Failed to save', 'error');
                }
            } catch (err) {
                showStatus('Error: ' + err.message, 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'Save Changes';
            }
        }

        function showRestartModal(services) {
            const container = document.getElementById('restart-services');
            container.innerHTML = services.map((svc, i) => `
                <div class="restart-item">
                    <input type="checkbox" id="restart-${i}" value="${svc.name}" data-type="${svc.type}" checked>
                    <label for="restart-${i}">${svc.name}.${svc.type}</label>
                </div>
            `).join('');
            document.getElementById('restart-modal').classList.add('show');
        }

        function closeModal() {
            document.getElementById('restart-modal').classList.remove('show');
        }

        async function restartSelected() {
            const checkboxes = document.querySelectorAll('#restart-services input:checked');
            const toRestart = Array.from(checkboxes).map(cb => ({
                name: cb.value,
                type: cb.dataset.type
            }));

            closeModal();

            for (const svc of toRestart) {
                try {
                    const endpoint = svc.type === 'timer' ? '/api/timer/' : '/api/service/';
                    await fetch(endpoint + svc.name, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ action: 'restart' })
                    });
                    showStatus(`Restarted ${svc.name}.${svc.type}`, 'success');
                } catch (err) {
                    showStatus(`Failed to restart ${svc.name}: ${err.message}`, 'error');
                }
            }
        }
    </script>
</body>
</html>
"""

IDENTIFY_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Pending Identification - DVD Ripper</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f0f2f5; color: #1a1a1a;
            min-height: 100vh;
        }
        h1 { margin: 0 0 8px 0; }
        h1 a { color: #3b82f6; text-decoration: none; }
        h1 a:hover { text-decoration: underline; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .identify-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
        }
        .identify-card {
            background: white;
            border-radius: 8px;
            padding: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .preview-container {
            position: relative;
            background: #000;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 12px;
        }
        .preview-video {
            width: 100%;
            max-height: 240px;
            display: block;
        }
        .no-preview {
            padding: 60px 20px;
            text-align: center;
            color: #666;
            background: #f8fafc;
            border-radius: 4px;
        }
        .current-name {
            font-family: monospace;
            background: #fef3c7;
            padding: 8px 12px;
            border-radius: 4px;
            margin-bottom: 12px;
            font-size: 13px;
            border-left: 3px solid #f59e0b;
        }
        .form-group { margin-bottom: 12px; }
        .form-group label {
            display: block;
            font-weight: 600;
            margin-bottom: 4px;
            color: #374151;
            font-size: 14px;
        }
        .form-group input {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            font-size: 14px;
        }
        .form-group input:focus {
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }
        .form-row {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 12px;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.2s;
        }
        .btn-primary {
            background: #3b82f6;
            color: white;
            width: 100%;
        }
        .btn-primary:hover { background: #2563eb; }
        .btn-primary:disabled {
            background: #9ca3af;
            cursor: not-allowed;
        }
        .state-info {
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid #e5e7eb;
            font-size: 12px;
            color: #6b7280;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .status-badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
        }
        .state-iso-ready { background: #fef3c7; color: #92400e; }
        .state-encoded-ready { background: #d1fae5; color: #065f46; }
        .state-transferred { background: #ddd6fe; color: #5b21b6; }
        .empty-state {
            background: white;
            border-radius: 8px;
            padding: 40px;
            text-align: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .empty-state h2 { color: #059669; margin-bottom: 8px; }
        .empty-state p { color: #6b7280; }
        .success-msg {
            background: #d1fae5;
            color: #065f46;
            padding: 8px 12px;
            border-radius: 4px;
            margin-bottom: 12px;
            display: none;
        }
        .error-msg {
            background: #fee2e2;
            color: #991b1b;
            padding: 8px 12px;
            border-radius: 4px;
            margin-bottom: 12px;
            display: none;
        }
        .footer {
            margin-top: 20px;
            font-size: 12px;
            color: #666;
            text-align: center;
        }
        .footer a { color: #3b82f6; text-decoration: none; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Pending Identification</h1>
    <p class="subtitle">These items have generic names and need proper titles for Plex. Watch the preview to identify each movie.</p>

    {% if pending %}
    <div class="identify-grid">
        {% for item in pending %}
        <div class="identify-card" data-state-file="{{ item.state_file }}">
            <div class="success-msg"></div>
            <div class="error-msg"></div>

            <div class="preview-container">
                {% if item.metadata.preview_path %}
                <video class="preview-video" controls preload="metadata">
                    <source src="/api/preview/{{ item.metadata.preview_path.split('/')[-1] }}" type="video/mp4">
                    Your browser does not support video playback.
                </video>
                {% else %}
                <div class="no-preview">
                    No preview available<br>
                    <small>Preview will be generated during encoding</small>
                </div>
                {% endif %}
            </div>

            <div class="current-name">
                Current: {{ item.metadata.title | replace('_', ' ') }}
                {% if item.metadata.year %} ({{ item.metadata.year }}){% endif %}
            </div>

            <form class="rename-form" onsubmit="return handleRename(this, event)">
                <div class="form-row">
                    <div class="form-group">
                        <label for="title-{{ loop.index }}">Movie Title</label>
                        <input type="text" id="title-{{ loop.index }}" name="title"
                               placeholder="The Matrix" required>
                    </div>
                    <div class="form-group">
                        <label for="year-{{ loop.index }}">Year</label>
                        <input type="text" id="year-{{ loop.index }}" name="year"
                               placeholder="1999" pattern="[0-9]{4}" maxlength="4">
                    </div>
                </div>
                <button type="submit" class="btn btn-primary">Rename &amp; Identify</button>
            </form>

            <div class="state-info">
                <span class="status-badge state-{{ item.state }}">{{ item.state }}</span>
                {% if item.metadata.nas_path %}
                <span title="{{ item.metadata.nas_path }}">On NAS</span>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="empty-state">
        <h2>All Clear!</h2>
        <p>No items need identification. All your DVDs have proper names.</p>
        <p><a href="/">Return to Dashboard</a></p>
    </div>
    {% endif %}

    <script>
    async function handleRename(form, event) {
        event.preventDefault();
        const card = form.closest('.identify-card');
        const stateFile = card.dataset.stateFile;
        const title = form.title.value.trim();
        const year = form.year.value.trim();
        const submitBtn = form.querySelector('button[type="submit"]');
        const successMsg = card.querySelector('.success-msg');
        const errorMsg = card.querySelector('.error-msg');

        // Hide previous messages
        successMsg.style.display = 'none';
        errorMsg.style.display = 'none';

        // Disable button during request
        submitBtn.disabled = true;
        submitBtn.textContent = 'Renaming...';

        try {
            const response = await fetch('/api/identify/' + encodeURIComponent(stateFile) + '/rename', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title, year})
            });
            const result = await response.json();

            if (response.ok) {
                successMsg.textContent = 'Renamed successfully to: ' + title + (year ? ' (' + year + ')' : '');
                successMsg.style.display = 'block';
                // Fade out and remove card after a moment
                setTimeout(() => {
                    card.style.opacity = '0.5';
                    card.style.pointerEvents = 'none';
                }, 1000);
                setTimeout(() => {
                    card.remove();
                    // Check if no more cards
                    if (document.querySelectorAll('.identify-card').length === 0) {
                        location.reload();
                    }
                }, 2000);
            } else {
                errorMsg.textContent = result.error || 'Rename failed';
                errorMsg.style.display = 'block';
                submitBtn.disabled = false;
                submitBtn.textContent = 'Rename & Identify';
            }
        } catch (e) {
            errorMsg.textContent = 'Request failed: ' + e.message;
            errorMsg.style.display = 'block';
            submitBtn.disabled = false;
            submitBtn.textContent = 'Rename & Identify';
        }
        return false;
    }
    </script>

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a>
    </div>
</body>
</html>
"""

STATUS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Service Status - DVD Ripper</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f0f2f5; color: #1a1a1a;
            min-height: 100vh;
        }
        h1 { margin: 0 0 8px 0; }
        h1 a { color: #3b82f6; text-decoration: none; }
        h1 a:hover { text-decoration: underline; }
        h2 { margin: 0 0 16px 0; font-size: 16px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; }
        .card {
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #eee; }
        th { color: #666; font-weight: 600; font-size: 12px; text-transform: uppercase; }
        .status-dot {
            display: inline-block;
            width: 10px; height: 10px;
            border-radius: 50%;
            margin-right: 8px;
        }
        .status-active { background: #10b981; }
        .status-inactive { background: #ef4444; }
        .status-unknown { background: #9ca3af; }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
        }
        .badge-active { background: #d1fae5; color: #065f46; }
        .badge-inactive { background: #fee2e2; color: #991b1b; }
        .badge-enabled { background: #dbeafe; color: #1e40af; }
        .badge-disabled { background: #f3f4f6; color: #6b7280; }
        .btn {
            padding: 6px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
            margin-right: 4px;
            transition: background 0.2s;
        }
        .btn-start { background: #10b981; color: white; }
        .btn-start:hover { background: #059669; }
        .btn-stop { background: #ef4444; color: white; }
        .btn-stop:hover { background: #dc2626; }
        .btn-restart { background: #f59e0b; color: white; }
        .btn-restart:hover { background: #d97706; }
        .btn-pause { background: #6b7280; color: white; }
        .btn-pause:hover { background: #4b5563; }
        .btn-unpause { background: #3b82f6; color: white; }
        .btn-unpause:hover { background: #2563eb; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .flash { padding: 12px 16px; border-radius: 4px; margin-bottom: 16px; }
        .flash-success { background: #d1fae5; color: #065f46; border: 1px solid #a7f3d0; }
        .flash-error { background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
        .meta { font-size: 12px; color: #6b7280; margin-top: 4px; }
        .footer {
            margin-top: 20px;
            font-size: 12px;
            color: #666;
            text-align: center;
        }
        .footer a { color: #3b82f6; text-decoration: none; }
        .refresh-note { font-size: 12px; color: #666; margin-top: 12px; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Service Status</h1>
    <p class="subtitle">Manage DVD ripper services and timers</p>

    {% if message %}
    <div class="flash flash-{{ message_type }}">{{ message }}</div>
    {% endif %}

    <div class="grid">
        <div class="card">
            <h2>Services</h2>
            <table>
                <tr>
                    <th>Service</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
                {% for svc in services %}
                <tr>
                    <td>
                        <span class="status-dot {% if svc.active %}status-active{% else %}status-inactive{% endif %}"></span>
                        <strong>{{ svc.description }}</strong>
                        <div class="meta">{{ svc.name }}.service</div>
                    </td>
                    <td>
                        <span class="badge {% if svc.active %}badge-active{% else %}badge-inactive{% endif %}">
                            {{ svc.state }}
                        </span>
                        {% if svc.active and svc.pid != "0" %}
                        <div class="meta">PID: {{ svc.pid }}</div>
                        {% endif %}
                    </td>
                    <td>
                        {% if svc.name != "dvd-dashboard" %}
                        <form method="POST" action="/api/service/{{ svc.name }}" style="display:inline">
                            {% if svc.active %}
                            <input type="hidden" name="action" value="stop">
                            <button class="btn btn-stop" type="submit">Stop</button>
                            {% else %}
                            <input type="hidden" name="action" value="start">
                            <button class="btn btn-start" type="submit">Start</button>
                            {% endif %}
                        </form>
                        <form method="POST" action="/api/service/{{ svc.name }}" style="display:inline">
                            <input type="hidden" name="action" value="restart">
                            <button class="btn btn-restart" type="submit" {% if not svc.active %}disabled{% endif %}>Restart</button>
                        </form>
                        {% else %}
                        <span class="meta">Cannot control from UI</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>Timers (Triggers)</h2>
            <table>
                <tr>
                    <th>Timer</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
                {% for tmr in timers %}
                <tr>
                    <td>
                        <span class="status-dot {% if tmr.active %}status-active{% else %}status-inactive{% endif %}"></span>
                        <strong>{{ tmr.description }}</strong>
                        <div class="meta">{{ tmr.name }}.timer</div>
                    </td>
                    <td>
                        <span class="badge {% if tmr.active %}badge-active{% else %}badge-inactive{% endif %}">
                            {% if tmr.active %}running{% else %}paused{% endif %}
                        </span>
                        <span class="badge {% if tmr.enabled %}badge-enabled{% else %}badge-disabled{% endif %}">
                            {% if tmr.enabled %}enabled{% else %}disabled{% endif %}
                        </span>
                        {% if tmr.next_trigger %}
                        <div class="meta">Next: {{ tmr.next_trigger }}</div>
                        {% endif %}
                    </td>
                    <td>
                        <form method="POST" action="/api/timer/{{ tmr.name }}" style="display:inline">
                            {% if tmr.active %}
                            <input type="hidden" name="action" value="stop">
                            <button class="btn btn-pause" type="submit">Pause</button>
                            {% else %}
                            <input type="hidden" name="action" value="start">
                            <button class="btn btn-unpause" type="submit">Unpause</button>
                            {% endif %}
                        </form>
                        <form method="POST" action="/api/timer/{{ tmr.name }}" style="display:inline">
                            {% if tmr.enabled %}
                            <input type="hidden" name="action" value="disable">
                            <button class="btn btn-stop" type="submit">Disable</button>
                            {% else %}
                            <input type="hidden" name="action" value="enable">
                            <button class="btn btn-start" type="submit">Enable</button>
                            {% endif %}
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </table>
            <p class="refresh-note">
                <strong>Pause</strong> = temporarily stop timer (until next reboot)<br>
                <strong>Disable</strong> = permanently stop timer (survives reboot)
            </p>
        </div>

        <div class="card">
            <h2>Disc Detection (udev)</h2>
            <table>
                <tr>
                    <th>Trigger</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
                <tr>
                    <td>
                        <span class="status-dot {% if udev_trigger.enabled %}status-active{% elif udev_trigger.status == 'missing' %}status-unknown{% else %}status-inactive{% endif %}"></span>
                        <strong>DVD Insert Detection</strong>
                        <div class="meta">99-dvd-ripper.rules</div>
                    </td>
                    <td>
                        <span class="badge {% if udev_trigger.enabled %}badge-active{% elif udev_trigger.status == 'missing' %}badge-disabled{% else %}badge-inactive{% endif %}">
                            {{ udev_trigger.status }}
                        </span>
                        <div class="meta">{{ udev_trigger.message }}</div>
                    </td>
                    <td>
                        {% if udev_trigger.status != 'missing' %}
                        <form method="POST" action="/api/udev/{% if udev_trigger.enabled %}pause{% else %}resume{% endif %}" style="display:inline">
                            {% if udev_trigger.enabled %}
                            <button class="btn btn-pause" type="submit">Pause</button>
                            {% else %}
                            <button class="btn btn-unpause" type="submit">Resume</button>
                            {% endif %}
                        </form>
                        {% else %}
                        <span class="meta">Run remote-install.sh to install</span>
                        {% endif %}
                    </td>
                </tr>
            </table>
            <p class="refresh-note">
                <strong>Pause</strong> = disable disc detection for manual operations<br>
                <strong>Resume</strong> = re-enable automatic disc detection
            </p>
        </div>
    </div>

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a> |
        <a href="/">Back to Dashboard</a>
    </div>
</body>
</html>
"""

CLUSTER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Cluster Status - DVD Ripper</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #f0f2f5; color: #1a1a1a;
            min-height: 100vh;
        }
        h1 { margin: 0 0 8px 0; }
        h1 a { color: #3b82f6; text-decoration: none; }
        h1 a:hover { text-decoration: underline; }
        h2 { margin: 0 0 16px 0; font-size: 16px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .card {
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .card-full { grid-column: 1 / -1; }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #eee; }
        th { color: #666; font-weight: 600; font-size: 12px; text-transform: uppercase; }
        .status-dot {
            display: inline-block;
            width: 10px; height: 10px;
            border-radius: 50%;
            margin-right: 8px;
        }
        .status-online { background: #10b981; }
        .status-offline { background: #ef4444; }
        .status-unknown { background: #9ca3af; }
        .status-busy { background: #f59e0b; }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
        }
        .badge-online { background: #d1fae5; color: #065f46; }
        .badge-offline { background: #fee2e2; color: #991b1b; }
        .badge-local { background: #dbeafe; color: #1e40af; }
        .badge-remote { background: #fef3c7; color: #92400e; }
        .badge-available { background: #d1fae5; color: #065f46; }
        .badge-busy { background: #fef3c7; color: #92400e; }
        .badge-disabled { background: #f3f4f6; color: #6b7280; }
        .node-card {
            background: white;
            border-radius: 8px;
            padding: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border-left: 4px solid #3b82f6;
        }
        .node-card.this-node { border-left-color: #10b981; }
        .node-card.offline { border-left-color: #ef4444; opacity: 0.7; }
        .node-card.add-worker-card { border-left-color: #6366f1; border-style: dashed; }
        .remove-peer-btn {
            background: transparent;
            border: 1px solid #ddd;
            border-radius: 4px;
            color: #888;
            cursor: pointer;
            font-size: 14px;
            padding: 2px 6px;
            transition: all 0.2s;
        }
        .remove-peer-btn:hover { background: #fee2e2; border-color: #ef4444; color: #ef4444; }
        .node-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .node-name {
            font-size: 18px;
            font-weight: 600;
        }
        .node-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-top: 12px;
        }
        .stat-box {
            text-align: center;
            padding: 8px;
            background: #f9fafb;
            border-radius: 6px;
        }
        .stat-value {
            font-size: 20px;
            font-weight: 700;
            color: #1a1a1a;
        }
        .stat-label {
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
        }
        .progress-bar {
            height: 8px;
            background: #e5e7eb;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 4px;
        }
        .progress-fill {
            height: 100%;
            background: #3b82f6;
            transition: width 0.3s;
        }
        .progress-fill.high { background: #ef4444; }
        .progress-fill.medium { background: #f59e0b; }
        .meta { font-size: 12px; color: #6b7280; margin-top: 4px; }
        .cluster-disabled {
            text-align: center;
            padding: 40px;
            color: #6b7280;
        }
        .cluster-disabled h3 { color: #1a1a1a; margin-bottom: 12px; }
        .cluster-disabled code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-size: 13px; }
        .footer {
            margin-top: 20px;
            font-size: 12px;
            color: #666;
            text-align: center;
        }
        .footer a { color: #3b82f6; text-decoration: none; }
        .empty-state { text-align: center; padding: 20px; color: #6b7280; }
        .refresh-btn {
            padding: 8px 16px;
            background: #3b82f6;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
        }
        .refresh-btn:hover { background: #2563eb; }

        /* I/O Panel styles (light theme) */
        .io-summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 16px;
            margin-bottom: 16px;
            text-align: center;
        }
        .io-stat {
            padding: 12px;
            background: #f9fafb;
            border-radius: 6px;
        }
        .io-value {
            font-size: 24px;
            font-weight: 600;
            color: #1a1a1a;
            display: block;
        }
        .io-label {
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
            margin-top: 4px;
        }
        .io-warn { color: #f59e0b; }
        .device-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .device-hdd { background: #9ca3af; color: white; }
        .device-ssd { background: #3b82f6; color: white; }
        .device-nvme { background: #10b981; color: white; }
        .device-usb { background: #f59e0b; color: white; }
        .device-unknown { background: #d1d5db; color: #374151; }
        .device-model { color: #9ca3af; font-size: 11px; display: block; }
        .io-unavailable { color: #6b7280; font-style: italic; font-size: 13px; text-align: center; padding: 20px; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Cluster Status</h1>
    <p class="subtitle">Distributed encoding across multiple machines</p>

    {% if not cluster_enabled %}
    <div class="card">
        <div class="cluster-disabled">
            <h3>Cluster Mode Disabled</h3>
            <p style="margin-bottom: 24px;">Enable cluster mode to distribute encoding across multiple machines.</p>

            <div id="enable-form" style="text-align: left; max-width: 400px; margin: 0 auto;">
                <div style="margin-bottom: 16px;">
                    <label style="display: block; font-weight: 600; margin-bottom: 6px; color: #374151;">
                        Node Name (this machine's identifier)
                    </label>
                    <input type="text" id="node-name" value="{{ hostname }}"
                           style="width: 100%; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px;">
                    <div style="font-size: 12px; color: #6b7280; margin-top: 4px;">
                        Used to identify this node in the cluster
                    </div>
                </div>

                <div style="margin-bottom: 16px;">
                    <label style="display: block; font-weight: 600; margin-bottom: 6px; color: #374151;">
                        Cluster Peers (optional)
                    </label>
                    <input type="text" id="cluster-peers" value=""
                           placeholder="name:host:port name:host:port"
                           style="width: 100%; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px;">
                    <div style="font-size: 12px; color: #6b7280; margin-top: 4px;">
                        Space-separated "name:host:port" entries (e.g., "plex:192.168.1.50:5000")
                    </div>
                </div>

                <button onclick="enableCluster()" class="refresh-btn" style="width: 100%; padding: 12px;">
                    Enable Cluster Mode
                </button>

                <div id="enable-status" style="margin-top: 12px; display: none;"></div>
            </div>

            <p style="margin-top: 24px; font-size: 13px;">
                Or edit the full configuration at <a href="/config" style="color: #3b82f6;">/config</a>
            </p>
        </div>
    </div>

    <script>
    async function enableCluster() {
        const nodeName = document.getElementById('node-name').value.trim();
        const peers = document.getElementById('cluster-peers').value.trim();
        const statusEl = document.getElementById('enable-status');

        if (!nodeName) {
            statusEl.style.display = 'block';
            statusEl.style.background = '#fee2e2';
            statusEl.style.color = '#991b1b';
            statusEl.style.padding = '10px';
            statusEl.style.borderRadius = '6px';
            statusEl.textContent = 'Please enter a node name';
            return;
        }

        statusEl.style.display = 'block';
        statusEl.style.background = '#dbeafe';
        statusEl.style.color = '#1e40af';
        statusEl.style.padding = '10px';
        statusEl.style.borderRadius = '6px';
        statusEl.textContent = 'Saving configuration...';

        try {
            const settings = {
                'CLUSTER_ENABLED': '1',
                'CLUSTER_NODE_NAME': nodeName
            };
            if (peers) {
                settings['CLUSTER_PEERS'] = peers;
            }

            const response = await fetch('/api/config/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ settings: settings })
            });

            const result = await response.json();

            if (result.success) {
                statusEl.style.background = '#d1fae5';
                statusEl.style.color = '#065f46';
                statusEl.innerHTML = 'Cluster mode enabled! <a href="/cluster" style="color: #065f46; font-weight: 600;">Reload page</a> to see cluster status.';

                // Auto-reload after a short delay
                setTimeout(() => {
                    window.location.reload();
                }, 1500);
            } else {
                statusEl.style.background = '#fee2e2';
                statusEl.style.color = '#991b1b';
                statusEl.textContent = 'Error: ' + result.message;
            }
        } catch (err) {
            statusEl.style.background = '#fee2e2';
            statusEl.style.color = '#991b1b';
            statusEl.textContent = 'Error: ' + err.message;
        }
    }
    </script>
    {% else %}

    <div class="grid">
        <!-- This Node -->
        <div class="node-card this-node">
            <div class="node-header">
                <span class="node-name">
                    <span class="status-dot status-online"></span>
                    {{ this_node.node_name or "This Node" }} (local)
                </span>
                <span class="badge badge-{{ 'local' if this_node.transfer_mode == 'local' else 'remote' }}">
                    {{ this_node.transfer_mode }} transfer
                </span>
            </div>
            <div class="node-stats">
                <div class="stat-box">
                    <div class="stat-value">{{ "%.1f"|format(this_node.capacity.load_1m) }}</div>
                    <div class="stat-label">Load (1m)</div>
                    <div class="progress-bar">
                        <div class="progress-fill {% if this_node.capacity.load_1m > this_node.capacity.max_load %}high{% elif this_node.capacity.load_1m > this_node.capacity.max_load * 0.7 %}medium{% endif %}"
                             style="width: {{ [100, (this_node.capacity.load_1m / this_node.capacity.max_load * 100)|int]|min }}%"></div>
                    </div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ this_node.capacity.slots_free }}/{{ this_node.capacity.slots_total }}</div>
                    <div class="stat-label">Slots Free</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ this_node.capacity.queue_depth }}</div>
                    <div class="stat-label">Queue</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ this_node.capacity.cpu_count }}</div>
                    <div class="stat-label">CPUs</div>
                </div>
            </div>
            <div class="meta" style="margin-top: 12px;">
                Max load threshold: {{ "%.1f"|format(this_node.capacity.max_load) }} |
                Status: <span class="badge badge-{{ 'available' if this_node.capacity.available else 'busy' }}">
                    {{ 'available' if this_node.capacity.available else 'busy' }}
                </span>
            </div>
        </div>

        <!-- Peer Nodes -->
        {% for peer in peers %}
        <div class="node-card {% if not peer.online %}offline{% endif %}">
            <div class="node-header">
                <span class="node-name">
                    <span class="status-dot status-{{ 'online' if peer.online else 'offline' }}"></span>
                    {{ peer.name }}
                </span>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span class="badge badge-{{ 'online' if peer.online else 'offline' }}">
                        {{ 'online' if peer.online else 'offline' }}
                    </span>
                    <button class="remove-peer-btn" onclick="removePeer('{{ peer.name }}')" title="Remove peer">✕</button>
                </div>
            </div>
            {% if peer.online and peer.capacity %}
            <div class="node-stats">
                <div class="stat-box">
                    <div class="stat-value">{{ "%.1f"|format(peer.capacity.load_1m) }}</div>
                    <div class="stat-label">Load (1m)</div>
                    <div class="progress-bar">
                        <div class="progress-fill {% if peer.capacity.load_1m > peer.capacity.max_load %}high{% elif peer.capacity.load_1m > peer.capacity.max_load * 0.7 %}medium{% endif %}"
                             style="width: {{ [100, (peer.capacity.load_1m / peer.capacity.max_load * 100)|int]|min }}%"></div>
                    </div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ peer.capacity.slots_free }}/{{ peer.capacity.slots_total }}</div>
                    <div class="stat-label">Slots Free</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ peer.capacity.queue_depth }}</div>
                    <div class="stat-label">Queue</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ peer.capacity.cpu_count }}</div>
                    <div class="stat-label">CPUs</div>
                </div>
            </div>
            <div class="meta" style="margin-top: 12px;">
                {{ peer.host }}:{{ peer.port }} |
                Transfer: {{ peer.capacity.transfer_mode }} |
                <span class="badge badge-{{ 'available' if peer.capacity.available else 'busy' }}">
                    {{ 'available' if peer.capacity.available else 'busy' }}
                </span>
            </div>
            {% else %}
            <div class="meta" style="margin-top: 12px;">
                {{ peer.host }}:{{ peer.port }} | Unable to connect
            </div>
            {% endif %}
        </div>
        {% endfor %}

        <!-- Add Worker Card -->
        <div class="node-card add-worker-card">
            <div class="node-header">
                <span class="node-name">+ Add Worker</span>
            </div>
            <div style="padding: 12px 0;">
                <div style="margin-bottom: 10px;">
                    <label style="display: block; font-size: 12px; color: #888; margin-bottom: 4px;">Name</label>
                    <input type="text" id="new-peer-name" placeholder="e.g. plex-server"
                           style="width: 100%; padding: 8px; border: 1px solid #444; border-radius: 4px; background: #2a2a2a; color: #fff;">
                </div>
                <div style="margin-bottom: 10px;">
                    <label style="display: block; font-size: 12px; color: #888; margin-bottom: 4px;">Host</label>
                    <input type="text" id="new-peer-host" placeholder="e.g. 192.168.1.50"
                           style="width: 100%; padding: 8px; border: 1px solid #444; border-radius: 4px; background: #2a2a2a; color: #fff;">
                </div>
                <div style="margin-bottom: 12px;">
                    <label style="display: block; font-size: 12px; color: #888; margin-bottom: 4px;">Port</label>
                    <input type="text" id="new-peer-port" placeholder="5000" value="5000"
                           style="width: 100%; padding: 8px; border: 1px solid #444; border-radius: 4px; background: #2a2a2a; color: #fff;">
                </div>
                <button onclick="addPeer()" class="action-btn" style="width: 100%;">Add Worker</button>
            </div>
        </div>
    </div>

    <!-- Distributed Jobs -->
    <div class="card card-full">
        <h2>Distributed Jobs</h2>
        {% if distributed_jobs %}
        <table>
            <tr>
                <th>Title</th>
                <th>Destination</th>
                <th>State</th>
                <th>Timestamp</th>
            </tr>
            {% for job in distributed_jobs %}
            <tr>
                <td><strong>{{ job.title }}</strong></td>
                <td>
                    <span class="badge badge-remote">{{ job.dest_node }}</span>
                </td>
                <td>{{ job.state }}</td>
                <td class="meta">{{ job.timestamp }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <div class="empty-state">
            No jobs currently distributed to peers
        </div>
        {% endif %}
    </div>

    <!-- Received Jobs (from other nodes) -->
    <div class="card card-full">
        <h2>Jobs Received from Peers</h2>
        {% if received_jobs %}
        <table>
            <tr>
                <th>Title</th>
                <th>Origin</th>
                <th>State</th>
                <th>Received</th>
            </tr>
            {% for job in received_jobs %}
            <tr>
                <td><strong>{{ job.title }}</strong></td>
                <td>
                    <span class="badge badge-local">{{ job.origin_node }}</span>
                </td>
                <td>{{ job.state }}</td>
                <td class="meta">{{ job.received_at or job.timestamp }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <div class="empty-state">
            No jobs received from peers
        </div>
        {% endif %}
    </div>

    <!-- Storage & I/O -->
    <div class="card card-full">
        <h2>Storage & I/O</h2>
        {% if io.available %}
        <div class="io-summary">
            <div class="io-stat">
                <span class="io-value">{{ "%.1f"|format(io.total_read_mb_s) }}</span>
                <span class="io-label">MB/s Read</span>
            </div>
            <div class="io-stat">
                <span class="io-value">{{ "%.1f"|format(io.total_write_mb_s) }}</span>
                <span class="io-label">MB/s Write</span>
            </div>
            <div class="io-stat">
                <span class="io-value {% if io.iowait_percent > 20 %}io-warn{% endif %}">{{ "%.1f"|format(io.iowait_percent) }}%</span>
                <span class="io-label">I/O Wait</span>
            </div>
        </div>
        {% if io.devices %}
        <table>
            <tr>
                <th>Device</th>
                <th>Type</th>
                <th>Size</th>
                <th>Mount</th>
                <th>Read</th>
                <th>Write</th>
            </tr>
            {% for dev in io.devices %}
            <tr>
                <td>{{ dev.name }}{% if dev.model %}<span class="device-model">{{ dev.model }}</span>{% endif %}</td>
                <td><span class="device-badge device-{{ dev.type }}">{{ dev.type }}</span></td>
                <td>{{ dev.size }}</td>
                <td>{{ dev.mountpoint or '-' }}</td>
                <td>{{ "%.1f"|format(dev.read_mb_s) }} MB/s</td>
                <td>{{ "%.1f"|format(dev.write_mb_s) }} MB/s</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <p class="io-unavailable">No block devices detected</p>
        {% endif %}
        {% else %}
        <p class="io-unavailable">
            {% if io.error %}
            {{ io.error }}
            {% else %}
            I/O statistics not available
            {% endif %}
        </p>
        {% endif %}
    </div>

    {% endif %}

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a> |
        <a href="/">Back to Dashboard</a> |
        <a href="/status">Services</a> |
        <a href="/health">Health</a>
    </div>

    {% if cluster_enabled %}
    <script>
        // Auto-refresh every 30 seconds
        setTimeout(function() {
            location.reload();
        }, 30000);

        // Current peers from server (for add/remove operations)
        const currentPeers = {{ peers_raw | tojson | safe }};

        async function addPeer() {
            const name = document.getElementById('new-peer-name').value.trim();
            const host = document.getElementById('new-peer-host').value.trim();
            const port = document.getElementById('new-peer-port').value.trim() || '5000';

            if (!name || !host) {
                alert('Name and Host are required');
                return;
            }

            // Validate port is numeric
            if (!/^\d+$/.test(port)) {
                alert('Port must be a number');
                return;
            }

            // Build new peer string
            const newPeer = `${name}:${host}:${port}`;
            const updatedPeers = currentPeers ? `${currentPeers} ${newPeer}` : newPeer;

            try {
                const response = await fetch('/api/config/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: { 'CLUSTER_PEERS': updatedPeers } })
                });

                const result = await response.json();
                if (result.success) {
                    window.location.reload();
                } else {
                    alert('Error: ' + result.message);
                }
            } catch (err) {
                alert('Error: ' + err.message);
            }
        }

        async function removePeer(peerName) {
            if (!confirm(`Remove worker "${peerName}" from cluster?`)) {
                return;
            }

            // Parse current peers and filter out the one to remove
            const peers = currentPeers.split(/\s+/).filter(p => p.trim());
            const updatedPeers = peers.filter(p => !p.startsWith(peerName + ':')).join(' ');

            try {
                const response = await fetch('/api/config/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: { 'CLUSTER_PEERS': updatedPeers } })
                });

                const result = await response.json();
                if (result.success) {
                    window.location.reload();
                } else {
                    alert('Error: ' + result.message);
                }
            } catch (err) {
                alert('Error: ' + err.message);
            }
        }
    </script>
    {% endif %}
</body>
</html>
"""

HEALTH_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>System Health - DVD Ripper</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 20px; background: #0f172a; color: #e2e8f0;
            min-height: 100vh;
        }
        h1 { margin: 0 0 8px 0; color: #f1f5f9; }
        h1 a { color: #60a5fa; text-decoration: none; }
        h1 a:hover { text-decoration: underline; }
        h2 { margin: 0 0 16px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }
        .subtitle { color: #94a3b8; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .card {
            background: #1e293b;
            border-radius: 8px;
            padding: 16px;
            border: 1px solid #334155;
        }
        .metric-value { font-size: 32px; font-weight: 700; color: #f1f5f9; }
        .metric-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; margin-top: 4px; }
        .metric-bar { background: #334155; height: 8px; border-radius: 4px; margin-top: 12px; overflow: hidden; }
        .metric-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
        .fill-ok { background: linear-gradient(90deg, #10b981, #34d399); }
        .fill-warn { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
        .fill-danger { background: linear-gradient(90deg, #ef4444, #f87171); }
        .load-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; text-align: center; }
        .load-value { font-size: 24px; font-weight: 600; color: #f1f5f9; }
        .load-label { font-size: 11px; color: #64748b; }
        .temp-item { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #334155; }
        .temp-item:last-child { border-bottom: none; }
        .temp-label { color: #94a3b8; font-size: 13px; }
        .temp-value { font-weight: 600; }
        .temp-ok { color: #10b981; }
        .temp-warn { color: #f59e0b; }
        .temp-critical { color: #ef4444; }
        .fan-rpm { color: #60a5fa; font-size: 13px; }

        /* Process table styles */
        .process-card { grid-column: 1 / -1; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; padding: 10px 8px; color: #64748b; font-weight: 600; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #334155; }
        td { padding: 10px 8px; border-bottom: 1px solid #1e293b; }
        tr:hover { background: #334155; }
        .pid { font-family: monospace; color: #94a3b8; }
        .cpu-high { color: #ef4444; font-weight: 600; }
        .cpu-med { color: #f59e0b; }
        .cpu-low { color: #10b981; }
        .command { font-family: monospace; font-size: 11px; color: #94a3b8; max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .type-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .type-encoder { background: #3b82f6; color: white; }
        .type-iso { background: #f59e0b; color: white; }
        .type-transfer { background: #8b5cf6; color: white; }
        .type-preview { background: #10b981; color: white; }
        .type-unknown { background: #6b7280; color: white; }

        .btn {
            padding: 4px 10px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 11px;
            font-weight: 500;
            transition: background 0.2s;
        }
        .btn-kill { background: #dc2626; color: white; }
        .btn-kill:hover { background: #b91c1c; }
        .btn-kill:disabled { background: #6b7280; cursor: not-allowed; }

        .no-processes { color: #64748b; text-align: center; padding: 40px; }
        .flash { padding: 12px 16px; border-radius: 4px; margin-bottom: 16px; }
        .flash-success { background: #065f46; color: #d1fae5; border: 1px solid #10b981; }
        .flash-error { background: #991b1b; color: #fee2e2; border: 1px solid #ef4444; }
        .footer {
            margin-top: 20px;
            font-size: 12px;
            color: #64748b;
            text-align: center;
        }
        .footer a { color: #60a5fa; text-decoration: none; }
        .auto-refresh { font-size: 11px; color: #64748b; margin-top: 8px; }
        .sensors-unavailable { color: #64748b; font-style: italic; font-size: 13px; }

        /* Kill confirmation modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 100;
            justify-content: center;
            align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: #1e293b;
            border-radius: 8px;
            padding: 24px;
            max-width: 400px;
            border: 1px solid #334155;
        }
        .modal h3 { margin: 0 0 12px 0; color: #f1f5f9; }
        .modal p { color: #94a3b8; margin: 0 0 20px 0; font-size: 14px; }
        .modal-buttons { display: flex; gap: 12px; justify-content: flex-end; }
        .btn-cancel { background: #475569; color: white; padding: 8px 16px; }
        .btn-cancel:hover { background: #64748b; }
        .btn-confirm-kill { background: #dc2626; color: white; padding: 8px 16px; }
        .btn-confirm-kill:hover { background: #b91c1c; }

        /* I/O Panel styles */
        .io-card { grid-column: 1 / -1; }
        .io-summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 16px;
            margin-bottom: 16px;
            text-align: center;
        }
        .io-stat {
            padding: 12px;
            background: #334155;
            border-radius: 6px;
        }
        .io-value {
            font-size: 24px;
            font-weight: 600;
            color: #f1f5f9;
            display: block;
        }
        .io-label {
            font-size: 11px;
            color: #94a3b8;
            text-transform: uppercase;
            margin-top: 4px;
        }
        .io-warn { color: #f59e0b; }
        .device-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .device-hdd { background: #6b7280; color: white; }
        .device-ssd { background: #3b82f6; color: white; }
        .device-nvme { background: #10b981; color: white; }
        .device-usb { background: #f59e0b; color: white; }
        .device-unknown { background: #475569; color: white; }
        .device-model { color: #64748b; font-size: 11px; display: block; }
        .io-unavailable { color: #64748b; font-style: italic; font-size: 13px; text-align: center; padding: 20px; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / System Health</h1>
    <p class="subtitle">Real-time system metrics and process monitoring</p>

    {% if message %}
    <div class="flash flash-{{ message_type }}">{{ message }}</div>
    {% endif %}

    <div class="grid">
        <div class="card">
            <h2>CPU Usage</h2>
            <div class="metric-value" id="cpu-value">{{ cpu.cpu.usage if cpu.cpu else 0 }}%</div>
            <div class="metric-label">Total CPU</div>
            <div class="metric-bar">
                <div class="metric-fill {% if cpu.cpu.usage > 80 %}fill-danger{% elif cpu.cpu.usage > 50 %}fill-warn{% else %}fill-ok{% endif %}"
                     id="cpu-bar" style="width: {{ cpu.cpu.usage if cpu.cpu else 0 }}%"></div>
            </div>
        </div>

        <div class="card">
            <h2>Memory</h2>
            <div class="metric-value" id="mem-value">{{ memory.percent }}%</div>
            <div class="metric-label">{{ memory.used_human }} / {{ memory.total_human }}</div>
            <div class="metric-bar">
                <div class="metric-fill {% if memory.percent > 80 %}fill-danger{% elif memory.percent > 60 %}fill-warn{% else %}fill-ok{% endif %}"
                     id="mem-bar" style="width: {{ memory.percent }}%"></div>
            </div>
        </div>

        <div class="card">
            <h2>Load Average</h2>
            <div class="load-grid">
                <div>
                    <div class="load-value {% if load.load_1m > load.cpu_count %}temp-critical{% elif load.load_1m > load.cpu_count * 0.8 %}temp-warn{% else %}temp-ok{% endif %}" id="load-1m">{{ "%.2f"|format(load.load_1m) }}</div>
                    <div class="load-label">1 min</div>
                </div>
                <div>
                    <div class="load-value" id="load-5m">{{ "%.2f"|format(load.load_5m) }}</div>
                    <div class="load-label">5 min</div>
                </div>
                <div>
                    <div class="load-value" id="load-15m">{{ "%.2f"|format(load.load_15m) }}</div>
                    <div class="load-label">15 min</div>
                </div>
            </div>
            <div class="metric-label" style="margin-top: 12px; text-align: center;">
                {{ load.cpu_count }} cores | {{ "%.2f"|format(load.load_per_core) }} per core
            </div>
        </div>

        <div class="card">
            <h2>Temperature & Fans</h2>
            {% if temps.available %}
                {% for temp in temps.temperatures %}
                <div class="temp-item">
                    <span class="temp-label">{{ temp.sensor }}</span>
                    <span class="temp-value temp-{{ temp.status }}">{{ temp.temp_c }}°C</span>
                </div>
                {% endfor %}
                {% for fan in temps.fans %}
                <div class="temp-item">
                    <span class="temp-label">{{ fan.sensor }}</span>
                    <span class="fan-rpm">{{ fan.rpm }} RPM</span>
                </div>
                {% endfor %}
                {% if not temps.temperatures and not temps.fans %}
                <p class="sensors-unavailable">No temperature/fan sensors detected</p>
                {% endif %}
            {% else %}
                <p class="sensors-unavailable">
                    {% if temps.error %}
                    {{ temps.error }}
                    {% else %}
                    Sensors not available
                    {% endif %}
                </p>
            {% endif %}
        </div>
    </div>

    <div class="grid">
        <div class="card io-card">
            <h2>Storage & I/O</h2>
            {% if io.available %}
            <div class="io-summary">
                <div class="io-stat">
                    <span class="io-value">{{ "%.1f"|format(io.total_read_mb_s) }}</span>
                    <span class="io-label">MB/s Read</span>
                </div>
                <div class="io-stat">
                    <span class="io-value">{{ "%.1f"|format(io.total_write_mb_s) }}</span>
                    <span class="io-label">MB/s Write</span>
                </div>
                <div class="io-stat">
                    <span class="io-value {% if io.iowait_percent > 20 %}io-warn{% endif %}">{{ "%.1f"|format(io.iowait_percent) }}%</span>
                    <span class="io-label">I/O Wait</span>
                </div>
            </div>
            {% if io.devices %}
            <table>
                <tr>
                    <th>Device</th>
                    <th>Type</th>
                    <th>Size</th>
                    <th>Mount</th>
                    <th>Read</th>
                    <th>Write</th>
                </tr>
                {% for dev in io.devices %}
                <tr>
                    <td>{{ dev.name }}{% if dev.model %}<span class="device-model">{{ dev.model }}</span>{% endif %}</td>
                    <td><span class="device-badge device-{{ dev.type }}">{{ dev.type }}</span></td>
                    <td>{{ dev.size }}</td>
                    <td>{{ dev.mountpoint or '-' }}</td>
                    <td>{{ "%.1f"|format(dev.read_mb_s) }} MB/s</td>
                    <td>{{ "%.1f"|format(dev.write_mb_s) }} MB/s</td>
                </tr>
                {% endfor %}
            </table>
            {% else %}
            <p class="io-unavailable">No block devices detected</p>
            {% endif %}
            {% else %}
            <p class="io-unavailable">
                {% if io.error %}
                {{ io.error }}
                {% else %}
                I/O statistics not available
                {% endif %}
            </p>
            {% endif %}
        </div>
    </div>

    <div class="grid">
        <div class="card process-card">
            <h2>DVD Ripper Processes</h2>
            {% if processes %}
            <table>
                <tr>
                    <th>PID</th>
                    <th>Type</th>
                    <th>CPU%</th>
                    <th>MEM%</th>
                    <th>Time</th>
                    <th>Command</th>
                    <th>Action</th>
                </tr>
                {% for proc in processes %}
                <tr>
                    <td class="pid">{{ proc.pid }}</td>
                    <td><span class="type-badge type-{{ proc.type }}">{{ proc.type }}</span></td>
                    <td class="{% if proc.cpu_percent > 80 %}cpu-high{% elif proc.cpu_percent > 30 %}cpu-med{% else %}cpu-low{% endif %}">
                        {{ "%.1f"|format(proc.cpu_percent) }}%
                    </td>
                    <td>{{ "%.1f"|format(proc.mem_percent) }}%</td>
                    <td>{{ proc.elapsed }}</td>
                    <td class="command" title="{{ proc.command_full }}">{{ proc.command }}</td>
                    <td>
                        <button class="btn btn-kill" onclick="confirmKill({{ proc.pid }}, '{{ proc.type }}')">Kill</button>
                    </td>
                </tr>
                {% endfor %}
            </table>
            {% else %}
            <p class="no-processes">No DVD ripper processes currently running</p>
            {% endif %}
            <p class="auto-refresh">Auto-refreshes every 5 seconds</p>
        </div>
    </div>

    <!-- Kill Confirmation Modal -->
    <div class="modal-overlay" id="kill-modal">
        <div class="modal">
            <h3>Confirm Kill Process</h3>
            <p>Are you sure you want to kill PID <strong id="kill-pid"></strong>?<br><br>
            This will terminate the <strong id="kill-type"></strong> process and revert any in-progress state files.</p>
            <div class="modal-buttons">
                <button class="btn btn-cancel" onclick="closeModal()">Cancel</button>
                <form method="POST" id="kill-form" style="display:inline">
                    <button class="btn btn-confirm-kill" type="submit">Kill Process</button>
                </form>
            </div>
        </div>
    </div>

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a> |
        <a href="/">Dashboard</a> |
        <a href="/status">Services</a>
    </div>

    <script>
    function confirmKill(pid, type) {
        document.getElementById('kill-pid').textContent = pid;
        document.getElementById('kill-type').textContent = type;
        document.getElementById('kill-form').action = '/api/kill/' + pid;
        document.getElementById('kill-modal').classList.add('active');
    }

    function closeModal() {
        document.getElementById('kill-modal').classList.remove('active');
    }

    // Close modal on overlay click
    document.getElementById('kill-modal').addEventListener('click', function(e) {
        if (e.target === this) closeModal();
    });

    // Auto-refresh health data every 5 seconds
    function updateHealth() {
        fetch('/api/health')
            .then(response => response.json())
            .then(data => {
                // Update CPU
                if (data.cpu && data.cpu.cpu) {
                    document.getElementById('cpu-value').textContent = data.cpu.cpu.usage + '%';
                    const cpuBar = document.getElementById('cpu-bar');
                    cpuBar.style.width = data.cpu.cpu.usage + '%';
                    cpuBar.className = 'metric-fill ' + (data.cpu.cpu.usage > 80 ? 'fill-danger' : data.cpu.cpu.usage > 50 ? 'fill-warn' : 'fill-ok');
                }

                // Update Memory
                if (data.memory) {
                    document.getElementById('mem-value').textContent = data.memory.percent + '%';
                    const memBar = document.getElementById('mem-bar');
                    memBar.style.width = data.memory.percent + '%';
                    memBar.className = 'metric-fill ' + (data.memory.percent > 80 ? 'fill-danger' : data.memory.percent > 60 ? 'fill-warn' : 'fill-ok');
                }

                // Update Load
                if (data.load) {
                    document.getElementById('load-1m').textContent = data.load.load_1m.toFixed(2);
                    document.getElementById('load-5m').textContent = data.load.load_5m.toFixed(2);
                    document.getElementById('load-15m').textContent = data.load.load_15m.toFixed(2);
                }
            })
            .catch(err => console.log('Health update failed:', err));
    }

    // Update every 5 seconds
    setInterval(updateHealth, 5000);

    // Refresh full page every 30 seconds for process list
    setTimeout(() => location.reload(), 30000);
    </script>
</body>
</html>
"""


# ============================================================================
# Routes
# ============================================================================

@app.route("/")
def dashboard():
    """Main dashboard page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template_string(
        DASHBOARD_HTML,
        counts=count_by_state(),
        queue=get_queue_items(),
        disk=get_disk_usage(),
        locks=get_lock_status(),
        progress=get_active_progress(),
        logs=get_recent_logs(30),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        message=message,
        message_type=message_type,
        pending_identification=len(get_pending_identification()),
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


@app.route("/logs")
def logs_page():
    """Full logs page."""
    lines = request.args.get("lines", 200, type=int)
    return render_template_string(
        LOGS_HTML,
        logs=get_recent_logs(lines),
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


@app.route("/config")
def config_page():
    """Configuration edit page with collapsible sections."""
    return render_template_string(
        CONFIG_HTML,
        config=read_config_full(),
        sections=CONFIG_SECTIONS,
        boolean_settings=BOOLEAN_SETTINGS,
        dropdown_settings=DROPDOWN_SETTINGS,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


@app.route("/architecture")
def architecture_page():
    """Architecture documentation page."""
    return render_template_string(
        ARCHITECTURE_HTML,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


@app.route("/identify")
def identify_page():
    """Pending identification page for generic-named movies."""
    return render_template_string(
        IDENTIFY_HTML,
        pending=get_pending_identification(),
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


@app.route("/status")
def status_page():
    """Service and timer status page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template_string(
        STATUS_HTML,
        services=get_all_service_status(),
        timers=get_all_timer_status(),
        udev_trigger=get_udev_trigger_status(),
        message=message,
        message_type=message_type,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )


@app.route("/health")
def health_page():
    """System health monitoring page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template_string(
        HEALTH_HTML,
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
        github_url=GITHUB_URL
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

    return render_template_string(
        CLUSTER_HTML,
        cluster_enabled=config["cluster_enabled"],
        this_node=this_node,
        peers=peers,
        peers_raw=config["peers_raw"],
        distributed_jobs=get_distributed_jobs(),
        received_jobs=get_received_jobs(),
        io=get_io_stats(),
        hostname=hostname,
        pipeline_version=get_pipeline_version(),
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
    """API: Get queue items."""
    return jsonify(get_queue_items())


@app.route("/api/logs")
def api_logs():
    """API: Get recent logs."""
    lines = request.args.get("lines", 100, type=int)
    return jsonify({"logs": get_recent_logs(lines)})


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


# ============================================================================
# Identification API Routes
# ============================================================================

@app.route("/api/identify/pending")
def api_identify_pending():
    """API: Get items pending identification."""
    return jsonify(get_pending_identification())


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

    # Use the existing shell scripts for pause/resume
    script = f"/usr/local/bin/dvd-ripper-trigger-{action}.sh"

    try:
        result = subprocess.run(
            [script],
            capture_output=True, text=True, timeout=10
        )
        success = result.returncode == 0
        message = result.stdout.strip() or result.stderr.strip()
    except FileNotFoundError:
        success = False
        message = f"Script not found: {script}"
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

    # Check if we have capacity
    capacity = get_worker_capacity()
    if not capacity["available"]:
        return jsonify({
            "error": "No capacity available",
            "load": capacity["load_1m"],
            "slots_free": capacity["slots_free"]
        }), 503

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

        return jsonify({
            "status": "accepted",
            "state_file": os.path.basename(state_file),
            "node_name": config["node_name"],
            "queue_position": capacity["queue_depth"] + 1
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
