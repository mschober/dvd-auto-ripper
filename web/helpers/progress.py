"""Progress tracking for active DVD ripper processes."""
import os
import re
import glob
import json
from datetime import datetime

from helpers.pipeline import STAGING_DIR
from helpers.locks import LockManager
from helpers.logs import LOG_DIR, LOG_FILES


class ProgressTracker:
    """Tracks progress of active encoding, transfer, and ISO creation processes."""

    @staticmethod
    def get_receiving_transfers():
        """Detect incoming rsync transfers by looking for temp files.

        Rsync creates temp files like .FILENAME.XXXXXX while transferring.

        Returns:
            list: Receiving transfers with filename and current size.
        """
        receiving = []

        try:
            for entry in os.listdir(STAGING_DIR):
                # Rsync temp files start with . and have .iso. in the name
                # Example: .FAR_FROM_HEAVEN-1768079402.iso.rxYU4v
                if entry.startswith('.') and '.iso.' in entry:
                    # Extract original filename: .NAME-123.iso.rxYU4v -> NAME-123.iso
                    # Remove leading dot, then split on last dot to remove random suffix
                    name_without_dot = entry[1:]
                    parts = name_without_dot.rsplit('.', 1)
                    if len(parts) == 2 and parts[0].endswith('.iso'):
                        original_name = parts[0]
                        temp_path = os.path.join(STAGING_DIR, entry)
                        try:
                            size = os.path.getsize(temp_path)
                            size_mb = size / (1024 * 1024)
                            receiving.append({
                                "filename": original_name,
                                "size_mb": round(size_mb, 1),
                                "temp_file": entry
                            })
                        except OSError:
                            pass
        except OSError:
            pass

        return receiving

    @staticmethod
    def get_active_progress():
        """Parse recent logs to extract progress for active processes.

        Returns:
            dict: Progress info for iso, encoder, distributing, transfer, receiving, and archive stages.
        """
        progress = {"iso": None, "encoder": None, "distributing": None, "transfer": None, "receiving": None}
        locks = LockManager.get_status()

        # Check for distributing state files (cluster distribution in progress)
        distributing_files = glob.glob(os.path.join(STAGING_DIR, "*.distributing"))
        is_distributing = len(distributing_files) > 0

        # Check for receiving transfers (rsync temp files) early so we don't skip them
        receiving = ProgressTracker.get_receiving_transfers()
        if receiving:
            progress["receiving"] = receiving

        # Only parse logs if something is actually running (but still return receiving if found)
        if not any(s["active"] for s in locks.values()) and not is_distributing:
            return progress

        # Parse HandBrake encoding progress (per-slot, like ISO per-drive)
        progress["encoder"] = ProgressTracker._parse_encoder_progress(locks)

        # Parse ddrescue ISO creation progress (per-device)
        progress["iso"] = ProgressTracker._parse_iso_progress(locks)

        # Parse rsync cluster distribution progress
        if is_distributing:
            progress["distributing"] = ProgressTracker._parse_distributing_progress()

        # Parse rsync transfer progress (per-slot)
        progress["transfer"] = ProgressTracker._parse_transfer_progress(locks)

        # Parse archive progress (xz compression)
        progress["archive"] = ProgressTracker._parse_archive_progress(locks)

        return progress

    @staticmethod
    def _parse_encoder_progress(locks):
        """Parse HandBrake encoding progress from logs."""
        encoder_status = locks.get("encoder", {})
        encoder_slots = encoder_status.get("slots", {})
        active_slots = [s for s, info in encoder_slots.items() if info.get("active")]

        if not encoder_status.get("active") or not active_slots:
            return None

        # Load all .encoding files to map slots to titles
        encoding_files = glob.glob(os.path.join(STAGING_DIR, "*.encoding"))
        slot_to_title = {}
        all_titles = []  # For fallback when encoder_slot not set
        for ef in encoding_files:
            try:
                with open(ef, 'r') as f:
                    meta = json.load(f)
                    slot = meta.get("encoder_slot", "")
                    title = meta.get("title", "").replace('_', ' ')
                    if title:
                        all_titles.append(title)
                    if slot and title:
                        slot_to_title[slot] = title
            except Exception:
                pass

        # Check if any per-slot logs exist (new behavior)
        has_per_slot_logs = any(
            os.path.exists(os.path.join(LOG_DIR, f"encoder-{slot}.log"))
            for slot in active_slots
        )

        # Fallback: read legacy encoder.log if no per-slot logs exist
        legacy_logs = ""
        if not has_per_slot_logs:
            encoder_log = LOG_FILES.get("encoder", "")
            if encoder_log and os.path.exists(encoder_log):
                try:
                    with open(encoder_log, 'r') as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 10240))
                        legacy_logs = f.read()
                except Exception:
                    pass

        encoder_progress_list = []
        for idx, slot in enumerate(active_slots):
            # Read per-slot log file
            slot_log_file = os.path.join(LOG_DIR, f"encoder-{slot}.log")
            slot_logs = ""
            if os.path.exists(slot_log_file):
                try:
                    with open(slot_log_file, 'r') as f:
                        f.seek(0, 2)  # End of file
                        size = f.tell()
                        f.seek(max(0, size - 10240))  # Last 10KB
                        slot_logs = f.read()
                except Exception:
                    pass

            # Find encoding progress in this slot's log
            encoder_matches = re.findall(
                r'Encoding:.*?(\d+\.?\d*)\s*%.*?(\d+\.?\d*)\s*fps.*?ETA\s*(\d+h\d+m\d+s|\d+m\d+s)',
                slot_logs
            )

            # Get title: prefer slot mapping, fallback to all_titles by index
            title = slot_to_title.get(slot)
            if not title and idx < len(all_titles):
                title = all_titles[idx]
            if not title:
                title = f"Slot {slot}"

            if encoder_matches:
                last_match = encoder_matches[-1]
                encoder_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": float(last_match[0]),
                    "speed": f"{last_match[1]} fps",
                    "eta": last_match[2]
                })
            elif legacy_logs:
                # Fallback: use legacy log progress (shared between encoders)
                legacy_matches = re.findall(
                    r'Encoding:.*?(\d+\.?\d*)\s*%.*?(\d+\.?\d*)\s*fps.*?ETA\s*(\d+h\d+m\d+s|\d+m\d+s)',
                    legacy_logs
                )
                if legacy_matches:
                    last_match = legacy_matches[-1]
                    encoder_progress_list.append({
                        "slot": slot,
                        "title": title,
                        "percent": float(last_match[0]),
                        "speed": f"{last_match[1]} fps",
                        "eta": last_match[2],
                        "shared_log": True  # Indicates progress from shared log
                    })
                else:
                    encoder_progress_list.append({
                        "slot": slot,
                        "title": title,
                        "percent": 0,
                        "speed": "starting",
                        "eta": "calculating"
                    })
            else:
                # Encoding active but no progress yet
                encoder_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": 0,
                    "speed": "starting",
                    "eta": "calculating"
                })

        return encoder_progress_list if encoder_progress_list else None

    @staticmethod
    def _parse_iso_progress(locks):
        """Parse ddrescue ISO creation progress from per-device logs."""
        iso_status = locks.get("iso", {})
        iso_drives = iso_status.get("drives", {})
        active_iso_drives = [d for d, info in iso_drives.items() if info.get("active")]

        if not iso_status.get("active") or not active_iso_drives:
            return None

        iso_progress_list = []
        for drive in active_iso_drives:
            # Read per-device log file for this drive
            device_log_file = os.path.join(LOG_DIR, f"iso-{drive}.log")
            drive_logs = ""
            if os.path.exists(device_log_file):
                try:
                    with open(device_log_file, 'r') as f:
                        # Read last 50 lines for recent progress
                        lines = f.readlines()
                        drive_logs = ''.join(lines[-50:])
                except Exception:
                    pass

            # Try ddrescue format: "pct rescued: 45.6%  ...  remaining time: 5m"
            iso_matches = re.findall(
                r'pct rescued:\s*(\d+\.?\d*)%.*?remaining time:\s*(\d+m|\d+s|n/a)',
                drive_logs
            )
            if iso_matches:
                last_match = iso_matches[-1]
                iso_progress_list.append({
                    "drive": drive,
                    "percent": float(last_match[0]),
                    "eta": last_match[1] if last_match[1] != "n/a" else "finishing..."
                })
                continue

            # Try dvdbackup format: "Copying Title, part 1/2: 45% done (1800/4000 MiB)"
            dvdbackup_matches = re.findall(
                r'Copying\s+[^:]+:\s*(\d+\.?\d*)%\s+done\s+\((\d+\.?\d*)/(\d+\.?\d*)\s+MiB\)',
                drive_logs
            )
            if dvdbackup_matches:
                last_match = dvdbackup_matches[-1]
                copied_mb = int(float(last_match[1]))
                total_mb = int(float(last_match[2]))
                iso_progress_list.append({
                    "drive": drive,
                    "percent": float(last_match[0]),
                    "eta": f"{copied_mb}/{total_mb} MiB"
                })
                continue

            # Drive is active but no progress yet (just started)
            iso_progress_list.append({
                "drive": drive,
                "percent": 0.0,
                "eta": "starting..."
            })

        return iso_progress_list if iso_progress_list else None

    @staticmethod
    def _parse_distributing_progress():
        """Parse rsync cluster distribution progress."""
        dist_log = LOG_FILES.get("distribute", "")
        dist_logs = ""
        if dist_log and os.path.exists(dist_log):
            try:
                with open(dist_log, 'r') as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 10240))
                    dist_logs = f.read()
            except Exception:
                pass

        dist_matches = re.findall(
            r'(\d+)%\s+([\d.]+[KMG]?B/s)\s+(\d+:\d+:\d+)',
            dist_logs
        )
        if dist_matches:
            last_match = dist_matches[-1]
            return {
                "percent": float(last_match[0]),
                "speed": last_match[1],
                "eta": last_match[2]
            }
        return None

    @staticmethod
    def _parse_transfer_progress(locks):
        """Parse rsync transfer progress from per-slot logs."""
        transfer_status = locks.get("transfer", {})
        transfer_slots = transfer_status.get("slots", {})
        active_transfer_slots = [s for s, info in transfer_slots.items() if info.get("active")]

        if not transfer_status.get("active") or not active_transfer_slots:
            return None

        # Load all .transferring files to map slots to titles
        transferring_files = glob.glob(os.path.join(STAGING_DIR, "*.transferring"))
        all_transfer_titles = []
        for tf in transferring_files:
            try:
                with open(tf, 'r') as f:
                    meta = json.load(f)
                    title = meta.get("title", "").replace('_', ' ')
                    if title:
                        all_transfer_titles.append(title)
            except Exception:
                pass

        transfer_progress_list = []
        for idx, slot in enumerate(active_transfer_slots):
            # Read per-slot log file
            slot_log_file = os.path.join(LOG_DIR, f"transfer.{slot}.log")
            slot_logs = ""
            if os.path.exists(slot_log_file):
                try:
                    with open(slot_log_file, 'r') as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 10240))
                        slot_logs = f.read()
                except Exception:
                    pass

            # Fallback to legacy log if no per-slot log
            if not slot_logs:
                transfer_log = LOG_FILES.get("transfer", "")
                if transfer_log and os.path.exists(transfer_log):
                    try:
                        with open(transfer_log, 'r') as f:
                            f.seek(0, 2)
                            size = f.tell()
                            f.seek(max(0, size - 10240))
                            slot_logs = f.read()
                    except Exception:
                        pass

            # Get title from transferring files
            title = all_transfer_titles[idx] if idx < len(all_transfer_titles) else f"Slot {slot}"

            transfer_matches = re.findall(
                r'(\d+)%\s+([\d.]+[KMG]?B/s)\s+(\d+:\d+:\d+)',
                slot_logs
            )
            if transfer_matches:
                last_match = transfer_matches[-1]
                transfer_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": float(last_match[0]),
                    "speed": last_match[1],
                    "eta": last_match[2]
                })
            else:
                transfer_progress_list.append({
                    "slot": slot,
                    "title": title,
                    "percent": 0,
                    "speed": "starting",
                    "eta": "calculating"
                })

        return transfer_progress_list if transfer_progress_list else None

    @staticmethod
    def _parse_archive_progress(locks):
        """Parse xz compression progress for archiving."""
        archive_status = locks.get("archive", {})
        if not archive_status.get("active"):
            return None

        archive_progress_list = []

        # Find .archiving state files
        archiving_files = glob.glob(os.path.join(STAGING_DIR, "*.archiving"))
        for state_file in archiving_files:
            try:
                with open(state_file, 'r') as f:
                    meta = json.load(f)

                iso_path = meta.get("iso_path", "")
                title = meta.get("title", "Unknown").replace('_', ' ')
                iso_size = meta.get("iso_size_bytes", 0)
                started_at = meta.get("started_at", "")
                started_time = ""
                if started_at:
                    try:
                        st = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                        started_time = st.strftime("%-I:%M %p")
                    except Exception:
                        started_time = started_at[:16] if len(started_at) > 16 else started_at

                # Calculate progress from xz output file size
                xz_path = f"{iso_path}.xz"
                percent = 0
                speed = "compressing"
                eta = "calculating"

                if os.path.exists(xz_path) and iso_size > 0:
                    try:
                        xz_size = os.path.getsize(xz_path)
                        # xz typically achieves 40-60% compression, so estimate based on that
                        # Max at 95% since we can't know exactly when it will finish
                        estimated_final = iso_size * 0.5  # Assume 50% compression ratio
                        percent = min(95, (xz_size / estimated_final) * 100)

                        # Calculate elapsed time and estimate ETA
                        if started_at:
                            try:
                                start_time = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                                elapsed = (datetime.now(start_time.tzinfo) - start_time).total_seconds()
                                if percent > 5 and elapsed > 60:
                                    total_estimated = elapsed / (percent / 100)
                                    remaining = total_estimated - elapsed
                                    if remaining > 0:
                                        hours = int(remaining // 3600)
                                        minutes = int((remaining % 3600) // 60)
                                        if hours > 0:
                                            eta = f"{hours}h{minutes}m"
                                        else:
                                            eta = f"{minutes}m"
                                    speed = f"{xz_size / 1024 / 1024 / elapsed:.1f} MB/s" if elapsed > 0 else "starting"
                            except Exception:
                                pass
                    except OSError:
                        pass

                archive_progress_list.append({
                    "title": title,
                    "percent": round(percent, 1),
                    "speed": speed,
                    "eta": eta,
                    "started_time": started_time
                })
            except Exception:
                pass

        return archive_progress_list if archive_progress_list else None
