/**
 * Archives page JavaScript
 * Handles drag-and-drop transfers, delete modal, archive triggering, and progress polling
 */

let draggedPrefix = null;

function handleDragStart(e) {
    draggedPrefix = e.target.dataset.prefix;
    e.target.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}

function handleDragEnd(e) {
    e.target.classList.remove('dragging');
    document.querySelectorAll('.drop-zone').forEach(z => z.classList.remove('drag-over'));
}

function handleDragOver(e) {
    e.preventDefault();
    if (e.currentTarget.classList.contains('online')) {
        e.currentTarget.classList.add('drag-over');
    }
}

function handleDragLeave(e) {
    e.currentTarget.classList.remove('drag-over');
}

async function handleDrop(e) {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');

    const peer = e.currentTarget.dataset.peer;
    const prefix = draggedPrefix;

    if (!peer || !prefix) return;
    if (!e.currentTarget.classList.contains('online')) {
        showNotification('Peer is offline', 'error');
        return;
    }

    const peerName = peer.split(':')[0];
    if (!confirm(`Transfer ${prefix} to ${peerName}?`)) return;

    try {
        const response = await fetch('/api/archives/transfer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prefix, peer })
        });
        const result = await response.json();

        if (result.status === 'started') {
            showNotification(`Transfer started: ${prefix} -> ${peerName}`, 'success');
            setTimeout(() => location.reload(), 1000);
        } else if (result.status === 'completed') {
            showNotification(`Transfer complete: ${prefix} -> ${peerName}`, 'success');
            setTimeout(() => location.reload(), 2000);
        } else {
            showNotification(result.error || 'Transfer failed', 'error');
        }
    } catch (err) {
        showNotification('Transfer request failed: ' + err.message, 'error');
    }
}

async function archiveNow(prefix) {
    const title = prefix.split('-')[0].replace(/_/g, ' ');
    if (!confirm(`Start archiving "${title}"? This will compress the ISO for long-term storage.`)) {
        return;
    }

    try {
        const response = await fetch('/api/archives/archive-now', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prefix })
        });
        const result = await response.json();

        if (result.status === 'started') {
            showNotification(`Archiving started: ${title}`, 'success');
            setTimeout(() => location.reload(), 1500);
        } else {
            showNotification(result.error || 'Archive failed to start', 'error');
        }
    } catch (err) {
        showNotification('Archive request failed: ' + err.message, 'error');
    }
}

// Delete modal handling
let pendingDeletePrefix = null;

function deleteArchive(prefix) {
    pendingDeletePrefix = prefix;
    const title = prefix.split('-')[0].replace(/_/g, ' ');
    document.getElementById('deleteModalText').textContent =
        `Are you sure you want to delete "${title}" and all associated files? This cannot be undone.`;
    document.getElementById('deleteModal').classList.add('active');
    setTimeout(() => document.getElementById('deleteModalNo').focus(), 50);
}

function closeDeleteModal() {
    document.getElementById('deleteModal').classList.remove('active');
    pendingDeletePrefix = null;
}

async function confirmDelete() {
    if (!pendingDeletePrefix) return;
    const prefix = pendingDeletePrefix;
    closeDeleteModal();

    try {
        const response = await fetch(`/api/archives/${encodeURIComponent(prefix)}`, {
            method: 'DELETE'
        });
        const result = await response.json();

        if (result.status === 'deleted' || result.status === 'partial') {
            showNotification(`Deleted: ${result.deleted.length} files`, 'success');
            setTimeout(() => location.reload(), 2000);
        } else {
            showNotification(result.error || 'Delete failed', 'error');
        }
    } catch (err) {
        showNotification('Delete request failed: ' + err.message, 'error');
    }
}

// Poll transfer progress for archives being transferred
async function pollTransferProgress() {
    const transfers = document.querySelectorAll('.transfer-progress[data-peer-host]');
    if (transfers.length === 0) return;

    for (const el of transfers) {
        const prefix = el.dataset.prefix;
        const peerHost = el.dataset.peerHost;
        const peerPort = el.dataset.peerPort;
        const isoSize = parseInt(el.dataset.isoSize) || 0;

        if (!peerHost || !peerPort || !isoSize) continue;

        try {
            const response = await fetch(`http://${peerHost}:${peerPort}/api/archives/receiving`);
            if (!response.ok) continue;

            const data = await response.json();
            const recv = data.receiving.find(r => r.prefix === prefix);

            if (recv) {
                const percent = Math.round((recv.current_size / isoSize) * 100);
                const percentEl = el.querySelector('.transfer-percent');
                const fillEl = el.querySelector('.progress-bar-fill');

                if (percentEl) percentEl.textContent = `${percent}%`;
                if (fillEl) fillEl.style.width = `${percent}%`;
            }
        } catch (err) {
            // Silently ignore polling errors
        }
    }
}

// Initialize event listeners when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Modal event listeners
    const deleteModalNo = document.getElementById('deleteModalNo');
    const deleteModalYes = document.getElementById('deleteModalYes');
    const deleteModal = document.getElementById('deleteModal');

    if (deleteModalNo) {
        deleteModalNo.addEventListener('click', closeDeleteModal);
    }
    if (deleteModalYes) {
        deleteModalYes.addEventListener('click', confirmDelete);
    }
    if (deleteModal) {
        deleteModal.addEventListener('click', (e) => {
            if (e.target.id === 'deleteModal') closeDeleteModal();
        });
    }

    // Close modal on Escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.getElementById('deleteModal').classList.contains('active')) {
            closeDeleteModal();
        }
    });

    // Poll every 3 seconds if there are active transfers
    if (document.querySelectorAll('.transfer-progress[data-peer-host]').length > 0) {
        pollTransferProgress();
        setInterval(pollTransferProgress, 3000);
    }

    // Auto-refresh page every 30 seconds to detect completed transfers
    if (document.querySelectorAll('.transfer-progress, .receiving-item').length > 0) {
        setTimeout(() => location.reload(), 30000);
    }
});
