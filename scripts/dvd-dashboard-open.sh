#!/bin/bash
# Open DVD Dashboard in Chrome
# Usage: dvd-dashboard-open.sh [page] [--kiosk] [--new-display]
#
# Pages: dashboard, status, logs, config, identify, architecture
# Options:
#   --kiosk        Run in kiosk mode (fullscreen, no chrome)
#   --new-display  Start new X display (for use from TTY)

set -euo pipefail

DASHBOARD_HOST="${DASHBOARD_HOST:-localhost}"
DASHBOARD_PORT="${DASHBOARD_PORT:-5000}"
BASE_URL="http://${DASHBOARD_HOST}:${DASHBOARD_PORT}"

# Parse arguments
PAGE=""
KIOSK=""
NEW_DISPLAY=""

usage() {
    echo "Usage: dvd-dashboard-open.sh [page] [--kiosk] [--new-display]"
    echo ""
    echo "Open DVD Dashboard in Chrome"
    echo ""
    echo "Pages:"
    echo "  dashboard     Main dashboard (default)"
    echo "  status        Service status and controls"
    echo "  logs          Log viewer"
    echo "  config        Configuration"
    echo "  identify      Pending identification"
    echo "  architecture  Technical architecture"
    echo "  health        System health"
    echo ""
    echo "Options:"
    echo "  --kiosk        Fullscreen kiosk mode"
    echo "  --new-display  Start new X display (for use from TTY)"
    echo ""
    echo "Examples:"
    echo "  dvd-dashboard-open.sh                    # Open main dashboard"
    echo "  dvd-dashboard-open.sh status             # Open status page"
    echo "  dvd-dashboard-open.sh --kiosk            # Fullscreen dashboard"
    echo "  dvd-dashboard-open.sh --new-display      # From TTY, start X + browser"
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            usage
            ;;
        --kiosk)
            KIOSK="--kiosk"
            ;;
        --new-display)
            NEW_DISPLAY="1"
            ;;
        dashboard|status|logs|config|identify|architecture|health)
            PAGE="/$arg"
            ;;
        /*)
            PAGE="$arg"
            ;;
        *)
            echo "Unknown argument: $arg"
            usage
            ;;
    esac
done

# Main page is root
[[ "$PAGE" == "/dashboard" ]] && PAGE=""

URL="${BASE_URL}${PAGE}"

# Check if dashboard is running
if ! curl -s --connect-timeout 2 "$BASE_URL" > /dev/null 2>&1; then
    echo "Warning: Dashboard not responding at $BASE_URL"
    echo "Start it with: sudo systemctl start dvd-dashboard"
fi

# Find Chrome
CHROME=""
for browser in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$browser" &> /dev/null; then
        CHROME="$browser"
        break
    fi
done

if [[ -z "$CHROME" ]]; then
    echo "Error: Chrome/Chromium not found"
    exit 1
fi

echo "Opening $URL"

if [[ -n "$NEW_DISPLAY" ]]; then
    # Start new X display with just the browser
    exec startx "$CHROME" $KIOSK "$URL" -- :1 2>/dev/null
else
    # Use existing display
    exec "$CHROME" $KIOSK "$URL" 2>/dev/null &
fi
