/**
 * Identify Page JavaScript
 * Handles renaming items, dismissing previews/audit flags, and auto-refresh.
 */

let cardCounter = 0;

function buildIdentifyCard(item) {
    cardCounter++;
    const idx = cardCounter;
    const meta = item.metadata;
    const previewFile = meta.preview_path ? meta.preview_path.split('/').pop() : '';
    const displayTitle = (meta.title || '').replace(/_/g, ' ');
    const yearDisplay = meta.year ? ' (' + meta.year + ')' : '';

    let previewHtml;
    if (previewFile) {
        previewHtml = `
            <video class="preview-video" controls preload="metadata">
                <source src="/api/preview/${encodeURIComponent(previewFile)}" type="video/mp4">
                Your browser does not support video playback.
            </video>`;
    } else {
        previewHtml = `
            <div class="no-preview">
                No preview available<br>
                <small>Preview will be generated during encoding</small>
            </div>`;
    }

    return `
    <div class="identify-card" data-state-file="${item.state_file}">
        <button class="btn-dismiss-card" onclick="dismissPreview(this)" title="Dismiss">&times;</button>
        <div class="success-msg"></div>
        <div class="error-msg"></div>
        <div class="preview-container">${previewHtml}</div>
        <div class="current-name">Current: ${displayTitle}${yearDisplay}</div>
        <form class="rename-form" onsubmit="return handleRename(this, event)">
            <div class="form-row">
                <div class="form-group">
                    <label for="title-${idx}">Movie Title</label>
                    <input type="text" id="title-${idx}" name="title"
                           placeholder="The Matrix" required>
                </div>
                <div class="form-group">
                    <label for="year-${idx}">Year</label>
                    <input type="text" id="year-${idx}" name="year"
                           placeholder="1999" pattern="[0-9]{4}" maxlength="4">
                </div>
            </div>
            <button type="submit" class="btn btn-primary" style="width: 100%;">Rename &amp; Identify</button>
        </form>
        <div class="state-info">
            <span class="status-badge state-${item.state}">${item.state}</span>
            ${meta.nas_path ? '<span title="' + meta.nas_path + '">On NAS</span>' : ''}
        </div>
    </div>`;
}

function buildAuditCard(flag) {
    const issues = flag.issues || [];
    let issueHtml = '';
    if (issues.includes('gibberish')) {
        issueHtml += '<span class="audit-issue issue-gibberish">Suspicious Title</span>';
    }
    if (issues.includes('small')) {
        issueHtml += `<span class="audit-issue issue-small">Small File (${flag.size_mb}MB)</span>`;
    }
    if (issues.includes('missing')) {
        issueHtml += '<span class="audit-issue issue-missing">Missing Archive</span>';
    }

    const flaggedAt = flag.flagged_at ? flag.flagged_at.substring(0, 19) : 'Unknown';
    const hostInfo = flag.hostname ? ' | Host: ' + flag.hostname : '';

    return `
    <div class="audit-card" data-title="${flag.title}">
        <div class="audit-title">${flag.title}</div>
        <div class="audit-issues">${issueHtml}</div>
        <div class="audit-meta">Flagged: ${flaggedAt}${hostInfo}</div>
        <button class="btn btn-dismiss" onclick="dismissAuditFlag('${flag.title}', this)">
            Dismiss Flag
        </button>
    </div>`;
}

async function refreshIdentifyCards() {
    try {
        const [pendingRes, auditRes] = await Promise.all([
            fetch('/api/identify/pending'),
            fetch('/api/audit/flags')
        ]);
        const pending = await pendingRes.json();
        const auditFlags = await auditRes.json();

        // Rebuild identify section
        const identifySection = document.getElementById('identify-section');
        if (pending.length > 0) {
            let html = '<h2 style="margin: 0 0 16px 0; color: #f59e0b;">Needs Identification</h2>';
            html += '<p style="color: #666; margin-bottom: 16px;">These items have generic names. Watch the preview to identify each movie.</p>';
            html += '<div class="identify-grid">';
            pending.forEach(item => { html += buildIdentifyCard(item); });
            html += '</div>';
            identifySection.innerHTML = html;
        } else if (auditFlags.length === 0) {
            identifySection.innerHTML = `
                <div class="empty-state">
                    <h2>All Clear!</h2>
                    <p>No issues found. All your DVDs have proper names and passed audit.</p>
                    <p><a href="/">Return to Dashboard</a></p>
                </div>`;
        } else {
            identifySection.innerHTML = '';
        }

        // Rebuild audit section
        const auditSection = document.getElementById('audit-section');
        if (auditFlags.length > 0) {
            let html = '<div class="audit-section">';
            html += '<h2>Audit Flags</h2>';
            html += '<p class="subtitle">These videos were flagged by the hourly audit for potential issues.</p>';
            auditFlags.forEach(flag => { html += buildAuditCard(flag); });
            html += '</div>';
            auditSection.innerHTML = html;
        } else {
            auditSection.innerHTML = '';
        }
    } catch (e) {
        console.log('Refresh failed:', e);
    }
}

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
            // Fade out then refresh from server
            setTimeout(() => {
                card.style.opacity = '0.5';
                card.style.pointerEvents = 'none';
            }, 1000);
            setTimeout(() => refreshIdentifyCards(), 2000);
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
    const errorMsg = card.querySelector('.error-msg');
    if (!confirm('Are you sure? This will delete the preview from the server.')) {
        return;
    }

    // Extract preview filename from video source
    const source = card.querySelector('video source');
    if (!source) {
        errorMsg.textContent = 'No preview file to delete';
        errorMsg.style.display = 'block';
        return;
    }

    const src = source.getAttribute('src');
    const filename = decodeURIComponent(src.split('/').pop());
    try {
        const response = await fetch('/api/preview/' + encodeURIComponent(filename), {
            method: 'DELETE'
        });
        const result = await response.json();

        if (!response.ok) {
            errorMsg.textContent = 'Delete failed: ' + (result.error || 'Unknown error');
            errorMsg.style.display = 'block';
            return;
        }
    } catch (e) {
        errorMsg.textContent = 'Request failed: ' + e.message;
        errorMsg.style.display = 'block';
        return;
    }

    // Fade out then refresh from server
    card.style.transition = 'opacity 0.3s';
    card.style.opacity = '0';
    card.style.pointerEvents = 'none';
    setTimeout(() => refreshIdentifyCards(), 500);
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
            refreshIdentifyCards();
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

// Initial load and auto-refresh every 30 seconds
refreshIdentifyCards();
setInterval(refreshIdentifyCards, 30000);
