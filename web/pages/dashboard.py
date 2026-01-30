"""Main dashboard page routes."""
import os
import socket
import subprocess
from datetime import datetime
from flask import Blueprint, render_template, request

from helpers.pipeline import get_queue_items, count_by_state, STAGING_DIR
from helpers.config import ConfigManager, CONFIG_SECTIONS, BOOLEAN_SETTINGS, DROPDOWN_SETTINGS
from helpers.system_health import SystemHealth
from helpers.locks import LockManager
from helpers.logs import LogReader, LOG_FILES
from helpers.progress import ProgressTracker
from helpers.services import ServiceController
from helpers.identifier import Identifier
from helpers.cluster_manager import ClusterManager

# Blueprint setup
dashboard_bp = Blueprint('dashboard', __name__)

# Version and metadata
PIPELINE_VERSION_FILE = os.environ.get("PIPELINE_VERSION_FILE", "/usr/local/bin/VERSION")
DASHBOARD_VERSION = "1.9.0"
GITHUB_URL = "https://github.com/mschober/dvd-auto-ripper"
HOSTNAME = socket.gethostname().split('.')[0]


def get_pipeline_version():
    """Read pipeline version from VERSION file."""
    try:
        with open(PIPELINE_VERSION_FILE, 'r') as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def get_disk_usage():
    """Get disk usage for staging directory."""
    try:
        result = subprocess.run(
            ["df", "-h", STAGING_DIR],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            return {
                "mount": parts[5] if len(parts) > 5 else parts[0],
                "total": parts[1],
                "used": parts[2],
                "available": parts[3],
                "percent": parts[4].rstrip("%"),
                "percent_num": int(parts[4].rstrip("%"))
            }
    except Exception:
        pass
    return {"mount": "N/A", "total": "N/A", "used": "N/A",
            "available": "N/A", "percent": "0", "percent_num": 0}


@dashboard_bp.route("/")
def dashboard():
    """Main dashboard page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")
    page = request.args.get("page", 1, type=int)

    cluster_config = ClusterManager.get_config()
    queue_data = get_queue_items(page=page)
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        counts=count_by_state(),
        queue=queue_data["items"],
        queue_total=queue_data["total"],
        queue_page=queue_data["page"],
        queue_total_pages=queue_data["total_pages"],
        disk=get_disk_usage(),
        locks=LockManager.get_status(),
        progress=ProgressTracker.get_active_progress(),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        message=message,
        message_type=message_type,
        pending_identification=len(Identifier.get_pending_identification()),
        audit_flag_count=len(Identifier.get_audit_flags()),
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        cluster_enabled=cluster_config.get("cluster_enabled", False),
        hostname=HOSTNAME
    )


@dashboard_bp.route("/logs")
def logs_page():
    """Per-stage logs overview page."""
    lines = request.args.get("lines", 50, type=int)
    logs = {stage: LogReader.get_stage_logs(stage, lines) for stage in LOG_FILES.keys()}
    return render_template(
        "logs.html",
        active_page="logs",
        version=DASHBOARD_VERSION,
        logs=logs,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/log/<stage>")
def stage_log_page(stage):
    """Individual stage log page."""
    if stage not in LOG_FILES:
        return f"Unknown stage: {stage}", 404
    lines = request.args.get("lines", 200, type=int)
    return render_template(
        "stage_log.html",
        stage=stage,
        logs=LogReader.get_stage_logs(stage, lines),
        version=DASHBOARD_VERSION,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/config")
def config_page():
    """Configuration edit page with collapsible sections."""
    return render_template(
        "config.html",
        active_page="config",
        config=ConfigManager.read_full(),
        sections=CONFIG_SECTIONS,
        boolean_settings=BOOLEAN_SETTINGS,
        dropdown_settings=DROPDOWN_SETTINGS,
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/architecture")
def architecture_page():
    """Architecture documentation page."""
    return render_template(
        "architecture.html",
        active_page="architecture",
        version=DASHBOARD_VERSION,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/issues")
@dashboard_bp.route("/identify")  # Keep old route for backwards compatibility
def issues_page():
    """Issues page for items needing attention (identification, audit flags)."""
    return render_template(
        "identify.html",
        active_page="issues",
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/status")
def status_page():
    """Service and timer status page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template(
        "status.html",
        active_page="status",
        services=ServiceController.get_all_service_status(),
        timers=ServiceController.get_all_timer_status(),
        udev_trigger=ServiceController.get_udev_trigger_status(),
        message=message,
        message_type=message_type,
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/health")
def health_page():
    """System health monitoring page."""
    message = request.args.get("message")
    message_type = request.args.get("type", "success")

    return render_template(
        "health.html",
        cpu=SystemHealth.get_cpu_usage(),
        memory=SystemHealth.get_memory_usage(),
        load=SystemHealth.get_load_average(),
        temps=SystemHealth.get_temperatures(),
        io=SystemHealth.get_io_stats(),
        processes=SystemHealth.get_dvd_processes(),
        message=message,
        message_type=message_type,
        pipeline_version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL,
        hostname=HOSTNAME
    )


@dashboard_bp.route("/cluster")
def cluster_page():
    """Cluster status page showing all nodes and distributed jobs."""
    config = ClusterManager.get_config()

    # Get this node's status
    this_node = {
        "node_name": config["node_name"],
        "transfer_mode": config["transfer_mode"],
        "capacity": ClusterManager.get_worker_capacity()
    }

    # Get peer status (only if cluster enabled)
    peers = []
    if config["cluster_enabled"]:
        peers = ClusterManager.get_all_peer_status()

    # Get hostname for quick-enable form
    try:
        hostname = socket.gethostname().split('.')[0]  # Short hostname
    except Exception:
        hostname = "node1"

    return render_template(
        "cluster.html",
        active_page="cluster",
        cluster_enabled=config["cluster_enabled"],
        this_node=this_node,
        peers=peers,
        peers_raw=config["peers_raw"],
        distributed_jobs=ClusterManager.get_distributed_jobs(),
        received_jobs=ClusterManager.get_received_jobs(),
        io=SystemHealth.get_io_stats(),
        hostname=hostname,
        version=get_pipeline_version(),
        dashboard_version=DASHBOARD_VERSION,
        github_url=GITHUB_URL
    )
