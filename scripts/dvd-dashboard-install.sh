#!/bin/bash
# Install/Update the DVD Ripper Web Dashboard
# Usage: sudo ./dvd-dashboard-install.sh [SOURCE_DIR]
#
# This script installs the web dashboard and restarts the service if running.
# Called by remote-install.sh or can be run standalone.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
print_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
print_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Installation paths
INSTALL_BIN="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="dvd-dashboard.service"

# Source directory (where this script is located, or passed as argument)
if [[ $# -ge 1 ]]; then
    SOURCE_DIR="$1"
else
    # Default to parent of scripts directory
    SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

DASHBOARD_SOURCE="$SOURCE_DIR/web/dvd-dashboard.py"
SERVICE_SOURCE="$SOURCE_DIR/config/dvd-dashboard.service"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    print_error "This script must be run as root (use sudo)"
    exit 1
fi

# Check if dashboard source exists
if [[ ! -f "$DASHBOARD_SOURCE" ]]; then
    print_error "Dashboard source not found: $DASHBOARD_SOURCE"
    exit 1
fi

# Check if Flask is installed
if ! python3 -c "import flask" 2>/dev/null; then
    print_info "Installing Flask..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y python3-flask >/dev/null 2>&1 || pip3 install flask
    elif command -v pip3 &>/dev/null; then
        pip3 install flask
    else
        print_error "Cannot install Flask - please install manually: pip3 install flask"
        exit 1
    fi
fi

# Get version from source file
DASHBOARD_VERSION=$(grep -oP 'DASHBOARD_VERSION = "\K[^"]+' "$DASHBOARD_SOURCE" 2>/dev/null || echo "unknown")

# Check if service is currently running
SERVICE_WAS_RUNNING=false
if systemctl is-active "$SERVICE_NAME" &>/dev/null; then
    SERVICE_WAS_RUNNING=true
    print_info "Dashboard service is running, will restart after install"
fi

# Install dashboard script
print_info "Installing dvd-dashboard.py to $INSTALL_BIN..."
cp "$DASHBOARD_SOURCE" "$INSTALL_BIN/dvd-dashboard.py"
chmod 755 "$INSTALL_BIN/dvd-dashboard.py"

# Install systemd service if source exists
if [[ -f "$SERVICE_SOURCE" ]]; then
    print_info "Installing systemd service..."
    cp "$SERVICE_SOURCE" "$SYSTEMD_DIR/"
    chmod 644 "$SYSTEMD_DIR/$SERVICE_NAME"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
fi

# Start or restart the service
if [[ "$SERVICE_WAS_RUNNING" == "true" ]]; then
    print_info "Restarting dashboard service..."
    systemctl restart "$SERVICE_NAME"
else
    print_info "Starting dashboard service..."
    systemctl start "$SERVICE_NAME"
fi

# Verify service started
if systemctl is-active "$SERVICE_NAME" &>/dev/null; then
    local_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    print_info "Web dashboard v${DASHBOARD_VERSION} installed and running"
    print_info "Dashboard URL: http://${local_ip:-localhost}:5000"
else
    print_error "Dashboard service failed to start"
    print_error "Check logs: journalctl -u $SERVICE_NAME -n 20"
    exit 1
fi
