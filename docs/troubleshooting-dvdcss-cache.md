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

## Current Solution: Key Packaging

The pipeline now packages CSS keys as a sidecar directory alongside each ISO:

```
/var/tmp/dvd-rips/
  Movie_Title-1234567890.iso
  Movie_Title-1234567890.iso.keys/      <- CSS keys from physical disc
    .disc_id                            <- Original cache dir name
    00003c9860                          <- Key files
    00003ca000
    ...
```

### How It Works

1. **Stage 1 (dvd-iso.sh)**: After creating the ISO, calls `package_dvdcss_keys()` to copy CSS keys from the dvdcss cache to `.iso.keys/` directory

2. **Stage 2 (dvd-encoder.sh)**: Before encoding, calls `prepare_dvdcss_cache()` which:
   - Imports keys from `.iso.keys/` to the local cache under the `-0000000000` suffix
   - Falls back to clearing partial cache if no keys are packaged

3. **Cluster mode**: The `distribute_to_peer()` function rsyncs both the ISO and its `.keys/` directory to peer nodes

### Verification

Check if keys are being packaged:
```bash
# After ISO creation
ls -la /var/tmp/dvd-rips/*.iso.keys/
```

Check if keys were imported before encoding:
```bash
# Look for import messages
grep -i "imported.*css" /var/log/dvd-ripper.log
```

Check the local cache has the ISO-derived directory:
```bash
ls -la /var/cache/dvdcss/*-0000000000/
```

## Diagnosis

Check the dvdcss cache for multiple directories with the same disc name but different suffixes:

```bash
ls -la /var/cache/dvdcss/
```

Look for patterns like:
```
drwxr-xr-x  dvd-encode  DVD_VIDEO-2000053013181600-0000000000  # From ISO (imported)
drwxr-xr-x  dvd-rip     DVD_VIDEO-2000053013181600-1762a2987d  # From disc (original)
```

Compare key counts:
```bash
for d in /var/cache/dvdcss/DVD_VIDEO-*/; do
    echo "$d: $(ls -1 "$d" 2>/dev/null | wc -l) keys"
done
```

## Manual Fix

If keys weren't packaged (older ISOs or failed packaging), you can manually fix:

### Option 1: Copy Keys to ISO Cache Directory

```bash
# Find the sector that failed in the log (e.g., 0x003c9860 -> 00003c9860)
SECTOR="00003c9860"
DISC="DVD_VIDEO-2000053013181600"

cp /var/cache/dvdcss/${DISC}-1762a2987d/${SECTOR} \
   /var/cache/dvdcss/${DISC}-0000000000/

chown dvd-encode:dvd-ripper /var/cache/dvdcss/${DISC}-0000000000/${SECTOR}
```

### Option 2: Symlink Entire Directory

```bash
rm -rf /var/cache/dvdcss/${DISC}-0000000000
ln -s ${DISC}-1762a2987d /var/cache/dvdcss/${DISC}-0000000000
```

### Option 3: Clear Partial Cache and Re-crack

```bash
# Remove the incomplete ISO-derived cache
rm -rf /var/cache/dvdcss/${DISC}-0000000000

# Restart encoding - libdvdcss will crack keys fresh from ISO
sudo systemctl restart dvd-encoder.service
```

After fixing, **restart the encode** (kill HandBrakeCLI and restart dvd-encoder.service) - it won't re-read the cache mid-process.

## Fallback Behavior

If no packaged keys are found, the encoder falls back to:
1. Clearing partial `-0000000000` cache directories
2. Letting libdvdcss crack keys fresh from the ISO

This is slower but works for most discs. Some discs with aggressive CSS protection may fail without the original physical-disc keys.

## Why Did v1 / Other Machines Work?

- **On dreamy**: No pre-existing cache, so libdvdcss cracked all keys fresh from the ISO
- **v1**: Ran as root with a single shared cache, or had existing cache from previous runs
- **Fresh systems**: No partial cache means libdvdcss always cracks fresh

## Related Files

- `/var/cache/dvdcss/` - CSS key cache directory
- `/var/tmp/dvd-rips/*.iso.keys/` - Packaged CSS keys per ISO
- `/var/log/dvd-ripper.log` - Look for "Error cracking CSS key" or "Imported CSS keys" messages
- `scripts/dvd-utils.sh` - `package_dvdcss_keys()`, `import_dvdcss_keys()` functions
- `scripts/dvd-iso.sh` - Stage 1, packages keys after ISO creation
- `scripts/dvd-encoder.sh` - Stage 2, `prepare_dvdcss_cache()` imports keys
- `config/dvd-iso@.service` - Stage 1 service (DVDCSS_CACHE setting)
- `config/dvd-encoder.service` - Stage 2 service (DVDCSS_CACHE setting)
