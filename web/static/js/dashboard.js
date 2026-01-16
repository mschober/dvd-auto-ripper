/**
 * Dashboard JavaScript
 * Handles progress bar updates, queue actions, and cancel modal
 */

// Auto-refresh progress bars every 10 seconds
function updateProgress() {
    fetch('/api/progress')
        .then(response => response.json())
        .then(data => {
            const section = document.getElementById('progress-section');
            if (!section) return;

            let html = '';

            if (data.iso && Array.isArray(data.iso)) {
                data.iso.forEach(iso => {
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">ISO Creation (${iso.drive})</span>
                                <span class="progress-stats">${iso.percent.toFixed(1)}% | ETA: ${iso.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-iso" style="width: ${iso.percent}%"></div>
                            </div>
                        </div>`;
                });
            }

            if (data.encoder && Array.isArray(data.encoder)) {
                data.encoder.forEach(enc => {
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">Encoding: ${enc.title}</span>
                                <span class="progress-stats">${enc.percent.toFixed(1)}% | ${enc.speed} | ETA: ${enc.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-encoder" style="width: ${enc.percent}%"></div>
                            </div>
                        </div>`;
                });
            }

            if (data.distributing) {
                html += `
                    <div class="progress-item">
                        <div class="progress-header">
                            <span class="progress-label">Distributing to Cluster</span>
                            <span class="progress-stats">${data.distributing.percent.toFixed(1)}% | ${data.distributing.speed} | ETA: ${data.distributing.eta}</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill progress-distributing" style="width: ${data.distributing.percent}%"></div>
                        </div>
                    </div>`;
            }

            if (data.transfer && Array.isArray(data.transfer)) {
                data.transfer.forEach(xfer => {
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">Transfer: ${xfer.title}</span>
                                <span class="progress-stats">${xfer.percent.toFixed(1)}% | ${xfer.speed} | ETA: ${xfer.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-transfer" style="width: ${xfer.percent}%"></div>
                            </div>
                        </div>`;
                });
            }

            if (data.archive && Array.isArray(data.archive)) {
                data.archive.forEach(arch => {
                    const startedInfo = arch.started_time ? ` (started ${arch.started_time})` : '';
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">Archiving: ${arch.title}${startedInfo}</span>
                                <span class="progress-stats">${arch.percent.toFixed(1)}% | ${arch.speed} | ETA: ${arch.eta}</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-archive" style="width: ${arch.percent}%"></div>
                            </div>
                        </div>`;
                });
            }

            if (data.receiving && Array.isArray(data.receiving)) {
                data.receiving.forEach(recv => {
                    const displayName = recv.filename.replace(/_/g, ' ');
                    html += `
                        <div class="progress-item">
                            <div class="progress-header">
                                <span class="progress-label">Receiving: ${displayName}</span>
                                <span class="progress-stats">${recv.size_mb} MB received</span>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill progress-receiving" style="width: 100%"></div>
                            </div>
                        </div>`;
                });
            }

            if ((!data.iso || data.iso.length === 0) && !data.encoder && !data.distributing && !data.transfer && (!data.archive || data.archive.length === 0) && (!data.receiving || data.receiving.length === 0)) {
                html = '<p style="color: #666; font-size: 13px; margin: 12px 0 0 0;">No active operations</p>';
            }

            section.innerHTML = html;
        })
        .catch(err => console.log('Progress update failed:', err));
}

// Update progress every 10 seconds
setInterval(updateProgress, 10000);

// Queue action handlers
let cancelTarget = null;

function confirmCancel(stateFile, state, title) {
    cancelTarget = { stateFile, state, title };
    document.getElementById('cancel-title').textContent = title;

    const warning = document.getElementById('cancel-warning');
    const deleteOption = document.getElementById('delete-files-option');

    if (['iso-creating', 'encoding', 'distributing', 'transferring'].includes(state)) {
        warning.textContent = 'This will kill the running process and may leave partial files.';
        warning.style.display = 'block';
        deleteOption.style.display = 'none';
    } else if (['iso-ready', 'encoded-ready'].includes(state)) {
        warning.style.display = 'none';
        deleteOption.style.display = 'block';
        document.getElementById('delete-files-checkbox').checked = false;
    } else {
        warning.style.display = 'none';
        deleteOption.style.display = 'none';
    }

    document.getElementById('cancel-modal').classList.add('active');
}

function closeCancelModal() {
    document.getElementById('cancel-modal').classList.remove('active');
    cancelTarget = null;
}

async function executeCancel() {
    if (!cancelTarget) return;

    const checkbox = document.getElementById('delete-files-checkbox');
    const deleteFiles = checkbox ? checkbox.checked : false;

    try {
        const response = await fetch('/api/queue/' + encodeURIComponent(cancelTarget.stateFile) + '/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({delete_files: deleteFiles})
        });

        if (response.ok) {
            location.reload();
        } else {
            const result = await response.json();
            alert('Cancel failed: ' + (result.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
    }

    closeCancelModal();
}

function triggerDistribute() {
    // Use force endpoint to bypass 'keep 1 for local' logic when manually triggered
    fetch('/api/trigger/distribute/force', { method: 'POST' })
        .then(response => {
            if (response.ok) {
                location.reload();
            } else {
                return response.json().then(data => {
                    alert('Failed to trigger distribute: ' + (data.error || 'Unknown error'));
                });
            }
        })
        .catch(e => alert('Request failed: ' + e.message));
}
