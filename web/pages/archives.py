"""Archives page - ISO archive management with cluster transfer support."""
import os
import glob
import json
import shutil
import socket
import subprocess
import time
from datetime import datetime
from flask import Blueprint, jsonify, render_template, request

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
            "archive_only": False,  # True when ISO deleted but .xz exists
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

    # Find archived-only items (ISO deleted but .archived state file exists with .xz)
    for archived_file in glob.glob(os.path.join(STAGING_DIR, "*.archived")):
        basename = os.path.basename(archived_file)
        prefix = basename.replace(".archived", "")

        # Skip if we already have this as an ISO entry
        if prefix in archives:
            continue

        # Read archived metadata
        try:
            with open(archived_file, 'r') as f:
                arch_meta = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        archive_path = arch_meta.get("archive_path", "")

        # Strip "local:" prefix if present (legacy format)
        if archive_path.startswith("local:"):
            archive_path = archive_path[6:]

        # Verify .xz file exists
        if not archive_path or not os.path.exists(archive_path):
            continue

        # Get compressed file size and mtime
        try:
            compressed_size = os.path.getsize(archive_path)
            mtime = os.path.getmtime(archive_path)
        except OSError:
            continue

        # Create entry for archived-only item
        archives[prefix] = {
            "prefix": prefix,
            "iso_path": None,  # No ISO, only archive
            "iso_size": arch_meta.get("original_size_bytes", 0),
            "deletable": False,
            "archive_ready": False,
            "archive_only": True,  # Key flag for UI
            "mapfile": None,
            "keys_dir": None,
            "keys_count": 0,
            "state_file": archived_file,
            "state": None,
            "metadata": arch_meta,
            "mtime": mtime,
            # Archive-related fields
            "archiving": False,
            "archived": True,
            "compressed_size": compressed_size,
            "archive_path": archive_path,
            "archived_at": arch_meta.get("archived_at", "")
        }

        # Check for xz-transferring state (archive transfer in progress)
        for transfer_file in glob.glob(os.path.join(STAGING_DIR, f"{prefix}.xz-transferring-to-*")):
            peer_name = os.path.basename(transfer_file).split('.xz-transferring-to-')[-1]
            archives[prefix]["state"] = "xz-transferring"
            archives[prefix]["transfer_peer"] = peer_name
            try:
                with open(transfer_file, 'r') as f:
                    archives[prefix]["transfer_metadata"] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
            break

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
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False


# ============================================================================
# Routes
# ============================================================================

@archives_bp.route("/archives")
def archives_page():
    """Archives management page."""
    config = get_cluster_config_for_archives()
    archives = get_iso_archives()

    # Get local disk usage
    disk_usage = get_local_disk_usage()

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

    return render_template(
        "archives.html",
        archives=archives,
        receiving=receiving,
        total_count=len(archives),
        total_size_gb=round(total_size / (1024**3), 2),
        disk_usage=disk_usage,
        cluster_enabled=config["cluster_enabled"],
        node_name=config["node_name"],
        peers=peers,
        archived_stats=archived_stats,
        format_size=format_size
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
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing", "archive-transferring", "xz-transferring"]:
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

    # Determine if this is an archived-only transfer (.xz file) or ISO transfer
    is_archive_only = archive.get("archive_only", False)

    # Create state file with all parameters for subprocess
    if is_archive_only:
        state_file = os.path.join(STAGING_DIR, f"{prefix}.xz-transferring-to-{peer_name}")
        state_data = {
            "status": "pending",
            "peer": peer_name,
            "peer_host": peer_host,
            "peer_port": peer_port,
            "archive_only": True,
            "archive_path": archive["archive_path"],
            "compressed_size": archive["compressed_size"],
            "original_size": archive["iso_size"],
            "ssh_user": ssh_user,
            "remote_staging": remote_staging,
            "started": time.time()
        }
    else:
        state_file = os.path.join(STAGING_DIR, f"{prefix}.archive-transferring-to-{peer_name}")
        state_data = {
            "status": "pending",
            "peer": peer_name,
            "peer_host": peer_host,
            "peer_port": peer_port,
            "archive_only": False,
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
    if archive["state"] in ["iso-creating", "encoding", "transferring", "distributing", "archive-transferring", "xz-transferring"]:
        return jsonify({"error": f"Cannot delete: archive is {archive['state']}"}), 400

    deleted = []
    errors = []

    # Handle archived-only items (delete .xz and .par2 files)
    if archive.get("archive_only"):
        archive_path = archive.get("archive_path")
        if archive_path and os.path.exists(archive_path):
            try:
                os.remove(archive_path)
                deleted.append(os.path.basename(archive_path))
            except OSError as e:
                errors.append(f"Archive: {e}")

            # Delete associated .par2 files
            par2_files = glob.glob(archive_path + "*.par2")
            for par2_file in par2_files:
                try:
                    os.remove(par2_file)
                    deleted.append(os.path.basename(par2_file))
                except OSError as e:
                    errors.append(f"PAR2: {e}")

    # Delete ISO file (for non-archived-only items)
    elif archive["iso_path"] and os.path.exists(archive["iso_path"]):
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
