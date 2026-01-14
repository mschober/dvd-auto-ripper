"""Service control API routes for the DVD ripper dashboard."""
import subprocess
from flask import Blueprint, jsonify, request, redirect, url_for

from helpers.services import ServiceController

# Blueprint setup
api_services_bp = Blueprint('api_services', __name__)


@api_services_bp.route("/api/service/<name>", methods=["POST"])
def api_control_service(name):
    """API: Start, stop, or restart a service."""
    action = request.form.get("action") or (request.get_json() or {}).get("action")

    if not action:
        return jsonify({"error": "Action required"}), 400

    success, message = ServiceController.control_service(name, action)

    # If called from form, redirect back to status page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard.status_page",
                                    message=f"Service {name} {action}ed successfully",
                                    type="success"))
        else:
            return redirect(url_for("dashboard.status_page",
                                    message=f"Failed to {action} {name}: {message}",
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "service": name, "action": action})
    else:
        return jsonify({"error": message}), 500


@api_services_bp.route("/api/timer/<name>", methods=["POST"])
def api_control_timer(name):
    """API: Start (unpause), stop (pause), enable, or disable a timer."""
    action = request.form.get("action") or (request.get_json() or {}).get("action")

    if not action:
        return jsonify({"error": "Action required"}), 400

    success, message = ServiceController.control_timer(name, action)

    # Human-readable action descriptions
    action_desc = {
        "start": "unpaused",
        "stop": "paused",
        "enable": "enabled",
        "disable": "disabled"
    }.get(action, action + "ed")

    # If called from form, redirect back to status page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard.status_page",
                                    message=f"Timer {name} {action_desc} successfully",
                                    type="success"))
        else:
            return redirect(url_for("dashboard.status_page",
                                    message=f"Failed to {action} timer {name}: {message}",
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "timer": name, "action": action})
    else:
        return jsonify({"error": message}), 500


@api_services_bp.route("/api/udev/<action>", methods=["POST"])
def api_control_udev(action):
    """API: Pause or resume the udev disc detection trigger."""
    if action not in ["pause", "resume"]:
        return jsonify({"error": "Invalid action. Use 'pause' or 'resume'"}), 400

    # Use systemctl to trigger the udev control service
    # This runs via polkit which allows dvd-web to manage dvd-udev-control@*.service
    service = f"dvd-udev-control@{action}.service"

    try:
        result = subprocess.run(
            ["systemctl", "start", service],
            capture_output=True, text=True, timeout=10
        )
        success = result.returncode == 0
        if success:
            message = f"Disc detection {action}d"
        else:
            message = result.stderr.strip() or result.stdout.strip() or "Unknown error"
    except subprocess.TimeoutExpired:
        success = False
        message = "Command timed out"
    except Exception as e:
        success = False
        message = str(e)

    # Human-readable action descriptions
    action_desc = "paused" if action == "pause" else "resumed"

    # If called from form, redirect back to status page
    if request.headers.get("Accept", "").startswith("text/html") or \
       request.content_type != "application/json":
        if success:
            return redirect(url_for("dashboard.status_page",
                                    message=f"Disc detection {action_desc}",
                                    type="success"))
        else:
            return redirect(url_for("dashboard.status_page",
                                    message=f"Failed to {action} disc detection: {message}",
                                    type="error"))

    # JSON response for API calls
    if success:
        return jsonify({"status": "ok", "action": action, "message": message})
    else:
        return jsonify({"error": message}), 500
