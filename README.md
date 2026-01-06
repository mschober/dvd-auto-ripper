# DVD Auto-Ripper

Automated DVD ripping system for Linux that monitors disc insertion and creates ISO backups. Optionally encodes to compressed video using HandBrake and transfers to a NAS.

## Current Configuration

The system is configured for **HandBrake encoding mode**:

| Setting | Value | Description |
|---------|-------|-------------|
| `CREATE_ISO` | `0` | ISO creation disabled |
| `ENCODE_VIDEO` | `1` | HandBrake encoding enabled |
| `NAS_ENABLED` | `0` | NAS transfer disabled |

**Workflow:** Insert DVD → Encode with HandBrake → MKV saved to `/var/tmp/dvd-rips/` → Disc ejected

To enable ISO creation or NAS transfer, edit `/etc/dvd-ripper.conf`.

## Architecture

The DVD auto-ripper uses a **3-stage pipeline** that decouples disc handling from encoding and transfer:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DVD AUTO-RIPPER PIPELINE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌───────────┐  │
│  │   DVD    │     │   STAGE 1    │     │   STAGE 2    │     │  STAGE 3  │  │
│  │  INSERT  │────▶│  ISO Create  │────▶│   Encoder    │────▶│ Transfer  │  │
│  │          │     │  (udev)      │     │  (15 min)    │     │ (15 min)  │  │
│  └──────────┘     └──────────────┘     └──────────────┘     └───────────┘  │
│                          │                    │                    │        │
│                          ▼                    ▼                    ▼        │
│                   ┌────────────┐       ┌────────────┐       ┌──────────┐   │
│                   │ .iso file  │       │ .mkv file  │       │   NAS    │   │
│                   │ + eject    │       │ Plex-ready │       │  Plex    │   │
│                   └────────────┘       └────────────┘       └──────────┘   │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  State Files: *.iso-ready → *.encoding → *.encoded-ready → (cleanup)       │
│  Lock Files:  /var/run/dvd-ripper-{iso,encoder,transfer}.lock              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pipeline Benefits
- **Drive freed immediately**: Disc ejects after ISO creation, ready for next DVD
- **Background processing**: Encoding happens via timer, doesn't block new rips
- **Resilient**: Each stage can fail/retry independently
- **Queue-based**: Multiple ISOs can queue up, processed one at a time

### Stage Details

| Stage | Script | Trigger | Purpose |
|-------|--------|---------|---------|
| 1 | `dvd-iso.sh` | udev (disc insert) | Create ISO with ddrescue, eject disc |
| 2 | `dvd-encoder.sh` | systemd timer (15 min) | Encode ONE ISO to MKV per run |
| 3 | `dvd-transfer.sh` | systemd timer (15 min) | Transfer ONE MKV to NAS per run |

### Web Dashboard

A web UI is available at `http://<server>:5000` for monitoring the pipeline.

```bash
# Start/stop/restart the dashboard
sudo systemctl start dvd-dashboard
sudo systemctl stop dvd-dashboard
sudo systemctl restart dvd-dashboard

# Check status
sudo systemctl status dvd-dashboard

# View dashboard logs
journalctl -u dvd-dashboard -f

# Disable/enable on boot
sudo systemctl disable dvd-dashboard
sudo systemctl enable dvd-dashboard
```

## Features

- **Automatic DVD Detection**: udev rule triggers systemd service on disc insertion
- **ISO Creation**: Uses ddrescue for reliable copying with error recovery
- **Smart Duplicate Detection**: Checks both local staging and NAS for existing files
- **HandBrake Integration** (optional): High-quality video encoding with customizable presets
- **NAS Transfer** (optional): Secure file transfer via rsync or scp
- **Robust Error Handling**: Retry logic, recovery from interruptions, comprehensive logging
- **Lock Management**: Prevents multiple simultaneous rip operations
- **State Tracking**: Recovers from interruptions (power loss, crashes)

## System Requirements

### Hardware
- Linux server with CD/DVD drive
- Network connection to NAS
- Sufficient disk space in staging directory (recommend 20GB+ for temporary storage)

### Software Dependencies

**Required (for ISO mode):**
- `gddrescue` - Reliable disc copying with error recovery
- `handbrake-cli` - DVD scanning (metadata extraction) and optional encoding
- `eject` - Disc ejection
- `udev` - Device event monitoring (typically pre-installed)

**Optional (for NAS transfer):**
- `rsync` - File transfer to NAS (recommended)
- `openssh-client` - SSH access to NAS

### Installation on Debian/Ubuntu
```bash
sudo apt-get update
sudo apt-get install gddrescue handbrake-cli eject rsync openssh-client
```

### Installation on RHEL/CentOS/Fedora
```bash
sudo yum install ddrescue handbrake-cli eject rsync openssh-clients
```

## Installation

There are two installation methods: **Local Installation** (install directly on the server) or **Remote Deployment** (recommended - deploy from your local machine).

### Option A: Remote Deployment (Recommended)

This method syncs files from your local machine to the remote server, then installs them.

#### Step 1: Clone Repository on Server

```bash
# SSH to the server
ssh <user>@<server>

# Clone the repository to /opt (standard location for third-party apps)
sudo git clone https://github.com/mschober/dvd-auto-ripper.git /opt/dvd-auto-ripper
```

#### Step 2: Install on Server

```bash
# Navigate to cloned directory
cd /opt/dvd-auto-ripper

# Run installation with sudo (include --install-libdvdcss for commercial DVDs)
sudo ./remote-install.sh --install-libdvdcss
```

> **Important:** The `--install-libdvdcss` flag is required to rip commercial/encrypted DVDs. Without it, most store-bought DVDs will fail to rip.

The `remote-install.sh` script will:
- Check for required dependencies
- Install scripts to `/usr/local/bin/`
- Copy configuration to `/etc/dvd-ripper.conf`
- Set up logrotate
- Create necessary directories
- Set proper permissions

### Option B: Local Installation

If you're already on the server, you can install directly:

```bash
cd /opt/dvd-auto-ripper
sudo ./scripts/install.sh
```

### 3. Configure Settings

Edit the configuration file:
```bash
sudo nano /etc/dvd-ripper.conf
```

**Required settings to configure:**
```bash
# NAS Configuration
NAS_HOST="your-nas-ip"             # Your NAS IP or hostname
NAS_USER="your-username"           # NAS username
NAS_PATH="/volume1/media/movies"   # Destination path on NAS

# HandBrake Settings (optional, defaults are reasonable)
HANDBRAKE_PRESET="Fast 1080p30"
HANDBRAKE_QUALITY="20"
HANDBRAKE_FORMAT="mkv"
```

### 4. Set Up SSH Key Authentication

For passwordless NAS access, set up SSH keys:

```bash
# Generate SSH key if you don't have one
ssh-keygen -t rsa -b 4096

# Copy key to NAS
ssh-copy-id your-username@your-nas-ip

# Test connection
ssh your-username@your-nas-ip
```

### 5. Configure udev Rule

Since you already have a udev rule for CDROM events, modify it to call the DVD ripper script:

```bash
# Edit your existing udev rule
sudo nano /etc/udev/rules.d/<your-cdrom-rule>.rules
```

Add or modify the RUN command to call:
```
RUN+="/usr/local/bin/dvd-ripper.sh /dev/%k"
```

Then reload udev:
```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Usage

### Automatic Mode (Recommended)

Simply insert a DVD. The system will automatically:
1. Detect the disc insertion via udev
2. Start the systemd service `dvd-ripper@sr0.service`
3. Scan DVD metadata with HandBrake
4. Check for duplicates
5. Encode to MKV using HandBrake (current config)
6. Eject the disc

**With ISO enabled** (`CREATE_ISO=1`): Creates ISO first, then encodes from ISO (better for damaged discs).

**With NAS enabled** (`NAS_ENABLED=1`): Transfers files to NAS, then cleans up local copies.

### Manual Mode (Testing)

For testing or manual ripping:

```bash
# Run manually with default device (/dev/sr0)
sudo /usr/local/bin/dvd-ripper.sh

# Specify a different device
sudo /usr/local/bin/dvd-ripper.sh /dev/sr1
```

### Monitor Progress

Watch the log in real-time:
```bash
tail -f /var/log/dvd-ripper.log
```

View recent log entries:
```bash
sudo tail -100 /var/log/dvd-ripper.log
```

Check systemd service status:
```bash
# See if a rip is currently running
systemctl status dvd-ripper@sr0.service

# View service logs via journald
journalctl -u dvd-ripper@sr0.service -f

# List recent rip jobs
journalctl -u 'dvd-ripper@*' --since today
```

Check staging directory:
```bash
ls -lh /var/tmp/dvd-rips/
```

## File Naming Convention

Output files use this naming pattern:
```
{SanitizedTitle}-{Timestamp}.{Extension}
```

Examples (ISO mode):
- `The_Matrix-1703615234.iso`
- `INCEPTION-1703615890.iso`
- `DVD_20251227_143022-1703616123.iso` (fallback if title is generic)

Examples (with encoding enabled):
- `The_Matrix-1999-1703615234.mkv`
- `Inception-2010-1703615890.mkv`

## Directory Structure

```
/var/tmp/dvd-rips/          # Staging directory (output storage)
├── *.iso-creating          # State file during ISO creation
├── *.iso-ready             # State file when ISO complete, awaiting encode
├── *.encoding              # State file during HandBrake encoding
├── *.encoded-ready         # State file when encode complete, awaiting transfer
├── *.transferring          # State file during NAS transfer
├── Movie-Title-*.iso       # ISO image (if CREATE_ISO=1)
├── Movie-Title-*.iso.mapfile  # ddrescue progress map
└── Movie-Title-*.mkv       # Encoded video (if ENCODE_VIDEO=1)

/var/log/
└── dvd-ripper.log          # Main log file

/var/run/
└── dvd-ripper.pid          # Lock file (prevents simultaneous rips)
```

## Configuration Options

See `config/dvd-ripper.conf.example` for all available options.

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `STAGING_DIR` | `/var/tmp/dvd-rips` | Local staging directory |
| `HANDBRAKE_PRESET` | `Fast 1080p30` | Encoding quality preset |
| `HANDBRAKE_QUALITY` | `20` | Quality level (18-22 recommended) |
| `HANDBRAKE_FORMAT` | `mkv` | Output format (mkv or mp4) |
| `MIN_FILE_SIZE_MB` | `100` | Minimum file size verification |
| `MAX_RETRIES` | `3` | Retry attempts for failures |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Troubleshooting

### DVD Not Being Ripped Automatically

1. Check if disc is detected:
   ```bash
   lsblk
   # Look for /dev/sr0 or similar
   ```

2. Monitor udev events:
   ```bash
   sudo udevadm monitor --environment --udev
   # Insert disc and watch for events
   ```

3. Check if script is being triggered:
   ```bash
   sudo journalctl -f
   # Insert disc and watch for messages
   ```

### Rip Fails or Produces Small Files

1. Check HandBrake can read the disc:
   ```bash
   HandBrakeCLI --scan -i /dev/sr0
   ```

2. Try different quality settings in `/etc/dvd-ripper.conf`

3. Check the disc isn't damaged or copy-protected

### NAS Transfer Fails

1. Verify SSH connection:
   ```bash
   ssh your-username@your-nas-ip
   ```

2. Check NAS path exists and is writable:
   ```bash
   ssh your-username@your-nas-ip "ls -la /volume1/media/movies"
   ```

3. Review transfer logs:
   ```bash
   grep -i "transfer" /var/log/dvd-ripper.log
   ```

### Check for Interrupted Operations

The system can recover from interrupted rips/transfers. To manually check:

```bash
# Look for state files
ls -la /var/tmp/dvd-rips/*.iso-ready
ls -la /var/tmp/dvd-rips/*.encoded-ready
ls -la /var/tmp/dvd-rips/*.transferring

# Check lock file
cat /var/run/dvd-ripper.pid
```

### View Detailed Debug Output

Enable debug logging:
```bash
sudo nano /etc/dvd-ripper.conf
# Change: LOG_LEVEL="DEBUG"
```

## Security Considerations

- SSH key-based authentication (no passwords in config)
- Script execution permissions restricted (chmod 750)
- All metadata sanitized before use in filenames
- Log rotation prevents disk fill
- Lock file prevents race conditions

## Advanced Usage

### Custom HandBrake Settings

Add custom HandBrake options in `/etc/dvd-ripper.conf`:
```bash
HANDBRAKE_EXTRA_OPTS="--all-audio --all-subtitles"
```

### Multiple DVD Drives

The script supports multiple drives. Your udev rule can call the script with different devices:
```
RUN+="/usr/local/bin/dvd-ripper.sh /dev/%k"
```
The `%k` will be replaced with the actual device name (sr0, sr1, etc.)

## Installed Files (on server)

After installation, the following files are deployed:

| Path | Description |
|------|-------------|
| `/usr/local/bin/dvd-ripper.sh` | Main ripping script |
| `/usr/local/bin/dvd-utils.sh` | Helper functions library |
| `/etc/dvd-ripper.conf` | Configuration file |
| `/etc/udev/rules.d/99-dvd-ripper.rules` | udev rule (triggers on disc insert) |
| `/etc/systemd/system/dvd-ripper@.service` | Systemd template service |
| `/etc/logrotate.d/dvd-ripper` | Log rotation config |
| `/var/log/dvd-ripper.log` | Log file |
| `/var/tmp/dvd-rips/` | Staging directory (ISO/video output) |
| `/var/run/dvd-ripper.pid` | Lock file (prevents concurrent runs) |

## Project Structure

```
dvd-ripper/
├── README.md                        # This file
├── PLAN.md                          # Detailed project plan
├── CLAUDE.md                        # Claude Code guidance
├── deploy.sh                        # Local deployment script (rsync to remote)
├── remote-install.sh                # Remote installation script (run with sudo)
├── scripts/
│   ├── dvd-ripper.sh               # Main ripping script
│   ├── dvd-utils.sh                # Helper functions library
│   └── install.sh                  # Local installation script
└── config/
    ├── dvd-ripper.conf.example     # Example configuration
    └── dvd-ripper.logrotate        # Logrotate configuration
```

### Deployment Scripts

- **deploy.sh**: Syncs the project from your local machine to the remote server using rsync
- **remote-install.sh**: Installs the DVD ripper on the remote server (must be run with sudo)
- **scripts/install.sh**: Alternative installation method if running directly on the server

## Contributing

This is a personal project, but suggestions and improvements are welcome.

## License

MIT License - Use freely for personal or commercial use.

## Author

Created for automated DVD archival on Linux servers.
