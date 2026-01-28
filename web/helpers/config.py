"""Configuration management for DVD ripper dashboard."""
import os

# Configuration file path
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/dvd-ripper.conf")

# Config sections for grouped display
CONFIG_SECTIONS = [
    {
        "id": "storage",
        "title": "Storage & Logging",
        "keys": ["STAGING_DIR", "LOG_FILE", "LOG_LEVEL", "DISK_USAGE_THRESHOLD"]
    },
    {
        "id": "device",
        "title": "DVD Device",
        "keys": ["DVD_DEVICE", "DEVICE_TIMEOUT"]
    },
    {
        "id": "pipeline",
        "title": "Pipeline Mode",
        "keys": ["PIPELINE_MODE", "CREATE_ISO", "ENCODE_VIDEO", "RIP_METHOD"]
    },
    {
        "id": "handbrake",
        "title": "HandBrake Encoding",
        "keys": ["HANDBRAKE_QUALITY", "HANDBRAKE_ENCODER", "HANDBRAKE_FORMAT", "HANDBRAKE_EXTRA_OPTS", "MIN_FILE_SIZE_MB"]
    },
    {
        "id": "parallel",
        "title": "Parallel Encoding",
        "keys": ["ENABLE_PARALLEL_ENCODING", "MAX_PARALLEL_ENCODERS", "ENCODER_LOAD_THRESHOLD"]
    },
    {
        "id": "preview",
        "title": "Preview Generation",
        "keys": ["GENERATE_PREVIEWS", "PREVIEW_DURATION", "PREVIEW_START_PERCENT", "PREVIEW_RESOLUTION"]
    },
    {
        "id": "nas",
        "title": "NAS Transfer",
        "keys": ["NAS_ENABLED", "NAS_HOST", "NAS_USER", "NAS_PATH", "NAS_TRANSFER_METHOD", "NAS_FILE_OWNER"]
    },
    {
        "id": "transfer",
        "title": "Transfer Mode",
        "keys": ["TRANSFER_MODE", "LOCAL_LIBRARY_PATH"]
    },
    {
        "id": "cluster",
        "title": "Cluster Mode",
        "keys": ["CLUSTER_ENABLED", "CLUSTER_NODE_NAME", "CLUSTER_PEERS", "CLUSTER_SSH_USER", "CLUSTER_REMOTE_STAGING"]
    },
    {
        "id": "cleanup",
        "title": "Cleanup Settings",
        "keys": ["CLEANUP_MKV_AFTER_TRANSFER", "CLEANUP_ISO_AFTER_TRANSFER", "CLEANUP_PREVIEW_AFTER_TRANSFER"]
    },
    {
        "id": "retry",
        "title": "Retry & Locks",
        "keys": ["MAX_RETRIES", "RETRY_DELAY", "LOCK_FILE", "ISO_LOCK_FILE", "ENCODER_LOCK_FILE", "TRANSFER_LOCK_FILE"]
    }
]

# Settings that use toggle switches (0/1 values)
BOOLEAN_SETTINGS = {
    "NAS_ENABLED", "PIPELINE_MODE", "CREATE_ISO", "ENCODE_VIDEO",
    "ENABLE_PARALLEL_ENCODING", "GENERATE_PREVIEWS", "CLUSTER_ENABLED",
    "CLEANUP_MKV_AFTER_TRANSFER", "CLEANUP_ISO_AFTER_TRANSFER", "CLEANUP_PREVIEW_AFTER_TRANSFER"
}

# Settings with dropdown options
DROPDOWN_SETTINGS = {
    "LOG_LEVEL": ["DEBUG", "INFO", "WARN", "ERROR"],
    "NAS_TRANSFER_METHOD": ["rsync", "scp"],
    "TRANSFER_MODE": ["remote", "local"],
    "HANDBRAKE_ENCODER": ["x265", "x264"],
    "HANDBRAKE_FORMAT": ["mkv", "mp4"],
    "RIP_METHOD": ["ddrescue", "dvdbackup"]
}


class ConfigManager:
    """Manages reading and writing of configuration files."""

    @staticmethod
    def read(mask_sensitive=True):
        """Read and parse config file.

        Args:
            mask_sensitive: If True, mask sensitive values like NAS credentials.

        Returns:
            dict: Configuration key-value pairs.
        """
        config = {}
        sensitive_keys = {"NAS_USER", "NAS_HOST", "NAS_PATH"}
        try:
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if mask_sensitive and key in sensitive_keys and value:
                            config[key] = value[:3] + "***"
                        else:
                            config[key] = value
        except Exception:
            pass
        return config

    @staticmethod
    def read_full():
        """Read and parse config file without masking - for editing."""
        return ConfigManager.read(mask_sensitive=False)

    @staticmethod
    def write(new_settings):
        """Write config file, preserving comments and structure.

        Args:
            new_settings: dict of key-value pairs to update.

        Returns:
            tuple: (success: bool, changed_keys: list, message: str)
        """
        try:
            # Read existing file to preserve structure and comments
            with open(CONFIG_FILE, 'r') as f:
                lines = f.readlines()

            # Track which keys we've updated and original values
            old_config = ConfigManager.read_full()
            changed_keys = []
            updated_keys = set()

            # Update values in-place
            new_lines = []
            for line in lines:
                stripped = line.strip()

                # Check if this is a config line (not comment, not empty, has =)
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, _ = stripped.partition("=")
                    key = key.strip()

                    if key in new_settings:
                        new_value = new_settings[key]
                        old_value = old_config.get(key, "")

                        # Validate: no newlines allowed in values
                        if "\n" in str(new_value) or "\r" in str(new_value):
                            return False, [], f"Invalid value for {key}: newlines not allowed"

                        # Check if value actually changed
                        if str(new_value) != str(old_value):
                            changed_keys.append(key)

                        # Write the updated line with proper quoting
                        new_lines.append(f'{key}="{new_value}"\n')
                        updated_keys.add(key)
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            # Add any new keys that weren't in the file
            for key, value in new_settings.items():
                if key not in updated_keys:
                    # Validate
                    if "\n" in str(value) or "\r" in str(value):
                        return False, [], f"Invalid value for {key}: newlines not allowed"
                    new_lines.append(f'{key}="{value}"\n')
                    if key not in old_config or str(value) != str(old_config.get(key, "")):
                        changed_keys.append(key)

            # Write back to file
            with open(CONFIG_FILE, 'w') as f:
                f.writelines(new_lines)

            return True, changed_keys, f"Saved {len(changed_keys)} change(s)"

        except PermissionError:
            return False, [], "Permission denied - config file requires root access"
        except Exception as e:
            return False, [], f"Failed to save config: {str(e)}"

    @staticmethod
    def get_restart_recommendations(changed_keys):
        """Determine which services should be restarted based on changed settings.

        Args:
            changed_keys: List of config keys that were changed.

        Returns:
            list of dicts with service info: [{"name": "dvd-encoder", "type": "timer"}, ...]
        """
        recommendations = []
        seen = set()

        # Mapping of config key patterns to affected services
        patterns = {
            # Encoder-related settings
            "HANDBRAKE_": [{"name": "dvd-encoder", "type": "timer"}],
            "ENABLE_PARALLEL_": [{"name": "dvd-encoder", "type": "timer"}],
            "MAX_PARALLEL_": [{"name": "dvd-encoder", "type": "timer"}],
            "ENCODER_LOAD_": [{"name": "dvd-encoder", "type": "timer"}],
            "PREVIEW_": [{"name": "dvd-encoder", "type": "timer"}],
            "GENERATE_PREVIEWS": [{"name": "dvd-encoder", "type": "timer"}],
            "MIN_FILE_SIZE": [{"name": "dvd-encoder", "type": "timer"}],

            # Transfer-related settings
            "NAS_": [{"name": "dvd-transfer", "type": "timer"}],
            "TRANSFER_MODE": [{"name": "dvd-transfer", "type": "timer"}],
            "LOCAL_LIBRARY_": [{"name": "dvd-transfer", "type": "timer"}],
            "CLEANUP_": [{"name": "dvd-transfer", "type": "timer"}],

            # Cluster settings affect both
            "CLUSTER_": [
                {"name": "dvd-encoder", "type": "timer"},
                {"name": "dvd-transfer", "type": "timer"}
            ],

            # Core settings affect everything
            "STAGING_DIR": [
                {"name": "dvd-encoder", "type": "timer"},
                {"name": "dvd-transfer", "type": "timer"},
                {"name": "dvd-dashboard", "type": "service"}
            ],
            "LOG_": [
                {"name": "dvd-encoder", "type": "timer"},
                {"name": "dvd-transfer", "type": "timer"}
            ],
        }

        for key in changed_keys:
            for pattern, services in patterns.items():
                if key.startswith(pattern) or key == pattern:
                    for svc in services:
                        svc_key = f"{svc['name']}:{svc['type']}"
                        if svc_key not in seen:
                            recommendations.append(svc)
                            seen.add(svc_key)

        return recommendations
