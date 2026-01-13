/**
 * Configuration Page JavaScript
 * Handles form editing, saving, and service restart
 */

let pendingRestarts = [];

function toggleSection(id) {
    document.getElementById('section-' + id).classList.toggle('collapsed');
}

function showStatus(msg, type) {
    const el = document.getElementById('status-msg');
    el.textContent = msg;
    el.className = 'status-msg show ' + type;
    setTimeout(() => el.classList.remove('show'), 5000);
}

function getFormData() {
    const form = document.getElementById('config-form');
    const data = {};

    // Get all inputs
    form.querySelectorAll('input[type="text"], input[type="number"], select').forEach(el => {
        data[el.name] = el.value;
    });

    // Get checkboxes (booleans)
    form.querySelectorAll('input[type="checkbox"]').forEach(el => {
        data[el.name] = el.checked ? '1' : '0';
    });

    return data;
}

function resetForm() {
    const form = document.getElementById('config-form');
    form.querySelectorAll('input[type="text"], input[type="number"]').forEach(el => {
        el.value = originalConfig[el.name] || '';
    });
    form.querySelectorAll('select').forEach(el => {
        el.value = originalConfig[el.name] || el.options[0].value;
    });
    form.querySelectorAll('input[type="checkbox"]').forEach(el => {
        el.checked = originalConfig[el.name] === '1';
        updateToggleLabel(el);
    });
    showStatus('Form reset to saved values', 'success');
}

function updateToggleLabel(checkbox) {
    const label = checkbox.parentElement.nextElementSibling;
    if (label) label.textContent = checkbox.checked ? 'Enabled' : 'Disabled';
}

// Update toggle labels on change
document.querySelectorAll('.toggle-switch input').forEach(el => {
    el.addEventListener('change', () => updateToggleLabel(el));
});

async function saveConfig() {
    const btn = document.getElementById('save-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const response = await fetch('/api/config/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings: getFormData() })
        });

        const result = await response.json();

        if (result.success) {
            showStatus(result.message, 'success');

            // Update original config with new values
            Object.assign(originalConfig, getFormData());

            // Show restart modal if needed
            if (result.restart_recommendations && result.restart_recommendations.length > 0) {
                pendingRestarts = result.restart_recommendations;
                showRestartModal(result.restart_recommendations);
            }
        } else {
            showStatus(result.message || 'Failed to save', 'error');
        }
    } catch (err) {
        showStatus('Error: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
    }
}

function showRestartModal(services) {
    const container = document.getElementById('restart-services');
    container.innerHTML = services.map((svc, i) => `
        <div class="restart-item">
            <input type="checkbox" id="restart-${i}" value="${svc.name}" data-type="${svc.type}" checked>
            <label for="restart-${i}">${svc.name}.${svc.type}</label>
        </div>
    `).join('');
    document.getElementById('restart-modal').classList.add('show');
}

function closeModal() {
    document.getElementById('restart-modal').classList.remove('show');
}

async function restartSelected() {
    const checkboxes = document.querySelectorAll('#restart-services input:checked');
    const toRestart = Array.from(checkboxes).map(cb => ({
        name: cb.value,
        type: cb.dataset.type
    }));

    closeModal();

    for (const svc of toRestart) {
        try {
            const endpoint = svc.type === 'timer' ? '/api/timer/' : '/api/service/';
            await fetch(endpoint + svc.name, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'restart' })
            });
            showStatus(`Restarted ${svc.name}.${svc.type}`, 'success');
        } catch (err) {
            showStatus(`Failed to restart ${svc.name}: ${err.message}`, 'error');
        }
    }
}
