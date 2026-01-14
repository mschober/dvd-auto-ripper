"""Process management for DVD ripper pipeline."""
import os
import glob
import json
import time

from helpers.pipeline import STAGING_DIR
from helpers.locks import LOCK_FILES, LOCK_DIR, STATE_CONFIG, LockManager
from helpers.system_health import SystemHealth


class ProcessManager:
    """Manages pipeline processes: killing, cleanup, and queue cancellation."""

    @staticmethod
    def revert_state_file(state_file_path, new_state):
        """Revert a state file to a previous state, or remove it.

        Args:
            state_file_path: Path to the state file.
            new_state: New state to transition to, or None to delete.

        Returns:
            tuple: (new_path or None, message)
        """
        basename = os.path.basename(state_file_path)
        if new_state is None:
            os.remove(state_file_path)
            return None, f"Removed {basename}"
        else:
            base = state_file_path.rsplit('.', 1)[0]
            new_state_file = f"{base}.{new_state}"
            os.rename(state_file_path, new_state_file)
            return new_state_file, f"Reverted {basename} to {new_state}"

    @staticmethod
    def kill_process_with_cleanup(pid):
        """Kill a DVD ripper process and clean up associated state.

        Args:
            pid: Process ID to kill.

        Returns:
            tuple: (success, message)
        """
        pid = int(pid)

        # Map process type to state name
        PROCESS_TO_STATE = {
            "encoder": "encoding",
            "iso": "iso-creating",
            "transfer": "transferring",
            "distribute": "distributing"
        }

        # First verify this is one of our processes
        processes = SystemHealth.get_dvd_processes()
        target_process = None
        for proc in processes:
            if proc.get("pid") == pid:
                target_process = proc
                break

        if not target_process:
            return False, f"PID {pid} is not a DVD ripper process"

        process_type = target_process.get("type", "unknown")
        state_name = PROCESS_TO_STATE.get(process_type)
        config = STATE_CONFIG.get(state_name, {}) if state_name else {}

        # Get lock file from STATE_CONFIG
        lock_stage = config.get("lock")
        lock_file = LOCK_FILES.get(lock_stage) if lock_stage else None

        try:
            # Send SIGTERM first
            os.kill(pid, 15)  # SIGTERM

            # Wait a moment for graceful shutdown
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

            # Revert state files using STATE_CONFIG
            cleanup_msg = ""
            if state_name:
                revert_to = config.get("revert_to")
                pattern = os.path.join(STAGING_DIR, f"*.{state_name}")
                for state_file in glob.glob(pattern):
                    try:
                        _, msg = ProcessManager.revert_state_file(state_file, revert_to)
                        cleanup_msg += f" {msg}."
                    except Exception as e:
                        cleanup_msg += f" Failed to clean up {os.path.basename(state_file)}: {e}"

            return True, f"Killed PID {pid} ({process_type}).{cleanup_msg}"

        except ProcessLookupError:
            return False, f"Process {pid} not found"
        except PermissionError:
            return False, f"Permission denied to kill PID {pid}"
        except Exception as e:
            return False, f"Failed to kill PID {pid}: {e}"

    @staticmethod
    def cancel_queue_item(state_file_name, delete_files=False):
        """Cancel a queue item by state file name.

        Handles process killing and state reversion based on current state.

        Args:
            state_file_name: Name of the state file.
            delete_files: If True, delete associated media files.

        Returns:
            tuple: (success, message)
        """
        state_file_path = os.path.join(STAGING_DIR, state_file_name)

        if not os.path.exists(state_file_path):
            return False, "State file not found"

        # Extract state from filename
        state = state_file_name.rsplit('.', 1)[-1]

        if state not in STATE_CONFIG:
            return False, f"Unknown or non-cancellable state: {state}"

        config = STATE_CONFIG[state]

        # Read metadata for file paths
        try:
            with open(state_file_path, 'r') as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, IOError):
            metadata = {}

        messages = []

        # Handle active processes
        if config["lock"]:
            pid = LockManager.find_process_for_lock(config["lock"])
            if pid:
                try:
                    os.kill(pid, 15)  # SIGTERM
                    time.sleep(2)
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, 9)  # SIGKILL if still running
                    except ProcessLookupError:
                        pass
                    messages.append(f"Killed process {pid}")
                except PermissionError:
                    messages.append(f"Permission denied killing PID {pid}")
                except Exception as e:
                    messages.append(f"Could not kill process: {e}")

            # Clean up lock file
            if config["lock"] == "iso":
                # For ISO, clean up all per-device lock files
                for lock_file in glob.glob(os.path.join(LOCK_DIR, "iso-*.lock")):
                    try:
                        with open(lock_file, 'r') as f:
                            lock_pid = int(f.read().strip())
                        # Only remove if this was the process we killed
                        if lock_pid == pid:
                            os.remove(lock_file)
                    except Exception:
                        pass
                # Also check legacy lock
                legacy_iso = os.path.join(LOCK_DIR, "iso.lock")
                if os.path.exists(legacy_iso):
                    try:
                        os.remove(legacy_iso)
                    except Exception:
                        pass
            elif config["lock"] in LOCK_FILES:
                lock_file = LOCK_FILES[config["lock"]]
                if os.path.exists(lock_file):
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass

        # Clean up partial files for active states
        if state == "iso-creating":
            iso_path = metadata.get("iso_path")
            if iso_path and os.path.exists(iso_path):
                try:
                    os.remove(iso_path)
                    messages.append("Removed partial ISO")
                except Exception:
                    pass
        elif state == "encoding":
            mkv_path = metadata.get("mkv_path")
            if mkv_path and os.path.exists(mkv_path):
                try:
                    os.remove(mkv_path)
                    messages.append("Removed partial MKV")
                except Exception:
                    pass

        # Handle optional file deletion for queued states
        if delete_files:
            if state == "iso-ready":
                iso_path = metadata.get("iso_path")
                if iso_path and os.path.exists(iso_path):
                    try:
                        os.remove(iso_path)
                        messages.append("Deleted ISO file")
                    except Exception as e:
                        messages.append(f"Could not delete ISO: {e}")
            elif state == "encoded-ready":
                mkv_path = metadata.get("mkv_path")
                if mkv_path and os.path.exists(mkv_path):
                    try:
                        os.remove(mkv_path)
                        messages.append("Deleted MKV file")
                    except Exception as e:
                        messages.append(f"Could not delete MKV: {e}")

        # Revert or remove state file
        try:
            _, msg = ProcessManager.revert_state_file(state_file_path, config["revert_to"])
            messages.append(msg)
        except Exception as e:
            return False, f"Failed to update state file: {e}"

        return True, ". ".join(messages)
