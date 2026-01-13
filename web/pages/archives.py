"""Archives page - ISO archive management with cluster transfer support."""
import os
import glob
import json
import shutil
import socket
import subprocess
import time
from datetime import datetime
from flask import Blueprint, jsonify, render_template_string, request

from helpers.pipeline import STAGING_DIR, STATE_ORDER
from helpers.cluster import call_peer_api

# Blueprint setup
archives_bp = Blueprint('archives', __name__)

# ============================================================================
# Helper Functions
# ============================================================================

def get_iso_archives():
    """Scan staging directory for ISO archives with associated metadata.

    Returns list of archive dicts sorted by modification time (newest first).
    Each archive contains:
        - prefix: title-timestamp identifier
        - iso_path: path to .iso or .iso.deletable file
        - iso_size: file size in bytes
        - deletable: bool - whether ISO is marked .deletable
        - mapfile: path to .iso.mapfile if exists
        - keys_dir: path to .iso.keys/ directory if exists
        - keys_count: number of key files if keys_dir exists
        - state_file: path to associated state file if any
        - state: pipeline state if state file exists
        - metadata: JSON contents of state file
        - mtime: modification timestamp
    """
    archives = {}

    # Find all ISO files (including .deletable)
    for iso_file in glob.glob(os.path.join(STAGING_DIR, "*.iso*")):
        basename = os.path.basename(iso_file)

        # Skip metadata files
        if basename.endswith('.mapfile') or '.keys' in basename:
            continue

        # Handle .deletable suffix
        if basename.endswith('.iso.deletable'):
            iso_base = basename[:-10]  # Remove .deletable
            deletable = True
        elif basename.endswith('.iso'):
            iso_base = basename
            deletable = False
        else:
            continue  # Skip other extensions

        # Extract title-timestamp prefix (e.g., "The_Matrix-1703615234")
        prefix = iso_base.rsplit('.iso', 1)[0]

        try:
            iso_size = os.path.getsize(iso_file)
            mtime = os.path.getmtime(iso_file)
        except OSError:
            continue

        archives[prefix] = {
            "prefix": prefix,
            "iso_path": iso_file,
            "iso_size": iso_size,
            "deletable": deletable,
            "archive_ready": False,  # New: .archive-ready marker exists
            "mapfile": None,
            "keys_dir": None,
            "keys_count": 0,
            "state_file": None,
            "state": None,
            "metadata": {},
            "mtime": mtime,
            # Archive-related fields
            "archiving": False,
            "archived": False,
            "compressed_size": 0,
            "archive_path": "",
            "archived_at": ""
        }

    # Associate metadata files with each archive
    for prefix, archive in archives.items():
        # Check for mapfile
        mapfile = os.path.join(STAGING_DIR, f"{prefix}.iso.mapfile")
        if os.path.exists(mapfile):
            archive["mapfile"] = mapfile

        # Check for keys directory
        keys_dir = os.path.join(STAGING_DIR, f"{prefix}.iso.keys")
        if os.path.isdir(keys_dir):
            archive["keys_dir"] = keys_dir
            try:
                archive["keys_count"] = len([f for f in os.listdir(keys_dir)
                                            if not f.startswith('.')])
            except OSError:
                archive["keys_count"] = 0

        # Find associated state file
        for state in STATE_ORDER:
            state_file = os.path.join(STAGING_DIR, f"{prefix}.{state}")
            if os.path.exists(state_file):
                archive["state_file"] = state_file
                archive["state"] = state
                try:
                    with open(state_file, 'r') as f:
                        archive["metadata"] = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass
                break

        # Also check distributed-to-* states
        for dist_file in glob.glob(os.path.join(STAGING_DIR, f"{prefix}.distributed-to-*")):
            archive["state_file"] = dist_file
            archive["state"] = os.path.basename(dist_file).split('.')[-1]
            try:
                with open(dist_file, 'r') as f:
                    archive["metadata"] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
            break

        # Check for archive-transferring-to-* states (dashboard transfers in progress)
        for transfer_file in glob.glob(os.path.join(STAGING_DIR, f"{prefix}.archive-transferring-to-*")):
            archive["state_file"] = transfer_file
            # Extract peer name from filename (prefix.archive-transferring-to-peername)
            peer_name = os.path.basename(transfer_file).split('.archive-transferring-to-')[-1]
            archive["state"] = "archive-transferring"
            archive["transfer_peer"] = peer_name
            try:
                with open(transfer_file, 'r') as f:
                    archive["metadata"] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
            break

        # Check for .archive-ready marker (new approach - ISO ready for archival)
        archive_ready_file = os.path.join(STAGING_DIR, f"{prefix}.iso.archive-ready")
        if os.path.exists(archive_ready_file):
            archive["archive_ready"] = True
            try:
                with open(archive_ready_file, 'r') as f:
                    archive["archive_ready_metadata"] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        # Check for archiving state (compression in progress)
        archiving_file = os.path.join(STAGING_DIR, f"{prefix}.archiving")
        if os.path.exists(archiving_file):
            archive["archiving"] = True
            try:
                with open(archiving_file, 'r') as f:
                    arch_meta = json.load(f)
                archive["archiving_started"] = arch_meta.get("started_at", "")
            except (json.JSONDecodeError, IOError):
                pass

        # Check for archived state (compression complete)
        archived_file = os.path.join(STAGING_DIR, f"{prefix}.archived")
        if os.path.exists(archived_file):
            archive["archived"] = True
            try:
                with open(archived_file, 'r') as f:
                    arch_meta = json.load(f)
                archive["compressed_size"] = arch_meta.get("compressed_size_bytes", 0)
                archive["archive_path"] = arch_meta.get("archive_path", "")
                archive["archived_at"] = arch_meta.get("archived_at", "")
            except (json.JSONDecodeError, IOError):
                pass

    return sorted(archives.values(), key=lambda x: x["mtime"], reverse=True)


def get_receiving_transfers():
    """Detect incoming rsync transfers by looking for temp files.

    rsync creates temp files like .FILENAME.XXXXXX during transfer.
    Returns list of receiving transfer dicts.
    """
    receiving = []

    # Look for rsync temp files (hidden files with .iso. in the name)
    for temp_file in glob.glob(os.path.join(STAGING_DIR, ".*")):
        basename = os.path.basename(temp_file)

        # rsync temp files look like: .TITLE-TIMESTAMP.iso.XXXXXX
        if not basename.startswith('.') or '.iso.' not in basename:
            continue

        # Skip non-temp patterns
        if basename.endswith('.mapfile') or basename.endswith('.keys'):
            continue

        # Extract the original filename (remove leading . and trailing random suffix)
        # .FLAWLESS_US-1767986637.iso.EPkPJt -> FLAWLESS_US-1767986637.iso
        parts = basename[1:].rsplit('.', 1)  # Remove leading dot, split on last dot
        if len(parts) != 2:
            continue

        original_name = parts[0]  # e.g., FLAWLESS_US-1767986637.iso
        if not original_name.endswith('.iso'):
            continue

        prefix = original_name.rsplit('.iso', 1)[0]

        try:
            current_size = os.path.getsize(temp_file)
            mtime = os.path.getmtime(temp_file)
        except OSError:
            continue

        receiving.append({
            "prefix": prefix,
            "title": prefix.rsplit('-', 1)[0].replace('_', ' '),
            "temp_file": temp_file,
            "current_size": current_size,
            "mtime": mtime
        })

    return sorted(receiving, key=lambda x: x["mtime"], reverse=True)


def format_size(size_bytes):
    """Format bytes as human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def get_archived_stats():
    """Get statistics about compressed ISO archives.

    Returns dict with:
        - total_archived: count of archived ISOs
        - total_original_bytes: sum of original ISO sizes
        - total_compressed_bytes: sum of compressed sizes
        - average_ratio: average compression ratio
        - space_saved_bytes: bytes saved through compression
    """
    stats = {
        "total_archived": 0,
        "total_original_bytes": 0,
        "total_compressed_bytes": 0,
        "average_ratio": 0.0,
        "space_saved_bytes": 0
    }

    # Find all .archived state files
    archived_files = glob.glob(os.path.join(STAGING_DIR, "*.archived"))

    for state_file in archived_files:
        try:
            with open(state_file, 'r') as f:
                metadata = json.load(f)

            original_size = metadata.get("original_size_bytes", 0)
            compressed_size = metadata.get("compressed_size_bytes", 0)

            if original_size > 0:
                stats["total_archived"] += 1
                stats["total_original_bytes"] += original_size
                stats["total_compressed_bytes"] += compressed_size
        except (json.JSONDecodeError, IOError):
            continue

    # Calculate averages
    if stats["total_original_bytes"] > 0:
        stats["average_ratio"] = stats["total_compressed_bytes"] / stats["total_original_bytes"]
        stats["space_saved_bytes"] = stats["total_original_bytes"] - stats["total_compressed_bytes"]

    return stats


def get_local_disk_usage():
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


def get_cluster_config_for_archives():
    """Get cluster configuration - imports from main dashboard to avoid circular imports."""
    # Import here to avoid circular dependency
    import sys
    dashboard_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if dashboard_dir not in sys.path:
        sys.path.insert(0, dashboard_dir)

    # Read config directly to avoid import issues
    config_file = os.environ.get("CONFIG_FILE", "/etc/dvd-ripper.conf")
    config = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        config[key.strip()] = value.strip().strip('"').strip("'")
        except IOError:
            pass

    return {
        "cluster_enabled": config.get("CLUSTER_ENABLED", "0") == "1",
        "node_name": config.get("CLUSTER_NODE_NAME", ""),
        "peers_raw": config.get("CLUSTER_PEERS", ""),
        "ssh_user": config.get("CLUSTER_SSH_USER", "dvd-distribute"),
        "remote_staging": config.get("CLUSTER_REMOTE_STAGING", "/var/tmp/dvd-rips"),
    }


def parse_peers(peers_raw):
    """Parse peer string into list of dicts."""
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


def ping_peer_simple(host, port):
    """Simple peer ping - returns True if reachable."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False


# ============================================================================
# HTML Template
# ============================================================================

ARCHIVES_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Archives | {{ hostname }}</title>
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
        .summary { display: flex; gap: 20px; margin-bottom: 20px; }
        .summary-stat {
            background: white;
            padding: 16px 24px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .summary-value { font-size: 28px; font-weight: 700; color: #1a1a1a; }
        .summary-label { font-size: 12px; color: #6b7280; text-transform: uppercase; }

        .layout { display: flex; gap: 20px; }
        .main-content { flex: 1; }
        .sidebar { width: 280px; flex-shrink: 0; }

        .card {
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }

        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #eee; }
        th { color: #666; font-weight: 600; font-size: 12px; text-transform: uppercase; }

        .archive-row { transition: background 0.2s; }
        .archive-row:hover { background: #f9fafb; }
        .archive-row.dragging { opacity: 0.5; background: #dbeafe; }
        .archive-row[draggable="true"] { cursor: grab; }
        .archive-row[draggable="true"]:active { cursor: grabbing; }

        .title-cell { font-weight: 500; }
        .title-cell small { display: block; font-weight: 400; color: #6b7280; font-size: 11px; }

        .size-cell { font-family: 'SF Mono', Monaco, monospace; font-size: 13px; }

        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
        }
        .badge-state { background: #dbeafe; color: #1e40af; }
        .badge-deletable { background: #fef3c7; color: #92400e; }
        .badge-none { background: #f3f4f6; color: #6b7280; }
        .badge-transferring { background: #d1fae5; color: #065f46; }
        .badge-archiving { background: #e0e7ff; color: #4338ca; }
        .badge-archived { background: #d1fae5; color: #047857; }

        .archive-cell { white-space: nowrap; }
        .archive-info { display: flex; flex-direction: column; gap: 2px; }
        .compression-ratio { font-size: 10px; color: #059669; }
        .btn-archive {
            background: #e0e7ff;
            color: #4338ca;
            padding: 4px 10px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 11px;
            font-weight: 500;
            transition: all 0.2s;
        }
        .btn-archive:hover { background: #c7d2fe; }
        .btn-archive:disabled { opacity: 0.5; cursor: not-allowed; }

        .transfer-progress {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .progress-bar-mini {
            height: 4px;
            background: #e5e7eb;
            border-radius: 2px;
            overflow: hidden;
            width: 80px;
        }
        .progress-bar-mini .progress-bar-fill {
            height: 100%;
            background: #10b981;
            transition: width 0.3s ease;
        }
        .progress-bar-mini.pulsing .progress-bar-fill {
            animation: pulse-progress 1.5s ease-in-out infinite;
        }
        @keyframes pulse-progress {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* Receiving transfers section */
        .receiving-section {
            background: #ecfdf5;
            border: 1px solid #a7f3d0;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
        }
        .receiving-header {
            font-size: 14px;
            font-weight: 600;
            color: #065f46;
            margin: 0 0 12px 0;
        }
        .receiving-item {
            background: white;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 8px;
        }
        .receiving-item:last-child {
            margin-bottom: 0;
        }
        .receiving-title {
            font-weight: 600;
            color: #111827;
            margin-bottom: 4px;
        }
        .receiving-info {
            display: flex;
            gap: 12px;
            font-size: 12px;
            color: #6b7280;
            margin-bottom: 8px;
        }
        .receiving-status {
            color: #059669;
            font-weight: 500;
        }
        .transfer-percent {
            font-size: 11px;
            color: #059669;
            font-weight: 600;
            margin-left: 8px;
        }

        .files-cell { font-size: 12px; color: #6b7280; }
        .files-cell span { margin-right: 8px; }
        .file-present { color: #059669; }
        .file-missing { color: #d1d5db; }

        .actions-cell { white-space: nowrap; }
        .btn {
            padding: 6px 12px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
            transition: all 0.2s;
        }
        .btn-delete {
            background: #fee2e2;
            color: #dc2626;
        }
        .btn-delete:hover { background: #fecaca; }

        /* Peer drop zones */
        .peer-panel h2 { margin-bottom: 12px; }
        .drop-zone {
            border: 2px dashed #d1d5db;
            border-radius: 8px;
            padding: 16px;
            text-align: center;
            margin-bottom: 12px;
            transition: all 0.2s;
            cursor: default;
        }
        .drop-zone.online { border-color: #10b981; }
        .drop-zone.offline { border-color: #ef4444; opacity: 0.5; }
        .drop-zone.drag-over {
            border-color: #3b82f6;
            background: #eff6ff;
            border-style: solid;
        }
        .drop-zone .peer-name { font-weight: 600; margin-bottom: 4px; }
        .drop-zone .peer-status { font-size: 12px; color: #6b7280; }
        .status-dot {
            display: inline-block;
            width: 8px; height: 8px;
            border-radius: 50%;
            margin-right: 6px;
        }
        .status-online { background: #10b981; }
        .status-offline { background: #ef4444; }

        /* Disk usage bars */
        .disk-usage { margin-top: 8px; }
        .disk-bar {
            height: 8px;
            background: #e5e7eb;
            border-radius: 4px;
            overflow: hidden;
        }
        .disk-bar-fill {
            height: 100%;
            background: #3b82f6;
            transition: width 0.3s ease;
        }
        .disk-bar-fill.warning { background: #f59e0b; }
        .disk-bar-fill.danger { background: #ef4444; }
        .disk-text {
            font-size: 11px;
            color: #6b7280;
            margin-top: 4px;
        }

        .empty-state {
            text-align: center;
            padding: 40px;
            color: #6b7280;
        }
        .empty-state h3 { color: #1a1a1a; margin-bottom: 8px; }

        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 12px 20px;
            border-radius: 8px;
            font-weight: 500;
            z-index: 1000;
            animation: slideIn 0.3s ease;
        }
        .notification.success { background: #d1fae5; color: #065f46; }
        .notification.error { background: #fee2e2; color: #991b1b; }
        .notification-close {
            margin-left: 12px;
            cursor: pointer;
            opacity: 0.7;
            font-weight: bold;
        }
        .notification-close:hover { opacity: 1; }
        .notification.fade-out {
            animation: fadeOut 0.5s ease forwards;
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        @keyframes fadeOut {
            to { opacity: 0; transform: translateX(100%); }
        }

        /* Confirmation Modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.5);
            z-index: 2000;
            align-items: center;
            justify-content: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 400px;
            width: 90%;
            box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 10px 10px -5px rgba(0,0,0,0.04);
        }
        .modal h3 {
            margin: 0 0 8px 0;
            font-size: 18px;
            color: #dc2626;
        }
        .modal p {
            margin: 0 0 20px 0;
            color: #4b5563;
            font-size: 14px;
        }
        .modal-buttons {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
        }
        .modal-btn {
            padding: 10px 20px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
        }
        .modal-btn-cancel {
            background: #f3f4f6;
            color: #374151;
        }
        .modal-btn-cancel:hover { background: #e5e7eb; }
        .modal-btn-cancel:focus {
            outline: 2px solid #3b82f6;
            outline-offset: 2px;
        }
        .modal-btn-danger {
            background: #dc2626;
            color: white;
        }
        .modal-btn-danger:hover { background: #b91c1c; }

        .footer {
            margin-top: 20px;
            font-size: 12px;
            color: #666;
            text-align: center;
        }
        .footer a { color: #3b82f6; text-decoration: none; }

        @media (max-width: 900px) {
            .layout { flex-direction: column; }
            .sidebar { width: 100%; }
        }
    </style>
</head>
<body>
    <h1><a href="/">Dashboard</a> / Archives</h1>
    <p class="subtitle">ISO archive management{% if cluster_enabled %} with cluster transfer{% endif %}</p>

    <div class="summary">
        <div class="summary-stat">
            <div class="summary-value">{{ total_count }}</div>
            <div class="summary-label">ISO Archives</div>
        </div>
        <div class="summary-stat">
            <div class="summary-value">{{ total_size_gb }} GB</div>
            <div class="summary-label">Total Size</div>
        </div>
        {% if archived_stats.total_archived > 0 %}
        <div class="summary-stat">
            <div class="summary-value">{{ archived_stats.total_archived }}</div>
            <div class="summary-label">Compressed Archives</div>
        </div>
        <div class="summary-stat">
            <div class="summary-value">{{ "%.1f"|format(archived_stats.space_saved_bytes / 1024 / 1024 / 1024) }} GB</div>
            <div class="summary-label">Space Saved ({{ "%.0f"|format((1 - archived_stats.average_ratio) * 100) }}%)</div>
        </div>
        {% endif %}
        <div class="summary-stat">
            <div class="summary-value">{{ disk_usage.available }}</div>
            <div class="summary-label">Available ({{ disk_usage.percent }}% used)</div>
            <div class="disk-bar" style="margin-top: 8px;">
                <div class="disk-bar-fill {{ 'danger' if disk_usage.percent_num > 90 else 'warning' if disk_usage.percent_num > 75 else '' }}"
                     style="width: {{ disk_usage.percent_num }}%;"></div>
            </div>
        </div>
    </div>

    <div class="layout">
        <div class="main-content">
            <div class="card">
                <h2>ISO Archives on {{ node_name or 'this node' }}</h2>

                {% if receiving %}
                <div class="receiving-section">
                    <h3 class="receiving-header">📥 Receiving Transfers</h3>
                    {% for recv in receiving %}
                    <div class="receiving-item">
                        <div class="receiving-title">{{ recv.title }}</div>
                        <div class="receiving-info">
                            <span class="receiving-size">{{ format_size(recv.current_size) }}</span>
                            <span class="receiving-status">receiving...</span>
                        </div>
                        <div class="progress-bar-mini pulsing">
                            <div class="progress-bar-fill" style="width: 100%;"></div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% endif %}

                {% if archives %}
                <table>
                    <thead>
                        <tr>
                            <th>Title</th>
                            <th>Size</th>
                            <th>State</th>
                            <th>Archive</th>
                            <th>Files</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for archive in archives %}
                        <tr class="archive-row"
                            data-prefix="{{ archive.prefix }}"
                            {% if cluster_enabled %}draggable="true"{% endif %}
                            ondragstart="handleDragStart(event)"
                            ondragend="handleDragEnd(event)">
                            <td class="title-cell">
                                {{ archive.prefix.rsplit('-', 1)[0] | replace('_', ' ') }}
                                <small>{{ archive.prefix }}</small>
                            </td>
                            <td class="size-cell">{{ format_size(archive.iso_size) }}</td>
                            <td>
                                {% if archive.state == 'archive-transferring' %}
                                <div class="transfer-progress"
                                     data-prefix="{{ archive.prefix }}"
                                     data-peer="{{ archive.transfer_peer }}"
                                     data-peer-host="{{ archive.metadata.peer_host if archive.metadata else '' }}"
                                     data-peer-port="{{ archive.metadata.peer_port if archive.metadata else '' }}"
                                     data-iso-size="{{ archive.iso_size }}">
                                    <span class="badge badge-transferring">→ {{ archive.transfer_peer }}</span>
                                    <span class="transfer-percent"></span>
                                    <div class="progress-bar-mini">
                                        <div class="progress-bar-fill" style="width: 0%;"></div>
                                    </div>
                                </div>
                                {% elif archive.state %}
                                <span class="badge badge-state">{{ archive.state }}</span>
                                {% elif archive.archive_ready %}
                                <span class="badge badge-deletable">archive-ready</span>
                                {% elif archive.deletable %}
                                <span class="badge badge-deletable">deletable</span>
                                {% else %}
                                <span class="badge badge-none">-</span>
                                {% endif %}
                            </td>
                            <td class="archive-cell">
                                {% if archive.archiving %}
                                <span class="badge badge-archiving">compressing...</span>
                                {% elif archive.archived %}
                                <div class="archive-info">
                                    <span class="badge badge-archived">{{ format_size(archive.compressed_size) }}</span>
                                    <small class="compression-ratio">{{ "%.0f"|format((1 - archive.compressed_size / archive.iso_size) * 100) if archive.iso_size > 0 else 0 }}% saved</small>
                                </div>
                                {% elif (archive.archive_ready or archive.deletable) and archive.state not in ['encoding', 'transferring', 'distributing'] %}
                                <button class="btn btn-archive" onclick="archiveNow('{{ archive.prefix }}')">
                                    Archive Now
                                </button>
                                {% else %}
                                <span class="badge badge-none">-</span>
                                {% endif %}
                            </td>
                            <td class="files-cell">
                                <span class="{{ 'file-present' if archive.mapfile else 'file-missing' }}"
                                      title="Recovery mapfile">MAP</span>
                                <span class="{{ 'file-present' if archive.keys_dir else 'file-missing' }}"
                                      title="CSS decryption keys">KEYS{% if archive.keys_count %}({{ archive.keys_count }}){% endif %}</span>
                            </td>
                            <td class="actions-cell">
                                <button class="btn btn-delete"
                                        onclick="deleteArchive('{{ archive.prefix }}')"
                                        {% if archive.state in ['iso-creating', 'encoding', 'transferring', 'distributing', 'archive-transferring'] %}
                                        disabled title="Cannot delete: {{ archive.state }}"
                                        {% endif %}>
                                    Delete
                                </button>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="empty-state">
                    <h3>No ISO Archives</h3>
                    <p>ISO files will appear here when DVDs are ripped.</p>
                </div>
                {% endif %}
            </div>
        </div>

        {% if cluster_enabled and peers %}
        <div class="sidebar">
            <div class="card peer-panel">
                <h2>Transfer to Peer</h2>
                <p style="font-size: 12px; color: #6b7280; margin-bottom: 16px;">
                    Drag an archive row and drop on a peer to transfer
                </p>
                {% for peer in peers %}
                <div class="drop-zone {{ 'online' if peer.online else 'offline' }}"
                     data-peer="{{ peer.name }}:{{ peer.host }}:{{ peer.port }}"
                     ondragover="handleDragOver(event)"
                     ondrop="handleDrop(event)"
                     ondragleave="handleDragLeave(event)">
                    <div class="peer-name">
                        <span class="status-dot status-{{ 'online' if peer.online else 'offline' }}"></span>
                        {{ peer.name }}
                    </div>
                    <div class="peer-status">
                        {% if peer.online %}
                        {{ peer.host }}:{{ peer.port }}
                        {% else %}
                        Offline
                        {% endif %}
                    </div>
                    {% if peer.disk %}
                    <div class="disk-usage">
                        <div class="disk-bar">
                            <div class="disk-bar-fill {{ 'danger' if peer.disk.percent_num > 90 else 'warning' if peer.disk.percent_num > 75 else '' }}"
                                 style="width: {{ peer.disk.percent_num }}%;"></div>
                        </div>
                        <div class="disk-text">{{ peer.disk.available }} free of {{ peer.disk.total }}</div>
                    </div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}
    </div>

    <!-- Delete Confirmation Modal -->
    <div class="modal-overlay" id="deleteModal">
        <div class="modal">
            <h3>Delete Archive?</h3>
            <p id="deleteModalText">Are you sure you want to delete this archive and all associated files? This cannot be undone.</p>
            <div class="modal-buttons">
                <button class="modal-btn modal-btn-cancel" id="deleteModalNo" autofocus>No, Keep It</button>
                <button class="modal-btn modal-btn-danger" id="deleteModalYes">Yes, Delete</button>
            </div>
        </div>
    </div>

    <div class="footer">
        Pipeline v{{ pipeline_version }} | Dashboard v{{ dashboard_version }} |
        <a href="{{ github_url }}" target="_blank">dvd-auto-ripper</a> |
        <a href="/">Dashboard</a> |
        <a href="/cluster">Cluster</a> |
        <a href="/issues">Issues</a>
    </div>

    <script>
        let draggedPrefix = null;

        function handleDragStart(e) {
            draggedPrefix = e.target.dataset.prefix;
            e.target.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        }

        function handleDragEnd(e) {
            e.target.classList.remove('dragging');
            document.querySelectorAll('.drop-zone').forEach(z => z.classList.remove('drag-over'));
        }

        function handleDragOver(e) {
            e.preventDefault();
            if (e.currentTarget.classList.contains('online')) {
                e.currentTarget.classList.add('drag-over');
            }
        }

        function handleDragLeave(e) {
            e.currentTarget.classList.remove('drag-over');
        }

        async function handleDrop(e) {
            e.preventDefault();
            e.currentTarget.classList.remove('drag-over');

            const peer = e.currentTarget.dataset.peer;
            const prefix = draggedPrefix;

            if (!peer || !prefix) return;
            if (!e.currentTarget.classList.contains('online')) {
                showNotification('Peer is offline', 'error');
                return;
            }

            const peerName = peer.split(':')[0];
            if (!confirm(`Transfer ${prefix} to ${peerName}?`)) return;

            try {
                const response = await fetch('/api/archives/transfer', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prefix, peer })
                });
                const result = await response.json();

                if (result.status === 'started') {
                    showNotification(`Transfer started: ${prefix} -> ${peerName}`, 'success');
                    // Refresh to show progress
                    setTimeout(() => location.reload(), 1000);
                } else if (result.status === 'completed') {
                    showNotification(`Transfer complete: ${prefix} -> ${peerName}`, 'success');
                    setTimeout(() => location.reload(), 2000);
                } else {
                    showNotification(result.error || 'Transfer failed', 'error');
                }
            } catch (err) {
                showNotification('Transfer request failed: ' + err.message, 'error');
            }
        }

        async function archiveNow(prefix) {
            const title = prefix.split('-')[0].replace(/_/g, ' ');
            if (!confirm(`Start archiving "${title}"? This will compress the ISO for long-term storage.`)) {
                return;
            }

            try {
                const response = await fetch('/api/archives/archive-now', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prefix })
                });
                const result = await response.json();

                if (result.status === 'started') {
                    showNotification(`Archiving started: ${title}`, 'success');
                    setTimeout(() => location.reload(), 1500);
                } else {
                    showNotification(result.error || 'Archive failed to start', 'error');
                }
            } catch (err) {
                showNotification('Archive request failed: ' + err.message, 'error');
            }
        }

        let pendingDeletePrefix = null;

        function deleteArchive(prefix) {
            pendingDeletePrefix = prefix;
            const title = prefix.split('-')[0].replace(/_/g, ' ');
            document.getElementById('deleteModalText').textContent =
                `Are you sure you want to delete "${title}" and all associated files? This cannot be undone.`;
            document.getElementById('deleteModal').classList.add('active');
            // Focus the No button (default safe action)
            setTimeout(() => document.getElementById('deleteModalNo').focus(), 50);
        }

        function closeDeleteModal() {
            document.getElementById('deleteModal').classList.remove('active');
            pendingDeletePrefix = null;
        }

        async function confirmDelete() {
            if (!pendingDeletePrefix) return;
            const prefix = pendingDeletePrefix;
            closeDeleteModal();

            try {
                const response = await fetch(`/api/archives/${encodeURIComponent(prefix)}`, {
                    method: 'DELETE'
                });
                const result = await response.json();

                if (result.status === 'deleted' || result.status === 'partial') {
                    showNotification(`Deleted: ${result.deleted.length} files`, 'success');
                    setTimeout(() => location.reload(), 2000);
                } else {
                    showNotification(result.error || 'Delete failed', 'error');
                }
            } catch (err) {
                showNotification('Delete request failed: ' + err.message, 'error');
            }
        }

        // Modal event listeners
        document.getElementById('deleteModalNo').addEventListener('click', closeDeleteModal);
        document.getElementById('deleteModalYes').addEventListener('click', confirmDelete);
        document.getElementById('deleteModal').addEventListener('click', (e) => {
            if (e.target.id === 'deleteModal') closeDeleteModal();
        });
        // Close on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && document.getElementById('deleteModal').classList.contains('active')) {
                closeDeleteModal();
            }
        });

        function showNotification(message, type) {
            const notif = document.createElement('div');
            notif.className = `notification ${type}`;
            notif.innerHTML = message + '<span class="notification-close">✕</span>';
            notif.querySelector('.notification-close').onclick = () => notif.remove();
            document.body.appendChild(notif);
            // Success fades after 1s, errors stay until closed
            if (type === 'success') {
                setTimeout(() => {
                    notif.classList.add('fade-out');
                    setTimeout(() => notif.remove(), 500);
                }, 1000);
            }
        }

        // Poll transfer progress for archives being transferred
        async function pollTransferProgress() {
            const transfers = document.querySelectorAll('.transfer-progress[data-peer-host]');
            if (transfers.length === 0) return;

            for (const el of transfers) {
                const prefix = el.dataset.prefix;
                const peerHost = el.dataset.peerHost;
                const peerPort = el.dataset.peerPort;
                const isoSize = parseInt(el.dataset.isoSize) || 0;

                if (!peerHost || !peerPort || !isoSize) continue;

                try {
                    const response = await fetch(`http://${peerHost}:${peerPort}/api/archives/receiving`);
                    if (!response.ok) continue;

                    const data = await response.json();
                    const recv = data.receiving.find(r => r.prefix === prefix);

                    if (recv) {
                        const percent = Math.round((recv.current_size / isoSize) * 100);
                        const percentEl = el.querySelector('.transfer-percent');
                        const fillEl = el.querySelector('.progress-bar-fill');

                        if (percentEl) percentEl.textContent = `${percent}%`;
                        if (fillEl) fillEl.style.width = `${percent}%`;
                    }
                } catch (err) {
                    // Silently ignore polling errors
                }
            }
        }

        // Poll every 3 seconds if there are active transfers
        if (document.querySelectorAll('.transfer-progress[data-peer-host]').length > 0) {
            pollTransferProgress();
            setInterval(pollTransferProgress, 3000);
        }

        // Auto-refresh page every 30 seconds to detect completed transfers
        if (document.querySelectorAll('.transfer-progress, .receiving-item').length > 0) {
            setTimeout(() => location.reload(), 30000);
        }
    </script>
</body>
</html>
"""


# ============================================================================
# Routes
# ============================================================================

@archives_bp.route("/archives")
def archives_page():
    """Archives management page."""
    config = get_cluster_config_for_archives()
    archives = get_iso_archives()

    # Get local disk usage and hostname
    disk_usage = get_local_disk_usage()
    hostname = socket.gethostname().split('.')[0]

    # Get archived (compressed) stats
    archived_stats = get_archived_stats()

    # Get peer status if clustered
    peers = []
    if config["cluster_enabled"]:
        raw_peers = parse_peers(config["peers_raw"])
        for peer in raw_peers:
            online = ping_peer_simple(peer["host"], peer["port"])
            peer_disk = None
            if online:
                disk_result = call_peer_api(peer["host"], peer["port"], "/api/disk", timeout=5)
                if disk_result["success"]:
                    peer_disk = disk_result["response"]
            peers.append({
                "name": peer["name"],
                "host": peer["host"],
                "port": peer["port"],
                "online": online,
                "disk": peer_disk
            })

    # Calculate totals
    total_size = sum(a["iso_size"] for a in archives)

    # Get receiving transfers (rsync in progress)
    receiving = get_receiving_transfers()

    # Get version info (try to import from main dashboard)
    try:
        from dvd_dashboard import get_pipeline_version, DASHBOARD_VERSION, GITHUB_URL
        pipeline_version = get_pipeline_version()
        dashboard_version = DASHBOARD_VERSION
        github_url = GITHUB_URL
    except ImportError:
        pipeline_version = "?.?.?"
        dashboard_version = "?.?.?"
        github_url = "https://github.com/mschober/dvd-auto-ripper"

    return render_template_string(
        ARCHIVES_HTML,
        archives=archives,
        receiving=receiving,
        total_count=len(archives),
        total_size_gb=round(total_size / (1024**3), 2),
        disk_usage=disk_usage,
        hostname=hostname,
        cluster_enabled=config["cluster_enabled"],
        node_name=config["node_name"],
        peers=peers,
        archived_stats=archived_stats,
        format_size=format_size,
        pipeline_version=pipeline_version,
        dashboard_version=dashboard_version,
        github_url=github_url
    )


@archives_bp.route("/api/archives")
def api_archives():
    """API: Get all ISO archives with metadata."""
    archives = get_iso_archives()

    # Convert to JSON-serializable format
    for archive in archives:
        archive["iso_size_human"] = format_size(archive["iso_size"])

    return jsonify({
        "count": len(archives),
        "archives": archives,
        "staging_dir": STAGING_DIR
    })


@archives_bp.route("/api/archives/receiving")
def api_archives_receiving():
    """API: Get receiving transfers (rsync temp files).

    Used by source node to poll transfer progress.
    """
    receiving = get_receiving_transfers()
    return jsonify({
        "count": len(receiving),
        "receiving": receiving
    })


@archives_bp.route("/api/archives/transfer", methods=["POST"])
def api_archives_transfer():
    """API: Transfer an ISO archive to a peer node (async subprocess).

    Starts background transfer subprocess and returns immediately:
    1. Creates state file with all transfer parameters
    2. Spawns archive_transfer.py subprocess (survives dashboard restart)
    3. Returns status "started" immediately
    4. Subprocess confirms and cleans up on success

    Expected JSON:
    {
        "prefix": "The_Matrix-1703615234",
        "peer": "plex:192.168.1.50:5000"
    }
    """
    data = request.json or {}
    prefix = data.get("prefix")
    peer = data.get("peer")

    if not prefix or not peer:
        return jsonify({"error": "Missing prefix or peer"}), 400

    # Verify archive exists
    archives = {a["prefix"]: a for a in get_iso_archives()}
    if prefix not in archives:
        return jsonify({"error": "Archive not found"}), 404

    archive = archives[prefix]

    # Don't transfer if actively processing or already transferring
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing", "archive-transferring"]:
        return jsonify({"error": f"Cannot transfer: archive is {archive['state']}"}), 400

    # Parse peer
    parts = peer.split(":")
    if len(parts) < 3:
        return jsonify({"error": "Invalid peer format"}), 400
    peer_name, peer_host, peer_port = parts[0], parts[1], int(parts[2])

    # Get cluster config
    config = get_cluster_config_for_archives()
    ssh_user = config["ssh_user"]
    remote_staging = config["remote_staging"]

    # Create state file with all parameters for subprocess
    state_file = os.path.join(STAGING_DIR, f"{prefix}.archive-transferring-to-{peer_name}")
    state_data = {
        "status": "pending",
        "peer": peer_name,
        "peer_host": peer_host,
        "peer_port": peer_port,
        "iso_path": archive["iso_path"],
        "mapfile": archive.get("mapfile"),
        "keys_dir": archive.get("keys_dir"),
        "iso_size": archive["iso_size"],
        "ssh_user": ssh_user,
        "remote_staging": remote_staging,
        "started": time.time()
    }
    try:
        with open(state_file, 'w') as f:
            json.dump(state_data, f)
            f.flush()
            os.fsync(f.fileno())  # Ensure data is on disk before subprocess starts
    except OSError as e:
        return jsonify({"error": f"Failed to create state file: {e}"}), 500

    # Spawn transfer worker as subprocess (survives dashboard restart)
    worker_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "helpers", "archive_transfer.py"
    )
    worker_script = os.path.normpath(worker_script)

    try:
        subprocess.Popen(
            ["python3", worker_script, "--state-file", state_file],
            start_new_session=True,  # Detach from dashboard process
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        # Clean up state file on spawn failure
        try:
            os.remove(state_file)
        except OSError:
            pass
        return jsonify({"error": f"Failed to spawn transfer worker: {e}"}), 500

    return jsonify({
        "status": "started",
        "prefix": prefix,
        "peer": peer_name,
        "message": f"Transfer to {peer_name} started in background"
    })


@archives_bp.route("/api/archives/<prefix>", methods=["DELETE"])
def api_archives_delete(prefix):
    """API: Delete an ISO archive and all associated files."""
    archives = {a["prefix"]: a for a in get_iso_archives()}
    if prefix not in archives:
        return jsonify({"error": "Archive not found"}), 404

    archive = archives[prefix]

    # Safety check: don't delete if actively being processed
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing", "archive-transferring"]:
        return jsonify({"error": f"Cannot delete: archive is {archive['state']}"}), 400

    deleted = []
    errors = []

    # Delete ISO file
    if os.path.exists(archive["iso_path"]):
        try:
            os.remove(archive["iso_path"])
            deleted.append(os.path.basename(archive["iso_path"]))
        except OSError as e:
            errors.append(f"ISO: {e}")

    # Delete mapfile
    if archive["mapfile"] and os.path.exists(archive["mapfile"]):
        try:
            os.remove(archive["mapfile"])
            deleted.append(os.path.basename(archive["mapfile"]))
        except OSError as e:
            errors.append(f"Mapfile: {e}")

    # Delete keys directory
    if archive["keys_dir"] and os.path.exists(archive["keys_dir"]):
        try:
            shutil.rmtree(archive["keys_dir"])
            deleted.append(os.path.basename(archive["keys_dir"]))
        except OSError as e:
            errors.append(f"Keys: {e}")

    # Delete state file
    if archive["state_file"] and os.path.exists(archive["state_file"]):
        try:
            os.remove(archive["state_file"])
            deleted.append(os.path.basename(archive["state_file"]))
        except OSError as e:
            errors.append(f"State: {e}")

    # Delete .archive-ready marker if it exists
    archive_ready_file = os.path.join(STAGING_DIR, f"{prefix}.iso.archive-ready")
    if os.path.exists(archive_ready_file):
        try:
            os.remove(archive_ready_file)
            deleted.append(os.path.basename(archive_ready_file))
        except OSError as e:
            errors.append(f"Archive-ready marker: {e}")

    return jsonify({
        "status": "deleted" if not errors else "partial",
        "deleted": deleted,
        "errors": errors
    })


@archives_bp.route("/api/archives/archive-now", methods=["POST"])
def api_archives_archive_now():
    """API: Trigger immediate ISO archival via systemd service.

    Expected JSON:
    {
        "prefix": "The_Matrix-1703615234"
    }
    """
    data = request.json or {}
    prefix = data.get("prefix")

    if not prefix:
        return jsonify({"error": "Missing prefix"}), 400

    # Verify archive exists and is eligible
    archives = {a["prefix"]: a for a in get_iso_archives()}
    if prefix not in archives:
        return jsonify({"error": "Archive not found"}), 404

    archive = archives[prefix]

    # Check if already archiving or archived
    if archive.get("archiving"):
        return jsonify({"error": "Already archiving"}), 400
    if archive.get("archived"):
        return jsonify({"error": "Already archived"}), 400

    # Must be archive-ready or deletable (legacy) to archive
    if not archive.get("archive_ready") and not archive.get("deletable"):
        return jsonify({"error": "ISO must be marked archive-ready to archive"}), 400

    # Don't archive if actively processing
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing"]:
        return jsonify({"error": f"Cannot archive: ISO is {archive['state']}"}), 400

    # Trigger the archive service via systemctl
    try:
        result = subprocess.run(
            ["systemctl", "start", "dvd-archive.service"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return jsonify({"error": f"Failed to start archive service: {result.stderr}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout starting archive service"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to trigger archive: {e}"}), 500

    return jsonify({
        "status": "started",
        "prefix": prefix,
        "message": "Archive service triggered"
    })
