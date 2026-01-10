# Feature: Dismissable Flash Messages

## Problem
Success and error messages (flash messages) persist on the dashboard until the page is manually refreshed. There's no way to dismiss them, and they can clutter the UI.

## Proposed Solution
1. Add an [x] dismiss button to each flash message
2. Success messages should auto-dismiss on dashboard refresh (10-second interval)
3. Error messages should persist until manually dismissed (they're more important)

## Implementation

### Changes to `web/dvd-dashboard.py`

**CSS:**
```css
.flash {
    position: relative;
    padding-right: 30px;  /* Make room for dismiss button */
}
.flash-dismiss {
    position: absolute;
    right: 8px;
    top: 50%;
    transform: translateY(-50%);
    background: none;
    border: none;
    cursor: pointer;
    font-size: 18px;
    opacity: 0.6;
}
.flash-dismiss:hover {
    opacity: 1;
}
```

**HTML Template:**
```html
{% for message in get_flashed_messages(with_categories=true) %}
<div class="flash flash-{{ message[0] }}" id="flash-{{ loop.index }}">
    {{ message[1] }}
    <button class="flash-dismiss" onclick="dismissFlash('flash-{{ loop.index }}')">&times;</button>
</div>
{% endfor %}
```

**JavaScript:**
```javascript
function dismissFlash(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

// Auto-dismiss success messages on progress update
function updateProgress() {
    // ... existing code ...
    // After successful update, remove success flashes
    document.querySelectorAll('.flash-success').forEach(el => el.remove());
}
```

## Files to Modify
- `web/dvd-dashboard.py` - CSS, HTML template, JavaScript

## Verification
1. Trigger an action that shows a success message
2. Verify [x] button appears and dismisses the message
3. Wait for progress update, verify success message auto-dismisses
4. Trigger an error, verify it persists until manually dismissed
