"""Title identification and renaming for DVD ripper."""
import os
import re
import glob
import json
import subprocess
from datetime import datetime

from helpers.pipeline import STAGING_DIR
from helpers.config import ConfigManager

# Generic title detection patterns (items needing identification)
GENERIC_PATTERNS = [
    r'^DVD_\d{8}_\d{6}$',           # DVD_YYYYMMDD_HHMMSS (our fallback format)
    r'^DVD_VIDEO$',                  # Common generic
    r'^DVDVIDEO$',                   # Another variant
    r'^DISC\d*$',                    # DISC, DISC1, etc.
    r'^DISK\d*$',                    # DISK, DISK1, etc.
    r'^VIDEO_TS$',                   # Raw folder name
    r'^MYDVD$',                      # Generic authoring tool name
    r'^DVD$',                        # Plain DVD
]

# States that allow renaming (not actively being processed)
RENAMEABLE_STATES = ["iso-ready", "encoded-ready", "transferred"]


class Identifier:
    """Handles title identification, renaming, and audit flags."""

    @staticmethod
    def is_generic_title(title):
        """Check if title appears to be generic/fallback and needs identification.

        Args:
            title: The title to check.

        Returns:
            bool: True if title is generic.
        """
        if not title:
            return True
        upper_title = title.upper()
        for pattern in GENERIC_PATTERNS:
            if re.match(pattern, upper_title):
                return True
        # Also flag very short titles
        if len(title) <= 3:
            return True
        return False

    @staticmethod
    def get_audit_flags():
        """Get audit flags for suspicious MKVs (created by dvd-audit.sh).

        Returns:
            list: Audit flag data sorted by modification time.
        """
        flags = []
        pattern = os.path.join(STAGING_DIR, ".audit-*")
        for audit_file in glob.glob(pattern):
            try:
                with open(audit_file, 'r') as f:
                    data = json.load(f)
                    data['audit_file'] = os.path.basename(audit_file)
                    data['mtime'] = os.path.getmtime(audit_file)
                    flags.append(data)
            except (json.JSONDecodeError, IOError):
                continue
        return sorted(flags, key=lambda x: x.get('mtime', 0))

    @staticmethod
    def get_pending_identification():
        """Get items that need identification or year in renameable states.

        Shows any item that has a preview file and is missing a valid
        4-digit year, plus any item explicitly flagged via
        needs_identification.

        Returns:
            list: Items needing attention sorted by modification time.
        """
        pending = []
        for state in RENAMEABLE_STATES:
            pattern = os.path.join(STAGING_DIR, f"*.{state}")
            for state_file in glob.glob(pattern):
                try:
                    with open(state_file, 'r') as f:
                        metadata = json.load(f)
                except (json.JSONDecodeError, IOError):
                    continue

                title = metadata.get('title', '')
                year = metadata.get('year', '').strip()
                has_year = bool(re.match(r'^\d{4}$', year))
                preview_path = metadata.get('preview_path', '').strip()
                has_preview = bool(preview_path) and os.path.isfile(preview_path)

                needs_id = metadata.get('needs_identification', Identifier.is_generic_title(title))
                # Show if explicitly flagged, or has a preview but missing a year
                if needs_id or (has_preview and not has_year):
                    pending.append({
                        "state_file": os.path.basename(state_file),
                        "state": state,
                        "metadata": metadata,
                        "mtime": os.path.getmtime(state_file)
                    })

        return sorted(pending, key=lambda x: x["mtime"])

    @staticmethod
    def sanitize_filename(name):
        """Sanitize string for use in filenames.

        Args:
            name: String to sanitize.

        Returns:
            str: Sanitized filename.
        """
        # Replace special characters with underscore
        sanitized = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
        # Collapse multiple underscores
        sanitized = re.sub(r'__+', '_', sanitized)
        # Remove leading/trailing underscores
        return sanitized.strip('_')

    @staticmethod
    def generate_plex_filename(title, year, extension):
        """Generate Plex-compatible filename like 'The Matrix (1999).mkv'.

        Args:
            title: Movie title.
            year: Release year.
            extension: File extension.

        Returns:
            str: Plex-compatible filename.
        """
        # Clean title (replace underscores with spaces, title case)
        clean = title.replace('_', ' ')
        clean = ' '.join(word.capitalize() for word in clean.split())

        if year and re.match(r'^\d{4}$', str(year)):
            return f"{clean} ({year}).{extension}"
        return f"{clean}.{extension}"

    @staticmethod
    def read_nas_config():
        """Read NAS configuration from config file.

        Returns:
            dict: NAS host, user, and path.
        """
        config = ConfigManager.read(mask_sensitive=False)
        return {
            "host": config.get("NAS_HOST", ""),
            "user": config.get("NAS_USER", ""),
            "path": config.get("NAS_PATH", ""),
            "ssh_identity": config.get("NAS_SSH_IDENTITY", "")
        }

    @staticmethod
    def rename_remote_file(nas_host, nas_user, old_path, new_path,
                           ssh_identity=""):
        """Rename a file on the NAS via SSH.

        Args:
            nas_host: NAS hostname.
            nas_user: SSH username.
            old_path: Current file path on NAS.
            new_path: New file path on NAS.
            ssh_identity: Optional path to SSH private key.

        Returns:
            tuple: (success, message)
        """
        try:
            cmd = ["ssh"]
            if ssh_identity and os.path.isfile(ssh_identity):
                cmd += ["-i", ssh_identity]
                # Use dvd-transfer's known_hosts alongside the key
                key_dir = os.path.dirname(ssh_identity)
                known_hosts = os.path.join(key_dir, "known_hosts")
                if os.path.isfile(known_hosts):
                    cmd += ["-o", f"UserKnownHostsFile={known_hosts}"]
            cmd += [f"{nas_user}@{nas_host}", f'mv "{old_path}" "{new_path}"']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.returncode == 0, result.stderr.strip() or "OK"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def rename_item(state_file_path, new_title, new_year):
        """Rename an item's files and update metadata.

        Steps:
        1. Read current metadata
        2. Generate new filenames
        3. Rename MKV, ISO, preview files (local or remote)
        4. Update metadata with new paths
        5. Create new state file with updated metadata
        6. Remove old state file

        Args:
            state_file_path: Path to the state file.
            new_title: New title for the item.
            new_year: Optional release year.

        Returns:
            str: Basename of the new state file.
        """
        # Read current metadata
        with open(state_file_path, 'r') as f:
            metadata = json.load(f)

        old_title = metadata.get('title', '')
        timestamp = metadata.get('timestamp', '')
        year = metadata.get('year', '')
        main_title = metadata.get('main_title', '')
        state = os.path.basename(state_file_path).rsplit('.', 1)[-1]

        # Generate new sanitized title for state files
        new_sanitized = Identifier.sanitize_filename(new_title)

        # Current paths
        old_mkv = metadata.get('mkv_path', '')
        old_iso = metadata.get('iso_path', '')
        old_preview = metadata.get('preview_path', '')
        old_nas = metadata.get('nas_path', '')

        # Initialize new paths
        new_mkv = old_mkv
        new_iso = old_iso
        new_preview = old_preview
        new_nas = old_nas

        # Rename local MKV/MP4 if exists
        if old_mkv and os.path.exists(old_mkv):
            mkv_ext = old_mkv.rsplit('.', 1)[-1] if '.' in old_mkv else 'mkv'
            new_mkv_name = Identifier.generate_plex_filename(new_title, new_year, mkv_ext)
            new_mkv = os.path.join(STAGING_DIR, new_mkv_name)
            os.rename(old_mkv, new_mkv)

        # Rename rip if exists (ISO file from ddrescue, or directory from dvdbackup)
        if old_iso and os.path.exists(old_iso):
            if os.path.isdir(old_iso):
                # dvdbackup: directory with no extension
                new_iso = os.path.join(STAGING_DIR, f"{new_sanitized}-{timestamp}")
            else:
                # ddrescue: .iso file
                new_iso = os.path.join(STAGING_DIR, f"{new_sanitized}-{timestamp}.iso")
            os.rename(old_iso, new_iso)

            # Rename .archive-ready marker if it exists
            old_marker = old_iso + ".archive-ready"
            if os.path.exists(old_marker):
                os.rename(old_marker, new_iso + ".archive-ready")

        # Rename preview if exists
        if old_preview and os.path.exists(old_preview):
            new_preview = os.path.join(STAGING_DIR, f"{new_sanitized}-{timestamp}.preview.mp4")
            os.rename(old_preview, new_preview)

        # Rename NAS file if transferred
        if state == "transferred" and old_nas:
            nas_config = Identifier.read_nas_config()
            if nas_config["host"] and nas_config["user"]:
                nas_ext = old_nas.rsplit('.', 1)[-1] if '.' in old_nas else 'mkv'
                new_nas_name = Identifier.generate_plex_filename(new_title, new_year, nas_ext)
                nas_dir = os.path.dirname(old_nas)
                new_nas = os.path.join(nas_dir, new_nas_name)
                success, msg = Identifier.rename_remote_file(
                    nas_config["host"], nas_config["user"], old_nas, new_nas,
                    ssh_identity=nas_config.get("ssh_identity", "")
                )
                if not success:
                    raise Exception(f"Failed to rename on NAS: {msg}")

        # Build updated metadata
        new_metadata = {
            "title": new_sanitized,
            "year": new_year or year,
            "timestamp": timestamp,
            "main_title": main_title,
            "iso_path": new_iso,
            "mkv_path": new_mkv,
            "preview_path": new_preview,
            "nas_path": new_nas,
            "needs_identification": False,
            "identified_at": datetime.now().isoformat(),
            "original_title": old_title,
            "created_at": metadata.get("created_at", "")
        }

        # Create new state file
        new_state_file = os.path.join(STAGING_DIR, f"{new_sanitized}-{timestamp}.{state}")

        with open(new_state_file, 'w') as f:
            json.dump(new_metadata, f, indent=2)

        # Remove old state file (if different)
        if state_file_path != new_state_file:
            os.remove(state_file_path)

        return os.path.basename(new_state_file)
