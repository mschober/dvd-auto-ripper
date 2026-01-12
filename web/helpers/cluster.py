"""Cluster transfer utilities for web UI operations.

Provides shared helpers for transferring files between cluster nodes,
making API calls to peers, and confirming file arrivals.
"""
import os
import subprocess
import socket
import json
import urllib.request
import urllib.error
from typing import List, Dict, Optional


def rsync_files(files: List[str], peer_host: str, ssh_user: str,
                remote_path: str, timeout: int = 3600) -> Dict:
    """
    Synchronous rsync transfer to a peer node.

    Args:
        files: List of local file paths to transfer
        peer_host: Hostname or IP of the peer
        ssh_user: SSH user for rsync
        remote_path: Destination directory on peer
        timeout: Maximum seconds to wait for transfer

    Returns:
        {
            "success": bool,
            "transferred": list of successfully transferred files,
            "errors": list of error messages,
            "stdout": rsync output
        }
    """
    if not files:
        return {"success": True, "transferred": [], "errors": [], "stdout": ""}

    # Filter to only existing files
    existing_files = [f for f in files if os.path.exists(f)]
    missing = [f for f in files if not os.path.exists(f)]

    if not existing_files:
        return {
            "success": False,
            "transferred": [],
            "errors": [f"File not found: {f}" for f in missing],
            "stdout": ""
        }

    # Build rsync command with SSH options for non-interactive use
    # Uses dvd-web SSH key (set up by remote-install.sh --setup-cluster-peer)
    # BatchMode=yes: fail instead of prompting for password
    identity_file = "/var/lib/dvd-web/.ssh/id_ed25519"
    ssh_opts = f"ssh -i {identity_file} -o BatchMode=yes"
    remote_dest = f"{ssh_user}@{peer_host}:{remote_path}/"
    cmd = ["rsync", "-avz", "--progress", "-e", ssh_opts] + existing_files + [remote_dest]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode == 0:
            return {
                "success": True,
                "transferred": [os.path.basename(f) for f in existing_files],
                "errors": [f"File not found: {f}" for f in missing] if missing else [],
                "stdout": result.stdout
            }
        else:
            return {
                "success": False,
                "transferred": [],
                "errors": [f"rsync failed: {result.stderr}"],
                "stdout": result.stdout
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "transferred": [],
            "errors": [f"Transfer timed out after {timeout} seconds"],
            "stdout": ""
        }
    except Exception as e:
        return {
            "success": False,
            "transferred": [],
            "errors": [f"Transfer error: {str(e)}"],
            "stdout": ""
        }


def rsync_directory(directory: str, peer_host: str, ssh_user: str,
                    remote_path: str, timeout: int = 300) -> Dict:
    """
    Synchronous rsync of a directory to a peer node.

    Args:
        directory: Local directory path to transfer
        peer_host: Hostname or IP of the peer
        ssh_user: SSH user for rsync
        remote_path: Destination directory on peer
        timeout: Maximum seconds to wait for transfer

    Returns:
        Same format as rsync_files()
    """
    if not os.path.isdir(directory):
        return {
            "success": False,
            "transferred": [],
            "errors": [f"Directory not found: {directory}"],
            "stdout": ""
        }

    # SSH options for non-interactive use
    # Uses dvd-web SSH key (set up by remote-install.sh --setup-cluster-peer)
    identity_file = "/var/lib/dvd-web/.ssh/id_ed25519"
    ssh_opts = f"ssh -i {identity_file} -o BatchMode=yes"
    remote_dest = f"{ssh_user}@{peer_host}:{remote_path}/"
    cmd = ["rsync", "-avz", "-r", "-e", ssh_opts, directory, remote_dest]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode == 0:
            return {
                "success": True,
                "transferred": [os.path.basename(directory)],
                "errors": [],
                "stdout": result.stdout
            }
        else:
            return {
                "success": False,
                "transferred": [],
                "errors": [f"rsync failed: {result.stderr}"],
                "stdout": result.stdout
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "transferred": [],
            "errors": [f"Transfer timed out after {timeout} seconds"],
            "stdout": ""
        }
    except Exception as e:
        return {
            "success": False,
            "transferred": [],
            "errors": [f"Transfer error: {str(e)}"],
            "stdout": ""
        }


def call_peer_api(host: str, port: int, endpoint: str,
                  method: str = "GET", data: Optional[Dict] = None,
                  timeout: int = 10) -> Dict:
    """
    Make an API call to a peer node.

    Args:
        host: Peer hostname or IP
        port: Peer dashboard port
        endpoint: API endpoint (e.g., "/api/cluster/confirm-files")
        method: HTTP method (GET, POST, etc.)
        data: JSON data to send (for POST)
        timeout: Request timeout in seconds

    Returns:
        {
            "success": bool,
            "response": parsed JSON response or None,
            "error": error message or None,
            "status_code": HTTP status code or None
        }
    """
    url = f"http://{host}:{port}{endpoint}"

    try:
        if data is not None:
            json_data = json.dumps(data).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=json_data,
                headers={"Content-Type": "application/json"},
                method=method
            )
        else:
            req = urllib.request.Request(url, method=method)

        with urllib.request.urlopen(req, timeout=timeout) as response:
            status_code = response.getcode()
            response_data = response.read().decode('utf-8')

            try:
                parsed = json.loads(response_data)
            except json.JSONDecodeError:
                parsed = {"raw": response_data}

            return {
                "success": True,
                "response": parsed,
                "error": None,
                "status_code": status_code
            }

    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode('utf-8')
            error_parsed = json.loads(error_body)
            error_msg = error_parsed.get("error", str(e))
        except:
            error_msg = str(e)

        return {
            "success": False,
            "response": None,
            "error": error_msg,
            "status_code": e.code
        }

    except urllib.error.URLError as e:
        return {
            "success": False,
            "response": None,
            "error": f"Connection failed: {e.reason}",
            "status_code": None
        }

    except Exception as e:
        return {
            "success": False,
            "response": None,
            "error": str(e),
            "status_code": None
        }


def confirm_files_on_peer(host: str, port: int, files: List[str]) -> Dict:
    """
    Verify that files exist on a peer node via API call.

    Args:
        host: Peer hostname or IP
        port: Peer dashboard port
        files: List of filenames (not full paths) to check

    Returns:
        {
            "success": bool (True if API call succeeded),
            "confirmed": list of files that exist on peer,
            "missing": list of files that don't exist on peer,
            "error": error message if API call failed
        }
    """
    result = call_peer_api(
        host, port,
        "/api/cluster/confirm-files",
        method="POST",
        data={"files": files}
    )

    if not result["success"]:
        return {
            "success": False,
            "confirmed": [],
            "missing": files,
            "error": result["error"]
        }

    response = result["response"] or {}
    return {
        "success": True,
        "confirmed": response.get("confirmed", []),
        "missing": response.get("missing", files),
        "error": None
    }


def get_peer_status(host: str, port: int) -> Dict:
    """
    Get the status and capacity of a peer node.

    Args:
        host: Peer hostname or IP
        port: Peer dashboard port

    Returns:
        {
            "online": bool,
            "capacity": capacity dict or None,
            "error": error message or None
        }
    """
    result = call_peer_api(host, port, "/api/worker/capacity", timeout=5)

    if not result["success"]:
        return {
            "online": False,
            "capacity": None,
            "error": result["error"]
        }

    return {
        "online": True,
        "capacity": result["response"],
        "error": None
    }


def ping_peer(host: str, port: int, timeout: int = 2) -> bool:
    """
    Simple TCP ping to check if peer is reachable.

    Args:
        host: Peer hostname or IP
        port: Peer dashboard port
        timeout: Connection timeout in seconds

    Returns:
        True if peer is reachable, False otherwise
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False
