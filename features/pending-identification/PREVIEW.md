# Preview & Identification Guide

## What Are Previews?

When a DVD is ripped and encoded, the system generates a short preview clip from the movie. This clip helps you identify discs that had generic or missing title metadata — names like `DVD_VIDEO`, `DISC1`, or `DVD_20251227_143022` instead of the actual movie title.

Previews are 2-minute MP4 clips, starting 25% into the movie to skip past studio logos and menu loops. They play directly in your browser on the dashboard.

## When Are Previews Created?

Previews are generated automatically during Stage 2 (encoding) for **every** disc, regardless of whether the title looks correct. This is intentional — even discs with plausible-looking names can be wrong (e.g., a bonus disc labeled `MAIN_FEATURE`).

Preview generation requires `ffmpeg` and `ffprobe`. If either is missing, the system logs a warning and continues without a preview. You can still identify items by filename alone.

## Identifying and Renaming Movies

### Finding Items That Need Attention

Open the dashboard at `http://<server>:5000` and look for the **Pending ID** link in the navigation. A count badge appears when items need identification.

The `/identify` page has two sections:

- **Needs Identification** — Discs with generic names detected by the system
- **Audit Flags** — Items flagged by the hourly audit for suspicious titles, small files, or missing archives

### The Rename Workflow

Each item on the page shows:

1. **Video preview** — Watch the clip to figure out what movie this is
2. **Current name** — The raw disc title, displayed for reference
3. **Pipeline state** — Where the item is in processing (e.g., `encoded-ready`, `transferred`)
4. **Title field** — Enter the correct movie title
5. **Year field** — Enter the 4-digit release year (optional but recommended for Plex)

Click **Rename & Identify** to apply. The system renames:

- The MKV file → Plex format: `Movie Title (2024).mkv`
- The ISO or dvdbackup directory (if still on disk)
- The `.archive-ready` marker (if present)
- The preview clip
- The NAS copy (if already transferred, via SSH)

The card fades out on success. If all items are handled, the page reloads automatically.

### Plex Naming Format

Plex expects movie files named as:

```
Movie Title (2024).mkv
```

The rename operation applies this format automatically. Enter the title in natural case (e.g., `The Matrix`, not `THE_MATRIX`) and the system handles the rest. If you omit the year, the file is named `Movie Title.mkv` — this works but Plex matching is more reliable with a year.

### When Can You Rename?

Renaming is available in these pipeline states:

| State | Meaning |
|-------|---------|
| `iso-ready` | ISO created, waiting for encode |
| `encoded-ready` | Encoded, waiting for NAS transfer |
| `transferred` | On NAS (rename happens locally and remotely) |

Items in active states (`iso-creating`, `encoding`, `transferring`) cannot be renamed until processing completes.

## Configuration

These settings go in `/etc/dvd-ripper.conf`:

```bash
# Enable or disable preview generation (0=disabled, 1=enabled)
GENERATE_PREVIEWS="1"

# Preview clip length in seconds
PREVIEW_DURATION="120"

# Where in the movie to start the preview, as a percentage
# 25 = one quarter in, past studio logos and ads
PREVIEW_START_PERCENT="25"

# Output resolution in ffmpeg scale format
# 640:360 (360p), 854:480 (480p), 1280:720 (720p)
PREVIEW_RESOLUTION="640:360"

# Delete preview files after NAS transfer (0=keep, 1=delete)
# Keep them if you want to identify movies after transfer
CLEANUP_PREVIEW_AFTER_TRANSFER="0"
```

### Tuning Tips

- **Longer previews** — Increase `PREVIEW_DURATION` if 2 minutes isn't enough to recognize a movie. Previews are low-resolution, so even 5 minutes is a small file.
- **Different start point** — If previews keep landing on opening credits, raise `PREVIEW_START_PERCENT` to 30 or 35.
- **Skip previews entirely** — Set `GENERATE_PREVIEWS="0"` if you always know what disc you're inserting. You can still rename items by filename on the identify page.

## Generic Title Detection

The system flags these patterns as needing identification:

| Pattern | Example |
|---------|---------|
| Fallback timestamp | `DVD_20251227_143022` |
| Generic volume labels | `DVD`, `DVD_VIDEO`, `DVDVIDEO` |
| Disc labels | `DISC1`, `DISK2` |
| Raw folder names | `VIDEO_TS` |
| Authoring defaults | `MYDVD` |
| Very short titles | Any title 3 characters or shorter |

Discs with titles that don't match these patterns are assumed to be correctly named. They still get previews (if enabled) but won't appear on the identification page unless flagged by the audit.

## Troubleshooting

**No preview available for an item:**
- Check that `ffmpeg` and `ffprobe` are installed: `which ffmpeg ffprobe`
- Check the log for errors: `grep -i preview /var/log/dvd-ripper.log`
- The video may have been too short or unreadable for ffprobe to determine duration

**Preview shows wrong part of the movie:**
- Adjust `PREVIEW_START_PERCENT` in `/etc/dvd-ripper.conf`
- Some DVDs have unusual chapter layouts that shift the effective start point

**Rename fails with NAS error:**
- The NAS must be reachable via SSH for transferred items
- Check SSH connectivity: `ssh <nas-host> ls <nas-path>`
- You can retry the rename later — the item stays on the identify page

**Item doesn't appear on identify page:**
- Only items with `needs_identification: true` in their state metadata appear automatically
- Items in active processing states (`encoding`, `transferring`) won't show until processing completes
- Check state files: `ls /var/tmp/dvd-rips/*.iso-ready /var/tmp/dvd-rips/*.encoded-ready /var/tmp/dvd-rips/*.transferred`
