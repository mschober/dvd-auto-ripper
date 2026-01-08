# Troubleshooting: libdvdcss Cache Mismatch

## Symptom

Encoding fails with CSS decryption errors even though the ISO was created successfully:

```
libdvdread: Error cracking CSS key for /VIDEO_TS/VTS_XX_X.VOB (0x00XXXXXX)
```

Encoding progress stays at 0.00-0.01% and the output file remains 0 bytes.

## Root Cause

libdvdcss computes a **disc ID** to organize its key cache. This ID includes:
- The disc's volume label
- A suffix derived from disc metadata

**The problem**: Physical disc access and ISO file access generate **different disc ID suffixes**.

| Stage | User | Access Method | Example Disc ID |
|-------|------|---------------|-----------------|
| Stage 1 (ISO creation) | dvd-rip | Physical `/dev/sr0` | `DVD_VIDEO-...-1762a2987d` |
| Stage 2 (Encoding) | dvd-encode | ISO file | `DVD_VIDEO-...-0000000000` |

When Stage 2 reads the ISO, libdvdcss looks for cached keys in the `-0000000000` directory, but the keys were cached in `-1762a2987d` during Stage 1.

## This is NOT a v2 User Isolation Bug

This issue exists in the pipeline architecture itself:
- Stage 1 reads from physical disc, caches CSS keys
- Stage 2 reads from ISO file, can't find those cached keys

It would occur even if both stages ran as the same user. The v2 changes made it more visible because:
1. Separate users create separate cache subdirectories
2. Better logging shows the disc ID mismatch

## Diagnosis

Check the dvdcss cache for multiple directories with the same disc name but different suffixes:

```bash
ls -la /var/cache/dvdcss/
```

Look for patterns like:
```
drwxr-xr-x  dvd-encode  DVD_VIDEO-2000053013181600-0000000000  # From ISO
drwxr-xr-x  dvd-rip     DVD_VIDEO-2000053013181600-1762a2987d  # From disc
```

Check which keys are missing by comparing the directories:
```bash
diff <(ls /var/cache/dvdcss/DVD_VIDEO-*-0000000000/) \
     <(ls /var/cache/dvdcss/DVD_VIDEO-*-1762a2987d/)
```

## Quick Fix

Copy the cached keys from the physical-disc cache to the ISO cache:

```bash
# Find the sector that failed in the log (e.g., 0x003c9860 -> 00003c9860)
SECTOR="00003c9860"
DISC="DVD_VIDEO-2000053013181600"

cp /var/cache/dvdcss/${DISC}-1762a2987d/${SECTOR} \
   /var/cache/dvdcss/${DISC}-0000000000/

chown dvd-encode:dvd-ripper /var/cache/dvdcss/${DISC}-0000000000/${SECTOR}
```

Or symlink the entire directory:

```bash
rm -rf /var/cache/dvdcss/${DISC}-0000000000
ln -s ${DISC}-1762a2987d /var/cache/dvdcss/${DISC}-0000000000
```

After fixing, **restart the encode** (kill HandBrakeCLI and restart dvd-encoder.service) - it won't re-read the cache mid-process.

## Permanent Solutions

### Option 1: Clear Partial Cache Before Encoding

Add to `dvd-encoder.sh` to remove partial cache directories so libdvdcss cracks keys fresh:

```bash
# Remove -0000000000 suffix directories (ISO-derived, often incomplete)
find /var/cache/dvdcss -maxdepth 1 -type d -name '*-0000000000' -exec rm -rf {} \;
```

**Tradeoff**: Slower encodes (must crack CSS each time), but always works.

### Option 2: Copy Keys After ISO Creation

Add to `dvd-iso.sh` after successful ISO creation to duplicate the cache:

```bash
# Find the cache dir created during this rip and create a -0000000000 symlink
DISC_CACHE=$(ls -td /var/cache/dvdcss/*-[0-9a-f]* 2>/dev/null | head -1)
if [[ -n "$DISC_CACHE" && ! "$DISC_CACHE" =~ -0000000000$ ]]; then
    DISC_BASE="${DISC_CACHE%-*}"
    ln -sfn "$(basename "$DISC_CACHE")" "${DISC_BASE}-0000000000"
fi
```

**Tradeoff**: More complex, but encoding uses cached keys.

### Option 3: Use Shared Group-Writable Cache

Ensure all cache directories are group-writable so any dvd-ripper user can use any cache:

```bash
chmod -R g+rwX /var/cache/dvdcss
```

This doesn't solve the disc ID mismatch but helps with permission issues.

## Why Did v1 / Other Machines Work?

- **On dreamy**: No pre-existing cache, so libdvdcss cracked all keys fresh from the ISO
- **v1**: Ran as root with a single shared cache, or had existing cache from previous runs
- **Fresh systems**: No partial cache means libdvdcss always cracks fresh

## Related Files

- `/var/cache/dvdcss/` - CSS key cache directory
- `/var/log/dvd-ripper.log` - Look for "Error cracking CSS key" messages
- `config/dvd-iso@.service` - Stage 1 service (DVDCSS_CACHE setting)
- `config/dvd-encoder.service` - Stage 2 service (DVDCSS_CACHE setting)
