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
import subprocess
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request, redirect, url_for, send_file

app = Flask(__name__)

# Configuration - can be overridden via environment variables
STAGING_DIR = os.environ.get("STAGING_DIR", "/var/tmp/dvd-rips")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/dvd-ripper.log")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/dvd-ripper.conf")
PIPELINE_VERSION_FILE = os.environ.get("PIPELINE_VERSION_FILE", "/usr/local/bin/VERSION")
DASHBOARD_VERSION = "1.3.0"
GITHUB_URL = "https://github.com/mschober/dvd-auto-ripper"

LOCK_FILES = {
    "iso": "/var/run/dvd-ripper-iso.lock",
    "encoder": "/var/run/dvd-ripper-encoder.lock",
    "transfer": "/var/run/dvd-ripper-transfer.lock"
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
    """Read last N lines from log file."""
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), LOG_FILE],
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
                # Check if process is actually running
                os.kill(int(pid), 0)
                status[stage] = {"active": True, "pid": pid}
            except (ValueError, ProcessLookupError, PermissionError, IOError):
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
│  Lock Files:  /var/run/dvd-ripper-{iso,encoder,transfer}.lock              │
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
            <tr><td><code>/var/run/dvd-ripper-*.lock</code></td><td>Stage lock files (prevent concurrent runs)</td></tr>
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
    <title>DVD Ripper Config</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, sans-serif; margin: 0; padding: 20px;
            background: #f0f2f5;
        }
        h1 { margin: 0 0 16px 0; }
        a { color: #3b82f6; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .card { background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #eee; }
        th { color: #666; font-weight: 600; }
        td:first-child { font-family: monospace; font-size: 13px; }
        .footer { margin-top: 16px; font-size: 12px; color: #666; text-align: center; }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Configuration</h1>
    <div class="card">
        <table>
            <tr><th>Setting</th><th>Value</th></tr>
            {% for key, value in config.items() %}
            <tr><td>{{ key }}</td><td>{{ value or '(empty)' }}</td></tr>
            {% endfor %}
        </table>
    </div>
    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a>
    </div>
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
    """Configuration view page."""
    return render_template_string(
        CONFIG_HTML,
        config=read_config(),
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


@app.route("/api/locks")
def api_locks():
    """API: Get lock status."""
    return jsonify(get_lock_status())


@app.route("/api/progress")
def api_progress():
    """API: Get real-time progress for active processes."""
    return jsonify(get_active_progress())


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
