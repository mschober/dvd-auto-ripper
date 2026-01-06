# Pending Identification Feature

## Overview

The Pending Identification feature helps users properly name DVDs that were ripped with generic or fallback names. Many DVDs don't have proper title metadata embedded, resulting in names like `DVD_20251227_143022` instead of the actual movie title.

This feature provides a workflow to:
1. Detect movies with generic names
2. Generate preview clips for visual identification
3. Allow users to enter the correct title via the dashboard
4. Rename files locally and on NAS

## How It Works

### 1. Generic Title Detection

During ISO creation, the system detects generic titles using pattern matching:

**Detected patterns:**
- `DVD_YYYYMMDD_HHMMSS` - Our fallback format when no metadata found
- `DVD`, `DVD_VIDEO`, `DVDVIDEO` - Common generic volume labels
- `DISC`, `DISK`, `DISC1`, etc. - Generic disc names
- `VIDEO_TS` - Raw folder names
- `MYDVD` - Generic authoring tool output
- Titles 3 characters or shorter

When a generic title is detected, `needs_identification: true` is set in the state file metadata.

### 2. Preview Generation

During the encoding stage (Stage 2), a 2-minute preview clip is generated:

- **Start position:** 25% into the movie (skips intro/commercials)
- **Duration:** 2 minutes
- **Resolution:** 360p (small file size)
- **Format:** MP4 with H.264 (browser-compatible)
- **File location:** `{STAGING_DIR}/{title}-{timestamp}.preview.mp4`

Preview generation requires `ffmpeg` and `ffprobe` to be installed.

### 3. Identification Workflow

1. User visits Dashboard → "Pending ID" link (shows count badge if items pending)
2. The `/identify` page shows all items needing identification
3. Each item displays:
   - Video player with the preview clip
   - Current generic name
   - Input fields for proper title and year
4. User enters correct information and clicks "Rename & Identify"
5. System renames:
   - MKV file (to Plex format: `Title (Year).mkv`)
   - State file
   - Preview file
   - ISO file (if still present)
   - NAS file (if already transferred, via SSH)

### 4. State Transitions

```
iso-creating → iso-ready → encoding → encoded-ready → transferring → transferred
                    ↑                        ↑                            ↑
                    └── Can rename ──────────┴────────────────────────────┘
```

Files can only be renamed in these "completed" states:
- `iso-ready` - Before encoding
- `encoded-ready` - After encoding, before transfer
- `transferred` - After NAS transfer (requires SSH rename)

Files **cannot** be renamed while in active processing states:
- `iso-creating`
- `encoding`
- `transferring`

## Configuration

Add these settings to `/etc/dvd-ripper.conf`:

```bash
# Preview Generation
GENERATE_PREVIEWS="1"           # Enable preview generation (requires ffmpeg)
PREVIEW_DURATION="120"          # Preview length in seconds
PREVIEW_START_PERCENT="25"      # Start position (% into video)
PREVIEW_RESOLUTION="640:360"    # Resolution (width:height)

# Cleanup
CLEANUP_PREVIEW_AFTER_TRANSFER="0"  # Keep previews for identification (default)
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/identify` | GET | Web page for identification |
| `/api/identify/pending` | GET | JSON list of items needing ID |
| `/api/identify/<file>/rename` | POST | Rename item with new title/year |
| `/api/preview/<filename>` | GET | Serve preview video file |

### Rename API

**Request:**
```json
POST /api/identify/DVD_20251227_143022-1703615234.encoded-ready/rename
{
    "title": "The Matrix",
    "year": "1999"
}
```

**Response:**
```json
{
    "status": "renamed",
    "new_state_file": "The_Matrix-1703615234.encoded-ready",
    "new_title": "The Matrix",
    "new_year": "1999"
}
```

## Files Modified

This feature adds/modifies the following files:

| File | Changes |
|------|---------|
| `scripts/dvd-utils.sh` | Added `is_generic_title()` function, updated `build_state_metadata()` |
| `scripts/dvd-encoder.sh` | Added `generate_preview()` function, preview config vars |
| `scripts/dvd-transfer.sh` | Added `transferred` state, preview cleanup option |
| `web/dvd-dashboard.py` | New routes, IDENTIFY_HTML template, rename logic |
| `config/dvd-ripper.conf.example` | Preview generation settings |

## State File Schema

Updated metadata JSON schema:

```json
{
  "title": "DVD_20251227_143022",
  "year": "",
  "timestamp": "1703615234",
  "main_title": "1",
  "iso_path": "/var/tmp/dvd-rips/DVD_20251227_143022-1703615234.iso",
  "mkv_path": "/var/tmp/dvd-rips/Dvd 20251227 143022.mkv",
  "preview_path": "/var/tmp/dvd-rips/DVD_20251227_143022-1703615234.preview.mp4",
  "nas_path": "/mnt/plex/Movies/Dvd 20251227 143022.mkv",
  "needs_identification": true,
  "created_at": "2024-12-27T14:07:14+00:00"
}
```

After identification:
```json
{
  "title": "The_Matrix",
  "year": "1999",
  "needs_identification": false,
  "identified_at": "2024-12-27T16:30:00+00:00",
  "original_title": "DVD_20251227_143022"
}
```

## Dependencies

- **ffmpeg**: Required for preview generation
- **ffprobe**: Part of ffmpeg, used to get video duration
- **SSH access**: Required for NAS remote rename (already configured for transfers)

## Edge Cases

| Scenario | Handling |
|----------|----------|
| ffmpeg not installed | Skip preview generation, log warning |
| Preview generation fails | Continue without preview, allow ID by filename |
| NAS unreachable during rename | Return error, user retries later |
| Same title already exists | Add timestamp suffix to prevent collision |
| Very short video | Preview starts at calculated percentage |

## Future Enhancements

- TMDB/OMDB integration for title suggestions
- Batch identification for multiple items
- Video fingerprinting for automatic matching
- Undo/history for renames
