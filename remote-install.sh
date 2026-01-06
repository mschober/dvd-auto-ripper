#!/bin/bash
# Remote Installation Script for DVD Ripper
# Run this script with sudo on the remote server after deploying files
# Usage: sudo ./remote-install.sh [--force-config]
#
# Options:
#   --force-config       Overwrite existing configuration file (creates backup)
#   --install-libdvdcss  Install libdvdcss for encrypted DVD support (Debian/Ubuntu)

# ==============================================================================
# DEFAULT INSTALLATION PATHS
# ==============================================================================
# This script installs the DVD ripper to the following default locations:
#
# Executables:
#   /usr/local/bin/dvd-ripper.sh    - Main orchestration script
#   /usr/local/bin/dvd-utils.sh     - Shared utility library
#
# Configuration:
#   /etc/dvd-ripper.conf            - Main configuration file
#   /etc/logrotate.d/dvd-ripper     - Logrotate configuration
#
# Runtime files:
#   /var/tmp/dvd-rips/              - Staging directory for ripped files
#   /var/log/dvd-ripper.log         - Application log file
#   /var/run/dvd-ripper.pid         - PID file (while ripping)
#   /var/run/dvd-ripper.lock        - Lock file (while ripping)
#
# Udev integration:
#   /etc/udev/rules.d/*.rules       - Your existing udev rule (not managed)
#                                     Should call: /usr/local/bin/dvd-ripper.sh
# ==============================================================================

set -euo pipefail

# Parse command line arguments
FORCE_CONFIG=false
INSTALL_LIBDVDCSS=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --force-config)
            FORCE_CONFIG=true
            shift
            ;;
        --install-libdvdcss)
            INSTALL_LIBDVDCSS=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: sudo ./remote-install.sh [--force-config] [--install-libdvdcss]"
            exit 1
            ;;
    esac
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Installation paths
INSTALL_BIN="/usr/local/bin"
INSTALL_CONFIG="/etc"
INSTALL_LOGROTATE="/etc/logrotate.d"

# Script directory (where this script is located on remote server)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================================
# Helper Functions
# ============================================================================

print_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        print_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

install_libdvdcss() {
    print_info "Installing libdvdcss for encrypted DVD support..."

    # Detect distribution
    if [[ -f /etc/debian_version ]]; then
        # Debian/Ubuntu
        print_info "Detected Debian/Ubuntu system"

        apt-get update -qq

        if apt-get install -y libdvd-pkg; then
            print_info "Running dpkg-reconfigure to build libdvdcss..."
            # Run non-interactively
            DEBIAN_FRONTEND=noninteractive dpkg-reconfigure libdvd-pkg
            print_info "✓ libdvdcss installed successfully"
        else
            print_error "Failed to install libdvd-pkg"
            print_info "You may need to enable contrib/non-free repositories"
            return 1
        fi
    elif [[ -f /etc/redhat-release ]]; then
        # RHEL/CentOS/Fedora
        print_info "Detected RHEL/CentOS/Fedora system"
        print_warn "Please ensure RPM Fusion repository is enabled"

        if yum install -y libdvdcss; then
            print_info "✓ libdvdcss installed successfully"
        else
            print_error "Failed to install libdvdcss"
            print_info "Enable RPM Fusion: https://rpmfusion.org/Configuration"
            return 1
        fi
    else
        print_error "Unsupported distribution for automatic libdvdcss installation"
        print_info "Please install libdvdcss manually for your distribution"
        return 1
    fi
}

check_dependencies() {
    local missing_deps=()

    print_info "Checking dependencies..."

    # Required dependencies
    local deps=("HandBrakeCLI" "rsync" "ssh" "eject" "ffmpeg" "ddrescue" "python3")

    for cmd in "${deps[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            missing_deps+=("$cmd")
        fi
    done

    if [[ ${#missing_deps[@]} -gt 0 ]]; then
        print_error "Missing required dependencies: ${missing_deps[*]}"
        print_info ""
        print_info "Install them with:"
        print_info "  Debian/Ubuntu:"
        print_info "    sudo apt-get install handbrake-cli rsync openssh-client eject ffmpeg gddrescue"
        print_info ""
        print_info "  RHEL/CentOS/Fedora:"
        print_info "    sudo yum install handbrake-cli rsync openssh-clients eject ffmpeg ddrescue"
        print_info ""
        exit 1
    fi

    # Check for libdvdcss (needed for encrypted DVDs)
    if ! ldconfig -p | grep -q libdvdcss; then
        if [[ "$INSTALL_LIBDVDCSS" == "true" ]]; then
            install_libdvdcss || print_warn "libdvdcss installation failed - continuing anyway"
            # Refresh library cache
            ldconfig
        else
            print_warn "⚠ libdvdcss not found - encrypted DVDs will not work"
            print_info ""
            print_info "To rip commercial/encrypted DVDs, either:"
            print_info "  1. Re-run with --install-libdvdcss flag:"
            print_info "     sudo ./remote-install.sh --install-libdvdcss"
            print_info ""
            print_info "  2. Or install manually:"
            print_info "     Debian/Ubuntu:"
            print_info "       sudo apt-get install libdvd-pkg"
            print_info "       sudo dpkg-reconfigure libdvd-pkg"
            print_info ""
            print_info "     RHEL/CentOS/Fedora:"
            print_info "       # Enable RPM Fusion repository first"
            print_info "       sudo yum install libdvdcss"
            print_info ""
        fi
    else
        print_info "✓ libdvdcss found - encrypted DVD support available"
    fi

    print_info "✓ All dependencies satisfied"
}

install_scripts() {
    print_info "Installing scripts to $INSTALL_BIN..."

    # Check if source scripts exist
    if [[ ! -f "$SCRIPT_DIR/scripts/dvd-ripper.sh" ]]; then
        print_error "Source scripts not found in $SCRIPT_DIR/scripts/"
        print_error "Did you run deploy.sh first?"
        exit 1
    fi

    # Copy main scripts (legacy monolithic mode)
    cp "$SCRIPT_DIR/scripts/dvd-ripper.sh" "$INSTALL_BIN/"
    cp "$SCRIPT_DIR/scripts/dvd-utils.sh" "$INSTALL_BIN/"
    cp "$SCRIPT_DIR/scripts/dvd-ripper-stop.sh" "$INSTALL_BIN/"

    # Copy pipeline scripts (3-stage mode)
    cp "$SCRIPT_DIR/scripts/dvd-iso.sh" "$INSTALL_BIN/"
    cp "$SCRIPT_DIR/scripts/dvd-encoder.sh" "$INSTALL_BIN/"
    cp "$SCRIPT_DIR/scripts/dvd-transfer.sh" "$INSTALL_BIN/"

    # Copy VERSION file for pipeline version tracking
    if [[ -f "$SCRIPT_DIR/scripts/VERSION" ]]; then
        cp "$SCRIPT_DIR/scripts/VERSION" "$INSTALL_BIN/VERSION"
        chmod 644 "$INSTALL_BIN/VERSION"
        print_info "✓ Pipeline version: $(cat "$INSTALL_BIN/VERSION")"
    fi

    # Set permissions
    chmod 755 "$INSTALL_BIN/dvd-ripper.sh"
    chmod 755 "$INSTALL_BIN/dvd-ripper-stop.sh"
    chmod 755 "$INSTALL_BIN/dvd-iso.sh"
    chmod 755 "$INSTALL_BIN/dvd-encoder.sh"
    chmod 755 "$INSTALL_BIN/dvd-transfer.sh"
    chmod 644 "$INSTALL_BIN/dvd-utils.sh"

    print_info "✓ Scripts installed successfully"
    print_info "  - dvd-ripper.sh (legacy monolithic mode)"
    print_info "  - dvd-iso.sh (pipeline stage 1: ISO creation)"
    print_info "  - dvd-encoder.sh (pipeline stage 2: encoding)"
    print_info "  - dvd-transfer.sh (pipeline stage 3: NAS transfer)"
}

install_config() {
    print_info "Installing configuration..."

    local config_file="$INSTALL_CONFIG/dvd-ripper.conf"
    local config_source="$SCRIPT_DIR/config/dvd-ripper.conf.example"

    if [[ ! -f "$config_source" ]]; then
        print_error "Configuration source not found: $config_source"
        exit 1
    fi

    if [[ -f "$config_file" ]] && [[ "$FORCE_CONFIG" != "true" ]]; then
        print_warn "Configuration file already exists: $config_file"

        # Create backup
        local backup_file="${config_file}.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$config_file" "$backup_file"
        print_info "✓ Backed up existing configuration to: $backup_file"

        print_warn "Keeping existing configuration (not overwriting)"
        print_warn "Use --force-config flag to overwrite"
        return
    fi

    # If forcing config update and file exists, create backup first
    if [[ -f "$config_file" ]] && [[ "$FORCE_CONFIG" == "true" ]]; then
        local backup_file="${config_file}.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$config_file" "$backup_file"
        print_info "✓ Backed up existing configuration to: $backup_file"
        print_warn "Overwriting configuration file (--force-config enabled)"
    fi

    # Install new config
    cp "$config_source" "$config_file"
    chmod 644 "$config_file"

    print_info "✓ Configuration installed to: $config_file"
    print_warn ""
    print_warn "*** YOU MUST EDIT THIS FILE TO SET YOUR NAS DETAILS ***"
    print_warn "    sudo nano $config_file"
    print_warn ""
}

install_logrotate() {
    print_info "Installing logrotate configuration..."

    local logrotate_source="$SCRIPT_DIR/config/dvd-ripper.logrotate"

    if [[ ! -f "$logrotate_source" ]]; then
        print_error "Logrotate config not found: $logrotate_source"
        exit 1
    fi

    cp "$logrotate_source" "$INSTALL_LOGROTATE/dvd-ripper"
    chmod 644 "$INSTALL_LOGROTATE/dvd-ripper"

    print_info "✓ Logrotate configuration installed"
}

create_directories() {
    print_info "Creating required directories..."

    # Create staging directory
    local staging_dir="/var/tmp/dvd-rips"
    if [[ ! -d "$staging_dir" ]]; then
        mkdir -p "$staging_dir"
        chmod 755 "$staging_dir"
        print_info "✓ Created staging directory: $staging_dir"
    else
        print_info "✓ Staging directory already exists: $staging_dir"
        # Update permissions on existing directory
        chmod 755 "$staging_dir"
        print_info "✓ Updated staging directory permissions to be world-readable"
    fi

    # Create log file
    local log_file="/var/log/dvd-ripper.log"
    if [[ ! -f "$log_file" ]]; then
        touch "$log_file"
        chmod 644 "$log_file"
        print_info "✓ Created log file: $log_file"
    else
        print_info "✓ Log file already exists: $log_file"
    fi

    # Ensure run directory exists (usually does)
    if [[ ! -d "/var/run" ]]; then
        mkdir -p "/var/run"
    fi

    print_info "✓ Directories ready"
}

install_systemd_service() {
    print_info "Installing systemd service..."

    local service_source="$SCRIPT_DIR/config/dvd-ripper@.service"
    local service_dest="/etc/systemd/system/dvd-ripper@.service"

    if [[ ! -f "$service_source" ]]; then
        print_error "Systemd service file not found: $service_source"
        exit 1
    fi

    # Install service file
    cp "$service_source" "$service_dest"
    chmod 644 "$service_dest"

    # Reload systemd to recognize new service
    systemctl daemon-reload

    print_info "✓ Systemd service installed"
}

install_pipeline_timers() {
    print_info "Installing pipeline systemd timers..."

    local systemd_dir="/etc/systemd/system"

    # Install encoder service and timer
    if [[ -f "$SCRIPT_DIR/config/dvd-encoder.service" ]]; then
        cp "$SCRIPT_DIR/config/dvd-encoder.service" "$systemd_dir/"
        chmod 644 "$systemd_dir/dvd-encoder.service"
    else
        print_warn "dvd-encoder.service not found, skipping"
    fi

    if [[ -f "$SCRIPT_DIR/config/dvd-encoder.timer" ]]; then
        cp "$SCRIPT_DIR/config/dvd-encoder.timer" "$systemd_dir/"
        chmod 644 "$systemd_dir/dvd-encoder.timer"
    else
        print_warn "dvd-encoder.timer not found, skipping"
    fi

    # Install transfer service and timer
    if [[ -f "$SCRIPT_DIR/config/dvd-transfer.service" ]]; then
        cp "$SCRIPT_DIR/config/dvd-transfer.service" "$systemd_dir/"
        chmod 644 "$systemd_dir/dvd-transfer.service"
    else
        print_warn "dvd-transfer.service not found, skipping"
    fi

    if [[ -f "$SCRIPT_DIR/config/dvd-transfer.timer" ]]; then
        cp "$SCRIPT_DIR/config/dvd-transfer.timer" "$systemd_dir/"
        chmod 644 "$systemd_dir/dvd-transfer.timer"
    else
        print_warn "dvd-transfer.timer not found, skipping"
    fi

    # Reload systemd
    systemctl daemon-reload

    # Enable and start timers
    if [[ -f "$systemd_dir/dvd-encoder.timer" ]]; then
        systemctl enable dvd-encoder.timer
        systemctl start dvd-encoder.timer
        print_info "✓ dvd-encoder.timer enabled and started"
    fi

    if [[ -f "$systemd_dir/dvd-transfer.timer" ]]; then
        systemctl enable dvd-transfer.timer
        systemctl start dvd-transfer.timer
        print_info "✓ dvd-transfer.timer enabled and started"
    fi

    print_info "✓ Pipeline timers installed (run every 15 minutes)"
}

install_udev_rule() {
    print_info "Installing udev rule..."

    local udev_source="$SCRIPT_DIR/config/99-dvd-ripper.rules"
    local udev_dest="/etc/udev/rules.d/99-dvd-ripper.rules"

    if [[ ! -f "$udev_source" ]]; then
        print_error "Udev rule file not found: $udev_source"
        exit 1
    fi

    # Check for conflicting rules
    local old_rules=$(grep -l "dvd-ripper.sh" /etc/udev/rules.d/*.rules 2>/dev/null | grep -v "99-dvd-ripper.rules" || true)
    if [[ -n "$old_rules" ]]; then
        print_warn "⚠ Found old udev rules that may conflict:"
        echo "$old_rules" | while read -r rule; do
            echo "    - $rule"
        done
        print_warn ""
        print_warn "You should remove or disable these old rules to avoid conflicts"
        print_warn ""
    fi

    # Install new rule
    cp "$udev_source" "$udev_dest"
    chmod 644 "$udev_dest"

    # Reload udev rules and systemd
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=block
    systemctl daemon-reload

    # Settle udev to ensure rules are fully applied
    udevadm settle --timeout=5

    print_info "✓ Udev rule installed and reloaded"
}

install_web_dashboard() {
    print_info "Installing web dashboard..."

    local dashboard_source="$SCRIPT_DIR/web/dvd-dashboard.py"
    local service_source="$SCRIPT_DIR/config/dvd-dashboard.service"
    local systemd_dir="/etc/systemd/system"

    # Check if dashboard exists
    if [[ ! -f "$dashboard_source" ]]; then
        print_warn "Web dashboard not found at $dashboard_source, skipping"
        return
    fi

    # Check if Flask is installed
    if ! python3 -c "import flask" 2>/dev/null; then
        print_info "Installing Flask..."
        if command -v apt-get &>/dev/null; then
            apt-get install -y python3-flask >/dev/null 2>&1 || pip3 install flask
        elif command -v pip3 &>/dev/null; then
            pip3 install flask
        else
            print_warn "Cannot install Flask - please install manually: pip3 install flask"
            return
        fi
    fi

    # Install dashboard script
    cp "$dashboard_source" "$INSTALL_BIN/dvd-dashboard.py"
    chmod 755 "$INSTALL_BIN/dvd-dashboard.py"

    # Install systemd service
    if [[ -f "$service_source" ]]; then
        cp "$service_source" "$systemd_dir/"
        chmod 644 "$systemd_dir/dvd-dashboard.service"
        systemctl daemon-reload
        systemctl enable dvd-dashboard.service
        systemctl restart dvd-dashboard.service

        # Get dashboard version
        local dashboard_version=$(grep -oP 'DASHBOARD_VERSION = "\K[^"]+' "$dashboard_source" 2>/dev/null || echo "unknown")
        print_info "✓ Web dashboard v${dashboard_version} installed and started"
    else
        print_warn "dvd-dashboard.service not found, skipping systemd integration"
    fi
}

test_installation() {
    print_info ""
    print_info "=========================================="
    print_info "Testing Installation"
    print_info "=========================================="

    local all_good=true

    # Check if scripts are executable
    if [[ -x "$INSTALL_BIN/dvd-ripper.sh" ]]; then
        print_info "✓ dvd-ripper.sh is installed and executable"
    else
        print_error "✗ dvd-ripper.sh is not executable"
        all_good=false
    fi

    # Check if utils exist
    if [[ -f "$INSTALL_BIN/dvd-utils.sh" ]]; then
        print_info "✓ dvd-utils.sh is installed"
    else
        print_error "✗ dvd-utils.sh is not installed"
        all_good=false
    fi

    # Check if config exists
    if [[ -f "$INSTALL_CONFIG/dvd-ripper.conf" ]]; then
        print_info "✓ Configuration file exists"
    else
        print_error "✗ Configuration file not found"
        all_good=false
    fi

    # Check HandBrake
    if HandBrakeCLI --version &>/dev/null; then
        local hb_version=$(HandBrakeCLI --version 2>&1 | head -1)
        print_info "✓ HandBrake: $hb_version"
    else
        print_error "✗ HandBrake not working"
        all_good=false
    fi

    # Check DVD device
    if [[ -e "/dev/sr0" ]]; then
        print_info "✓ DVD device exists: /dev/sr0"
    else
        print_warn "⚠ DVD device /dev/sr0 not found (may need to insert disc)"
    fi

    # Check staging directory
    if [[ -d "/var/tmp/dvd-rips" ]]; then
        print_info "✓ Staging directory exists"
    else
        print_error "✗ Staging directory not created"
        all_good=false
    fi

    # Check systemd service
    if [[ -f "/etc/systemd/system/dvd-ripper@.service" ]]; then
        print_info "✓ Systemd service installed"
    else
        print_error "✗ Systemd service not installed"
        all_good=false
    fi

    # Check udev rule
    if [[ -f "/etc/udev/rules.d/99-dvd-ripper.rules" ]]; then
        print_info "✓ Udev rule installed"
    else
        print_error "✗ Udev rule not installed"
        all_good=false
    fi

    # Check pipeline scripts
    if [[ -x "$INSTALL_BIN/dvd-iso.sh" ]]; then
        print_info "✓ dvd-iso.sh installed (pipeline stage 1)"
    else
        print_warn "⚠ dvd-iso.sh not installed"
    fi

    if [[ -x "$INSTALL_BIN/dvd-encoder.sh" ]]; then
        print_info "✓ dvd-encoder.sh installed (pipeline stage 2)"
    else
        print_warn "⚠ dvd-encoder.sh not installed"
    fi

    if [[ -x "$INSTALL_BIN/dvd-transfer.sh" ]]; then
        print_info "✓ dvd-transfer.sh installed (pipeline stage 3)"
    else
        print_warn "⚠ dvd-transfer.sh not installed"
    fi

    # Check pipeline timers
    if systemctl is-enabled dvd-encoder.timer &>/dev/null; then
        print_info "✓ dvd-encoder.timer enabled"
    else
        print_warn "⚠ dvd-encoder.timer not enabled"
    fi

    if systemctl is-enabled dvd-transfer.timer &>/dev/null; then
        print_info "✓ dvd-transfer.timer enabled"
    else
        print_warn "⚠ dvd-transfer.timer not enabled"
    fi

    # Check libdvdcss
    if ldconfig -p | grep -q libdvdcss; then
        print_info "✓ libdvdcss installed (encrypted DVD support)"
    else
        print_warn "⚠ libdvdcss not installed (encrypted DVDs won't work)"
    fi

    # Check web dashboard
    if systemctl is-active dvd-dashboard.service &>/dev/null; then
        local ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}')
        print_info "✓ Web dashboard running at http://${ip_addr:-localhost}:5000"
    else
        print_warn "⚠ Web dashboard not running"
    fi

    print_info ""
    if [[ "$all_good" == "true" ]]; then
        print_info "✓ All checks passed!"
    else
        print_warn "⚠ Some checks failed - review above"
    fi
}

print_next_steps() {
    print_info ""
    print_info "=========================================="
    print_info "Installation Complete!"
    print_info "=========================================="
    print_info ""
    print_warn "NEXT STEPS:"
    print_info ""
    print_info "1. Edit configuration file:"
    print_info "   sudo nano /etc/dvd-ripper.conf"
    print_info ""
    print_info "2. Set your NAS details (for transfer stage):"
    print_info "   - NAS_ENABLED=1"
    print_info "   - NAS_HOST (IP or hostname)"
    print_info "   - NAS_USER (username)"
    print_info "   - NAS_PATH (destination directory)"
    print_info ""
    print_info "3. Set up SSH key authentication for NAS:"
    print_info "   ssh-keygen -t rsa -b 4096"
    print_info "   ssh-copy-id <nas-user>@<nas-host>"
    print_info "   ssh <nas-user>@<nas-host>  # Test connection"
    print_info ""
    print_info "4. Test manually with a DVD:"
    print_info "   sudo /usr/local/bin/dvd-iso.sh /dev/sr0"
    print_info ""
    print_info "5. Monitor the pipeline:"
    print_info "   tail -f /var/log/dvd-ripper.log"
    print_info "   systemctl list-timers | grep dvd"
    print_info ""
    print_info "6. Check queue status:"
    print_info "   ls -la /var/tmp/dvd-rips/*.iso-ready       # Pending encodes"
    print_info "   ls -la /var/tmp/dvd-rips/*.encoded-ready   # Pending transfers"
    print_info ""
    print_info "PIPELINE MODE (default):"
    print_info "  Stage 1: Insert DVD -> Create ISO -> Eject (immediate)"
    print_info "  Stage 2: Encoder timer runs every 15 min -> Encode ISOs"
    print_info "  Stage 3: Transfer timer runs every 15 min -> Transfer to NAS"
    print_info ""
    print_info "  Benefits: Drive is freed quickly, encoding happens in background"
    print_info ""
    print_info "MANUAL TRIGGERS:"
    print_info "   sudo systemctl start dvd-encoder.service   # Encode now"
    print_info "   sudo systemctl start dvd-transfer.service  # Transfer now"
    print_info ""
    print_info "WEB DASHBOARD:"
    local ip_addr=$(hostname -I 2>/dev/null | awk '{print $1}')
    print_info "   http://${ip_addr:-localhost}:5000"
    print_info "   View queue, logs, disk usage, and trigger stages from browser"
    print_info ""
}

# ============================================================================
# Main Installation
# ============================================================================

main() {
    print_info "DVD Ripper Remote Installation Script"
    print_info "======================================"
    print_info ""

    # Check if running as root
    check_root

    # Verify we're in the right location
    if [[ ! -d "$SCRIPT_DIR/scripts" ]] || [[ ! -d "$SCRIPT_DIR/config" ]]; then
        print_error "Cannot find scripts/ and config/ directories"
        print_error "Current directory: $SCRIPT_DIR"
        print_error "Did you run deploy.sh from your local machine first?"
        exit 1
    fi

    # Check dependencies
    check_dependencies

    # Install components
    install_scripts
    install_config
    install_logrotate

    # Create directories
    create_directories

    # Install systemd integration
    install_systemd_service
    install_pipeline_timers
    install_udev_rule

    # Install web dashboard (optional but recommended)
    install_web_dashboard

    # Test installation
    test_installation

    # Print next steps
    print_next_steps
}

# Run main function
main "$@"
