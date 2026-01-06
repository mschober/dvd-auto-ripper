#!/bin/bash
# Start/resume DVD ripper services
# Usage: sudo dvd-ripper-start.sh

set -euo pipefail

echo "Starting DVD ripper services..."

# Reload systemd in case of changes
systemctl daemon-reload

# Start and enable timers
if systemctl start dvd-encoder.timer 2>/dev/null; then
    echo "  Started dvd-encoder.timer"
else
    echo "  dvd-encoder.timer already running or failed"
fi

if systemctl start dvd-transfer.timer 2>/dev/null; then
    echo "  Started dvd-transfer.timer"
else
    echo "  dvd-transfer.timer already running or failed"
fi

# Start web dashboard if installed
if systemctl start dvd-dashboard.service 2>/dev/null; then
    echo "  Started dvd-dashboard.service"
else
    echo "  dvd-dashboard.service not available or failed"
fi

# Reload udev rules to ensure disc insertion triggers work
udevadm control --reload-rules 2>/dev/null && echo "  Reloaded udev rules"

echo ""
echo "Done. Services status:"
systemctl list-timers --no-pager | grep -E 'dvd|NEXT' || echo "  No DVD timers found"

echo ""
echo "To manually trigger stages:"
echo "  systemctl start dvd-encoder.service   # Encode now"
echo "  systemctl start dvd-transfer.service  # Transfer now"
