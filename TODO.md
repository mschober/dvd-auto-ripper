# TODO

## Security: Proper User and Permissions Setup

Currently all services run as root, which is not ideal. This worklist tracks the migration to proper user separation with resource limits.

### Phase 1: Create dedicated users

- [ ] Create `dvd-ripper` user for ISO creation and encoding
  - Home: `/var/lib/dvd-ripper`
  - Groups: `cdrom` (for DVD access), `video` (if needed for hardware encoding)
  - Shell: `/usr/sbin/nologin`

- [ ] Create `plex` user on cartography (if not exists) for file ownership consistency
  - Should match UID/GID with dreamy-streamer's plex user

### Phase 2: Update systemd services

- [ ] Update `dvd-ripper@.service` to run as `dvd-ripper` user
- [ ] Update `dvd-encoder.service` to run as `dvd-ripper` user
- [ ] Update `dvd-transfer.service` to run as `dvd-ripper` user
- [ ] Update `dvd-dashboard.service` to run as `dvd-ripper` user

### Phase 3: Set up SSH keys for dvd-ripper user

- [ ] Generate SSH keypair for `dvd-ripper` user on cartography
- [ ] Add public key to `plex@dreamy-streamer` authorized_keys
- [ ] Update `/etc/dvd-ripper.conf` to use `NAS_USER="plex"` instead of `root`
- [ ] Remove `NAS_FILE_OWNER` config (no longer needed if transferring as plex)

### Phase 4: File permissions

- [ ] Set ownership of `/var/tmp/dvd-rips` to `dvd-ripper:dvd-ripper`
- [ ] Set ownership of `/var/log/dvd-ripper.log` to `dvd-ripper:dvd-ripper`
- [ ] Update logrotate config for correct ownership
- [ ] Ensure udev rule allows `dvd-ripper` user to access DVD device

### Phase 5: Resource limits (systemd)

- [ ] Add `MemoryMax=` to encoder service (HandBrake can be memory-hungry)
- [ ] Add `CPUQuota=` to encoder service (limit CPU usage during encoding)
- [ ] Add `IOWeight=` to deprioritize disk I/O during encoding
- [ ] Consider `Nice=` for lower scheduling priority

### Phase 6: Security hardening (systemd)

- [ ] Add `ProtectSystem=strict` (read-only filesystem except allowed paths)
- [ ] Add `ProtectHome=true` (no access to /home)
- [ ] Add `PrivateTmp=true` (isolated /tmp)
- [ ] Add `NoNewPrivileges=true`
- [ ] Add `ReadWritePaths=` for staging dir and log file only

### References

- systemd.exec(5) - Resource control and security options
- systemd.resource-control(5) - CPU, memory, IO limits
