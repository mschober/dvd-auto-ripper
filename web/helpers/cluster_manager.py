"""Cluster management for distributed DVD encoding."""
import os
import glob
import json
import subprocess
import urllib.request
import urllib.error

from helpers.pipeline import STAGING_DIR
from helpers.config import ConfigManager
from helpers.system_health import SystemHealth


class ClusterManager:
    """Manages cluster configuration, peer status, and distributed jobs."""

    @staticmethod
    def get_config():
        """Read cluster-related configuration from config file.

        Returns:
            dict: Cluster configuration.
        """
        config = ConfigManager.read()
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

    @staticmethod
    def parse_peers(peers_raw):
        """Parse peer string into list of peer dicts.

        Format: "name:host:port name2:host2:port2"
        Example: "plex:192.168.1.50:5000 cart:192.168.1.34:5000"

        Args:
            peers_raw: Space-separated peer string.

        Returns:
            list: List of peer dicts with name, host, port.
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

    @staticmethod
    def count_active_encoders():
        """Count currently running HandBrakeCLI processes.

        Returns:
            int: Number of active encoders.
        """
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

    @staticmethod
    def get_worker_capacity():
        """Calculate available encoding capacity for this node.

        Returns:
            dict: Capacity information including slots, load, queue depth.
        """
        config = ClusterManager.get_config()
        load = SystemHealth.get_load_average()
        cpu_count = os.cpu_count() or 1
        max_load = cpu_count * config["load_threshold"]
        slots_used = ClusterManager.count_active_encoders()
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

    @staticmethod
    def ping_peer(host, port, timeout=5):
        """Check if a peer is alive and get its capacity.

        Args:
            host: Peer hostname.
            port: Peer port.
            timeout: Request timeout.

        Returns:
            dict or None: Capacity dict on success, None on failure.
        """
        url = f"http://{host}:{port}/api/worker/capacity"
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data
        except Exception:
            return None

    @staticmethod
    def get_all_peer_status():
        """Get status of all configured peers.

        Returns:
            list: Peer status dicts with online flag and capacity.
        """
        config = ClusterManager.get_config()
        peers = ClusterManager.parse_peers(config["peers_raw"])

        results = []
        for peer in peers:
            capacity = ClusterManager.ping_peer(peer["host"], peer["port"])
            results.append({
                "name": peer["name"],
                "host": peer["host"],
                "port": peer["port"],
                "online": capacity is not None,
                "capacity": capacity
            })
        return results

    @staticmethod
    def get_distributed_jobs():
        """Get jobs that have been distributed to peer nodes.

        Returns:
            list: Distributed job information.
        """
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
                except Exception:
                    pass
        return jobs

    @staticmethod
    def get_received_jobs():
        """Get jobs received from peer nodes (is_remote_job=true).

        Returns:
            list: Received job information.
        """
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
                except Exception:
                    pass
        return jobs
