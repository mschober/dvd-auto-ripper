# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a DVD auto-ripper utility for Linux servers. It automatically detects DVD insertion via udev, creates an ISO backup using ddrescue, encodes to video using HandBrake, and transfers the output to a NAS/Plex server.

## Architecture: 3-Stage Pipeline

The system uses a decoupled pipeline architecture for reliability and efficiency:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Stage 1: ISO  │     │ Stage 2: Encode │     │ Stage 3: NAS    │
│   (udev trigger)│────▶│ (15 min timer)  │────▶│ (15 min timer)  │
│                 │     │                 │     │                 │
│ dvd-iso.sh      │     │ dvd-encoder.sh  │     │ dvd-transfer.sh │
│ - ddrescue ISO  │     │ - HandBrake     │     │ - rsync to NAS  │
│ - Eject disc    │     │ - Mark ISO done │     │ - Cleanup local │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                       │
        ▼                       ▼                       ▼
  .iso-ready-*           .encoded-ready-*         [Complete]
  (state file)            (state file)
```

### Benefits of Pipeline Mode
- **Drive freed immediately** - disc ejected after ISO creation (~30 min vs 2-4 hours)
- **Resumable** - ddrescue can resume interrupted ISO creation
- **Decoupled** - encoding/transfer failures don't affect disc operations
- **Queue-based** - multiple ISOs can queue up for encoding

### Scripts

| Script | Trigger | Purpose |
|--------|---------|---------|
| `dvd-iso.sh` | udev (disc insert) | Create ISO from DVD, eject immediately |
| `dvd-encoder.sh` | systemd timer (15 min) | Encode ONE queued ISO to MKV |
| `dvd-transfer.sh` | systemd timer (15 min) | Transfer ONE encoded video to NAS |
| `dvd-ripper.sh` | manual | Legacy monolithic mode (all-in-one) |
| `dvd-utils.sh` | sourced | Shared library functions |

### State Files

State files in `/var/tmp/dvd-rips/` track pipeline progress:

| State File | Meaning |
|------------|---------|
| `.iso-creating-TITLE-TS` | ISO creation in progress |
| `.iso-ready-TITLE-TS` | ISO complete, waiting for encoder |
| `.encoding-TITLE-TS` | HandBrake encoding in progress |
| `.encoded-ready-TITLE-TS` | Video ready for NAS transfer |
| `.transferring-TITLE-TS` | NAS transfer in progress |
| `*.iso.deletable` | ISO marked for cleanup after encode |

### Lock Files

| Lock File | Purpose |
|-----------|---------|
| `/var/run/dvd-ripper-iso.lock` | Stage 1: Only one ISO creation |
| `/var/run/dvd-ripper-encoder.lock` | Stage 2: Only one encode |
| `/var/run/dvd-ripper-transfer.lock` | Stage 3: Only one transfer |

### Configuration

Key settings in `/etc/dvd-ripper.conf`:
- `PIPELINE_MODE="1"` - Enable 3-stage pipeline (default)
- `NAS_ENABLED="1"` - Enable NAS transfer stage
- `NAS_HOST`, `NAS_USER`, `NAS_PATH` - NAS connection details

### Other Files

- **config/dvd-ripper.conf.example**: Configuration template
- **config/99-dvd-ripper.rules**: udev rule for disc detection
- **config/dvd-encoder.timer**: systemd timer for encoding
- **config/dvd-transfer.timer**: systemd timer for NAS transfer
- **deploy.sh**: Deployment script for syncing to remote server
- **remote-install.sh**: Installation script for remote server

## Development Workflow

### Branch Strategy

Use feature branches with naming convention: `<github-username>/<branch-type>/<branch-name>`

```bash
# Create feature branch
git checkout -b mschober/feature/my-feature

# Push and create PR
git push -u origin mschober/feature/my-feature
gh pr create --title "Feature: Description" --body "Details"

# Merge via PR (squash)
gh pr merge --squash
```

Branch types: `feature`, `fix`, `refactor`, `docs`, `test`

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full guidelines.

### Making Changes

1. Create feature branch from `main`
2. Make changes and commit
3. Push branch and create PR
4. After merge, deploy to server:
   ```bash
   ssh <user>@<server> 'cd /opt/dvd-auto-ripper && sudo git pull && sudo ./remote-install.sh'
   ```

### Initial Server Setup

```bash
# SSH to server (use -J <jump-host> if behind a jump box)
ssh <user>@<server>

# Install dependencies (Ubuntu/Debian)
sudo apt update
sudo apt install -y git handbrake-cli rsync openssh-client eject gddrescue ffmpeg

# Clone the repo to /opt (standard location for third-party apps)
sudo git clone https://github.com/mschober/dvd-auto-ripper.git /opt/dvd-auto-ripper

# Run installation (--install-libdvdcss required for commercial DVDs)
cd /opt/dvd-auto-ripper
sudo ./remote-install.sh --install-libdvdcss
```

### Testing

Manual testing (pipeline mode):
```bash
# On remote server - test each stage
sudo /usr/local/bin/dvd-iso.sh /dev/sr0      # Stage 1: Create ISO
sudo systemctl start dvd-encoder.service      # Stage 2: Encode
sudo systemctl start dvd-transfer.service     # Stage 3: Transfer

# Monitor progress
tail -f /var/log/dvd-ripper.log
```

Legacy monolithic mode (if needed):
```bash
sudo /usr/local/bin/dvd-ripper.sh /dev/sr0
```

### Deployment

```bash
# From local machine - commit and push changes
git add -A && git commit -m "Your message" && git push

# On remote server
ssh <user>@<server> 'cd /opt/dvd-auto-ripper && sudo git pull && sudo ./remote-install.sh'
```

## Dependencies

- HandBrake CLI (handbrake-cli)
- rsync
- openssh-client
- eject
- bash 4.0+

## Common Tasks

### Monitoring

```bash
# View logs
tail -f /var/log/dvd-ripper.log

# Check timer status
systemctl list-timers | grep dvd

# Check queue status
ls -la /var/tmp/dvd-rips/.iso-ready-*      # Pending encodes
ls -la /var/tmp/dvd-rips/.encoded-ready-*  # Pending transfers
ls -la /var/tmp/dvd-rips/*.iso.deletable   # ISOs awaiting cleanup
```

### Manual Triggers

```bash
# Manually trigger stages
sudo systemctl start dvd-encoder.service   # Encode now
sudo systemctl start dvd-transfer.service  # Transfer now

# Test ISO creation with a DVD
sudo /usr/local/bin/dvd-iso.sh /dev/sr0
```

### Troubleshooting

```bash
# Check if locks are held
ls -la /var/run/dvd-ripper-*.lock

# View systemd journal for specific service
journalctl -u dvd-encoder.service -f
journalctl -u dvd-transfer.service -f

# Check config
sudo cat /etc/dvd-ripper.conf
```
