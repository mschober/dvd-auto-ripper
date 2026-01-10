# Feature: Receiving Progress Percentage

## Problem
The "Receiving" progress bar shows only the current size received (e.g., "7866.5 MB received") but not a percentage, because the receiver doesn't know the total file size.

## Proposed Solution
The sending machine knows the file size and should communicate it to the receiver before starting the rsync transfer. The receiver can then show a percentage-based progress bar.

## Implementation Options

### Option A: Metadata File (Recommended)
Create a `.incoming` metadata file on the receiver before starting rsync.

**Sender (dvd-distribute.sh or dvd-utils.sh):**
```bash
# Before rsync, create metadata file on receiver
local file_size=$(stat -c%s "$iso_path")
local metadata="{\"filename\": \"$iso_filename\", \"size\": $file_size, \"sender\": \"$CLUSTER_NODE_NAME\"}"
ssh "${CLUSTER_SSH_USER}@${peer_host}" "echo '$metadata' > ${CLUSTER_REMOTE_STAGING}/.incoming-${iso_filename}.json"

# Then run rsync
rsync -avz --progress "$iso_path" "${CLUSTER_SSH_USER}@${peer_host}:${CLUSTER_REMOTE_STAGING}/"

# After rsync completes, remove metadata file
ssh "${CLUSTER_SSH_USER}@${peer_host}" "rm -f ${CLUSTER_REMOTE_STAGING}/.incoming-${iso_filename}.json"
```

**Receiver (dvd-dashboard.py):**
```python
def get_receiving_transfers():
    receiving = []
    for entry in os.listdir(STAGING_DIR):
        if entry.startswith('.') and '.iso.' in entry:
            # ... existing detection code ...

            # Look for matching .incoming metadata file
            incoming_meta = os.path.join(STAGING_DIR, f".incoming-{original_name}.json")
            total_size = None
            sender = None
            if os.path.exists(incoming_meta):
                try:
                    with open(incoming_meta) as f:
                        meta = json.load(f)
                        total_size = meta.get("size")
                        sender = meta.get("sender")
                except:
                    pass

            receiving.append({
                "filename": original_name,
                "size_mb": round(size_mb, 1),
                "total_mb": round(total_size / (1024*1024), 1) if total_size else None,
                "percent": round(size / total_size * 100, 1) if total_size else None,
                "sender": sender
            })
    return receiving
```

### Option B: API Call Before Transfer
Call receiver's API with file size before starting rsync. Requires storing expected transfers in memory/file.

### Option C: Parse Rsync Output on Sender
The sender already has the rsync progress. Could push updates to receiver via API. More complex.

## Recommended Approach
Option A (Metadata File) is simplest:
- No API changes needed
- Works even if dashboard restarts
- Self-cleaning (file removed after transfer)
- Can include sender name for UI display

## Files to Modify
- `scripts/dvd-utils.sh` - Add metadata file creation before rsync
- `web/dvd-dashboard.py` - Read metadata file, calculate percentage

## UI Update
```html
<span class="progress-stats">
    {% if recv.percent %}
        {{ recv.size_mb }} / {{ recv.total_mb }} MB ({{ recv.percent }}%)
    {% else %}
        {{ recv.size_mb }} MB received
    {% endif %}
    {% if recv.sender %} from {{ recv.sender }}{% endif %}
</span>
```

## Verification
1. Start distribution from dreamy to cart
2. Verify `.incoming-*.json` file created on cart
3. Verify progress bar shows percentage
4. Verify metadata file removed after transfer completes
