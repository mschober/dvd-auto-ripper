# Technical Reference

This document covers the internal architecture, file formats, and system integration details for developers and system administrators.

## Pipeline Architecture

The system uses a 3-stage decoupled pipeline that separates disc handling from encoding and transfer:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Stage 1: ISO  │     │ Stage 2: Encode │     │ Stage 3: NAS    │
│   (udev trigger)│────▶│ (15 min timer)  │────▶│ (15 min timer)  │
│                 │     │                 │     │                 │
│ dvd-iso.sh      │     │ dvd-encoder.sh  │     │ dvd-transfer.sh │
│ - ddrescue ISO  │     │ - HandBrake     │     │ - rsync to NAS  │
│ - Eject disc    │     │ - Preview gen   │     │ - Cleanup local │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                       │
        ▼                       ▼                       ▼
  *.iso-ready             *.encoded-ready          *.transferred
  (state file)            (state file)             (state file)
```

### Why Decoupled?

| Benefit | Explanation |
|---------|-------------|
| **Drive freed quickly** | Disc ejects after ISO (~30 min), not after encode (~2-3 hrs) |
| **Resumable** | ddrescue can resume interrupted ISO creation |
| **Fault isolation** | Encoding failure doesn't block new rips |
| **Queue-based** | Multiple ISOs queue up, processed one at a time |
| **Resource efficient** | Only one encode/transfer runs at a time |

## Scripts

### Pipeline Scripts

| Script | Trigger | Purpose |
|--------|---------|---------|
| `dvd-iso.sh` | udev (disc insert) | Create ISO with ddrescue, eject disc |
| `dvd-encoder.sh` | systemd timer (15 min) | Encode ONE ISO to MKV with HandBrake |
| `dvd-transfer.sh` | systemd timer (15 min) | Transfer ONE MKV to NAS |
| `dvd-ripper.sh` | manual | Legacy monolithic mode (all stages in one process) |
| `dvd-utils.sh` | sourced | Shared library (~40 functions) |

### Management Scripts

| Script | Purpose |
|--------|---------|
| `dvd-dashboard-ctl.sh` | Start/stop/restart/status for web dashboard |
| `dvd-ripper-services-start.sh` | Resume all pipeline services and timers |
| `dvd-ripper-services-stop.sh` | Stop all DVD ripper processes gracefully |
| `dvd-ripper-trigger-pause.sh` | Disable udev rule (for manual operations) |
| `dvd-ripper-trigger-resume.sh` | Re-enable udev rule |

## State File System

State files track pipeline progress. Located in `/var/tmp/dvd-rips/` with visible suffix format.

### State Transitions

```
iso-creating → iso-ready → encoding → encoded-ready → transferring → transferred
     │              │           │            │              │             │
     │              │           │            │              │             └─ On NAS, kept for rename
     │              │           │            │              └─ rsync in progress
     │              │           │            └─ Ready for NAS transfer
     │              │           └─ HandBrake encoding
     │              └─ Ready for encoder pickup
     └─ ddrescue creating ISO
```

### State File Format

Filename: `{SANITIZED_TITLE}-{UNIX_TIMESTAMP}.{STATE}`

Example: `The_Matrix-1703615234.encoded-ready`

### State File Contents (JSON)

```json
{
  "title": "The_Matrix",
  "year": "1999",
  "timestamp": "1703615234",
  "main_title": "1",
  "iso_path": "/var/tmp/dvd-rips/The_Matrix-1703615234.iso",
  "mkv_path": "/var/tmp/dvd-rips/The Matrix (1999).mkv",
  "preview_path": "/var/tmp/dvd-rips/The_Matrix-1703615234.preview.mp4",
  "nas_path": "/volume1/Movies/The Matrix (1999).mkv",
  "needs_identification": false,
  "created_at": "2024-12-27T14:07:14+00:00"
}
```

### Additional State Files

| File Pattern | Purpose |
|--------------|---------|
| `*.iso.deletable` | ISO marked for cleanup after successful encode |
| `*.iso.mapfile` | ddrescue progress map (for resume capability) |

## Lock Files

Prevent concurrent operations within each stage:

| Lock File | Stage | Purpose |
|-----------|-------|---------|
| `/var/run/dvd-ripper-iso.lock` | 1 | Only one ISO creation at a time |
| `/var/run/dvd-ripper-encoder.lock` | 2 | Only one encode at a time |
| `/var/run/dvd-ripper-transfer.lock` | 3 | Only one transfer at a time |

Lock files contain the PID of the holding process. Stale locks (process dead) are automatically detected and cleaned up.

## Systemd Integration

### Timers

| Timer | Interval | Starts At | Purpose |
|-------|----------|-----------|---------|
| `dvd-encoder.timer` | 15 min | 5 min after boot | Trigger encoder service |
| `dvd-transfer.timer` | 15 min | 7 min after boot | Trigger transfer service |

Timers use `RandomizedDelaySec=30s` to avoid thundering herd.

### Services

| Service | Type | Description |
|---------|------|-------------|
| `dvd-encoder.service` | oneshot | Runs dvd-encoder.sh once |
| `dvd-transfer.service` | oneshot | Runs dvd-transfer.sh once |
| `dvd-dashboard.service` | simple | Flask web dashboard (auto-restart) |
| `dvd-ripper@.service` | template | Legacy service for monolithic mode |

### udev Rule

`/etc/udev/rules.d/99-dvd-ripper.rules`:
- Triggers on disc insertion (not eject)
- Uses `systemd-run` for proper cgroup isolation
- Passes device path to dvd-iso.sh

## Web Dashboard

Flask application (`web/dvd-dashboard.py`) running on port 5000.

### Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Main dashboard (queue, disk, logs) |
| `/status` | GET | Service/timer control page |
| `/identify` | GET | Pending identification page |
| `/logs` | GET | Full log viewer |
| `/config` | GET | Configuration display |
| `/architecture` | GET | Pipeline diagram |

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/status` | GET | Pipeline status JSON |
| `/api/queue` | GET | Queue items with metadata |
| `/api/logs` | GET | Recent log lines |
| `/api/disk` | GET | Disk usage stats |
| `/api/trigger/<stage>` | POST | Manually trigger encoder/transfer |
| `/api/identify/pending` | GET | Items needing identification |
| `/api/identify/<file>/rename` | POST | Rename item with new title |
| `/api/preview/<filename>` | GET | Serve preview video |
| `/api/service/<name>` | POST | Control service (start/stop/restart) |
| `/api/timer/<name>` | POST | Control timer (start/stop/enable/disable) |

## Configuration

Config file: `/etc/dvd-ripper.conf`

### Storage

| Setting | Default | Description |
|---------|---------|-------------|
| `STAGING_DIR` | `/var/tmp/dvd-rips` | Local staging directory |
| `DISK_USAGE_THRESHOLD` | `80` | Skip ripping if disk % exceeds this |

### Pipeline Mode

| Setting | Default | Description |
|---------|---------|-------------|
| `PIPELINE_MODE` | `1` | Enable 3-stage pipeline |
| `CREATE_ISO` | `0` | Create ISO from DVD |
| `ENCODE_VIDEO` | `1` | Encode with HandBrake |

### NAS Transfer

| Setting | Default | Description |
|---------|---------|-------------|
| `NAS_ENABLED` | `0` | Enable NAS transfer stage |
| `NAS_HOST` | - | NAS hostname or IP |
| `NAS_USER` | - | SSH username |
| `NAS_PATH` | - | Destination path on NAS |
| `NAS_TRANSFER_METHOD` | `rsync` | `rsync` or `scp` |
| `NAS_FILE_OWNER` | `plex:plex` | chown after transfer |

### HandBrake

| Setting | Default | Description |
|---------|---------|-------------|
| `HANDBRAKE_PRESET` | `Fast 1080p30` | Encoding preset |
| `HANDBRAKE_QUALITY` | `20` | Quality (18=high, 22=smaller) |
| `HANDBRAKE_FORMAT` | `mkv` | Output format |
| `HANDBRAKE_EXTRA_OPTS` | - | Additional CLI options |
| `MIN_FILE_SIZE_MB` | `100` | Minimum valid file size |

### Preview Generation

| Setting | Default | Description |
|---------|---------|-------------|
| `GENERATE_PREVIEWS` | `1` | Create preview clips |
| `PREVIEW_DURATION` | `120` | Preview length (seconds) |
| `PREVIEW_START_PERCENT` | `25` | Start position (% into video) |
| `PREVIEW_RESOLUTION` | `640:360` | Preview resolution |

### Cleanup

| Setting | Default | Description |
|---------|---------|-------------|
| `CLEANUP_MKV_AFTER_TRANSFER` | `1` | Delete local MKV after NAS transfer |
| `CLEANUP_ISO_AFTER_TRANSFER` | `1` | Delete ISO after transfer |
| `CLEANUP_PREVIEW_AFTER_TRANSFER` | `0` | Keep previews for identification |

### Retry/Recovery

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_RETRIES` | `3` | Retry attempts for failed operations |
| `RETRY_DELAY` | `60` | Seconds between retries |

## Generic Title Detection

DVDs with unhelpful metadata are flagged for manual identification.

### Detected Patterns

- `DVD_YYYYMMDD_HHMMSS` - Fallback format when no title found
- `DVD`, `DVD_VIDEO`, `DVDVIDEO` - Common generic labels
- `DISC`, `DISK`, `DISC1`, etc. - Generic disc names
- `VIDEO_TS` - Raw folder names
- `MYDVD` - Authoring tool output
- Titles ≤3 characters

### Identification Workflow

1. Generic title detected during ISO creation
2. `needs_identification: true` set in state file
3. Preview clip generated during encoding
4. Item appears in dashboard `/identify` page
5. User watches preview, enters correct title/year
6. Files renamed everywhere (local + NAS via SSH)

## File Naming

### Internal Names (State Files, ISOs)

```
{SANITIZED_TITLE}-{UNIX_TIMESTAMP}.{extension}
```
- Sanitized: alphanumeric, underscores, hyphens only
- Example: `The_Matrix-1703615234.iso`

### Plex-Compatible Names (MKVs)

```
{Title} ({Year}).mkv
```
- Spaces preserved, title case
- Example: `The Matrix (1999).mkv`

## Dependencies

| Package | Purpose |
|---------|---------|
| `handbrake-cli` | DVD scanning and video encoding |
| `gddrescue` | Reliable ISO creation with error recovery |
| `ffmpeg` | Preview clip generation |
| `ffprobe` | Video duration detection |
| `rsync` | NAS file transfer |
| `openssh-client` | SSH for NAS operations |
| `eject` | Disc ejection |
| `python3-flask` | Web dashboard |

### libdvdcss

Required for commercial/encrypted DVDs. Without it, most store-bought DVDs will fail.

```bash
# Debian/Ubuntu
sudo apt install libdvd-pkg
sudo dpkg-reconfigure libdvd-pkg
```

## Directory Structure

```
/var/tmp/dvd-rips/          # Staging directory
├── *.iso-creating          # ISO creation in progress
├── *.iso-ready             # Ready for encoding
├── *.encoding              # Encoding in progress
├── *.encoded-ready         # Ready for transfer
├── *.transferring          # Transfer in progress
├── *.transferred           # Complete (kept for rename)
├── *.iso                   # ISO image files
├── *.iso.mapfile           # ddrescue progress maps
├── *.iso.deletable         # ISOs marked for cleanup
├── *.mkv                   # Encoded video files
└── *.preview.mp4           # Preview clips

/var/log/dvd-ripper.log     # Application log
/var/run/dvd-ripper-*.lock  # Stage lock files
/etc/dvd-ripper.conf        # Configuration
/usr/local/bin/dvd-*.sh     # Installed scripts
```

## Error Recovery

### Interrupted ISO Creation

State file `*.iso-creating` detected on next run → ddrescue resumes using mapfile.

### Interrupted Encoding

State file `*.encoding` detected → reverts to `*.iso-ready` for retry.

### Interrupted Transfer

State file `*.transferring` detected → checks if local file exists:
- Exists: reverts to `*.encoded-ready` for retry
- Gone: assumes success, removes state file

### Stale Locks

Lock file exists but PID not running → lock automatically released.
