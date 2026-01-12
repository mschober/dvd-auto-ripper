"""Archives page - ISO archive management with cluster transfer support."""
import os
import glob
import json
import shutil
import subprocess
from datetime import datetime
from flask import Blueprint, jsonify, render_template_string, request

from helpers.pipeline import STAGING_DIR, STATE_ORDER

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
            "mapfile": None,
            "keys_dir": None,
            "keys_count": 0,
            "state_file": None,
            "state": None,
            "metadata": {},
            "mtime": mtime
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

    return sorted(archives.values(), key=lambda x: x["mtime"], reverse=True)


def format_size(size_bytes):
    """Format bytes as human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


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
    <title>Archives - DVD Ripper</title>
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
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
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
    </div>

    <div class="layout">
        <div class="main-content">
            <div class="card">
                <h2>ISO Archives on {{ node_name or 'this node' }}</h2>
                {% if archives %}
                <table>
                    <thead>
                        <tr>
                            <th>Title</th>
                            <th>Size</th>
                            <th>State</th>
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
                                {% if archive.state %}
                                <span class="badge badge-state">{{ archive.state }}</span>
                                {% elif archive.deletable %}
                                <span class="badge badge-deletable">deletable</span>
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
                                        {% if archive.state in ['iso-creating', 'encoding', 'transferring', 'distributing'] %}
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
                    // Refresh after short delay
                    setTimeout(() => location.reload(), 2000);
                } else {
                    showNotification(result.error || 'Transfer failed', 'error');
                }
            } catch (err) {
                showNotification('Transfer request failed: ' + err.message, 'error');
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
                    setTimeout(() => location.reload(), 1000);
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
            notif.textContent = message;
            document.body.appendChild(notif);
            setTimeout(() => notif.remove(), 4000);
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

    # Get peer status if clustered
    peers = []
    if config["cluster_enabled"]:
        raw_peers = parse_peers(config["peers_raw"])
        for peer in raw_peers:
            peers.append({
                "name": peer["name"],
                "host": peer["host"],
                "port": peer["port"],
                "online": ping_peer_simple(peer["host"], peer["port"])
            })

    # Calculate totals
    total_size = sum(a["iso_size"] for a in archives)

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
        total_count=len(archives),
        total_size_gb=round(total_size / (1024**3), 2),
        cluster_enabled=config["cluster_enabled"],
        node_name=config["node_name"],
        peers=peers,
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


@archives_bp.route("/api/archives/transfer", methods=["POST"])
def api_archives_transfer():
    """API: Transfer an ISO archive to a peer node.

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

    # Don't transfer if actively processing
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing"]:
        return jsonify({"error": f"Cannot transfer: archive is {archive['state']}"}), 400

    # Parse peer
    parts = peer.split(":")
    if len(parts) < 3:
        return jsonify({"error": "Invalid peer format"}), 400
    peer_name, peer_host = parts[0], parts[1]

    # Get cluster config
    config = get_cluster_config_for_archives()
    ssh_user = config["ssh_user"]
    remote_staging = config["remote_staging"]

    # Build file list to transfer
    files_to_transfer = [archive["iso_path"]]
    if archive["mapfile"]:
        files_to_transfer.append(archive["mapfile"])

    # Start background rsync
    try:
        remote_dest = f"{ssh_user}@{peer_host}:{remote_staging}/"

        # Transfer ISO and mapfile
        cmd = ["rsync", "-avz", "--progress"] + files_to_transfer + [remote_dest]

        # Run in background
        log_file = f"/var/log/dvd-ripper/archive-transfer-{prefix}.log"
        with open(log_file, 'w') as log:
            log.write(f"Transfer started: {datetime.now().isoformat()}\n")
            log.write(f"Files: {files_to_transfer}\n")
            log.write(f"Destination: {remote_dest}\n\n")
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)

        # Transfer keys directory separately if exists (rsync -r for directory)
        if archive["keys_dir"]:
            keys_cmd = ["rsync", "-avz", "-r", archive["keys_dir"], remote_dest]
            subprocess.Popen(keys_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return jsonify({
            "status": "started",
            "prefix": prefix,
            "peer": peer_name,
            "files": files_to_transfer,
            "log_file": log_file
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@archives_bp.route("/api/archives/<prefix>", methods=["DELETE"])
def api_archives_delete(prefix):
    """API: Delete an ISO archive and all associated files."""
    archives = {a["prefix"]: a for a in get_iso_archives()}
    if prefix not in archives:
        return jsonify({"error": "Archive not found"}), 404

    archive = archives[prefix]

    # Safety check: don't delete if actively being processed
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing"]:
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

    return jsonify({
        "status": "deleted" if not errors else "partial",
        "deleted": deleted,
        "errors": errors
    })
