/**
 * Identify Page JavaScript
 * Handles renaming items and dismissing audit flags
 */

async function handleRename(form, event) {
    event.preventDefault();
    const card = form.closest('.identify-card');
    const stateFile = card.dataset.stateFile;
    const title = form.title.value.trim();
    const year = form.year.value.trim();
    const submitBtn = form.querySelector('button[type="submit"]');
    const successMsg = card.querySelector('.success-msg');
    const errorMsg = card.querySelector('.error-msg');

    // Hide previous messages
    successMsg.style.display = 'none';
    errorMsg.style.display = 'none';

    // Disable button during request
    submitBtn.disabled = true;
    submitBtn.textContent = 'Renaming...';

    try {
        const response = await fetch('/api/identify/' + encodeURIComponent(stateFile) + '/rename', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title, year})
        });
        const result = await response.json();

        if (response.ok) {
            successMsg.textContent = 'Renamed successfully to: ' + title + (year ? ' (' + year + ')' : '');
            successMsg.style.display = 'block';
            // Fade out and remove card after a moment
            setTimeout(() => {
                card.style.opacity = '0.5';
                card.style.pointerEvents = 'none';
            }, 1000);
            setTimeout(() => {
                card.remove();
                // Check if no more cards
                if (document.querySelectorAll('.identify-card').length === 0) {
                    location.reload();
                }
            }, 2000);
        } else {
            errorMsg.textContent = result.error || 'Rename failed';
            errorMsg.style.display = 'block';
            submitBtn.disabled = false;
            submitBtn.textContent = 'Rename & Identify';
        }
    } catch (e) {
        errorMsg.textContent = 'Request failed: ' + e.message;
        errorMsg.style.display = 'block';
        submitBtn.disabled = false;
        submitBtn.textContent = 'Rename & Identify';
    }
    return false;
}

async function dismissPreview(btn) {
    const card = btn.closest('.identify-card');
    if (!confirm('Are you sure? This will delete the preview from the server.')) {
        return;
    }

    // Extract preview filename from video source
    const source = card.querySelector('video source');
    if (source) {
        const src = source.getAttribute('src');
        const filename = src.split('/').pop();
        try {
            await fetch('/api/preview/' + encodeURIComponent(filename), {
                method: 'DELETE'
            });
        } catch (e) {
            // Continue with card removal even if delete fails
        }
    }

    // Fade out and remove card
    card.style.transition = 'opacity 0.3s';
    card.style.opacity = '0';
    card.style.pointerEvents = 'none';
    setTimeout(() => {
        card.remove();
        if (document.querySelectorAll('.identify-card').length === 0) {
            location.reload();
        }
    }, 300);
}

async function dismissAuditFlag(title, btn) {
    btn.disabled = true;
    btn.textContent = 'Dismissing...';
    try {
        const response = await fetch('/api/audit/clear/' + encodeURIComponent(title), {
            method: 'POST'
        });
        if (response.ok) {
            btn.closest('.audit-card').remove();
        } else {
            const result = await response.json();
            alert('Failed: ' + (result.error || 'Unknown error'));
            btn.disabled = false;
            btn.textContent = 'Dismiss Flag';
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
        btn.disabled = false;
        btn.textContent = 'Dismiss Flag';
    }
}
