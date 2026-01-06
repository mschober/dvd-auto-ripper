# Testing Roadmap

This document outlines the testing strategy for the DVD Auto-Ripper project, with a focus on the web dashboard (`web/dvd-dashboard.py`).

## Test Structure

```
tests/
├── __init__.py                    # Package marker
├── conftest.py                    # Shared pytest fixtures
├── test_pure_functions.py         # Pure logic tests (no mocking) ✅
├── test_system_helpers.py         # CPU, memory, load, temps, I/O
├── test_pipeline_helpers.py       # Queue, state files, locks
├── test_cluster_helpers.py        # Peer status, capacity
└── test_api_endpoints.py          # Flask test client for API routes
```

## Setup

Create a virtual environment for testing:

```bash
# Create venv (one-time setup)
python3 -m venv .venv

# Activate venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Or install directly
pip install pytest flask
```

## Running Tests

```bash
# Activate venv first
source .venv/bin/activate

# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_pure_functions.py -v

# Run with coverage
pip install pytest-cov
pytest tests/ --cov=web --cov-report=term-missing

# Run only tests matching a pattern
pytest tests/ -k "format_bytes"

# Without activating venv
.venv/bin/pytest tests/ -v
```

## Phase 1: Pure Functions (COMPLETE)

**File:** `tests/test_pure_functions.py`

These tests require no mocking - they validate pure logic functions that have no external dependencies.

| Function | Location | Test Cases |
|----------|----------|------------|
| `_format_bytes()` | Line 321 | B, KB, MB, GB, TB, PB conversions; negative values |
| `is_generic_title()` | Line 1229 | DVD_*, DISC*, VIDEO_TS, short titles, valid movie titles |
| `sanitize_filename()` | Line 1270 | Special chars, spaces, underscores, stripping |
| `generate_plex_filename()` | Line 1280 | Title casing, year formatting, no year, extensions |
| `parse_cluster_peers()` | Line 4355 | Single/multiple peers, invalid format, empty input |
| `get_restart_recommendations()` | Line 873 | Config key → service mapping, deduplication |

---

## Phase 2: System Helpers (File I/O Mocking)

**File:** `tests/test_system_helpers.py`

Mock file reads from `/proc` filesystem and subprocess calls.

### Functions to Test

| Function | Lines | Mock Strategy | Test Cases |
|----------|-------|---------------|------------|
| `get_cpu_usage()` | 255-295 | Mock `/proc/stat` reads | CPU % calculation, multi-core, delta timing |
| `get_memory_usage()` | 298-318 | Mock `/proc/meminfo` | Memory %, used/available, format_bytes |
| `get_load_average()` | 330-355 | Mock `/proc/loadavg` + `os.cpu_count` | Load per core, high/normal status |
| `get_disk_usage()` | 357-400 | Mock `subprocess.run` (df) | Parsing, percent extraction, multiple mounts |
| `count_by_state()` | 404-420 | Mock `glob.glob` | Count dict structure, empty dir |

### Fixtures Needed (in conftest.py)

```python
@pytest.fixture
def mock_proc_stat():
    """Mock /proc/stat content."""
    return """cpu  10132153 290696 3084719 46828483 16683 0 25195 0 0 0
cpu0 1254063 72797 765972 11709517 3698 0 11902 0 0 0
"""

@pytest.fixture
def mock_proc_meminfo():
    """Mock /proc/meminfo content."""
    return """MemTotal:        8167848 kB
MemFree:         1234567 kB
MemAvailable:    4062400 kB
"""
```

### Example Test Pattern

```python
from unittest.mock import patch, mock_open

def test_get_memory_usage(mock_proc_meminfo):
    with patch("builtins.open", mock_open(read_data=mock_proc_meminfo)):
        result = dashboard.get_memory_usage()

    assert result["total"] > 0
    assert 0 <= result["percent"] <= 100
    assert "total_human" in result
```

---

## Phase 3: Pipeline Helpers (File System Mocking)

**File:** `tests/test_pipeline_helpers.py`

Mock file system operations for queue and state management.

### Functions to Test

| Function | Lines | Mock Strategy | Test Cases |
|----------|-------|---------------|------------|
| `get_queue_items()` | 460-520 | Mock `glob.glob`, file reads | Sorting by mtime, metadata loading, empty queue |
| `get_lock_status()` | 535-570 | Mock `os.path.exists`, `os.kill` | Lock active/inactive, stale lock, no lock file |
| `get_active_progress()` | 580-640 | `get_lock_status`, `get_recent_logs` | HandBrake/ddrescue/rsync regex extraction |
| `get_recent_logs()` | 119-168 | Mock file reads, `subprocess.run` | Normal logs, rotated log fallback, empty file |
| `get_state_file_info()` | 650-700 | Mock file reads | Metadata parsing, missing fields |

### Example Test Pattern

```python
def test_get_queue_items_empty():
    with patch("glob.glob", return_value=[]):
        result = dashboard.get_queue_items()
    assert result == []

def test_get_queue_items_sorted_by_mtime(mock_state_file_generic):
    # Setup mock files with different mtimes
    with patch("glob.glob") as mock_glob, \
         patch("os.path.getmtime") as mock_mtime, \
         patch("builtins.open", mock_open(read_data=json.dumps(mock_state_file_generic))):

        mock_glob.return_value = ["/path/a.iso-ready", "/path/b.iso-ready"]
        mock_mtime.side_effect = [1000, 500]  # a is newer

        result = dashboard.get_queue_items()

    # Should be sorted oldest first
    assert result[0]["mtime"] < result[1]["mtime"]
```

---

## Phase 4: Subprocess Functions (Mock subprocess.run)

**Add to:** `tests/test_system_helpers.py`

Mock subprocess calls to system utilities.

### Functions to Test

| Function | Lines | Subprocess | Test Cases |
|----------|-------|------------|------------|
| `get_temperatures()` | 405-450 | `sensors -j` | JSON parsing, missing sensors, error handling |
| `get_dvd_processes()` | 455-500 | `ps aux` | Process parsing, HandBrake/ddrescue/rsync detection |
| `get_io_stats()` | 505-580 | `lsblk -J`, `/proc/diskstats`, `/proc/stat` | Device enumeration, read/write rates |

### Example Test Pattern

```python
def test_get_temperatures(mock_sensors_json):
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(mock_sensors_json)

    with patch("subprocess.run", return_value=mock_result):
        result = dashboard.get_temperatures()

    assert "cpu" in result or "cores" in result
```

---

## Phase 5: Cluster Helpers

**File:** `tests/test_cluster_helpers.py`

Test cluster coordination and peer communication.

### Functions to Test

| Function | Lines | Mock Strategy | Test Cases |
|----------|-------|---------------|------------|
| `get_cluster_config()` | 4340-4352 | Mock `read_config()` | Type conversions, defaults |
| `count_active_encoders()` | 4376-4388 | Mock `subprocess.run` (pgrep) | Count parsing, no processes |
| `get_worker_capacity()` | 4420-4470 | Mock multiple helpers | Capacity calculation, available flag |
| `ping_peer()` | 4480-4520 | Mock `urllib.request.urlopen` | Success/timeout/error responses |
| `get_all_peer_status()` | 4530-4580 | Mock `ping_peer()` | Online/offline status aggregation |

### Example Test Pattern

```python
def test_ping_peer_success():
    mock_response = Mock()
    mock_response.read.return_value = b'{"status": "ok"}'
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = dashboard.ping_peer("192.168.1.50", 5000)

    assert result["online"] is True

def test_ping_peer_timeout():
    with patch("urllib.request.urlopen", side_effect=TimeoutError):
        result = dashboard.ping_peer("192.168.1.50", 5000)

    assert result["online"] is False
    assert "timeout" in result.get("error", "").lower()
```

---

## Phase 6: API Endpoints (Integration Tests)

**File:** `tests/test_api_endpoints.py`

Use Flask test client for API validation.

### Endpoints to Test

| Endpoint | Method | Test Cases |
|----------|--------|------------|
| `/api/status` | GET | Returns JSON with all status fields |
| `/api/health` | GET | Includes CPU, memory, disk, load metrics |
| `/api/queue` | GET | Returns queue items in correct format |
| `/api/queue/<id>` | GET | Returns single item or 404 |
| `/api/config` | GET | Returns config with masked sensitive values |
| `/api/config` | POST | Updates config, returns changed keys |
| `/api/cluster/capacity` | GET | Returns worker capacity info |
| `/api/cluster/peers` | GET | Returns peer status list |
| `/api/identify` | POST | Renames item, validates input |

### Setup with Flask Test Client

```python
@pytest.fixture
def app():
    """Flask test client fixture."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dvd_dashboard",
        os.path.join(os.path.dirname(__file__), '..', 'web', 'dvd-dashboard.py')
    )
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)

    dashboard.app.config['TESTING'] = True
    return dashboard.app.test_client()

def test_api_status_structure(app):
    response = app.get('/api/status')
    assert response.status_code == 200
    data = response.get_json()

    assert "queue" in data
    assert "locks" in data
    assert "version" in data
```

### Mocking for API Tests

API tests will need comprehensive mocking since endpoints call many helper functions:

```python
def test_api_health_mocked(app):
    with patch.object(dashboard, 'get_cpu_usage', return_value={"percent": 50}), \
         patch.object(dashboard, 'get_memory_usage', return_value={"percent": 60}), \
         patch.object(dashboard, 'get_load_average', return_value={"load_1m": 1.0}), \
         patch.object(dashboard, 'get_disk_usage', return_value={"percent": 40}):

        response = app.get('/api/health')

    assert response.status_code == 200
    data = response.get_json()
    assert data["cpu"]["percent"] == 50
```

---

## Fixtures Reference (conftest.py)

Current fixtures available:

| Fixture | Description |
|---------|-------------|
| `mock_proc_stat` | /proc/stat content for CPU tests |
| `mock_proc_meminfo` | /proc/meminfo for memory tests |
| `mock_proc_loadavg` | /proc/loadavg for load average tests |
| `mock_state_file_generic` | State file needing identification |
| `mock_state_file_identified` | State file with movie title/year |
| `mock_df_output` | df -h output for disk usage |
| `mock_ps_output` | ps aux output with HandBrake/ddrescue/rsync |
| `mock_sensors_json` | sensors -j output for temperatures |
| `mock_lsblk_json` | lsblk -J output for I/O stats |
| `app` | Flask test client |

---

## Priority Order

1. **Phase 1** ✅ - Pure functions (no mocking needed, validates core logic)
2. **Phase 2** - System helpers (high value for health monitoring)
3. **Phase 3** - Pipeline helpers (queue/state management)
4. **Phase 4** - Subprocess functions (process detection)
5. **Phase 5** - Cluster helpers (lower priority unless cluster issues arise)
6. **Phase 6** - API endpoints (integration-level validation)

---

## Notes

- Tests are designed to run without a real `/proc` filesystem or DVD drive
- All external dependencies (file I/O, subprocess, network) should be mocked
- Fixtures in `conftest.py` provide realistic mock data
- Flask test client runs in-memory, no server required
