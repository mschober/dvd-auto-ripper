# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.0] - 2026-01-05

### Added
- **Distributed Encoding Cluster** - Share encoding workload across multiple machines
- Local transfer mode for machines that ARE the media server (move vs rsync)
- Cluster configuration options for distributed encoding
- Cluster API endpoints: `/api/cluster/status`, `/api/cluster/peers`, `/api/worker/capacity`, `/api/cluster/ping`
- Job acceptance endpoint: `/api/worker/accept-job` for receiving remote encoding jobs
- Job completion callback: `/api/cluster/job-complete` for notifying origin nodes
- Worker capacity calculation based on load and encoding slots
- Load-based job distribution - offload to peers when local load is high
- Remote job handling - encode on peer, return MKV to origin for final transfer
- New state files for distributed jobs: `*.distributing`, `*.distributed-to-{peer}`
- Cluster dashboard page (`/cluster`) showing all nodes, capacity, and distributed jobs

### Configuration
- `TRANSFER_MODE` - "remote" (rsync to NAS) or "local" (mv to local path)
- `LOCAL_LIBRARY_PATH` - Destination for local transfer mode
- `CLUSTER_ENABLED` - Enable cluster mode
- `CLUSTER_NODE_NAME` - This machine's identifier
- `CLUSTER_PEERS` - Space-separated "name:host:port" entries
- `CLUSTER_SSH_USER` - SSH user for rsync between nodes
- `CLUSTER_REMOTE_STAGING` - Remote staging directory

## [1.4.0] - 2026-01-05

### Added
- System Health monitoring page (`/health`) with HTOP-like process view
- Real-time CPU, memory, load average monitoring
- Temperature and fan speed monitoring via lm-sensors
- Kill button for DVD ripper processes with state cleanup
- Parallel encoding support (disabled by default, load-based dynamic worker pool)
- API endpoints: `/api/health`, `/api/processes`, `/api/kill/<pid>`
- lm-sensors auto-installation in remote-install.sh

### Configuration
- `ENABLE_PARALLEL_ENCODING` - Enable/disable parallel encoding
- `MAX_PARALLEL_ENCODERS` - Maximum concurrent encoding processes
- `ENCODER_LOAD_THRESHOLD` - Load threshold for starting new encoders

## [1.3.0] - 2026-01-05

### Added
- Real-time progress bars on main dashboard
- Progress parsing for HandBrake, ddrescue, and rsync operations
- ETA and speed display for active encoding/transfer jobs
- Auto-refresh progress updates every 10 seconds
- API endpoint: `/api/progress`
- `dvd-dashboard-install.sh` script for standalone dashboard installation
- Dashboard auto-restart on remote-install updates

## [1.2.0] - 2025-01-05

### Added
- Udev trigger control in Status page (pause/resume disc detection from web UI)
- API endpoint for udev control (`POST /api/udev/<action>`)
- `dvd-dashboard-ctl.sh` script for start/stop/restart/status of web dashboard

### Documentation
- Complete README overhaul with Plex streaming focus
- New TECHNICAL.md with architecture details and API reference
- Slimmed CLAUDE.md to focus on development workflow

## [1.1.0] - 2025-01-05

### Added
- Service status page (`/status`) with start/stop/restart controls for services
- Timer management with pause/unpause and enable/disable controls
- API endpoints for service control (`POST /api/service/<name>`)
- API endpoints for timer control (`POST /api/timer/<name>`)
- Status link in dashboard footer
- Versioning documentation in CONTRIBUTING.md

## [1.0.0] - 2024-12-27

### Added
- Initial 3-stage pipeline architecture (ISO → Encode → Transfer)
- Web dashboard for monitoring pipeline status
- Pending identification feature for generic-named DVDs
- Preview generation during encoding for movie identification
- Remote rename capability for NAS-transferred files
- Configuration merge option for remote-install.sh
- Management scripts (dvd-ripper-start.sh, dvd-ripper-stop.sh, dvd-ripper-status.sh)

### Pipeline Scripts
- `dvd-iso.sh` - Stage 1: Create ISO from DVD via udev trigger
- `dvd-encoder.sh` - Stage 2: Encode ISO to MKV via systemd timer
- `dvd-transfer.sh` - Stage 3: Transfer MKV to NAS via systemd timer
- `dvd-utils.sh` - Shared utility functions

### Dashboard Features
- Real-time pipeline status monitoring
- Queue management and visualization
- Log viewing
- Configuration display
- Architecture documentation page
- Disk usage monitoring
