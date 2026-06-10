"""
V-CTRL — Video generation pipeline
  Phase 1 : fetch news → 10-segment script + 20 image prompts
  Phase 2 : images (chained sequential) + audio (parallel) run SIMULTANEOUSLY
  Phase 3 : pure ffmpeg assembly — Ken Burns via scale+crop, subtitles via PIL
Audio note: Gemini TTS returns raw PCM (24 kHz, 16-bit, mono) — we wrap it in WAV.
"""

import io
import os
import json
import uuid
import wave
import base64
import array
import shutil
import logging
import subprocess
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import numpy as np
from PIL import Image as PILImage, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SESSIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sessions",
)
os.makedirs(SESSIONS_DIR, exist_ok=True)

TEXT_API_URL   = "https://delfaapiai.vercel.app/ai/copilot"
IMAGE_EDIT_URL = "https://gem-tw6a.onrender.com/edit"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

VIDEO_W, VIDEO_H = 1280, 720
FPS              = 24

_status_lock = threading.Lock()


# ---------------------------------------------------------------------------
# ffmpeg helper
# ---------------------------------------------------------------------------

def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _run_ff(args: list, timeout: int = 180):
    """Run ffmpeg/ffprobe, raise on error with stderr in message."""
    result = subprocess.run(
        args, capture_output=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg error (rc={result.returncode}):\n{result.stderr.decode(errors='replace')[-2000:]}"
        )
    return result


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


def update_status(session_id: str, status: str, progress: int,
                  current_step: str, error: str | None = None):
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
    raise ValueError(f"No JSON found: {text[:200]}")


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
                    f"Cinematic news scene about {topic[:40]}, photorealistic, scene {i*2+1}",
                    f"Dramatic close-up related to {topic[:40]}, studio lighting, scene {i*2+2}",
                ],
            }
            for i in range(10)
        ],
    }


def fetch_news_and_generate_script() -> dict:
    session_id = str(uuid.uuid4())

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

    t30 = topic[:30]
    script_prompt = (
        f'Tu es un scénariste. Sujet: "{topic}"\n'
        'Réponds UNIQUEMENT avec un JSON valide, sans markdown.\n'
        'Format:\n'
        '{"title":"Titre court accrocheur","description":"Description courte",'
        '"hashtags":["#tag1","#tag2","#tag3","#tag4","#tag5"],'
        '"segments":['
        + ",".join(
            f'{{"index":{i},"text":"Texte narratif percutant en français segment {i+1}",'
            f'"image_prompts":["cinematic scene {i*2+1} about {t30}, photorealistic","cinematic scene {i*2+2} about {t30}, dramatic lighting"]}}'
            for i in range(10)
        )
        + ']}'
        + '\nRemplace les textes par du contenu réel. JSON uniquement.'
    )

    try:
        script_raw  = _call_text_api(script_prompt, timeout=90)
        script_data = _extract_json(script_raw)
    except Exception as e:
        logger.warning("Script generation failed (%s), fallback", e)
        script_data = _fallback_script(topic)

    segments = script_data.get("segments", [])
    while len(segments) < 10:
        i = len(segments)
        segments.append({
            "index": i,
            "text":  f"Un aspect crucial : {topic[:50]}",
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
        "title":        script_data.get("title",       f"Actualité: {topic}")[:80],
        "description":  script_data.get("description", "Une vidéo sur l'actualité du jour.")[:200],
        "hashtags":     script_data.get("hashtags",    ["#actu", "#news", "#viral", "#info", "#tendance"])[:5],
        "segments":     segments,
        "status":       "pending",
        "progress":     0,
        "current_step": "Script généré. En attente de l'image du personnage.",
        "error":        None,
    }
    save_session(session_id, result)
    return result


# ---------------------------------------------------------------------------
# Phase 2a — Image generation (sequential, chained)
# ---------------------------------------------------------------------------

def _encode_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def generate_images(session_id: str, character_image_path: str,
                    session_data: dict, counters: dict, lock: threading.Lock) -> list[str]:
    segments    = session_data["segments"]
    all_prompts = [p for seg in segments for p in seg["image_prompts"]]
    image_paths = []
    prev_path   = character_image_path

    for i, prompt in enumerate(all_prompts):
        with lock:
            counters["img"] = i + 1
        pct = 5 + int((i / 20) * 35)
        update_status(session_id, "generating", pct,
                      f"Images {i+1}/20 • Audio {counters['aud']}/10 en parallèle...")

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
                d   = resp.json()
                b64 = d.get("image") or d.get("data") or d.get("result", "")
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                raw = base64.b64decode(b64)
            with open(img_path, "wb") as f:
                f.write(raw)
            prev_path = img_path
            logger.info("[%s] Image %d/20 OK", session_id, i + 1)
        except Exception as e:
            logger.error("[%s] Image %d failed: %s", session_id, i + 1, e)
            shutil.copy(prev_path, img_path)
        image_paths.append(img_path)

    return image_paths


# ---------------------------------------------------------------------------
# Phase 2b — Audio generation (parallel segments)
# ---------------------------------------------------------------------------

def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000,
                channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _generate_gemini_tts(text: str) -> bytes:
    """Call Gemini TTS. Returns proper WAV bytes."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash-preview-tts:generateContent")
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": "Kore"}
            }},
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data  = resp.json()
    part  = data["candidates"][0]["content"]["parts"][0]["inlineData"]
    mime  = part.get("mimeType", "audio/pcm")
    raw   = base64.b64decode(part["data"])

    # Gemini TTS returns raw PCM (24 kHz, 16-bit, mono) — NOT a valid WAV file.
    # Detect by checking RIFF magic or mime type.
    is_wav = raw[:4] == b"RIFF"
    if not is_wav:
        # Assume 24 kHz, 16-bit, mono PCM
        raw = _pcm_to_wav(raw, sample_rate=24000, channels=1, sample_width=2)
    return raw


def _silent_wav(path: str, duration: float = 7.5, sample_rate: int = 24000):
    """Write a silent WAV file."""
    num_frames = int(sample_rate * duration)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(array.array("h", [0] * num_frames).tobytes())


def _wav_duration(path: str) -> float:
    """Read duration from WAV header."""
    try:
        with wave.open(path, "r") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 7.5


def _generate_one_audio(session_id: str, i: int, seg: dict,
                        counters: dict, lock: threading.Lock) -> str:
    audio_path = os.path.join(session_dir(session_id), f"audio_{i:02d}.wav")
    try:
        wav_bytes = _generate_gemini_tts(seg["text"])
        with open(audio_path, "wb") as f:
            f.write(wav_bytes)
        logger.info("[%s] Audio %d/10 OK (%.1fs)", session_id, i + 1,
                    _wav_duration(audio_path))
    except Exception as e:
        logger.error("[%s] Audio %d failed: %s — silent fallback", session_id, i + 1, e)
        _silent_wav(audio_path, duration=7.5)
    with lock:
        counters["aud"] += 1
    return audio_path


def generate_audio(session_id: str, session_data: dict,
                   counters: dict, lock: threading.Lock) -> list[str]:
    """Generate all 10 audio files in parallel (max 3 concurrent Gemini calls)."""
    segments    = session_data["segments"]
    audio_paths = [None] * len(segments)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_generate_one_audio, session_id, i, seg, counters, lock): i
            for i, seg in enumerate(segments)
        }
        for future in as_completed(futures):
            i              = futures[future]
            audio_paths[i] = future.result()
    return audio_paths


# ---------------------------------------------------------------------------
# Phase 3 — Video assembly via pure ffmpeg (no MoviePy write_videofile)
# ---------------------------------------------------------------------------

def _find_font(size: int = 34) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for c in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _bake_subtitle_to_file(src_path: str, text: str, dst_path: str):
    """Load image, resize to VIDEO dimensions, burn subtitle, save as JPEG."""
    try:
        img = PILImage.open(src_path).convert("RGB")
    except Exception:
        img = PILImage.new("RGB", (VIDEO_W, VIDEO_H), (20, 20, 20))

    img  = img.resize((VIDEO_W, VIDEO_H), PILImage.LANCZOS)
    draw = ImageDraw.Draw(img)
    font = _find_font(34)

    # Word-wrap into ≤2 lines
    words = text.split()
    mid   = max(1, len(words) // 2)
    lines = [" ".join(words[:mid])]
    if words[mid:]:
        lines.append(" ".join(words[mid:]))

    y = VIDEO_H - 110
    for line in lines:
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw   = bbox[2] - bbox[0]
        except AttributeError:
            tw = len(line) * 18
        x = (VIDEO_W - tw) // 2
        # Shadow + background bar
        draw.rectangle([x - 8, y - 4, x + tw + 8, y + 40], fill=(0, 0, 0, 140))
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x,     y    ), line, font=font, fill=(255, 255, 255, 255))
        y += 46

    img.save(dst_path, "JPEG", quality=90)


# Ken Burns via ffmpeg: scale to 110 %, slow-crop to VIDEO_WxVIDEO_H
# Even images pan left→right, odd images pan right→left.
_KB_SCALE = f"{int(VIDEO_W*1.1)}:{int(VIDEO_H*1.1)}"  # 1408:792
_KB_DX    = VIDEO_W  * 1.1 - VIDEO_W                   # 128 px available
_KB_DY    = VIDEO_H  * 1.1 - VIDEO_H                   # 72 px available


def _ken_burns_vf(duration: float, direction: str = "fwd") -> str:
    """Return ffmpeg -vf filter string for a slow Ken Burns crop."""
    if direction == "fwd":
        x_expr = f"'trunc({_KB_DX:.1f}*t/{duration:.4f})'"
        y_expr = f"'trunc({_KB_DY:.1f}*t/{duration:.4f})'"
    else:
        x_expr = f"'trunc({_KB_DX:.1f}*(1-t/{duration:.4f}))'"
        y_expr = f"'trunc({_KB_DY:.1f}*(1-t/{duration:.4f}))'"
    return (
        f"scale={_KB_SCALE}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H}:{x_expr}:{y_expr},"
        f"setsar=1"
    )


def assemble_video(session_id: str, image_paths: list[str],
                   audio_paths: list[str], session_data: dict) -> str:
    """
    Build final MP4 using pure ffmpeg:
      1. Bake subtitles into image files (PIL).
      2. For each of the 10 segments, create one MP4 clip via ffmpeg
         (2 images concatenated via filter_complex + 1 audio track).
      3. Concatenate all 10 clips with ffmpeg concat demuxer.
    """
    ff   = _ffmpeg()
    sdir = session_dir(session_id)

    update_status(session_id, "assembling", 72, "Assemblage vidéo en cours...")

    segments      = session_data["segments"]
    segment_clips = []

    for seg_i, seg in enumerate(segments):
        update_status(
            session_id, "assembling",
            72 + int((seg_i / 10) * 24),
            f"Rendu segment {seg_i+1}/10...",
        )

        # Audio for this segment
        audio_path = audio_paths[seg_i] if seg_i < len(audio_paths) else None
        if not audio_path or not os.path.exists(audio_path):
            audio_path = os.path.join(sdir, f"audio_{seg_i:02d}.wav")
            _silent_wav(audio_path)
        audio_dur  = _wav_duration(audio_path)
        img_dur    = max(audio_dur / 2.0, 1.0)

        # Bake subtitle into both images
        baked = []
        for offset in range(2):
            img_idx  = seg_i * 2 + offset
            src      = image_paths[img_idx] if img_idx < len(image_paths) else image_paths[-1]
            baked_p  = os.path.join(sdir, f"sub_{img_idx:02d}.jpg")
            _bake_subtitle_to_file(src, seg["text"], baked_p)
            baked.append(baked_p)

        # Ken Burns direction alternates per segment
        direction = "fwd" if seg_i % 2 == 0 else "rev"
        vf0 = _ken_burns_vf(img_dur, direction)
        vf1 = _ken_burns_vf(img_dur, "rev" if direction == "fwd" else "fwd")

        seg_clip = os.path.join(sdir, f"seg_{seg_i:02d}.mp4")
        filter_complex = (
            f"[0:v]{vf0}[v0];"
            f"[1:v]{vf1}[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[vout]"
        )

        cmd = [
            ff, "-y",
            "-loop", "1", "-t", f"{img_dur:.4f}", "-i", baked[0],
            "-loop", "1", "-t", f"{img_dur:.4f}", "-i", baked[1],
            "-i", audio_path,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "2:a",
            "-t", f"{audio_dur:.4f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1",
            seg_clip,
        ]
        try:
            _run_ff(cmd, timeout=180)
            logger.info("[%s] Segment %d/10 assembled", session_id, seg_i + 1)
        except Exception as e:
            logger.error("[%s] Segment %d assembly failed: %s", session_id, seg_i + 1, e)
            raise

        segment_clips.append(seg_clip)

    # Write concat list
    concat_txt = os.path.join(sdir, "concat.txt")
    with open(concat_txt, "w") as f:
        for clip in segment_clips:
            f.write(f"file '{clip}'\n")

    out_path = os.path.join(sdir, "final_video.mp4")
    cmd = [
        ff, "-y",
        "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    update_status(session_id, "assembling", 97, "Finalisation de la vidéo...")
    _run_ff(cmd, timeout=300)
    logger.info("[%s] Final video written: %s", session_id, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Main production runner
# ---------------------------------------------------------------------------

def run_production(session_id: str, character_image_path: str):
    """Full pipeline. Images + audio run simultaneously, then assemble."""
    try:
        session_data = load_session(session_id)
        if not session_data:
            logger.error("[%s] Session not found", session_id)
            return

        lock     = threading.Lock()
        counters = {"img": 0, "aud": 0}

        update_status(session_id, "generating", 5,
                      "Génération images et audio en parallèle...")

        img_result   = {"paths": None, "error": None}
        audio_result = {"paths": None, "error": None}

        def run_images():
            try:
                img_result["paths"] = generate_images(
                    session_id, character_image_path, session_data, counters, lock
                )
            except Exception as e:
                img_result["error"] = e
                logger.error("[%s] Image thread: %s", session_id, e, exc_info=True)

        def run_audio():
            try:
                audio_result["paths"] = generate_audio(
                    session_id, session_data, counters, lock
                )
            except Exception as e:
                audio_result["error"] = e
                logger.error("[%s] Audio thread: %s", session_id, e, exc_info=True)

        t1 = threading.Thread(target=run_images, daemon=True)
        t2 = threading.Thread(target=run_audio,  daemon=True)
        t1.start(); t2.start()
        t1.join();  t2.join()

        image_paths = img_result["paths"] or []
        audio_paths = audio_result["paths"] or []

        if not image_paths:
            raise RuntimeError(str(img_result["error"] or "No images generated"))

        logger.info("[%s] Parallel done: %d images, %d audio",
                    session_id, len(image_paths), len(audio_paths))

        # Phase 3
        video_path = assemble_video(
            session_id, image_paths, audio_paths,
            load_session(session_id) or session_data,
        )

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
