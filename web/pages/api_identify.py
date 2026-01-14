"""Identification API routes for the DVD ripper dashboard."""
import os
import re
from flask import Blueprint, jsonify, request, send_file

from helpers.pipeline import STAGING_DIR
from helpers.identifier import Identifier, RENAMEABLE_STATES

# Blueprint setup
api_identify_bp = Blueprint('api_identify', __name__)


@api_identify_bp.route("/api/identify/pending")
def api_identify_pending():
    """API: Get items pending identification."""
    return jsonify(Identifier.get_pending_identification())


@api_identify_bp.route("/api/audit/flags")
def api_audit_flags():
    """API: Get audit flags for suspicious MKVs."""
    return jsonify(Identifier.get_audit_flags())


@api_identify_bp.route("/api/audit/clear/<path:title>", methods=["POST"])
def api_audit_clear(title):
    """API: Clear an audit flag after issue is resolved."""
    sanitized = title.replace(' ', '_')
    audit_file = os.path.join(STAGING_DIR, f".audit-{sanitized}")
    if os.path.exists(audit_file):
        try:
            os.remove(audit_file)
            return jsonify({"status": "ok", "message": f"Cleared audit flag for {title}"})
        except OSError as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Audit flag not found"}), 404


@api_identify_bp.route("/api/identify/<path:state_file>/rename", methods=["POST"])
def api_identify_rename(state_file):
    """API: Rename an item with new title/year."""
    data = request.get_json() or {}
    new_title = data.get('title', '').strip()
    new_year = data.get('year', '').strip()

    if not new_title:
        return jsonify({"error": "Title is required"}), 400

    if new_year and not re.match(r'^\d{4}$', new_year):
        return jsonify({"error": "Year must be 4 digits"}), 400

    # Build full path and verify state file exists
    full_path = os.path.join(STAGING_DIR, state_file)
    if not os.path.exists(full_path):
        return jsonify({"error": "Item not found"}), 404

    # Verify state is renameable
    state = state_file.rsplit('.', 1)[-1] if '.' in state_file else ''
    if state not in RENAMEABLE_STATES:
        return jsonify({"error": f"Cannot rename items in '{state}' state"}), 400

    try:
        new_state_file = Identifier.rename_item(full_path, new_title, new_year)
        return jsonify({
            "status": "renamed",
            "new_state_file": new_state_file,
            "new_title": new_title,
            "new_year": new_year
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_identify_bp.route("/api/preview/<filename>")
def api_serve_preview(filename):
    """API: Serve preview video file."""
    # Security: only allow .preview.mp4 files
    if not filename.endswith('.preview.mp4'):
        return jsonify({"error": "Invalid preview file"}), 400

    preview_path = os.path.join(STAGING_DIR, filename)
    if not os.path.exists(preview_path):
        return jsonify({"error": "Preview not found"}), 404

    return send_file(preview_path, mimetype='video/mp4')
