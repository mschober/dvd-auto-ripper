#!/bin/bash
# Control the DVD ripper web dashboard service
# Usage: dvd-dashboard-ctl.sh [start|stop|restart|status]

set -euo pipefail

SERVICE="dvd-dashboard.service"

usage() {
    echo "Usage: $0 {start|stop|restart|status}"
    echo ""
    echo "Commands:"
    echo "  start    Start the web dashboard"
    echo "  stop     Stop the web dashboard"
    echo "  restart  Restart the web dashboard"
    echo "  status   Show dashboard status"
    exit 1
}

start_dashboard() {
    echo "Starting DVD dashboard..."
    if systemctl start "$SERVICE"; then
        echo "Dashboard started successfully"
        show_url
    else
        echo "Failed to start dashboard"
        exit 1
    fi
}

stop_dashboard() {
    echo "Stopping DVD dashboard..."
    if systemctl stop "$SERVICE"; then
        echo "Dashboard stopped"
    else
        echo "Failed to stop dashboard"
        exit 1
    fi
}

restart_dashboard() {
    echo "Restarting DVD dashboard..."
    if systemctl restart "$SERVICE"; then
        echo "Dashboard restarted successfully"
        show_url
    else
        echo "Failed to restart dashboard"
        exit 1
    fi
}

show_status() {
    echo "DVD Dashboard Status"
    echo "===================="
    systemctl status "$SERVICE" --no-pager 2>/dev/null || echo "Service not found or not running"
}

show_url() {
    # Try to get the dashboard URL from the service
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [[ -n "$ip" ]]; then
        echo ""
        echo "Dashboard URL: http://${ip}:5000"
    fi
}

# Main
if [[ $# -lt 1 ]]; then
    usage
fi

case "$1" in
    start)
        start_dashboard
        ;;
    stop)
        stop_dashboard
        ;;
    restart)
        restart_dashboard
        ;;
    status)
        show_status
        ;;
    *)
        usage
        ;;
esac
