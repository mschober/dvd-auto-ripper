# DVD Auto-Ripper

**Turn your DVD collection into a personal streaming library.**

Got a shelf full of DVDs collecting dust? This tool automates the entire process of converting your physical DVD collection into a Plex-ready digital library. Insert a disc, walk away, and find it ready to stream on any device.

## Why This Exists

You have DVDs you never watch because:
- Finding and loading a disc is a hassle
- DVD players are going extinct
- You can't watch them on your phone, tablet, or smart TV
- Streaming is just more convenient

DVD Auto-Ripper solves this by automating the entire pipeline from disc to stream. No manual ripping, no file management, no naming headaches. Just insert discs and watch your Plex library grow.

## How It Works

```
Insert DVD → Automatic Rip → Encode → Transfer to NAS → Stream on Plex
   (you)       (30 min)      (background)   (automatic)    (anywhere)
```

1. **Insert a DVD** - The system detects it automatically
2. **ISO created & disc ejected** - Your drive is free in ~30 minutes
3. **Encoding happens in background** - HandBrake converts to Plex-friendly MKV
4. **Transfer to NAS** - Files land in your Plex library folder
5. **Stream anywhere** - Plex picks it up automatically

You can keep inserting DVDs - they queue up and process one at a time.

## Features

| Feature | Description |
|---------|-------------|
| **Fully Automatic** | Insert disc, walk away. Everything else is handled. |
| **Plex-Ready Output** | Files named correctly: `Movie Title (Year).mkv` |
| **Web Dashboard** | Monitor progress, view queue, control services from any browser |
| **Smart Identification** | Handles DVDs with missing/generic metadata |
| **Queue-Based Processing** | Rip multiple DVDs, they process in order |
| **Crash Recovery** | Resumes interrupted operations after power loss |
| **Commercial DVD Support** | Handles copy-protected discs (with libdvdcss) |

## Architecture

The system assumes a **separate Plex server + NAS** setup:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  DVD Drive  │────▶│ Ripper Box  │────▶│     NAS     │◀────│ Plex Server │
│             │     │  (this tool)│     │  (storage)  │     │  (streams)  │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                   │
                                                                   ▼
                                                          ┌───────────────┐
                                                          │ TV, Phone,    │
                                                          │ Tablet, Roku, │
                                                          │ Fire TV, Web  │
                                                          └───────────────┘
```

**Other supported setups:**
- NAS with built-in Plex (Synology/QNAP with Plex package)
- Local Plex on the ripper machine (no NAS needed)

### Why a 3-Stage Pipeline?

The system separates disc handling from encoding and transfer:

| Stage | What Happens | Trigger |
|-------|--------------|---------|
| **1. Rip** | Create ISO, eject disc | Disc insertion (udev) |
| **2. Encode** | Convert ISO → MKV | Timer (every 15 min) |
| **3. Transfer** | Send to NAS, cleanup | Timer (every 15 min) |

**Benefits:**
- Your DVD drive is free in ~30 minutes (not 2-3 hours)
- You can keep inserting discs without waiting
- Encoding failures don't block new rips
- Power loss? It picks up where it left off

## Quick Start

### Prerequisites

**Hardware:**
- Linux server/PC with a DVD drive
- NAS for storage (or local storage)
- Plex Media Server (on NAS, separate server, or local)

**Software (installed automatically or manually):**
```bash
# Debian/Ubuntu
sudo apt install handbrake-cli gddrescue ffmpeg eject rsync python3-flask

# Install libdvdcss for commercial DVDs (required!)
sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg
```

### Installation

```bash
# Clone to /opt (standard location)
sudo git clone https://github.com/mschober/dvd-auto-ripper.git /opt/dvd-auto-ripper
cd /opt/dvd-auto-ripper

# Install (--install-libdvdcss is required for commercial DVDs)
sudo ./remote-install.sh --install-libdvdcss
```

### Configuration

Edit `/etc/dvd-ripper.conf`:

```bash
# Essential settings for Plex integration
NAS_ENABLED="1"
NAS_HOST="192.168.1.100"        # Your NAS IP
NAS_USER="media"                 # NAS username
NAS_PATH="/volume1/Movies"       # Plex Movies library folder
NAS_FILE_OWNER="plex:plex"       # File ownership for Plex access
```

### SSH Key Setup (Required for NAS Transfer)

```bash
# Generate key if you don't have one
ssh-keygen -t rsa -b 4096

# Copy to NAS
ssh-copy-id media@192.168.1.100

# Test connection
ssh media@192.168.1.100
```

### Test It

```bash
# Insert a DVD, then watch the logs
tail -f /var/log/dvd-ripper.log

# Or open the web dashboard
# http://<ripper-ip>:5000
```

## Web Dashboard

Access the dashboard at `http://<ripper-ip>:5000`

### Main Dashboard
- **Pipeline Status**: See what's queued at each stage
- **Queue View**: All items with titles, states, timestamps
- **Disk Usage**: Monitor staging directory space
- **Active Processes**: See what's currently running
- **Quick Actions**: Trigger encoder/transfer manually

### Status Page (`/status`)
- Start/stop/restart services
- Pause/unpause automatic timers
- Enable/disable timers (survives reboot)

### Identification Page (`/identify`)
- Lists DVDs with generic/unknown names
- Watch 2-minute preview clips to identify movies
- Enter correct title and year
- Files renamed automatically (even on NAS)

### Other Pages
- `/logs` - Full log viewer
- `/config` - Current configuration
- `/architecture` - Technical pipeline diagram

## Plex Integration

### File Naming

Output uses Plex's preferred format:
```
Movie Title (Year).mkv
```
Examples:
- `The Matrix (1999).mkv`
- `Inception (2010).mkv`
- `Jurassic Park (1993).mkv`

### Automatic Library Updates

When transfer completes, Plex detects new files automatically (if library scanning is enabled). No manual refresh needed.

### Generic Title Handling

Some DVDs have unhelpful metadata like "DVD_VIDEO" or "DISC1". The identification feature:
1. Detects these generic names
2. Generates a preview clip during encoding
3. Shows them in the dashboard for manual identification
4. Renames files everywhere (local + NAS) when you provide the real title

## Configuration Reference

Key settings in `/etc/dvd-ripper.conf`:

| Setting | Default | Description |
|---------|---------|-------------|
| `NAS_ENABLED` | `0` | Enable transfer to NAS |
| `NAS_HOST` | - | NAS IP or hostname |
| `NAS_USER` | - | SSH username for NAS |
| `NAS_PATH` | - | Destination folder (your Plex library) |
| `NAS_FILE_OWNER` | `plex:plex` | Set file ownership after transfer |
| `HANDBRAKE_PRESET` | `Fast 1080p30` | Encoding quality preset |
| `HANDBRAKE_QUALITY` | `20` | Quality level (18=high, 22=smaller files) |
| `GENERATE_PREVIEWS` | `1` | Create preview clips for identification |

See `config/dvd-ripper.conf.example` for all options.

## Management Scripts

Control the system from the command line:

```bash
# Dashboard control
dvd-dashboard-ctl.sh start|stop|restart|status

# Start/stop all services
dvd-ripper-services-start.sh
dvd-ripper-services-stop.sh

# Pause/resume automatic disc detection
dvd-ripper-trigger-pause.sh    # For manual operations
dvd-ripper-trigger-resume.sh
```

## Monitoring

```bash
# Watch logs in real-time
tail -f /var/log/dvd-ripper.log

# Check timer status
systemctl list-timers | grep dvd

# View queue
ls -la /var/tmp/dvd-rips/

# Check what's running
systemctl status dvd-encoder dvd-transfer dvd-dashboard
```

## Troubleshooting

### DVD Not Detected

```bash
# Check if disc is recognized
lsblk | grep sr

# Watch for udev events
sudo udevadm monitor --environment --udev
# (insert disc and watch output)
```

### Encoding Fails

```bash
# Test HandBrake directly
HandBrakeCLI --scan -i /dev/sr0

# Check for libdvdcss (required for commercial DVDs)
ldconfig -p | grep dvdcss
```

### NAS Transfer Fails

```bash
# Test SSH connection
ssh media@nas-ip

# Check path exists
ssh media@nas-ip "ls -la /volume1/Movies"

# View transfer errors
grep -i "transfer" /var/log/dvd-ripper.log
```

### Plex Not Seeing Files

- Verify files are in the correct Plex library folder
- Check file ownership matches Plex's user
- Ensure Plex library has "Scan my library automatically" enabled
- Try manual library scan in Plex

## Project Structure

```
dvd-auto-ripper/
├── scripts/
│   ├── dvd-iso.sh              # Stage 1: Create ISO from DVD
│   ├── dvd-encoder.sh          # Stage 2: Encode ISO to MKV
│   ├── dvd-transfer.sh         # Stage 3: Transfer to NAS
│   ├── dvd-utils.sh            # Shared utility functions
│   ├── dvd-ripper.sh           # Legacy all-in-one mode
│   ├── dvd-dashboard-ctl.sh    # Dashboard control script
│   └── dvd-ripper-*.sh         # Management scripts
├── web/
│   └── dvd-dashboard.py        # Flask web dashboard
├── config/
│   ├── dvd-ripper.conf.example # Configuration template
│   ├── dvd-encoder.timer       # Systemd timer (15 min)
│   ├── dvd-transfer.timer      # Systemd timer (15 min)
│   ├── dvd-dashboard.service   # Dashboard service
│   └── 99-dvd-ripper.rules     # udev rule for disc detection
├── features/
│   └── pending-identification/ # Feature documentation
├── README.md                   # This file
├── CLAUDE.md                   # Development guidance
├── CONTRIBUTING.md             # Contribution guidelines
├── CHANGELOG.md                # Version history
└── remote-install.sh           # Installation script
```

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License - Use freely for personal or commercial use.

---

*Get those dusty DVDs off the shelf and into your streaming library.*
