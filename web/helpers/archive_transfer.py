#!/usr/bin/env python3
"""Standalone archive transfer worker.

This script runs as a subprocess to transfer archives between cluster nodes.
It survives dashboard restarts and supports parallel transfers.

Usage:
    python3 archive_transfer.py --state-file /path/to/state.json

The state file contains all transfer parameters and is updated with progress.
"""
import argparse
import json
import os
import shutil
import sys
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.cluster import rsync_files, rsync_directory, confirm_files_on_peer


def do_transfer(state_file: str):
    """Perform the archive transfer based on state file parameters."""

    # Read state file for parameters
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
    except Exception as e:
        print(f"Failed to read state file: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract parameters
    iso_path = state.get("iso_path")
    mapfile = state.get("mapfile")
    keys_dir = state.get("keys_dir")
    peer_host = state.get("peer_host")
    peer_port = state.get("peer_port")
    peer_name = state.get("peer")
    ssh_user = state.get("ssh_user")
    remote_staging = state.get("remote_staging")
    iso_size = state.get("iso_size", 0)

    if not iso_path or not peer_host or not ssh_user:
        update_state(state_file, "failed", error="Missing required parameters")
        sys.exit(1)

    start_time = time.time()

    # Update state to show we're running
    update_state(state_file, "transferring", pid=os.getpid())

    try:
        # Build file list
        files_to_transfer = [iso_path]
        if mapfile and os.path.exists(mapfile):
            files_to_transfer.append(mapfile)

        # Transfer files via rsync
        result = rsync_files(files_to_transfer, peer_host, ssh_user, remote_staging)

        if not result["success"]:
            update_state(state_file, "failed", error=result["errors"])
            sys.exit(1)

        transferred = result["transferred"]

        # Transfer keys directory if exists
        if keys_dir and os.path.isdir(keys_dir):
            keys_result = rsync_directory(keys_dir, peer_host, ssh_user, remote_staging)
            if keys_result["success"]:
                transferred.extend(keys_result["transferred"])

        # Confirm files arrived on peer
        filenames_to_confirm = [os.path.basename(f) for f in files_to_transfer]
        confirm = confirm_files_on_peer(peer_host, peer_port, filenames_to_confirm)

        if not confirm["success"]:
            update_state(state_file, "failed", error=f"Confirmation failed: {confirm.get('error')}")
            sys.exit(1)

        if confirm["missing"]:
            update_state(state_file, "failed", error=f"Missing files on peer: {confirm['missing']}")
            sys.exit(1)

        # Transfer confirmed - delete source files (move semantics)
        deleted = []

        if os.path.exists(iso_path):
            try:
                os.remove(iso_path)
                deleted.append(os.path.basename(iso_path))
            except OSError as e:
                pass  # Non-fatal

        if mapfile and os.path.exists(mapfile):
            try:
                os.remove(mapfile)
                deleted.append(os.path.basename(mapfile))
            except OSError:
                pass

        if keys_dir and os.path.exists(keys_dir):
            try:
                shutil.rmtree(keys_dir)
                deleted.append(os.path.basename(keys_dir))
            except OSError:
                pass

        # Success - remove state file
        try:
            os.remove(state_file)
        except OSError:
            pass

        print(f"Transfer complete: {transferred}")
        sys.exit(0)

    except Exception as e:
        update_state(state_file, "failed", error=str(e))
        sys.exit(1)


def update_state(state_file: str, status: str, **kwargs):
    """Update the state file with new status and optional fields."""
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
    except:
        state = {}

    state["status"] = status
    state["updated"] = time.time()
    state.update(kwargs)

    try:
        with open(state_file, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Failed to update state file: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Archive transfer worker")
    parser.add_argument("--state-file", required=True, help="Path to state file")
    args = parser.parse_args()

    do_transfer(args.state_file)


if __name__ == "__main__":
    main()
