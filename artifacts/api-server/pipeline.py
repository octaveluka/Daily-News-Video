"""
Video generation pipeline — V-CTRL
  Phase 1 : fetch news  → generate 10-segment script + 20 image prompts
  Phase 2 : images (chained, sequential) + audio (parallel) run SIMULTANEOUSLY
  Phase 3 : MoviePy 2.x assembly with Ken Burns + PIL-baked subtitles
Sessions stored on persistent disk at /home/runner/workspace/data/sessions/
"""

import os
import json
import uuid
import base64
import time
import logging
import threading
import shutil
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import numpy as np
from PIL import Image as PILImage, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Persistent storage — survives server restarts
SESSIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sessions",
)
os.makedirs(SESSIONS_DIR, exist_ok=True)

TEXT_API_URL   = "https://delfaapiai.vercel.app/ai/copilot"
IMAGE_EDIT_URL = "https://gem-tw6a.onrender.com/edit"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

VIDEO_SIZE = (1280, 720)
FPS        = 24

# Lock used when multiple threads write progress concurrently
_status_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def session_dir(session_id: str) -> str:
    path = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(path, exist_ok=True)
    return path


def load_session(session_id: str) -> dict | None:
    path = os.path.join(SESSIONS_DIR, session_id, "data.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_session(session_id: str, data: dict):
    path = os.path.join(SESSIONS_DIR, session_id, "data.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_sessions() -> list[dict]:
    """Return all sessions sorted newest-first."""
    sessions = []
    if not os.path.isdir(SESSIONS_DIR):
        return sessions
    for sid in os.listdir(SESSIONS_DIR):
        data = load_session(sid)
        if data:
            sessions.append({
                "session_id":   sid,
                "topic":        data.get("topic", ""),
                "title":        data.get("title", ""),
                "status":       data.get("status", "unknown"),
                "progress":     data.get("progress", 0),
                "current_step": data.get("current_step", ""),
                "error":        data.get("error"),
                "video_url":    f"/api/download/{sid}" if data.get("status") == "done" else None,
                "created_at":   data.get("created_at", ""),
            })
    sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return sessions


def update_status(session_id: str, status: str, progress: int, current_step: str, error: str | None = None):
    with _status_lock:
        data = load_session(session_id) or {}
        data["status"]       = status
        data["progress"]     = progress
        data["current_step"] = current_step
        data["error"]        = error
        save_session(session_id, data)


# ---------------------------------------------------------------------------
# Phase 1 — Narrative brain
# ---------------------------------------------------------------------------

def _call_text_api(message: str, timeout: int = 60) -> str:
    resp = requests.get(
        TEXT_API_URL,
        params={"message": message, "model": "default"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        for key in ("answer", "response", "text", "content", "result"):
            val = data.get(key)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
        return " ".join(v for v in data.values() if isinstance(v, str))
    return str(data).strip()


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            try:
                return json.loads(part)
            except Exception:
                pass
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        return json.loads(m.group())
    raise ValueError(f"No JSON found in: {text[:200]}")


def _fallback_script(topic: str) -> dict:
    return {
        "title":       f"L'actualité : {topic[:60]}",
        "description": "Découvrez l'essentiel de l'actualité en 75 secondes.",
        "hashtags":    ["#actu", "#news", "#viral", "#info", "#tendance"],
        "segments": [
            {
                "index": i,
                "text":  f"Segment {i+1} : {topic[:50]}",
                "image_prompts": [
                    f"Cinematic news reporter scene about {topic[:40]}, photorealistic, scene {i*2+1}",
                    f"Close-up dramatic portrait related to {topic[:40]}, studio lighting, scene {i*2+2}",
                ],
            }
            for i in range(10)
        ],
    }


def fetch_news_and_generate_script() -> dict:
    """Fetch today's top news and build a 10-segment script. Returns SessionInit-shaped dict."""
    session_id = str(uuid.uuid4())

    # --- Fetch news topic ---
    try:
        topic_raw = _call_text_api(
            "Donne-moi l'actualité la plus importante d'aujourd'hui en une seule phrase courte, "
            "en français. Réponds UNIQUEMENT avec la phrase, sans explication.",
            timeout=30,
        )
        topic = topic_raw.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning("News API failed: %s", e)
        topic = "L'intelligence artificielle transforme le monde du travail"

    # --- Generate script ---
    t30 = topic[:30]
    script_prompt = (
        f'Tu es un scénariste. Sujet: "{topic}"\n'
        'Réponds UNIQUEMENT avec un JSON valide, sans markdown.\n'
        'Format:\n'
        '{"title":"Titre court accrocheur","description":"Description courte",'
        '"hashtags":["#tag1","#tag2","#tag3","#tag4","#tag5"],'
        '"segments":['
        + ",".join(
            f'{{"index":{i},"text":"Texte narratif percutant en français pour le segment {i+1} sur le sujet",'
            f'"image_prompts":["cinematic scene {i*2+1} about {t30}, photorealistic, dramatic","cinematic scene {i*2+2} about {t30}, studio lighting"]}}'
            for i in range(10)
        )
        + ']}'
        + '\nRemplace les textes génériques par du contenu réel. JSON uniquement.'
    )

    try:
        script_raw = _call_text_api(script_prompt, timeout=90)
        script_data = _extract_json(script_raw)
    except Exception as e:
        logger.warning("Script generation failed (%s), using fallback", e)
        script_data = _fallback_script(topic)

    # Validate & pad segments
    segments = script_data.get("segments", [])
    while len(segments) < 10:
        i = len(segments)
        segments.append({
            "index": i,
            "text": f"Un aspect crucial : {topic[:50]}",
            "image_prompts": [
                f"Cinematic scene about {topic[:40]}, photorealistic, scene {i*2+1}",
                f"Dramatic close-up about {topic[:40]}, studio lighting, scene {i*2+2}",
            ],
        })
    segments = segments[:10]
    for i, seg in enumerate(segments):
        seg["index"] = i
        prompts = seg.get("image_prompts", [])
        while len(prompts) < 2:
            prompts.append(f"Cinematic scene about {topic[:40]}, scene {i*2+len(prompts)+1}")
        seg["image_prompts"] = prompts[:2]

    import datetime
    result = {
        "session_id":   session_id,
        "created_at":   datetime.datetime.utcnow().isoformat(),
        "topic":        topic,
        "title":        script_data.get("title", f"Actualité: {topic}")[:80],
        "description":  script_data.get("description", "Une vidéo sur l'actualité du jour.")[:200],
        "hashtags":     script_data.get("hashtags", ["#actu", "#news", "#viral", "#info", "#tendance"])[:5],
        "segments":     segments,
        "status":       "pending",
        "progress":     0,
        "current_step": "Script généré. En attente de l'image du personnage.",
        "error":        None,
    }
    save_session(session_id, result)
    return result


# ---------------------------------------------------------------------------
# Phase 2a — Image generation  (sequential, chained for character consistency)
# ---------------------------------------------------------------------------

def _encode_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def generate_images(session_id: str, character_image_path: str, session_data: dict,
                    img_counter: list, lock: threading.Lock) -> list[str]:
    """Generate 20 images sequentially, chaining each as context for the next."""
    segments   = session_data["segments"]
    all_prompts = [p for seg in segments for p in seg["image_prompts"]]
    image_paths = []
    prev_path   = character_image_path

    for i, prompt in enumerate(all_prompts):
        with lock:
            img_counter[0] = i + 1
        update_status(session_id, "generating",
                      5 + int((i / 20) * 35),
                      f"Images {i+1}/20 • Audio {img_counter[1]}/10 en cours...")

        img_path = os.path.join(session_dir(session_id), f"image_{i:02d}.jpg")
        try:
            resp = requests.post(
                IMAGE_EDIT_URL,
                json={"prompt": prompt, "image": _encode_b64(prev_path)},
                timeout=120,
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "image" in ct:
                raw = resp.content
            else:
                d = resp.json()
                b64 = d.get("image") or d.get("data") or d.get("result", "")
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                raw = base64.b64decode(b64)
            with open(img_path, "wb") as f:
                f.write(raw)
            prev_path = img_path
            logger.info("[%s] Image %d/20 OK", session_id, i + 1)
        except Exception as e:
            logger.error("[%s] Image %d failed: %s — copying previous", session_id, i + 1, e)
            shutil.copy(prev_path, img_path)
        image_paths.append(img_path)

    return image_paths


# ---------------------------------------------------------------------------
# Phase 2b — Audio generation  (parallel segments)
# ---------------------------------------------------------------------------

def _generate_gemini_tts(text: str) -> bytes:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set")
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    return base64.b64decode(b64)


def _silent_wav(path: str, duration: float = 7.5):
    import wave, array
    sample_rate = 22050
    num_frames  = int(sample_rate * duration)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(array.array("h", [0] * num_frames).tobytes())


def _generate_one_audio(session_id: str, i: int, seg: dict, audio_counter: list, lock: threading.Lock) -> str:
    audio_path = os.path.join(session_dir(session_id), f"audio_{i:02d}.wav")
    try:
        audio_bytes = _generate_gemini_tts(seg["text"])
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
        logger.info("[%s] Audio %d/10 OK", session_id, i + 1)
    except Exception as e:
        logger.error("[%s] Audio %d failed: %s — silent fallback", session_id, i + 1, e)
        _silent_wav(audio_path, duration=7.5)
    with lock:
        audio_counter[0] += 1
    return audio_path


def generate_audio(session_id: str, session_data: dict,
                   audio_counter: list, lock: threading.Lock) -> list[str]:
    """Generate all 10 audio files in parallel (max 3 concurrent Gemini calls)."""
    segments    = session_data["segments"]
    audio_paths = [None] * len(segments)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_generate_one_audio, session_id, i, seg, audio_counter, lock): i
            for i, seg in enumerate(segments)
        }
        for future in as_completed(futures):
            i = futures[future]
            audio_paths[i] = future.result()
            update_status(session_id, "generating",
                          5 + int((audio_counter[0] / 10) * 35),
                          f"Images {audio_counter[1]}/20 • Audio {audio_counter[0]}/10 en cours...")

    return audio_paths


# ---------------------------------------------------------------------------
# Phase 3 — Video assembly  (MoviePy 2.x)
# ---------------------------------------------------------------------------

def _load_frame(img_path: str) -> np.ndarray:
    """Load image, resize to VIDEO_SIZE, return RGB numpy array."""
    try:
        img = PILImage.open(img_path).convert("RGB")
        img = img.resize(VIDEO_SIZE, PILImage.LANCZOS)
        return np.array(img)
    except Exception:
        return np.zeros((VIDEO_SIZE[1], VIDEO_SIZE[0], 3), dtype=np.uint8)


def _find_font(size: int = 34) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _bake_subtitle(frame_arr: np.ndarray, text: str) -> np.ndarray:
    """Draw subtitle text with shadow directly into the frame array."""
    img  = PILImage.fromarray(frame_arr)
    draw = ImageDraw.Draw(img)
    font = _find_font(34)
    W, H = img.size

    # Word-wrap: split into at most 2 lines
    words = text.split()
    mid   = max(1, len(words) // 2)
    line1 = " ".join(words[:mid])
    line2 = " ".join(words[mid:])
    lines = [line1, line2] if line2 else [line1]

    y_start = H - 110
    for line in lines:
        try:
            bbox  = draw.textbbox((0, 0), line, font=font)
            tw    = bbox[2] - bbox[0]
        except AttributeError:
            tw = font.getlength(line) if hasattr(font, "getlength") else len(line) * 18
        x = (W - tw) // 2
        # Semi-transparent black bar behind text
        draw.rectangle([x - 8, y_start - 4, x + tw + 8, y_start + 40], fill=(0, 0, 0, 160))
        # Shadow
        draw.text((x + 2, y_start + 2), line, font=font, fill=(0, 0, 0, 255))
        # White text
        draw.text((x, y_start), line, font=font, fill=(255, 255, 255, 255))
        y_start += 44

    return np.array(img)


def _make_ken_burns_clip(img_path: str, subtitle_text: str, duration: float):
    """Return a VideoClip with Ken Burns zoom + subtitle baked in."""
    from moviepy import VideoClip

    base_arr = _load_frame(img_path)
    h, w = base_arr.shape[:2]

    def make_frame(t: float) -> np.ndarray:
        zoom    = 1.0 + 0.05 * min(t / max(duration, 0.001), 1.0)
        new_w   = int(w / zoom)
        new_h   = int(h / zoom)
        x0      = (w - new_w) // 2
        y0      = (h - new_h) // 2
        cropped = base_arr[y0:y0 + new_h, x0:x0 + new_w]
        frame   = np.array(PILImage.fromarray(cropped).resize((w, h), PILImage.LANCZOS))
        return _bake_subtitle(frame, subtitle_text)

    return VideoClip(make_frame, duration=duration).with_fps(FPS)


def assemble_video(session_id: str, image_paths: list[str], audio_paths: list[str],
                   session_data: dict) -> str:
    """Assemble the final MP4 using MoviePy 2.x."""
    from moviepy import AudioFileClip, concatenate_videoclips

    update_status(session_id, "assembling", 72, "Assemblage vidéo en cours...")

    segments = session_data["segments"]
    clips    = []

    for seg_i, seg in enumerate(segments):
        update_status(
            session_id, "assembling",
            72 + int((seg_i / 10) * 22),
            f"Rendu segment {seg_i+1}/10...",
        )

        # Audio duration determines clip length
        audio_path = audio_paths[seg_i] if seg_i < len(audio_paths) else None
        try:
            audio_clip    = AudioFileClip(audio_path) if audio_path and os.path.exists(audio_path) else None
            audio_duration = audio_clip.duration if audio_clip else 7.5
        except Exception:
            audio_clip    = None
            audio_duration = 7.5

        img_duration = audio_duration / 2.0  # 2 images share each audio segment

        for img_offset in range(2):
            img_idx  = seg_i * 2 + img_offset
            img_path = image_paths[img_idx] if img_idx < len(image_paths) else None
            if img_path is None or not os.path.exists(img_path):
                img_path = None

            clip = _make_ken_burns_clip(
                img_path or (image_paths[0] if image_paths else ""),
                seg["text"],
                img_duration,
            )

            # Audio only on first image of each segment pair
            if audio_clip is not None and img_offset == 0:
                clip = clip.with_audio(audio_clip)

            clips.append(clip)

    if not clips:
        raise ValueError("No clips were created")

    final    = concatenate_videoclips(clips, method="compose")
    out_path = os.path.join(session_dir(session_id), "final_video.mp4")

    final.write_videofile(
        out_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=os.path.join(session_dir(session_id), "temp_audio.m4a"),
        remove_temp=True,
        logger=None,
    )
    final.close()
    return out_path


# ---------------------------------------------------------------------------
# Main production runner — images + audio in PARALLEL, then assemble
# ---------------------------------------------------------------------------

def run_production(session_id: str, character_image_path: str):
    """Full pipeline. Images and audio run simultaneously in separate threads."""
    try:
        session_data = load_session(session_id)
        if not session_data:
            logger.error("[%s] Session not found", session_id)
            return

        update_status(session_id, "generating", 5,
                      "Génération images et audio en parallèle...")

        # Shared counters for status display
        lock          = threading.Lock()
        img_counter   = [0, 0]   # [images_done, audio_done] — img thread writes [0], audio writes [1]
        audio_counter = [0, img_counter[0]]  # audio thread references same list

        # We'll collect results here
        image_result  = {"paths": None, "error": None}
        audio_result  = {"paths": None, "error": None}

        def run_images():
            try:
                image_result["paths"] = generate_images(
                    session_id, character_image_path, session_data, img_counter, lock
                )
            except Exception as e:
                image_result["error"] = e
                logger.error("[%s] Image thread error: %s", session_id, e, exc_info=True)

        def run_audio():
            try:
                audio_result["paths"] = generate_audio(
                    session_id, session_data, audio_counter, lock
                )
            except Exception as e:
                audio_result["error"] = e
                logger.error("[%s] Audio thread error: %s", session_id, e, exc_info=True)

        t_img   = threading.Thread(target=run_images, daemon=True)
        t_audio = threading.Thread(target=run_audio,  daemon=True)
        t_img.start()
        t_audio.start()
        t_img.join()
        t_audio.join()

        # Check for failures
        if image_result["error"] and audio_result["error"]:
            raise RuntimeError(f"Images: {image_result['error']} | Audio: {audio_result['error']}")

        image_paths = image_result["paths"] or []
        audio_paths = audio_result["paths"] or []

        if not image_paths:
            raise RuntimeError(str(image_result["error"] or "No images generated"))

        logger.info("[%s] Parallel phase done: %d images, %d audio files",
                    session_id, len(image_paths), len(audio_paths))

        # Phase 3 — assembly
        update_status(session_id, "assembling", 72, "Assemblage de la vidéo...")
        video_path = assemble_video(session_id, image_paths, audio_paths,
                                    load_session(session_id) or session_data)

        # Final save
        with _status_lock:
            data = load_session(session_id) or session_data
            data["status"]           = "done"
            data["progress"]         = 100
            data["current_step"]     = "Vidéo prête !"
            data["video_path"]       = video_path
            data["video_url"]        = f"/api/download/{session_id}"
            data["duration_seconds"] = 75.0
            data["error"]            = None
            save_session(session_id, data)

        logger.info("[%s] Production complete → %s", session_id, video_path)

    except Exception as e:
        logger.error("[%s] Production failed: %s", session_id, e, exc_info=True)
        update_status(session_id, "error", 0, "Une erreur est survenue.", str(e))
