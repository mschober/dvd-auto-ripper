#!/bin/bash
# Pause DVD auto-detection (disable udev rule)
# Usage: sudo dvd-ripper-trigger-pause.sh
#
# This allows inserting discs without triggering the pipeline,
# useful for debugging or manual operations.

set -euo pipefail

UDEV_RULE="/etc/udev/rules.d/99-dvd-ripper.rules"
UDEV_DISABLED="${UDEV_RULE}.disabled"

if [[ ! -f "$UDEV_RULE" ]]; then
    if [[ -f "$UDEV_DISABLED" ]]; then
        echo "Already paused (udev rule disabled)"
        exit 0
    else
        echo "Error: udev rule not found at $UDEV_RULE"
        exit 1
    fi
fi

mv "$UDEV_RULE" "$UDEV_DISABLED"
udevadm control --reload-rules

echo "DVD auto-detection paused."
echo "Insert disc manually and run commands as needed."
echo ""
echo "To resume: dvd-ripper-trigger-resume.sh"
