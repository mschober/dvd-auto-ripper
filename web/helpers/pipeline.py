"""Pipeline state helpers for queue management."""
import os
import glob
import json
from math import ceil

# Constants
STAGING_DIR = os.environ.get("STAGING_DIR", "/var/tmp/dvd-rips")
STATE_ORDER = ["iso-creating", "iso-ready", "distributing", "encoding",
               "encoded-ready", "transferring", "transferred",
               "archiving", "archived"]
QUEUE_ITEMS_PER_PAGE = 10


def get_queue_items(page=None, per_page=None):
    """Read all state files and return queue items.

    Args:
        page: Page number (1-indexed). If None, returns all items.
        per_page: Items per page. If None, uses QUEUE_ITEMS_PER_PAGE.

    Returns:
        If page is None: List of all items (sorted newest first)
        If page is set: Dict with items, total, page, per_page, total_pages
    """
    items = []
    for state in STATE_ORDER:
        pattern = os.path.join(STAGING_DIR, f"*.{state}")
        for state_file in glob.glob(pattern):
            try:
                with open(state_file, 'r') as f:
                    metadata = json.load(f)
            except (json.JSONDecodeError, IOError):
                metadata = {}
            items.append({
                "state": state,
                "file": os.path.basename(state_file),
                "metadata": metadata,
                "mtime": os.path.getmtime(state_file)
            })

    # Sort newest first
    items = sorted(items, key=lambda x: x["mtime"], reverse=True)

    # Return all items if no pagination requested
    if page is None:
        return items

    # Paginate
    per_page = per_page or QUEUE_ITEMS_PER_PAGE
    total = len(items)
    total_pages = ceil(total / per_page) if total > 0 else 1
    page = max(1, min(page, total_pages))  # Clamp to valid range

    start = (page - 1) * per_page
    end = start + per_page

    return {
        "items": items[start:end],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages
    }


def count_by_state():
    """Return dict of counts by state."""
    counts = {}
    for state in STATE_ORDER:
        pattern = os.path.join(STAGING_DIR, f"*.{state}")
        counts[state] = len(glob.glob(pattern))
    return counts
