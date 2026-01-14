"""Lock file management for DVD ripper pipeline."""
import os
import glob

# Lock file configuration
LOCK_DIR = "/run/dvd-ripper"
LOCK_FILES = {
    "encoder": f"{LOCK_DIR}/encoder.lock",
    "transfer": f"{LOCK_DIR}/transfer.lock",
    "distribute": f"{LOCK_DIR}/distribute.lock"
}
# ISO locks are now per-device (iso-sr0.lock, iso-sr1.lock) and detected dynamically

# State configuration for cancellation and reversion
STATE_CONFIG = {
    "iso-creating": {"lock": "iso", "revert_to": None},
    "iso-ready": {"lock": None, "revert_to": None},
    "distributing": {"lock": "distribute", "revert_to": "iso-ready"},
    "encoding": {"lock": "encoder", "revert_to": "iso-ready"},
    "encoded-ready": {"lock": None, "revert_to": None},
    "transferring": {"lock": "transfer", "revert_to": "encoded-ready"},
}


class LockManager:
    """Manages pipeline lock files for coordinating stages."""

    @staticmethod
    def check_lock_file(lock_file):
        """Check if a lock file exists and has an active process.

        Args:
            lock_file: Path to the lock file.

        Returns:
            dict: {"active": bool, "pid": str or None}
        """
        if os.path.exists(lock_file):
            try:
                with open(lock_file, 'r') as f:
                    pid = f.read().strip()
                # Check if process is actually running via /proc (works across users)
                # os.kill(pid, 0) requires same-user or root permissions
                if os.path.exists(f"/proc/{pid}"):
                    return {"active": True, "pid": pid}
                else:
                    return {"active": False, "pid": None}
            except (ValueError, IOError):
                return {"active": False, "pid": None}
        return {"active": False, "pid": None}

    @staticmethod
    def get_status():
        """Check which stages are currently locked/running.

        Returns:
            dict: Lock status for each stage including parallel slots.
        """
        status = {}

        # Check distribute lock (single instance)
        status["distribute"] = LockManager.check_lock_file(LOCK_FILES["distribute"])

        # Check parallel transfer locks (transfer-1.lock, transfer-2.lock, etc.)
        transfer_locks = glob.glob(os.path.join(LOCK_DIR, "transfer-*.lock"))
        transfer_slots = {}
        for lock_file in transfer_locks:
            # Extract slot number: transfer-1.lock -> 1
            slot = os.path.basename(lock_file).replace("transfer-", "").replace(".lock", "")
            transfer_slots[slot] = LockManager.check_lock_file(lock_file)

        # Also check legacy transfer.lock for backwards compatibility
        legacy_transfer = LOCK_FILES["transfer"]
        legacy_status = LockManager.check_lock_file(legacy_transfer)
        if legacy_status["active"]:
            transfer_slots["legacy"] = legacy_status

        # Provide combined "transfer" status (active if any slot is active)
        any_transfer_active = any(s.get("active") for s in transfer_slots.values())
        status["transfer"] = {"active": any_transfer_active, "pid": None, "slots": transfer_slots}

        # Check parallel encoder locks (encoder-1.lock, encoder-2.lock, etc.)
        encoder_locks = glob.glob(os.path.join(LOCK_DIR, "encoder-*.lock"))
        encoder_slots = {}
        for lock_file in encoder_locks:
            # Extract slot number: encoder-1.lock -> 1
            slot = os.path.basename(lock_file).replace("encoder-", "").replace(".lock", "")
            encoder_slots[slot] = LockManager.check_lock_file(lock_file)

        # Also check legacy encoder.lock for backwards compatibility
        legacy_encoder = LOCK_FILES["encoder"]
        legacy_status = LockManager.check_lock_file(legacy_encoder)
        if legacy_status["active"]:
            encoder_slots["legacy"] = legacy_status

        # Provide combined "encoder" status (active if any slot is active)
        any_encoder_active = any(s.get("active") for s in encoder_slots.values())
        status["encoder"] = {"active": any_encoder_active, "pid": None, "slots": encoder_slots}

        # Check per-device ISO locks (iso-sr0.lock, iso-sr1.lock, etc.)
        iso_locks = glob.glob(os.path.join(LOCK_DIR, "iso-*.lock"))
        iso_drives = {}
        for lock_file in iso_locks:
            # Extract device name: iso-sr0.lock -> sr0
            device = os.path.basename(lock_file).replace("iso-", "").replace(".lock", "")
            iso_drives[device] = LockManager.check_lock_file(lock_file)

        # Also check legacy iso.lock for backwards compatibility
        legacy_iso = os.path.join(LOCK_DIR, "iso.lock")
        legacy_status = LockManager.check_lock_file(legacy_iso)
        if legacy_status["active"]:
            iso_drives["default"] = legacy_status

        # Provide combined "iso" status for backwards compat (active if any drive is active)
        any_iso_active = any(d.get("active") for d in iso_drives.values())
        status["iso"] = {"active": any_iso_active, "pid": None, "drives": iso_drives}

        # Check archive lock (single instance - CPU intensive)
        archive_lock = os.path.join(LOCK_DIR, "archive.lock")
        status["archive"] = LockManager.check_lock_file(archive_lock)

        return status

    @staticmethod
    def find_process_for_lock(lock_stage):
        """Find PID of process from lock file if it's still running.

        Args:
            lock_stage: The stage name (e.g., "encoder", "transfer", "iso").

        Returns:
            int or None: PID if process is running, None otherwise.
        """
        # Handle per-device ISO locks
        if lock_stage == "iso":
            # Check all per-device ISO locks (iso-sr0.lock, iso-sr1.lock, etc.)
            iso_locks = glob.glob(os.path.join(LOCK_DIR, "iso-*.lock"))
            # Also check legacy iso.lock
            legacy_iso = os.path.join(LOCK_DIR, "iso.lock")
            if os.path.exists(legacy_iso):
                iso_locks.append(legacy_iso)
            for lock_file in iso_locks:
                try:
                    with open(lock_file, 'r') as f:
                        pid = int(f.read().strip())
                    if os.path.exists(f"/proc/{pid}"):
                        return pid
                except (ValueError, IOError):
                    pass
            return None

        if lock_stage not in LOCK_FILES:
            return None
        lock_file = LOCK_FILES[lock_stage]
        if not os.path.exists(lock_file):
            return None
        try:
            with open(lock_file, 'r') as f:
                pid = int(f.read().strip())
            if os.path.exists(f"/proc/{pid}"):
                return pid
        except (ValueError, IOError):
            pass
        return None
