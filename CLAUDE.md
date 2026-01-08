# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DVD Auto-Ripper converts physical DVD collections into Plex-ready streaming libraries. Insert a disc, walk away, find it ready to stream.

**Goal**: Get dusty DVDs off the shelf and into a streamable private library.

**Architecture**: 3-stage pipeline (ISO → Encode → Transfer) with queue-based processing.

See [TECHNICAL.md](./TECHNICAL.md) for detailed architecture, state files, and configuration reference.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/dvd-iso.sh` | Stage 1: Create ISO, eject disc |
| `scripts/dvd-encoder.sh` | Stage 2: Encode to MKV |
| `scripts/dvd-transfer.sh` | Stage 3: Transfer to NAS |
| `scripts/dvd-utils.sh` | Shared library (~40 functions) |
| `web/dvd-dashboard.py` | Flask dashboard |
| `config/dvd-ripper.conf.example` | Configuration template |

## Development Workflow

### Branch Strategy

Use feature branches: `<github-username>/<branch-type>/<branch-name>`

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

See [CONTRIBUTING.md](./CONTRIBUTING.md) for versioning and code style.

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
# SSH to server
ssh <user>@<server>

# Install dependencies (Ubuntu/Debian)
sudo apt update
sudo apt install -y git handbrake-cli rsync openssh-client eject gddrescue ffmpeg python3-flask

# Clone and install
sudo git clone https://github.com/mschober/dvd-auto-ripper.git /opt/dvd-auto-ripper
cd /opt/dvd-auto-ripper
sudo ./remote-install.sh --install-libdvdcss
```

## Testing

### Manual Pipeline Test

```bash
# Stage 1: Create ISO
sudo /usr/local/bin/dvd-iso.sh /dev/sr0

# Stage 2: Encode (or wait for timer)
sudo systemctl start dvd-encoder.service

# Stage 3: Transfer (or wait for timer)
sudo systemctl start dvd-transfer.service

# Monitor
tail -f /var/log/dvd-ripper.log
```

### Check Queue Status

```bash
ls -la /var/tmp/dvd-rips/*.iso-ready       # Pending encodes
ls -la /var/tmp/dvd-rips/*.encoded-ready   # Pending transfers
ls -la /var/tmp/dvd-rips/*.transferred     # Completed
```

## Common Tasks

### Monitoring

```bash
# View logs
tail -f /var/log/dvd-ripper.log

# Check timers
systemctl list-timers | grep dvd

# Dashboard
open http://<server>:5000
```

### Manual Triggers

```bash
sudo systemctl start dvd-encoder.service   # Encode now
sudo systemctl start dvd-transfer.service  # Transfer now
```

### Troubleshooting

```bash
# Check locks
ls -la /run/dvd-ripper/*.lock

# Service logs
journalctl -u dvd-encoder.service -f
journalctl -u dvd-transfer.service -f

# Config
cat /etc/dvd-ripper.conf
```

## Dependencies

- handbrake-cli, gddrescue, ffmpeg, rsync, openssh-client, eject, python3-flask, curl, jq
- libdvdcss (required for commercial DVDs)
