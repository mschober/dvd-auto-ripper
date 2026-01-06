# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
