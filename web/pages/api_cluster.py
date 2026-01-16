"""Cluster API routes for the DVD ripper dashboard."""
import os
import glob
import json
from datetime import datetime
from flask import Blueprint, jsonify, request

from helpers.pipeline import STAGING_DIR
from helpers.system_health import SystemHealth
from helpers.cluster_manager import ClusterManager

# Blueprint setup
api_cluster_bp = Blueprint('api_cluster', __name__)


@api_cluster_bp.route("/api/cluster/status")
def api_cluster_status():
    """API: Get this node's cluster configuration and status."""
    config = ClusterManager.get_config()
    load = SystemHealth.get_load_average()

    return jsonify({
        "node_name": config["node_name"],
        "cluster_enabled": config["cluster_enabled"],
        "transfer_mode": config["transfer_mode"],
        "local_library_path": config["local_library_path"] if config["transfer_mode"] == "local" else None,
        "peers": ClusterManager.parse_peers(config["peers_raw"]),
        "load": load,
        "capacity": ClusterManager.get_worker_capacity()
    })


@api_cluster_bp.route("/api/cluster/peers")
def api_cluster_peers():
    """API: List all configured peers and their current status."""
    config = ClusterManager.get_config()

    if not config["cluster_enabled"]:
        return jsonify({
            "cluster_enabled": False,
            "peers": [],
            "message": "Cluster mode is not enabled"
        })

    return jsonify({
        "cluster_enabled": True,
        "this_node": config["node_name"],
        "peers": ClusterManager.get_all_peer_status()
    })


@api_cluster_bp.route("/api/worker/capacity")
def api_worker_capacity():
    """API: Get this node's current encoding capacity.

    Called by peer nodes to check if we can accept work.
    """
    config = ClusterManager.get_config()
    capacity = ClusterManager.get_worker_capacity()

    return jsonify({
        "node_name": config["node_name"],
        **capacity
    })


@api_cluster_bp.route("/api/cluster/ping", methods=["POST"])
def api_cluster_ping():
    """API: Health check endpoint for peer nodes.

    Peers call this to verify connectivity and get basic status.
    """
    config = ClusterManager.get_config()

    return jsonify({
        "status": "ok",
        "node_name": config["node_name"],
        "cluster_enabled": config["cluster_enabled"],
        "timestamp": datetime.now().isoformat()
    })


@api_cluster_bp.route("/api/worker/accept-job", methods=["POST"])
def api_accept_job():
    """API: Accept an encoding job from a peer node.

    Expected JSON body:
    {
        "metadata": {...},  # State file metadata
        "origin": "node_name"  # Originating node
    }

    Creates a local iso-ready state file for the encoder to pick up.
    """
    config = ClusterManager.get_config()

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

        # Make group-writable so encoder (dvd-encode) can update it
        os.chmod(state_file, 0o664)

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


@api_cluster_bp.route("/api/cluster/job-complete", methods=["POST"])
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
    config = ClusterManager.get_config()

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
            except Exception:
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
            except Exception:
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


@api_cluster_bp.route("/api/cluster/confirm-files", methods=["POST"])
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
