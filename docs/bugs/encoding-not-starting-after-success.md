# Bug: Encoding Not Starting After Success Message

## Problem
User clicked "Start Encoding" on blazer dashboard for LOVE_HAPPENS, got a success message, but encoding did not actually start.

## Symptoms
- Dashboard shows success message after clicking "Start Encoding"
- Encoder service does not start or starts but exits immediately
- ISO remains in `iso-ready` state

## Investigation Steps

### 1. Check Current State
```bash
# On blazer
ls -la /var/tmp/dvd-rips/LOVE_HAPPENS*.iso-ready
systemctl status dvd-encoder.service
journalctl -u dvd-encoder.service --since "30 minutes ago"
```

### 2. Check Dashboard Action Handler
The "Start Encoding" button triggers `/action/start-encoder`. Check:
- Does the endpoint actually start the service?
- Is there error handling that swallows failures?
- Does it check if service started successfully?

**Location:** `web/dvd-dashboard.py` - look for `start-encoder` route

### 3. Check Encoder Service
```bash
# Try starting manually
sudo systemctl start dvd-encoder.service
systemctl status dvd-encoder.service

# Check logs
tail -50 /var/log/dvd-ripper/encoder.log
journalctl -u dvd-encoder.service -n 50
```

### 4. Possible Causes

**A. Service start succeeds but encoder exits immediately**
- No ISOs in `iso-ready` state
- Lock file already held
- Permission issues

**B. Dashboard reports success but service didn't start**
- `systemctl start` returns 0 even if service fails later
- Need to check service status after start

**C. Polkit/permission issues**
- Dashboard runs as `dvd-web` user
- May need polkit rules to start services

**D. Race condition**
- Service starts, finds nothing to do, exits
- Success message based on `systemctl start` return code, not actual encoding

### 5. Fix Approaches

**If service exits immediately:**
- Check why encoder finds no work
- May need to wait for service to actually start processing

**If permission issue:**
- Check polkit rules in `/etc/polkit-1/rules.d/50-dvd-web.rules`
- Verify `dvd-web` user can start services

**If success reported incorrectly:**
- Change dashboard to check service status after starting
- Add delay and verify service is running
- Return actual status, not just "start command succeeded"

## Files to Check
- `web/dvd-dashboard.py` - `/action/start-encoder` route
- `config/dvd-encoder.service` - Service definition
- `scripts/dvd-encoder.sh` - Encoder script
- `/etc/polkit-1/rules.d/50-dvd-web.rules` - Permissions
