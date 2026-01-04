#!/bin/bash
# DVD Ripper Installation Script
# Installs scripts, configuration, and sets up the DVD auto-ripper system

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Installation paths
INSTALL_BIN="/usr/local/bin"
INSTALL_CONFIG="/etc"
INSTALL_LOGROTATE="/etc/logrotate.d"
INSTALL_UDEV="/etc/udev/rules.d"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

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

check_dependencies() {
    local missing_deps=()

    print_info "Checking dependencies..."

    # Required dependencies
    local deps=("HandBrakeCLI" "rsync" "ssh" "eject")

    for cmd in "${deps[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            missing_deps+=("$cmd")
        fi
    done

    if [[ ${#missing_deps[@]} -gt 0 ]]; then
        print_error "Missing required dependencies: ${missing_deps[*]}"
        print_info "Install them with:"
        print_info "  sudo apt-get install handbrake-cli rsync openssh-client eject"
        print_info "  OR"
        print_info "  sudo yum install handbrake-cli rsync openssh-clients eject"
        exit 1
    fi

    print_info "All dependencies satisfied"
}

install_scripts() {
    print_info "Installing scripts to $INSTALL_BIN..."

    # Copy main scripts
    cp "$SCRIPT_DIR/dvd-ripper.sh" "$INSTALL_BIN/"
    cp "$SCRIPT_DIR/dvd-utils.sh" "$INSTALL_BIN/"

    # Set permissions
    chmod 755 "$INSTALL_BIN/dvd-ripper.sh"
    chmod 644 "$INSTALL_BIN/dvd-utils.sh"

    print_info "Scripts installed successfully"
}

install_config() {
    print_info "Installing configuration..."

    local config_file="$INSTALL_CONFIG/dvd-ripper.conf"

    if [[ -f "$config_file" ]]; then
        print_warn "Configuration file already exists: $config_file"
        read -p "Overwrite? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            print_info "Skipping configuration installation"
            return
        fi
        # Backup existing config
        cp "$config_file" "${config_file}.backup.$(date +%Y%m%d_%H%M%S)"
        print_info "Backed up existing configuration"
    fi

    cp "$PROJECT_ROOT/config/dvd-ripper.conf.example" "$config_file"
    chmod 644 "$config_file"

    print_warn "Configuration installed to: $config_file"
    print_warn "*** YOU MUST EDIT THIS FILE TO SET YOUR NAS DETAILS ***"
}

install_logrotate() {
    print_info "Installing logrotate configuration..."

    cp "$PROJECT_ROOT/config/dvd-ripper.logrotate" "$INSTALL_LOGROTATE/dvd-ripper"
    chmod 644 "$INSTALL_LOGROTATE/dvd-ripper"

    print_info "Logrotate configuration installed"
}

install_udev() {
    print_info "Checking udev rules..."

    # Check if user already has a udev rule
    local existing_rules=$(find /etc/udev/rules.d/ -name "*cdrom*" -o -name "*dvd*" 2>/dev/null)

    if [[ -n "$existing_rules" ]]; then
        print_info "Existing DVD/CDROM udev rules found:"
        echo "$existing_rules"
        print_warn "You mentioned there's already a udev rule for CDROM"
        print_info "Make sure it calls: $INSTALL_BIN/dvd-ripper.sh"
        return
    fi

    print_warn "No existing DVD udev rules found"
    print_info "You'll need to configure your existing udev rule to call:"
    print_info "  $INSTALL_BIN/dvd-ripper.sh"
}

create_directories() {
    print_info "Creating required directories..."

    # Create staging directory
    local staging_dir="/var/tmp/dvd-rips"
    mkdir -p "$staging_dir"
    chmod 750 "$staging_dir"

    # Create log directory
    local log_dir="/var/log"
    mkdir -p "$log_dir"
    touch "$log_dir/dvd-ripper.log"
    chmod 640 "$log_dir/dvd-ripper.log"

    # Create run directory for lock file
    mkdir -p "/var/run"

    print_info "Directories created"
}

setup_ssh_keys() {
    print_info ""
    print_info "=========================================="
    print_info "SSH Key Setup for NAS Access"
    print_info "=========================================="
    print_warn "For passwordless NAS access, you need to set up SSH keys"
    print_info ""
    print_info "Steps:"
    print_info "1. Generate SSH key (if you don't have one):"
    print_info "   ssh-keygen -t rsa -b 4096"
    print_info ""
    print_info "2. Copy key to NAS:"
    print_info "   ssh-copy-id user@nas-hostname"
    print_info ""
    print_info "3. Test connection:"
    print_info "   ssh user@nas-hostname"
    print_info ""
}

test_installation() {
    print_info ""
    print_info "=========================================="
    print_info "Testing Installation"
    print_info "=========================================="

    # Check if scripts are executable
    if [[ -x "$INSTALL_BIN/dvd-ripper.sh" ]]; then
        print_info "✓ dvd-ripper.sh is executable"
    else
        print_error "✗ dvd-ripper.sh is not executable"
    fi

    # Check if config exists
    if [[ -f "$INSTALL_CONFIG/dvd-ripper.conf" ]]; then
        print_info "✓ Configuration file exists"
    else
        print_warn "✗ Configuration file not found"
    fi

    # Check HandBrake
    if HandBrakeCLI --version &>/dev/null; then
        local hb_version=$(HandBrakeCLI --version 2>&1 | head -1)
        print_info "✓ HandBrake: $hb_version"
    else
        print_error "✗ HandBrake not working"
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
    print_info "2. Set your NAS details:"
    print_info "   - NAS_HOST (IP or hostname)"
    print_info "   - NAS_USER (username)"
    print_info "   - NAS_PATH (destination directory)"
    print_info ""
    print_info "3. Set up SSH key authentication (see above)"
    print_info ""
    print_info "4. Configure your existing udev rule to call:"
    print_info "   /usr/local/bin/dvd-ripper.sh"
    print_info ""
    print_info "5. Test with a DVD:"
    print_info "   sudo /usr/local/bin/dvd-ripper.sh /dev/sr0"
    print_info ""
    print_info "6. Monitor logs:"
    print_info "   tail -f /var/log/dvd-ripper.log"
    print_info ""
}

# ============================================================================
# Main Installation
# ============================================================================

main() {
    print_info "DVD Ripper Installation Script"
    print_info "==============================="
    print_info ""

    # Check if running as root
    check_root

    # Check dependencies
    check_dependencies

    # Install components
    install_scripts
    install_config
    install_logrotate
    install_udev

    # Create directories
    create_directories

    # Test installation
    test_installation

    # Print SSH setup info
    setup_ssh_keys

    # Print next steps
    print_next_steps
}

# Run main function
main "$@"
