"""
Flask API server for the automated video generation platform.
Replaces the Node.js Express server.
"""

import os
import json
import threading
import logging
import sys

from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

from pipeline import (
    fetch_news_and_generate_script,
    load_session,
    save_session,
    list_sessions,
    update_status,
    run_production,
    session_dir,
    SESSIONS_DIR,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, origins="*")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB upload limit


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/healthz")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/init", methods=["POST"])
def init_session():
    """Fetch today's news and generate the full script + image prompts."""
    try:
        logger.info("Initializing new session...")
        result = fetch_news_and_generate_script()
        logger.info("Session created: %s | Topic: %s", result["session_id"], result["topic"])
        return jsonify(result), 200
    except Exception as e:
        logger.error("Init failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/produce/<session_id>", methods=["POST"])
def produce_video(session_id: str):
    """
    Receive the character image and start the full production pipeline.
    Expects multipart/form-data with field 'character_image'.
    """
    session_data = load_session(session_id)
    if not session_data:
        return jsonify({"error": "Session not found"}), 404

    if "character_image" not in request.files:
        return jsonify({"error": "Missing character_image file"}), 400

    image_file = request.files["character_image"]
    if not image_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Save uploaded image
    import uuid
    char_image_path = os.path.join(session_dir(session_id), f"character_{uuid.uuid4().hex}.jpg")
    image_file.save(char_image_path)
    logger.info("Character image saved: %s", char_image_path)

    # Update status to pending
    update_status(session_id, "pending", 0, "Démarrage de la production...")

    # Launch pipeline in background thread
    t = threading.Thread(
        target=run_production,
        args=(session_id, char_image_path),
        daemon=True,
    )
    t.start()

    return jsonify({"session_id": session_id, "status": "pending"}), 202


@app.route("/api/status/<session_id>")
def get_status(session_id: str):
    """Poll production status."""
    data = load_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404

    return jsonify({
        "session_id": session_id,
        "status": data.get("status", "pending"),
        "progress": data.get("progress", 0),
        "current_step": data.get("current_step", "..."),
        "error": data.get("error"),
    }), 200


@app.route("/api/result/<session_id>")
def get_result(session_id: str):
    """Get the final video result metadata."""
    data = load_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404

    if data.get("status") != "done":
        return jsonify({"error": "Video not ready yet"}), 404

    return jsonify({
        "session_id": session_id,
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "hashtags": data.get("hashtags", []),
        "video_url": f"/api/download/{session_id}",
        "duration_seconds": data.get("duration_seconds", 75.0),
    }), 200


@app.route("/api/sessions")
def get_all_sessions():
    """List all sessions (newest first) for the history panel."""
    return jsonify(list_sessions()), 200


@app.route("/api/download/<session_id>")
def download_video(session_id: str):
    """Download the final MP4 video."""
    data = load_session(session_id)
    if not data:
        abort(404)

    video_path = data.get("video_path")
    if not video_path or not os.path.exists(video_path):
        abort(404)

    return send_file(
        video_path,
        mimetype="video/mp4",
        as_attachment=False,
        download_name=f"video_{session_id[:8]}.mp4",
    )


# ---------------------------------------------------------------------------
# Dev server entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting Flask server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
