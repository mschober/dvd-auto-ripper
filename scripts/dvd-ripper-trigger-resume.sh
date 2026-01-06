#!/bin/bash
# Resume DVD auto-detection (re-enable udev rule)
# Usage: sudo dvd-ripper-trigger-resume.sh

set -euo pipefail

UDEV_RULE="/etc/udev/rules.d/99-dvd-ripper.rules"
UDEV_DISABLED="${UDEV_RULE}.disabled"

if [[ ! -f "$UDEV_DISABLED" ]]; then
    if [[ -f "$UDEV_RULE" ]]; then
        echo "Already active (udev rule enabled)"
        exit 0
    else
        echo "Error: udev rule not found"
        echo "Run remote-install.sh to reinstall"
        exit 1
    fi
fi

mv "$UDEV_DISABLED" "$UDEV_RULE"
udevadm control --reload-rules

echo "DVD auto-detection resumed."
echo "Disc insertion will now trigger the pipeline."
