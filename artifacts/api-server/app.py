"""Flask API server — V-CTRL automated video production platform."""

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/api/healthz")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@app.route("/api/init", methods=["POST"])
def init_session():
    """
    Fetch today's news (or use a submitted topic) and generate the narrative script.
    Optional JSON body: { "topic": "custom topic string" }
    """
    custom_topic = None
    if request.is_json and request.json:
        custom_topic = request.json.get("topic", "").strip() or None
    elif request.form.get("topic"):
        custom_topic = request.form.get("topic", "").strip() or None

    try:
        logger.info("Initializing session | custom_topic=%s", custom_topic)
        result = fetch_news_and_generate_script(custom_topic=custom_topic)
        logger.info("Session %s | topic: %s", result["session_id"], result["topic"])
        return jsonify(result), 200
    except Exception as e:
        logger.error("Init failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/produce/<session_id>", methods=["POST"])
def produce_video(session_id: str):
    """Receive character image, start full production pipeline in background."""
    session_data = load_session(session_id)
    if not session_data:
        return jsonify({"error": "Session not found"}), 404

    if "character_image" not in request.files:
        return jsonify({"error": "Missing character_image file"}), 400

    image_file = request.files["character_image"]
    if not image_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    import uuid
    char_image_path = os.path.join(
        session_dir(session_id), f"character_{uuid.uuid4().hex}.jpg"
    )
    image_file.save(char_image_path)
    logger.info("Character image saved: %s", char_image_path)

    update_status(session_id, "pending", 0, "Démarrage de la production...",
                  images_done=[], audio_done=[])

    t = threading.Thread(
        target=run_production,
        args=(session_id, char_image_path),
        daemon=True,
    )
    t.start()

    return jsonify({"session_id": session_id, "status": "pending"}), 202


@app.route("/api/status/<session_id>")
def get_status(session_id: str):
    data = load_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({
        "session_id":   session_id,
        "status":       data.get("status",       "pending"),
        "progress":     data.get("progress",     0),
        "current_step": data.get("current_step", "..."),
        "error":        data.get("error"),
        "images_done":  data.get("images_done",  []),
        "audio_done":   data.get("audio_done",   []),
    }), 200


@app.route("/api/result/<session_id>")
def get_result(session_id: str):
    data = load_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404
    if data.get("status") != "done":
        return jsonify({"error": "Video not ready yet"}), 404
    return jsonify({
        "session_id":       session_id,
        "title":            data.get("title",            ""),
        "description":      data.get("description",      ""),
        "hashtags":         data.get("hashtags",         []),
        "video_url":        f"/api/download/{session_id}",
        "duration_seconds": data.get("duration_seconds", 75.0),
    }), 200


@app.route("/api/sessions")
def get_all_sessions():
    return jsonify(list_sessions()), 200


# ---------------------------------------------------------------------------
# Real-time preview — images & audio served while generating
# ---------------------------------------------------------------------------

@app.route("/api/preview/<session_id>/manifest")
def preview_manifest(session_id: str):
    """Return lists of which image and audio indices are ready on disk."""
    sdir = session_dir(session_id)
    images = [i for i in range(20)
              if os.path.exists(os.path.join(sdir, f"image_{i:02d}.jpg"))
              and os.path.getsize(os.path.join(sdir, f"image_{i:02d}.jpg")) > 1000]
    audio  = [i for i in range(10)
              if os.path.exists(os.path.join(sdir, f"audio_{i:02d}.wav"))
              and os.path.getsize(os.path.join(sdir, f"audio_{i:02d}.wav")) > 44]
    return jsonify({"images": images, "audio": audio}), 200


@app.route("/api/preview/<session_id>/image/<int:index>")
def preview_image(session_id: str, index: int):
    img_path = os.path.join(session_dir(session_id), f"image_{index:02d}.jpg")
    if not os.path.exists(img_path):
        abort(404)
    return send_file(img_path, mimetype="image/jpeg")


@app.route("/api/preview/<session_id>/audio/<int:index>")
def preview_audio(session_id: str, index: int):
    audio_path = os.path.join(session_dir(session_id), f"audio_{index:02d}.wav")
    if not os.path.exists(audio_path):
        abort(404)
    return send_file(audio_path, mimetype="audio/wav")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@app.route("/api/download/<session_id>")
def download_video(session_id: str):
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
# Dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting Flask server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
