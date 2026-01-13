/* DVD Ripper Dashboard - Common JavaScript Utilities */

/**
 * Show a toast notification
 * @param {string} message - The message to display
 * @param {string} type - Type of notification: 'success', 'error', 'info'
 * @param {number} duration - How long to show the notification in ms
 */
function showNotification(message, type = 'info', duration = 3000) {
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.textContent = message;
    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => notification.remove(), 300);
    }, duration);
}

/**
 * Format bytes to human readable size
 * @param {number} bytes - Number of bytes
 * @param {number} decimals - Number of decimal places
 * @returns {string} Formatted size string
 */
function formatSize(bytes, decimals = 1) {
    if (bytes === 0) return '0 B';

    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return parseFloat((bytes / Math.pow(k, i)).toFixed(decimals)) + ' ' + sizes[i];
}

/**
 * Format duration in seconds to human readable string
 * @param {number} seconds - Duration in seconds
 * @returns {string} Formatted duration string
 */
function formatDuration(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;

    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${mins}m`;
}

/**
 * Format a timestamp to relative time
 * @param {string|number} timestamp - ISO string or Unix timestamp
 * @returns {string} Relative time string
 */
function formatRelativeTime(timestamp) {
    const date = typeof timestamp === 'number' ? new Date(timestamp * 1000) : new Date(timestamp);
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;

    return date.toLocaleDateString();
}

/**
 * Make an API request with JSON handling
 * @param {string} url - The URL to request
 * @param {object} options - Fetch options
 * @returns {Promise<object>} The JSON response
 */
async function apiRequest(url, options = {}) {
    const defaults = {
        headers: {
            'Content-Type': 'application/json',
        },
    };

    const config = { ...defaults, ...options };
    if (options.body && typeof options.body === 'object') {
        config.body = JSON.stringify(options.body);
    }

    try {
        const response = await fetch(url, config);
        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }

        return data;
    } catch (error) {
        showNotification(error.message, 'error');
        throw error;
    }
}

/**
 * Debounce a function
 * @param {function} func - Function to debounce
 * @param {number} wait - Wait time in ms
 * @returns {function} Debounced function
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Poll an endpoint at regular intervals
 * @param {string} url - URL to poll
 * @param {function} callback - Callback with response data
 * @param {number} interval - Polling interval in ms
 * @returns {function} Stop function to cancel polling
 */
function poll(url, callback, interval = 5000) {
    let active = true;

    async function doPoll() {
        if (!active) return;

        try {
            const response = await fetch(url);
            const data = await response.json();
            callback(data);
        } catch (error) {
            console.error('Poll error:', error);
        }

        if (active) {
            setTimeout(doPoll, interval);
        }
    }

    doPoll();

    return function stop() {
        active = false;
    };
}

/**
 * Escape HTML to prevent XSS
 * @param {string} str - String to escape
 * @returns {string} Escaped string
 */
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * Copy text to clipboard
 * @param {string} text - Text to copy
 */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        showNotification('Copied to clipboard', 'success');
    } catch (error) {
        showNotification('Failed to copy', 'error');
    }
}

/**
 * Show a confirmation modal
 * @param {string} message - Confirmation message
 * @param {function} onConfirm - Callback if confirmed
 * @param {function} onCancel - Callback if cancelled
 */
function showConfirm(message, onConfirm, onCancel) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay active';
    overlay.innerHTML = `
        <div class="modal">
            <h3>Confirm</h3>
            <p>${escapeHtml(message)}</p>
            <div class="modal-buttons">
                <button class="btn btn-modal-cancel">Cancel</button>
                <button class="btn btn-modal-confirm">Confirm</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    overlay.querySelector('.btn-modal-cancel').onclick = () => {
        overlay.remove();
        if (onCancel) onCancel();
    };

    overlay.querySelector('.btn-modal-confirm').onclick = () => {
        overlay.remove();
        if (onConfirm) onConfirm();
    };

    overlay.onclick = (e) => {
        if (e.target === overlay) {
            overlay.remove();
            if (onCancel) onCancel();
        }
    };
}

// Export for module usage (if needed)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        showNotification,
        formatSize,
        formatDuration,
        formatRelativeTime,
        apiRequest,
        debounce,
        poll,
        escapeHtml,
        copyToClipboard,
        showConfirm
    };
}
