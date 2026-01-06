"""
Shared pytest fixtures for DVD Auto-Ripper tests.

See TESTING.md for the full testing roadmap.
"""
import sys
import os
import pytest

# Add web directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web'))


# =============================================================================
# Mock Data Fixtures - /proc filesystem
# =============================================================================

@pytest.fixture
def mock_proc_stat():
    """Mock /proc/stat content for CPU tests."""
    return """cpu  10132153 290696 3084719 46828483 16683 0 25195 0 0 0
cpu0 1254063 72797 765972 11709517 3698 0 11902 0 0 0
cpu1 2572100 72902 862034 11752315 4003 0 6564 0 0
cpu2 2512442 76570 851439 11706322 3676 0 12052 0 0
cpu3 3793548 68427 605274 11660129 5306 0 4677 0 0
"""


@pytest.fixture
def mock_proc_meminfo():
    """Mock /proc/meminfo for memory tests."""
    return """MemTotal:        8167848 kB
MemFree:         1234567 kB
MemAvailable:    4062400 kB
Buffers:          123456 kB
Cached:          2345678 kB
SwapCached:            0 kB
Active:          3456789 kB
"""


@pytest.fixture
def mock_proc_loadavg():
    """Mock /proc/loadavg for load average tests."""
    return "3.45 2.10 1.50 4/256 12345"


# =============================================================================
# Mock Data Fixtures - State Files
# =============================================================================

@pytest.fixture
def mock_state_file_generic():
    """Mock state file with generic DVD title."""
    return {
        "title": "DVD_20240101_120000",
        "timestamp": "2024-01-01 12:00:00",
        "needs_identification": True
    }


@pytest.fixture
def mock_state_file_identified():
    """Mock state file with identified movie."""
    return {
        "title": "The_Matrix",
        "year": "1999",
        "timestamp": "2024-01-01 12:00:00",
        "needs_identification": False
    }


# =============================================================================
# Mock Data Fixtures - Subprocess Outputs
# =============================================================================

@pytest.fixture
def mock_df_output():
    """Mock df -h output for disk usage tests."""
    return """Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1       465G  234G  230G  51% /
tmpfs           3.9G     0  3.9G   0% /dev/shm
/dev/sdb1       932G  500G  432G  54% /var/tmp/dvd-rips
"""


@pytest.fixture
def mock_ps_output():
    """Mock ps aux output for process tests."""
    return """USER         PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
root       12345 78.5  5.2 1587552 213744 ?      SNLl 10:30   45:23 HandBrakeCLI -i /var/tmp/dvd-rips/test.iso -o /var/tmp/dvd-rips/test.mkv
root       12346  2.1  0.3  45632  12288 ?      S    10:25    1:05 ddrescue /dev/sr0 /var/tmp/dvd-rips/disc.iso
root       12347  0.5  0.1  23456   4096 ?      S    10:35    0:15 rsync -avz test.mkv user@host:/path
"""


@pytest.fixture
def mock_sensors_json():
    """Mock sensors -j output for temperature tests."""
    return {
        "coretemp-isa-0000": {
            "Adapter": "ISA adapter",
            "Core 0": {"temp2_input": 45.0, "temp2_max": 100.0, "temp2_crit": 100.0},
            "Core 1": {"temp3_input": 47.0, "temp3_max": 100.0, "temp3_crit": 100.0}
        },
        "nct6798-isa-0290": {
            "Adapter": "ISA adapter",
            "SYSTIN": {"temp1_input": 35.0},
            "fan1": {"fan1_input": 1200}
        }
    }


@pytest.fixture
def mock_lsblk_json():
    """Mock lsblk -J output for I/O tests."""
    return {
        "blockdevices": [
            {"name": "sda", "size": "500G", "type": "disk", "mountpoint": None,
             "model": "Samsung SSD 870", "tran": "sata", "rota": "0"},
            {"name": "sda1", "size": "499G", "type": "part", "mountpoint": "/",
             "model": None, "tran": None, "rota": "0"},
            {"name": "sdb", "size": "2T", "type": "disk", "mountpoint": None,
             "model": "WDC WD20EZRZ", "tran": "sata", "rota": "1"},
            {"name": "sr0", "size": "4.7G", "type": "rom", "mountpoint": None,
             "model": "DVD-RW", "tran": "sata", "rota": "1"}
        ]
    }


# =============================================================================
# Flask Test Client (for API tests)
# =============================================================================

@pytest.fixture
def app():
    """Flask test client for API endpoint tests.

    Note: This fixture requires mocking of system calls to work
    in non-Linux environments or without real /proc filesystem.
    """
    # Import here to avoid import errors if flask not available
    try:
        # The dashboard uses hyphens, need to handle import
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dvd_dashboard",
            os.path.join(os.path.dirname(__file__), '..', 'web', 'dvd-dashboard.py')
        )
        dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard)

        dashboard.app.config['TESTING'] = True
        return dashboard.app.test_client()
    except Exception as e:
        pytest.skip(f"Could not load Flask app: {e}")
