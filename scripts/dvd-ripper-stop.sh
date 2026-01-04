#!/bin/bash
# Stop all DVD ripper processes
# Usage: sudo dvd-ripper-stop.sh

set -euo pipefail

echo "Stopping DVD ripper processes..."

# Kill HandBrake first (child process)
if pkill -9 -f HandBrakeCLI 2>/dev/null; then
    echo "  Killed HandBrakeCLI"
fi

# Kill ddrescue if running
if pkill -9 -f ddrescue 2>/dev/null; then
    echo "  Killed ddrescue"
fi

# Kill dvd-ripper script
if pkill -9 -f dvd-ripper.sh 2>/dev/null; then
    echo "  Killed dvd-ripper.sh"
fi

# Stop any systemd transient services
for svc in $(systemctl list-units --type=service --state=running --no-legend | grep -E 'run-.*dvd-ripper|dvd-ripper@' | awk '{print $1}'); do
    echo "  Stopping $svc"
    systemctl stop "$svc" 2>/dev/null || true
done

# Clean up stale files
rm -f /var/run/dvd-ripper.pid 2>/dev/null && echo "  Removed PID file"
rm -f /var/run/dvd-ripper.lock 2>/dev/null && echo "  Removed lock file"

# Reset failed units
systemctl reset-failed 2>/dev/null || true

echo "Done."

# Show remaining processes (should be none)
remaining=$(ps aux | grep -E 'dvd-ripper|HandBrake' | grep -v grep | grep -v dvd-ripper-stop || true)
if [[ -n "$remaining" ]]; then
    echo ""
    echo "Warning: Some processes may still be running:"
    echo "$remaining"
else
    echo "All DVD ripper processes stopped."
fi
