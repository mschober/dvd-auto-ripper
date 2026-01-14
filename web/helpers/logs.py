"""Log file reading for DVD ripper dashboard."""
import os
import subprocess

# Log configuration
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/dvd-ripper")
LOG_FILES = {
    "iso": f"{LOG_DIR}/iso.log",
    "encoder": f"{LOG_DIR}/encoder.log",
    "transfer": f"{LOG_DIR}/transfer.log",
    "distribute": f"{LOG_DIR}/distribute.log",
}


class LogReader:
    """Reads and manages log files for the pipeline stages."""

    @staticmethod
    def get_stage_logs(stage, lines=100):
        """Read last N lines from a stage-specific log file.

        Also checks rotated log files in case a process is still writing
        to the old (rotated) file handle after logrotate runs.

        Args:
            stage: The stage name (e.g., "iso", "encoder").
            lines: Number of lines to read.

        Returns:
            str: Log content or error message.
        """
        log_file = LOG_FILES.get(stage)
        if not log_file:
            return f"(unknown stage: {stage})"

        try:
            if not os.path.exists(log_file):
                return "(no logs yet)"

            # Get main log mtime
            try:
                main_mtime = os.path.getmtime(log_file)
            except OSError:
                main_mtime = 0

            # Check for rotated log files that may be more recently modified
            log_base = os.path.basename(log_file)
            try:
                for f in os.listdir(LOG_DIR):
                    if f.startswith(log_base) and f != log_base and not f.endswith('.gz'):
                        full_path = os.path.join(LOG_DIR, f)
                        try:
                            mtime = os.path.getmtime(full_path)
                            if mtime > main_mtime:
                                log_file = full_path
                                main_mtime = mtime
                        except OSError:
                            pass
            except OSError:
                pass

            result = subprocess.run(
                ["tail", "-n", str(lines), log_file],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout or "(no logs)"
        except Exception:
            return "(unable to read log file)"

    @staticmethod
    def get_all_logs(lines=50):
        """Read last N lines from all stage log files combined.

        Returns logs from all stages merged and sorted by timestamp.

        Args:
            lines: Number of lines to return.

        Returns:
            str: Combined log content.
        """
        all_lines = []
        for stage, log_file in LOG_FILES.items():
            try:
                if os.path.exists(log_file):
                    result = subprocess.run(
                        ["tail", "-n", str(lines), log_file],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.stdout:
                        all_lines.extend(result.stdout.strip().split('\n'))
            except Exception:
                pass

        # Sort by timestamp (logs start with [YYYY-MM-DD HH:MM:SS])
        all_lines.sort()
        # Return last N lines
        return '\n'.join(all_lines[-lines:]) if all_lines else "(no logs)"

    @staticmethod
    def get_recent_logs(lines=50):
        """Legacy function - returns combined logs from all stages."""
        return LogReader.get_all_logs(lines)
