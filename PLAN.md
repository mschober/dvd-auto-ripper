# DVD Auto-Ripper Plan

## Project Overview
Automated DVD ripping system for Linux servers that monitors disc insertion, rips DVDs using handbrake-cli, and transfers completed files to NAS over LAN.

## System Requirements

### Hardware/Network
- Linux server with CD/DVD drive
- CD/DVD drive with udev event support
- NAS device on LAN (optional)

### Software Dependencies
- handbrake-cli
- udev
- inotify-tools (optional, for monitoring)
- rsync or scp (for NAS transfer)
- Standard Linux utilities (bash, date, etc.)

## Architecture Components

### 1. udev Rule (`/etc/udev/rules.d/*cdrom*.rules`)
- Triggers on DVD insertion events
- Executes main ripping script in `/usr/local/bin/`
- Should handle both insertion and removal events

### 2. Main Ripping Script (`/usr/local/bin/dvd-ripper.sh`)
**Responsibilities:**
- Detect DVD insertion
- Extract DVD title using handbrake-cli or lsdvd
- Generate filename with pattern: `{title}-{year}-{timestamp}`
  - Timestamp format: `$(date +%s)` (Unix epoch)
- Check for duplicates (existing files matching `{title}-{year}-*`)
- Rip DVD to local staging directory
- Verify rip completed successfully
- Transfer to NAS
- Cleanup local files after successful transfer
- Log all operations

### 3. State Management
**Lock/PID file:** Prevent multiple simultaneous rips
- Location: `/var/run/dvd-ripper.pid` or `/tmp/dvd-ripper.lock`

**State tracking:** Resume capability for interrupted rips
- Track current operation (ripping, transferring, etc.)
- Allow recovery from interruptions

### 4. Configuration File (`/etc/dvd-ripper.conf`)
```bash
# Local staging directory
STAGING_DIR="/var/tmp/dvd-rips"

# NAS configuration
NAS_HOST="your-nas-ip"  # Your NAS IP or hostname
NAS_USER="username"      # TBD
NAS_PATH="/path/to/dvds" # TBD
NAS_TRANSFER_METHOD="rsync"  # or "scp"

# Handbrake settings
HANDBRAKE_PRESET="Fast 1080p30"  # TBD
HANDBRAKE_QUALITY="20"           # TBD
HANDBRAKE_FORMAT="mkv"           # or "mp4"

# Logging
LOG_FILE="/var/log/dvd-ripper.log"
LOG_LEVEL="INFO"  # DEBUG, INFO, WARN, ERROR

# Retry settings
MAX_RETRIES="3"
RETRY_DELAY="60"  # seconds
```

### 5. Logging System
- Location: `/var/log/dvd-ripper.log`
- Rotation via logrotate
- Include timestamps, operation stage, success/failure, errors

## Workflow

### Detailed Process Flow

1. **DVD Insertion Event**
   - udev detects disc insertion
   - Triggers `/usr/local/bin/dvd-ripper.sh`

2. **Pre-Flight Checks**
   - Check if script already running (PID/lock file)
   - Verify disc is readable DVD (not CD, not blank)
   - Wait for disc to be fully mounted/recognized
   - Create/update lock file

3. **DVD Information Extraction**
   - Use `handbrake-cli --scan` or `lsdvd` to get:
     - DVD title
     - Year (if available in metadata)
     - Main title track number
     - Duration (to verify it's a movie, not menu loop)
   - Sanitize title for filename (remove special chars, spaces)

4. **Duplicate Detection**
   - Search staging directory for `{title}-{year}-*`
   - Search NAS (via SSH/rsync) for `{title}-{year}-*`
   - If found: Log warning, eject disc, exit
   - If not found: Proceed

5. **Generate Filename**
   - Pattern: `{title}-{year}-$(date +%s).{ext}`
   - Example: `TheMatrix-1999-1703615234.mkv`
   - Full path: `${STAGING_DIR}/{title}-{year}-{timestamp}.{ext}`

6. **Ripping Process**
   - Create state file: `${STAGING_DIR}/.ripping-{title}-{timestamp}`
   - Execute handbrake-cli with:
     - Input: DVD device (`/dev/sr0` or similar)
     - Output: Staging directory file
     - Preset and quality settings from config
     - Progress monitoring
   - Monitor for interruptions (check exit code)
   - On failure: Log error, retry up to MAX_RETRIES
   - On success: Verify output file exists and size > 0

7. **Post-Rip Verification**
   - Check file size (should be > 100MB for movies)
   - Optional: Quick integrity check with ffprobe
   - Update state file: `.ripping-*` → `.completed-*`

8. **NAS Transfer**
   - Update state file: `.transferring-{title}-{timestamp}`
   - Use rsync or scp:
     - `rsync -avz --progress {local_file} {NAS_USER}@{NAS_HOST}:{NAS_PATH}/`
   - Verify transfer (compare file sizes or checksums)
   - On failure: Retry up to MAX_RETRIES
   - On success: Verify remote file exists

9. **Cleanup**
   - Delete local staging file
   - Delete state files
   - Remove lock file
   - Eject disc
   - Log completion

10. **Error Handling**
    - All errors logged to `/var/log/dvd-ripper.log`
    - Critical errors: Email notification (optional)
    - State files allow resume on next run

## Error Scenarios & Recovery

### Interrupted Rip (Power Loss, Process Kill)
- **Detection:** State file `.ripping-*` exists on next run
- **Recovery:** Delete partial file, restart rip

### Interrupted Transfer
- **Detection:** State file `.transferring-*` exists, local file exists
- **Recovery:** Resume/restart transfer, verify completion

### Network Failure During Transfer
- **Detection:** rsync/scp exit code != 0
- **Recovery:** Retry with exponential backoff (MAX_RETRIES)
- **Fallback:** Leave file in staging, log error, continue

### Duplicate Detected Mid-Process
- **Detection:** File appears on NAS during our rip
- **Recovery:** Abort, cleanup local files, eject disc

### Unreadable/Damaged Disc
- **Detection:** handbrake scan fails or times out
- **Recovery:** Log error, eject disc, exit

### Disc Ejected During Rip
- **Detection:** handbrake error, device disappear
- **Recovery:** Log error, cleanup partial file, exit

## File Structure

```
dvd-auto-ripper/
├── PLAN.md                          # This file
├── CLAUDE.md                        # Claude Code guidance
├── README.md                        # User documentation
├── scripts/
│   ├── dvd-ripper.sh               # Main ripping script
│   ├── dvd-utils.sh                # Helper functions library
│   └── install.sh                  # Installation script
├── config/
│   ├── dvd-ripper.conf.example     # Example configuration
│   ├── 99-dvd-ripper.rules         # udev rule file
│   └── dvd-ripper.logrotate        # logrotate configuration
└── tests/
    ├── test-duplicate-detection.sh
    ├── test-filename-generation.sh
    └── test-error-recovery.sh
```

## Installation Steps (Future)

1. Install dependencies: `handbrake-cli`, `lsdvd`, etc.
2. Copy scripts to `/usr/local/bin/`
3. Copy config to `/etc/dvd-ripper.conf`
4. Copy udev rule to `/etc/udev/rules.d/99-dvd-ripper.rules`
5. Copy logrotate config to `/etc/logrotate.d/dvd-ripper`
6. Reload udev: `udevadm control --reload-rules`
7. Create staging directory
8. Configure NAS credentials (SSH keys for passwordless access)
9. Test with sample DVD

## Testing Strategy

### Unit Tests
- Filename generation and sanitization
- Duplicate detection logic
- Configuration parsing
- State file management

### Integration Tests
- Full workflow with test DVD
- Error injection (kill process mid-rip)
- Network failure simulation
- Duplicate handling

### Manual Tests
- Insert multiple DVDs in sequence
- Test with various DVD types (movie, TV series, damaged)
- Verify NAS transfer and permissions

## Security Considerations

- SSH key-based authentication for NAS (no passwords in config)
- Restrict script execution permissions (chmod 750)
- Validate/sanitize all extracted metadata before using in filenames
- Log rotation to prevent disk fill
- Consider SELinux/AppArmor policies

## Future Enhancements (Out of Scope for v1)

- Web UI for monitoring rip queue and status
- Email notifications on completion/errors
- Metadata extraction and NFO file generation
- Automatic subtitle extraction
- Multi-disc detection and naming (Disc 1, Disc 2)
- Barcode scanner integration for accurate metadata
- Queue management for batch processing
- Remote management API

## Questions to Resolve

1. **NAS Details:**
   - NAS IP address or hostname?
   - NAS username?
   - NAS destination path?
   - NAS file transfer method preference (rsync vs scp)?

2. **Handbrake Settings:**
   - Preferred output format (MKV, MP4)?
   - Quality preset?
   - Audio/subtitle track preferences?

3. **DVD Metadata:**
   - How to extract year? (may not be in all DVDs)
   - Fallback if title is generic (e.g., "DVD_VIDEO")?
   - Use external API (OMDb, TMDb) for accurate metadata?

4. **Notification:**
   - Email notifications desired?
   - Other notification methods (Slack, Discord, etc.)?

5. **Storage:**
   - Maximum staging directory size limit?
   - Cleanup policy for failed rips?
