/**
 * Health Page JavaScript
 * Handles process kill confirmation and auto-refresh of health metrics
 */

function confirmKill(pid, type) {
    document.getElementById('kill-pid').textContent = pid;
    document.getElementById('kill-type').textContent = type;
    document.getElementById('kill-form').action = '/api/kill/' + pid;
    document.getElementById('kill-modal').classList.add('active');
}

function closeModal() {
    document.getElementById('kill-modal').classList.remove('active');
}

// Close modal on overlay click
document.getElementById('kill-modal').addEventListener('click', function(e) {
    if (e.target === this) closeModal();
});

// Auto-refresh health data every 5 seconds
function updateHealth() {
    fetch('/api/health')
        .then(response => response.json())
        .then(data => {
            // Update CPU
            if (data.cpu && data.cpu.cpu) {
                document.getElementById('cpu-value').textContent = data.cpu.cpu.usage + '%';
                const cpuBar = document.getElementById('cpu-bar');
                cpuBar.style.width = data.cpu.cpu.usage + '%';
                cpuBar.className = 'metric-fill ' + (data.cpu.cpu.usage > 80 ? 'fill-danger' : data.cpu.cpu.usage > 50 ? 'fill-warn' : 'fill-ok');
            }

            // Update Memory
            if (data.memory) {
                document.getElementById('mem-value').textContent = data.memory.percent + '%';
                const memBar = document.getElementById('mem-bar');
                memBar.style.width = data.memory.percent + '%';
                memBar.className = 'metric-fill ' + (data.memory.percent > 80 ? 'fill-danger' : data.memory.percent > 60 ? 'fill-warn' : 'fill-ok');
            }

            // Update Load
            if (data.load) {
                document.getElementById('load-1m').textContent = data.load.load_1m.toFixed(2);
                document.getElementById('load-5m').textContent = data.load.load_5m.toFixed(2);
                document.getElementById('load-15m').textContent = data.load.load_15m.toFixed(2);
            }
        })
        .catch(err => console.log('Health update failed:', err));
}

// Update every 5 seconds
setInterval(updateHealth, 5000);

// Refresh full page every 30 seconds for process list
setTimeout(() => location.reload(), 30000);
