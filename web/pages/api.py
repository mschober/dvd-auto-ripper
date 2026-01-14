"""Core API routes for the DVD ripper dashboard."""
from flask import Blueprint, jsonify, request, redirect, url_for

from helpers.pipeline import get_queue_items, count_by_state
from helpers.config import ConfigManager
from helpers.system_health import SystemHealth
from helpers.locks import LockManager
from helpers.logs import LogReader, LOG_FILES
from helpers.progress import ProgressTracker
from helpers.processes import ProcessManager
from helpers.services import ServiceController

from pages.dashboard import get_pipeline_version, get_disk_usage, DASHBOARD_VERSION

# Blueprint setup
api_bp = Blueprint('api', __name__)


@api_bp.route("/api/status")
def api_status():
    """API: Get pipeline status counts."""
    return jsonify({
        "counts": count_by_state(),
        "locks": LockManager.get_status(),
        "pipeline_version": get_pipeline_version(),
        "dashboard_version": DASHBOARD_VERSION
    })


@api_bp.route("/api/queue")
def api_queue():
    """API: Get queue items with optional pagination."""
    page = request.args.get("page", type=int)
    per_page = request.args.get("per_page", type=int)

    if page is not None:
        return jsonify(get_queue_items(page=page, per_page=per_page))
    return jsonify(get_queue_items())  # All items for backward compat


@api_bp.route("/api/logs")
def api_logs():
    """API: Get recent logs (combined from all stages)."""
    lines = request.args.get("lines", 100, type=int)
    return jsonify({"logs": LogReader.get_all_logs(lines)})


@api_bp.route("/api/logs/<stage>")
def api_stage_logs(stage):
    """API: Get logs for a specific stage."""
    if stage not in LOG_FILES:
        return jsonify({"error": f"Unknown stage: {stage}"}), 404
    lines = request.args.get("lines", 100, type=int)
    return jsonify({"stage": stage, "logs": LogReader.get_stage_logs(stage, lines)})


@api_bp.route("/api/disk")
def api_disk():
    """API: Get disk usage."""
    return jsonify(get_disk_usage())


@api_bp.route("/api/config")
def api_config():
    """API: Get configuration."""
    return jsonify(ConfigManager.read())


@api_bp.route("/api/config/save", methods=["POST"])
def api_config_save():
    """API: Save configuration changes."""
    data = request.get_json() or {}
    settings = data.get("settings", {})

    if not settings:
        return jsonify({"success": False, "message": "No settings provided"}), 400

    # Write config and get results
    success, changed_keys, message = ConfigManager.write(settings)

    if success:
        # Get restart recommendations for changed settings
        restart_recs = ConfigManager.get_restart_recommendations(changed_keys)
        return jsonify({
            "success": True,
            "message": message,
            "changed_keys": changed_keys,
            "restart_recommendations": restart_recs
        })
    else:
        return jsonify({
            "success": False,
            "message": message
        }), 500


@api_bp.route("/api/locks")
def api_locks():
    """API: Get lock status."""
    return jsonify(LockManager.get_status())


@api_bp.route("/api/progress")
def api_progress():
    """API: Get real-time progress for active processes."""
    return jsonify(ProgressTracker.get_active_progress())


@api_bp.route("/api/health")
def api_health():
    """API: Get system health metrics."""
    return jsonify({
        "cpu": SystemHealth.get_cpu_usage(),
        "memory": SystemHealth.get_memory_usage(),
        "load": SystemHealth.get_load_average(),
        "temps": SystemHealth.get_temperatures(),
        "io": SystemHealth.get_io_stats()
    })


@api_bp.route("/api/processes")
def api_processes():
    """API: Get list of DVD ripper processes."""
    return jsonify(SystemHealth.get_dvd_processes())


@api_bp.route("/api/kill/<int:pid>", methods=["POST"])
def api_kill_process(pid):
    """API: Kill a DVD ripper process with cleanup."""
    success, message = ProcessManager.kill_process_with_cleanup(pid)

    # If called from form, redirect back to health page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard.health_page",
                                    message=message,
                                    type="success"))
        else:
            return redirect(url_for("dashboard.health_page",
                                    message=message,
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"error": message}), 500


@api_bp.route("/api/trigger/<stage>", methods=["POST"])
def api_trigger(stage):
    """API: Trigger encoder or transfer stage."""
    success, message = ServiceController.trigger_service(stage)

    # If called from form, redirect back to dashboard
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard.dashboard", message=f"{stage.title()} triggered successfully", type="success"))
        else:
            return redirect(url_for("dashboard.dashboard", message=f"Failed to trigger {stage}: {message}", type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "triggered", "stage": stage})
    else:
        return jsonify({"error": message}), 500


@api_bp.route("/api/queue/<path:state_file>/cancel", methods=["POST"])
def api_cancel_queue_item(state_file):
    """API: Cancel/remove a queue item by state file name."""
    data = request.get_json() or {}
    delete_files = data.get('delete_files', False)

    success, message = ProcessManager.cancel_queue_item(state_file, delete_files)

    # If called from form, redirect back to dashboard
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard.dashboard", message=message, type="success"))
        else:
            return redirect(url_for("dashboard.dashboard", message=f"Cancel failed: {message}", type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"error": message}), 500
