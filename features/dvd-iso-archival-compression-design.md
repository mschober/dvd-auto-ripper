DVD ISO Archival Compression Design
Overview

This design describes a bit-exact archival strategy for DVD ISO images that prioritizes storage efficiency, data integrity, and long-term recoverability, using commodity Linux tools on Ubuntu 25.10.

The original ISO can always be reconstructed exactly.

Goals

Preserve bit-identical DVD ISO images

Minimize storage usage

Leverage available CPU resources

Detect and recover from data corruption (bit rot)

Non-Goals

Video re-encoding or lossy compression

Fast compression times

ISO-in-place playback without decompression

Compression Strategy
Tooling

Compressor: xz (LZMA2)

Integrity check: CRC64 (embedded in xz stream)

Recovery: par2

Compression Command
xz -9e --threads=0 --keep --check=crc64 dvd.iso


Rationale

-9e: Maximum compression (CPU-expensive, best ratio)

--threads=0: Utilize all available CPU cores

--keep: Preserve the original ISO

--check=crc64: Strong integrity verification

Integrity Verification
Verify compressed archive
xz -t dvd.iso.xz

Restore original ISO
xz -dk dvd.iso.xz

Recovery Data (Bit Rot Protection)
Create PAR2 recovery files
par2 create -r5 dvd.iso.xz


Rationale

5% redundancy balances space overhead and recovery capability

Allows reconstruction from partial corruption
