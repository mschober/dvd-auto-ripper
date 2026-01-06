#!/usr/bin/env python3
"""
DVD Ripper Web Dashboard
A minimal Flask web UI for monitoring the DVD auto-ripper pipeline.

https://github.com/mschober/dvd-auto-ripper
"""

import os
import json
import glob
import subprocess
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request, redirect, url_for

app = Flask(__name__)

# Configuration - can be overridden via environment variables
STAGING_DIR = os.environ.get("STAGING_DIR", "/var/tmp/dvd-rips")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/dvd-ripper.log")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/dvd-ripper.conf")
PIPELINE_VERSION_FILE = os.environ.get("PIPELINE_VERSION_FILE", "/usr/local/bin/VERSION")
DASHBOARD_VERSION = "1.0.0"
GITHUB_URL = "https://github.com/mschober/dvd-auto-ripper"

LOCK_FILES = {
    "iso": "/var/run/dvd-ripper-iso.lock",
    "encoder": "/var/run/dvd-ripper-encoder.lock",
    "transfer": "/var/run/dvd-ripper-transfer.lock"
}
STATE_ORDER = ["iso-creating", "iso-ready", "encoding", "encoded-ready", "transferring"]


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
            <a href="/architecture">Architecture</a>
        </p>
        <p style="margin-top: 4px;">
            Auto-refreshes every 30 seconds. Last update: {{ now }}
        </p>
    </div>
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
        logs=get_recent_logs(30),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        message=message,
        message_type=message_type,
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
