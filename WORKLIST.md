# DVD Auto-Ripper Worklist

Future improvements and backlog items. See [GitHub Issues](https://github.com/mschober/dvd-auto-ripper/issues) for tracking.

## Pending Items

### Plex-Ready Naming Convention
- **Issue:** [#2](https://github.com/mschober/dvd-auto-ripper/issues/2)
- **Priority:** High
- **Description:** Update video naming to use Plex-compatible format
- **Current:** `The_Matrix-1999-1703615234.mkv`
- **Desired:** `The Matrix (1999).mkv`
- **Notes:**
  - Remove underscores, use spaces
  - Format: `Title (Year).ext`
  - Remove timestamp from filename (only needed for dedup during processing)
  - Consider using TMDb/OMDb API for accurate title/year lookup
  - Handle TV shows differently: `Show Name - S01E01 - Episode Title.mkv`

### ISO Cleanup Job
- **Issue:** [#3](https://github.com/mschober/dvd-auto-ripper/issues/3)
- **Priority:** Medium
- **Description:** Create monthly cron job to delete `*.iso.deletable` files
- **Notes:** Files are marked `.deletable` after successful encoding

### Metadata Lookup Integration
- **Issue:** [#4](https://github.com/mschober/dvd-auto-ripper/issues/4)
- **Priority:** Low
- **Description:** Integrate with TMDb or OMDb API for accurate movie metadata
- **Notes:**
  - Would improve title accuracy
  - Could fetch year, genres, etc.
  - Requires API key configuration

### Web UI for Monitoring
- **Issue:** [#5](https://github.com/mschober/dvd-auto-ripper/issues/5)
- **Priority:** Low
- **Description:** Simple web interface to view queue status and logs
- **Notes:** Could show pending ISOs, encoding progress, transfer status

### Email/Notification on Completion
- **Issue:** [#6](https://github.com/mschober/dvd-auto-ripper/issues/6)
- **Priority:** Low
- **Description:** Send notification when rip/encode/transfer completes
- **Notes:** Could integrate with email, Slack, Discord, etc.
