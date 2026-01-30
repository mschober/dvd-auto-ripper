"""Identification API routes for the DVD ripper dashboard."""
import json
import logging
import os
import re
from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger(__name__)

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


def _resolve_preview_path(filename):
    """Resolve a preview filename to its actual path on disk.

    Handles case mismatches between metadata and filesystem by falling
    back to a case-insensitive search in STAGING_DIR.
    """
    # Try exact match first
    path = os.path.join(STAGING_DIR, filename)
    if os.path.exists(path):
        return path
    # Case-insensitive fallback
    lower = filename.lower()
    try:
        for entry in os.listdir(STAGING_DIR):
            if entry.lower() == lower:
                return os.path.join(STAGING_DIR, entry)
    except OSError:
        pass
    return None


@api_identify_bp.route("/api/preview/<filename>")
def api_serve_preview(filename):
    """API: Serve preview video file."""
    # Security: only allow .preview.mp4 files
    if not filename.endswith('.preview.mp4'):
        return jsonify({"error": "Invalid preview file"}), 400

    preview_path = _resolve_preview_path(filename)
    if not preview_path:
        return jsonify({"error": "Preview not found"}), 404

    return send_file(preview_path, mimetype='video/mp4')


@api_identify_bp.route("/api/preview/<filename>", methods=["DELETE"])
def api_delete_preview(filename):
    """API: Delete a preview video file."""
    # Security: only allow .preview.mp4 files
    if not filename.endswith('.preview.mp4'):
        return jsonify({"error": "Invalid preview file"}), 400

    preview_path = _resolve_preview_path(filename)
    if not preview_path:
        return jsonify({"error": "Preview not found"}), 404

    try:
        os.remove(preview_path)
        logger.info("Deleted preview: %s", os.path.basename(preview_path))
        return jsonify({"status": "deleted"})
    except OSError as e:
        logger.error("Failed to delete preview %s: %s", filename, e)
        return jsonify({"error": str(e)}), 500


@api_identify_bp.route("/api/identify/<path:state_file>/dismiss", methods=["POST"])
def api_identify_dismiss(state_file):
    """API: Dismiss an item from the identify page.

    Clears needs_identification and removes the preview file so the
    item no longer appears on the identify page.
    """
    full_path = os.path.join(STAGING_DIR, state_file)
    if not os.path.exists(full_path):
        return jsonify({"error": "Item not found"}), 404

    try:
        with open(full_path, 'r') as f:
            metadata = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return jsonify({"error": str(e)}), 500

    # Delete preview file if it exists
    preview_path = metadata.get('preview_path', '').strip()
    if preview_path and os.path.isfile(preview_path):
        try:
            os.remove(preview_path)
            logger.info("Deleted preview: %s", os.path.basename(preview_path))
        except OSError as e:
            logger.error("Failed to delete preview %s: %s", preview_path, e)
            return jsonify({"error": f"Could not delete preview: {e}"}), 500

    # Clear identification flag and preview path
    metadata['needs_identification'] = False
    metadata['preview_path'] = ''
    try:
        with open(full_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        logger.info("Dismissed: %s", state_file)
        return jsonify({"status": "dismissed"})
    except IOError as e:
        logger.error("Failed to update state file %s: %s", state_file, e)
        return jsonify({"error": str(e)}), 500
