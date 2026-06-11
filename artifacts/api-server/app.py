"""Flask API server — V-CTRL automated video production platform."""

import os, json, threading, logging, sys, time

from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

from pipeline import (
    fetch_news_and_generate_script,
    load_session, save_session, list_sessions,
    update_status, run_production,
    session_dir, find_character_image,
    auto_resume_stalled_sessions,
    create_bundle_zip,
    repair_audio_files,
    audio_file_has_speech,
    SESSIONS_DIR,
)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins="*")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


# ── Startup: restart any sessions that were interrupted ────────────────────
auto_resume_stalled_sessions()


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
    custom_topic = None
    if request.is_json and request.json:
        custom_topic = (request.json.get("topic") or "").strip() or None
    elif request.form.get("topic"):
        custom_topic = request.form.get("topic", "").strip() or None
    try:
        result = fetch_news_and_generate_script(custom_topic=custom_topic)
        logger.info("Session %s | topic: %s", result["session_id"], result["topic"])
        return jsonify(result), 200
    except Exception as e:
        logger.error("Init failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/produce/<session_id>", methods=["POST"])
def produce_video(session_id: str):
    session_data = load_session(session_id)
    if not session_data:
        return jsonify({"error": "Session not found"}), 404
    if "character_image" not in request.files:
        return jsonify({"error": "Missing character_image file"}), 400

    # Read video format (default 16:9)
    video_format = "16:9"
    if request.form.get("video_format") in ("16:9", "9:16"):
        video_format = request.form.get("video_format")

    image_file = request.files["character_image"]
    import uuid as _uuid
    char_path = os.path.join(session_dir(session_id),
                             f"character_{_uuid.uuid4().hex}.jpg")
    image_file.save(char_path)
    logger.info("Character image saved: %s | format: %s", char_path, video_format)

    update_status(session_id, "pending", 0,
                  f"Démarrage de la production ({video_format})…",
                  images_done=[], audio_done=[],
                  character_image_path=char_path,
                  video_format=video_format)

    threading.Thread(
        target=run_production,
        args=(session_id, char_path, video_format),
        daemon=True
    ).start()
    return jsonify({"session_id": session_id, "status": "pending"}), 202


@app.route("/api/resume/<session_id>", methods=["POST"])
def resume_session(session_id: str):
    data = load_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404

    status = data.get("status")
    if status == "done":
        return jsonify({"status": "done", "message": "Already complete"}), 200
    if status == "error":
        return jsonify({"status": "error", "message": data.get("error")}), 200

    hb = data.get("last_heartbeat", 0)
    if time.time() - hb < 120:
        return jsonify({"status": status, "message": "Production already running"}), 200

    char_img = find_character_image(session_id)
    if not char_img:
        return jsonify({"error": "no_character_image",
                        "message": "Character image not found — please upload again"}), 409

    video_format = data.get("video_format", "16:9")
    logger.info("Resuming session %s (status was %s, format=%s)", session_id, status, video_format)
    update_status(session_id, "pending", data.get("progress", 0),
                  "Reprise de la production…",
                  images_done=data.get("images_done", []),
                  audio_done=data.get("audio_done", []))
    threading.Thread(
        target=run_production,
        args=(session_id, char_img, video_format),
        daemon=True
    ).start()
    return jsonify({"status": "resuming", "message": "Production restarted"}), 202


@app.route("/api/status/<session_id>")
def get_status(session_id: str):
    data = load_session(session_id)
    if not data:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({
        "session_id":   session_id,
        "status":       data.get("status",       "pending"),
        "progress":     data.get("progress",     0),
        "current_step": data.get("current_step", "…"),
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
        "video_format":     data.get("video_format",     "16:9"),
    }), 200


@app.route("/api/sessions")
def get_all_sessions():
    return jsonify(list_sessions()), 200


# ---------------------------------------------------------------------------
# Real-time preview
# ---------------------------------------------------------------------------

@app.route("/api/preview/<session_id>/manifest")
def preview_manifest(session_id: str):
    sdir   = session_dir(session_id)
    images = [i for i in range(20)
              if os.path.exists(os.path.join(sdir, f"image_{i:02d}.jpg"))
              and os.path.getsize(os.path.join(sdir, f"image_{i:02d}.jpg")) > 1000]
    audio  = [i for i in range(10)
              if os.path.exists(os.path.join(sdir, f"audio_{i:02d}.wav"))
              and os.path.getsize(os.path.join(sdir, f"audio_{i:02d}.wav")) > 44]
    return jsonify({"images": images, "audio": audio}), 200

@app.route("/api/preview/<session_id>/image/<int:index>")
def preview_image(session_id: str, index: int):
    p = os.path.join(session_dir(session_id), f"image_{index:02d}.jpg")
    if not os.path.exists(p): abort(404)
    return send_file(p, mimetype="image/jpeg")

@app.route("/api/preview/<session_id>/audio/<int:index>")
def preview_audio(session_id: str, index: int):
    p = os.path.join(session_dir(session_id), f"audio_{index:02d}.wav")
    if not os.path.exists(p): abort(404)
    return send_file(p, mimetype="audio/wav")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@app.route("/api/download/<session_id>")
def download_video(session_id: str):
    data = load_session(session_id)
    if not data: abort(404)
    vp = data.get("video_path")
    if not vp or not os.path.exists(vp): abort(404)
    return send_file(vp, mimetype="video/mp4", as_attachment=False,
                     download_name=f"video_{session_id[:8]}.mp4")


@app.route("/api/download-bundle/<session_id>")
def download_bundle(session_id: str):
    """Return a ZIP bundle with all session files."""
    data = load_session(session_id)
    if not data: abort(404)
    if data.get("status") != "done":
        return jsonify({"error": "Production not complete yet"}), 400
    try:
        zip_path = create_bundle_zip(session_id)
    except Exception as e:
        logger.error("Bundle ZIP error for %s: %s", session_id, e, exc_info=True)
        return jsonify({"error": str(e)}), 500
    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name=f"bundle_{session_id[:8]}.zip")


# ---------------------------------------------------------------------------
# Audio scan & repair
# ---------------------------------------------------------------------------

@app.route("/api/audio-scan/<session_id>")
def audio_scan(session_id: str):
    """
    Scan all 10 audio files and report which ones contain real speech.
    Returns per-file results without modifying anything.
    """
    data = load_session(session_id)
    if not data: abort(404)
    sdir    = session_dir(session_id)
    results = []
    for i in range(10):
        ap = os.path.join(sdir, f"audio_{i:02d}.wav")
        if not os.path.exists(ap):
            results.append({"index": i, "status": "missing"})
            continue
        size = os.path.getsize(ap)
        ok, reason = audio_file_has_speech(ap)
        results.append({
            "index":  i,
            "status": "ok" if ok else "no_speech",
            "reason": reason,
            "size":   size,
        })
    bad = [r["index"] for r in results if r["status"] != "ok"]
    return jsonify({"session_id": session_id, "results": results, "bad_indices": bad}), 200


@app.route("/api/repair-audio/<session_id>", methods=["POST"])
def repair_audio(session_id: str):
    """
    Scan all audio files; regenerate any that have no speech.
    Runs synchronously (blocks until done). Returns which files were repaired.
    After repair you should re-assemble: call /api/reassemble/<session_id>.
    """
    data = load_session(session_id)
    if not data: abort(404)

    logger.info("Audio repair requested for session %s", session_id)
    result = repair_audio_files(session_id)
    return jsonify({"session_id": session_id, **result}), 200


@app.route("/api/reassemble/<session_id>", methods=["POST"])
def reassemble(session_id: str):
    """
    Re-run only the assembly phase (Phase 3) for a completed or errored session.
    Useful after /api/repair-audio to rebuild the video with fixed audio.
    """
    data = load_session(session_id)
    if not data: abort(404)

    sdir = session_dir(session_id)

    # Build image / audio path lists from disk
    from pipeline import assemble_video, TOTAL_IMGS, _wav_duration
    image_paths = []
    for i in range(TOTAL_IMGS):
        p = os.path.join(sdir, f"image_{i:02d}.jpg")
        image_paths.append(p if os.path.exists(p) else None)

    audio_paths = []
    for i in range(10):
        p = os.path.join(sdir, f"audio_{i:02d}.wav")
        audio_paths.append(p if os.path.exists(p) else None)

    def _do_reassemble():
        try:
            video_path = assemble_video(session_id, image_paths, audio_paths, data)
            d = load_session(session_id) or data
            d.update({
                "status": "done", "progress": 100,
                "current_step": "Vidéo ré-assemblée !",
                "video_path": video_path,
                "video_url": f"/api/download/{session_id}",
                "error": None,
            })
            save_session(session_id, d)
            logger.info("[%s] Re-assembly complete", session_id)
        except Exception as e:
            logger.error("[%s] Re-assembly failed: %s", session_id, e, exc_info=True)
            update_status(session_id, "error", 0, "Échec du ré-assemblage.", str(e))

    threading.Thread(target=_do_reassemble, daemon=True).start()
    return jsonify({"session_id": session_id, "status": "reassembling"}), 202


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting Flask server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
