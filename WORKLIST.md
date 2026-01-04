# DVD Auto-Ripper Worklist

Future improvements and backlog items.

## Pending Items

### Plex-Ready Naming Convention
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
- **Priority:** Medium
- **Description:** Create monthly cron job to delete `*.iso.deletable` files
- **Notes:** Files are marked `.deletable` after successful encoding

### Metadata Lookup Integration
- **Priority:** Low
- **Description:** Integrate with TMDb or OMDb API for accurate movie metadata
- **Notes:**
  - Would improve title accuracy
  - Could fetch year, genres, etc.
  - Requires API key configuration

### Web UI for Monitoring
- **Priority:** Low
- **Description:** Simple web interface to view queue status and logs
- **Notes:** Could show pending ISOs, encoding progress, transfer status

### Email/Notification on Completion
- **Priority:** Low
- **Description:** Send notification when rip/encode/transfer completes
- **Notes:** Could integrate with email, Slack, Discord, etc.
