"""
V-CTRL — Video generation pipeline
  Phase 1 : fetch news / custom topic → 10-segment DOCUMENTARY script + 20 image prompts
  Phase 2 : images (chained sequential) + audio (parallel, validated, retry) run SIMULTANEOUSLY
  Phase 3 : pure ffmpeg assembly — Ken Burns, burned subtitles via PIL

Narrative style: journalistic documentary (Al Jazeera / Vice News), NOT character-movement fiction.
Image prompts: every prompt explicitly places THE CHARACTER from the reference photo in the scene.
Audio: WAV duration validated after each TTS call; re-generated if empty/silent.
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
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import numpy as np
from PIL import Image as PILImage, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

# In development: data/sessions/ at the workspace root (persistent).
# In production: /tmp/sessions/ (writable, but ephemeral — resets on restart).
# Override with SESSIONS_DIR env var if needed.
_default_sessions_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sessions",
)
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", _default_sessions_dir)
# Fallback to /tmp if the default path is not writable (e.g. production container)
try:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    # quick write test
    _test = os.path.join(SESSIONS_DIR, ".write_test")
    with open(_test, "w") as _f:
        _f.write("ok")
    os.remove(_test)
except OSError:
    SESSIONS_DIR = "/tmp/v_ctrl_sessions"
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
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg error (rc={result.returncode}):\n"
            f"{result.stderr.decode(errors='replace')[-2000:]}"
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
                  current_step: str, error: str | None = None, **extra):
    with _status_lock:
        data = load_session(session_id) or {}
        data["status"]       = status
        data["progress"]     = progress
        data["current_step"] = current_step
        data["error"]        = error
        data.update(extra)
        save_session(session_id, data)


# ---------------------------------------------------------------------------
# Phase 1 — Script generation (documentary / journalistic style)
# ---------------------------------------------------------------------------

def _call_text_api(message: str, timeout: int = 90) -> str:
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


# ─── THE SINGLE MOST IMPORTANT PROMPT ────────────────────────────────────────
# Style cible : documentaire journalistique (Al Jazeera, Vice, France 24).
# Raconter des FAITS, des CONTEXTES, des ÉVÉNEMENTS — jamais les états d'âme
# ni les gestes physiques de personnages fictifs.
# ─────────────────────────────────────────────────────────────────────────────

_NARRATIVE_SYSTEM = """Tu es le narrateur d'un documentaire journalistique de haut niveau (style Al Jazeera / Vice News / France 24).
Tu écris en français, sur un sujet donné, un récit en 10 segments pour une vidéo narrative virale.

═══════════════════════════════════════════
RÈGLE N°1 — STYLE DOCUMENTAIRE UNIQUEMENT
═══════════════════════════════════════════
Chaque segment doit décrire :
  • Des FAITS concrets, des ÉVÉNEMENTS réels ou plausibles
  • Des CONTEXTES historiques, géopolitiques, sociaux
  • Des CITATIONS de personnes impliquées (inventées mais crédibles)
  • Des CONSÉQUENCES, des ENJEUX, des RETOURNEMENTS de situation

═══════════════════════════════════════════
RÈGLE N°2 — CE QUI EST STRICTEMENT INTERDIT
═══════════════════════════════════════════
❌ Décrire les gestes, mouvements ou sensations physiques d'un personnage fictif
   Mauvais : "Elle ouvre les yeux. Ses mains tremblent. Il respire profondément."
❌ Narrer des états d'âme internes ("il se sent", "elle ressent", "son cœur bat")
❌ Raconter comme un roman ("il se leva et marcha vers la fenêtre")
❌ Statistiques froides sans contexte humain
❌ Bullet points, listes, titres

═══════════════════════════════════════════
EXEMPLE PARFAIT À IMITER
═══════════════════════════════════════════
"Depuis quelque temps, un mouvement inédit et profondément émouvant prend de l'ampleur. À la suite du vote d'une loi historique permettant d'accorder la nationalité béninoise aux afro-descendants, de nombreuses personnes venues d'Haïti, des Caraïbes et des Amériques font le voyage pour renouer avec leurs racines."

"L'un des récits les plus poignants est celui de ces visiteurs qui se retrouvent face à la célèbre Porte du Non-Retour à Ouidah. Historiquement, ce monument commémore la tragédie de plus d'un million de personnes arrachées à leur terre et à leur culture."

"Aujourd'hui, l'histoire s'inverse. Une jeune femme venue déposer sa demande de nationalité a récemment témoigné de ce bouleversement en traversant ce lieu de mémoire : 'Aujourd'hui, je franchis la porte, mais de mon plein gré. Volontairement. Je ne suis pas enchaînée.'"

═══════════════════════════════════════════
RÈGLE N°3 — PROMPTS IMAGE (TRÈS IMPORTANT)
═══════════════════════════════════════════
Chaque prompt image DOIT :
1. Commencer par le TYPE DE PLAN en majuscules
2. Mentionner explicitement "the person from the reference photo" pour que le personnage apparaisse
3. Décrire précisément le LIEU, le DÉCOR et la LUMIÈRE qui CORRESPONDENT aux paroles du segment
4. Varier FORTEMENT chaque prompt (pas deux fois le même type de plan ou décor)

TYPES DE PLANS À UTILISER (chaque prompt = un type différent) :
WIDE ESTABLISHING SHOT | CLOSE-UP PORTRAIT | EXTREME CLOSE-UP | AERIAL VIEW |
LOW ANGLE SHOT | OVER-THE-SHOULDER | SILHOUETTE SHOT | DUTCH ANGLE |
MEDIUM SHOT | TRACKING SHOT | POV SHOT | TWO-SHOT

EXEMPLES DE BONS PROMPTS IMAGE :
• "WIDE ESTABLISHING SHOT - the person from the reference photo standing before the Door of No Return in Ouidah Benin at sunset, fog rolling from the ocean, documentary photography, photorealistic, 8k, cinematic"
• "EXTREME CLOSE-UP - the person from the reference photo's hands holding official citizenship documents, warm golden light, depth of field, photorealistic, 8k, cinematic"
• "AERIAL VIEW - a coastal West African port town at dawn, crowded pier with hundreds of people arriving by boat, photorealistic, 8k, cinematic"

═══════════════════════════════════════════
FORMAT DE SORTIE — JSON STRICT
═══════════════════════════════════════════
Réponds UNIQUEMENT avec ce JSON, sans markdown, sans commentaires :
{
  "title": "Titre court et accrocheur (60 chars max)",
  "description": "Résumé factuel et émouvant en 2 phrases",
  "hashtags": ["#tag1","#tag2","#tag3","#tag4","#tag5"],
  "segments": [
    {
      "index": 0,
      "text": "Premier segment documentaire (30-45 mots). Accroche factuelle qui plante le contexte.",
      "image_prompts": [
        "TYPE DE PLAN - the person from the reference photo [action] in [lieu précis correspondant au texte], [lumière], photorealistic, 8k, cinematic",
        "AUTRE TYPE DE PLAN - the person from the reference photo [action différente] in [autre décor lié au texte], [autre lumière], photorealistic, 8k, cinematic"
      ]
    }
  ]
}
Les 10 segments doivent former un arc narratif complet : contexte → développement → tension → climax → resolution."""


def _build_script_prompt(topic: str) -> str:
    return f"""{_NARRATIVE_SYSTEM}

SUJET : "{topic}"

Génère maintenant les 10 segments documentaires sur ce sujet.
Chaque "text" doit être entre 30 et 45 mots — assez long pour une narration audio fluide.
Les image_prompts doivent correspondre exactement au lieu et au contexte décrit dans le texte.
JSON uniquement, pas de markdown."""


def _fallback_script(topic: str) -> dict:
    t = topic[:60]
    return {
        "title":       f"Reportage : {t}",
        "description": f"Un documentaire sur {t} et ses enjeux humains.",
        "hashtags":    ["#reportage", "#documentaire", "#info", "#actualité", "#viral"],
        "segments": [
            {
                "index": i,
                "text":  f"Segment {i+1} — Au cœur de cette actualité mondiale, les faits révèlent une réalité complexe et profondément humaine qui touche des millions de personnes à travers le monde.",
                "image_prompts": [
                    f"WIDE ESTABLISHING SHOT - the person from the reference photo in a dramatic scene related to {t}, golden hour, photorealistic, 8k, cinematic",
                    f"CLOSE-UP PORTRAIT - the person from the reference photo facing camera in context of {t}, dramatic lighting, photorealistic, 8k, cinematic",
                ]
            }
            for i in range(10)
        ],
    }


def fetch_news_and_generate_script(custom_topic: str | None = None) -> dict:
    session_id = str(uuid.uuid4())

    # Step 1: Get topic
    if custom_topic and custom_topic.strip():
        topic = custom_topic.strip()
        logger.info("Using custom topic: %s", topic)
    else:
        try:
            topic_raw = _call_text_api(
                "Donne-moi en une phrase courte (max 15 mots) le fait d'actualité le plus marquant "
                "et le plus humain d'aujourd'hui, en français. Uniquement la phrase, sans ponctuation finale.",
                timeout=30,
            )
            topic = topic_raw.strip().strip('"').strip("'").rstrip(".")
        except Exception as e:
            logger.warning("News fetch failed: %s", e)
            topic = "Le réchauffement climatique force des communautés entières à quitter leurs terres ancestrales"

    # Step 2: Generate narrative documentary script
    for attempt in range(3):
        try:
            raw  = _call_text_api(_build_script_prompt(topic), timeout=120)
            data = _extract_json(raw)
            if data.get("segments") and len(data["segments"]) >= 5:
                break
        except Exception as e:
            logger.warning("Script attempt %d failed: %s", attempt + 1, e)
            data = {}
    else:
        data = _fallback_script(topic)

    # Validate & pad to exactly 10 segments
    segments = data.get("segments", [])
    while len(segments) < 10:
        i = len(segments)
        segments.append({
            "index": i,
            "text":  f"Ce phénomène mondial, qui touche des millions de personnes, révèle les enjeux profonds d'une réalité que peu osent aborder : {topic[:50]}.",
            "image_prompts": [
                f"MEDIUM SHOT - the person from the reference photo in a meaningful location related to {topic[:40]}, warm natural light, photorealistic, 8k, cinematic",
                f"DUTCH ANGLE - the person from the reference photo in a dramatic context of {topic[:40]}, blue hour, photorealistic, 8k, cinematic",
            ],
        })
    segments = segments[:10]
    for i, seg in enumerate(segments):
        seg["index"] = i
        prompts = seg.get("image_prompts", [])
        while len(prompts) < 2:
            prompts.append(
                f"WIDE SHOT - the person from the reference photo in a scene about {topic[:40]}, photorealistic, 8k, cinematic"
            )
        seg["image_prompts"] = prompts[:2]

    import datetime
    result = {
        "session_id":   session_id,
        "created_at":   datetime.datetime.utcnow().isoformat(),
        "topic":        topic,
        "title":        data.get("title",       f"Reportage : {topic}")[:80],
        "description":  data.get("description", "Un documentaire sur l'actualité du monde.")[:200],
        "hashtags":     data.get("hashtags",    ["#reportage", "#documentaire", "#info", "#actualité", "#viral"])[:5],
        "segments":     segments,
        "status":       "pending",
        "progress":     0,
        "current_step": "Script généré — en attente de l'image du personnage.",
        "images_done":  [],
        "audio_done":   [],
        "error":        None,
    }
    save_session(session_id, result)
    return result


# ---------------------------------------------------------------------------
# Phase 2a — Image generation (sequential, chained for character consistency)
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

        img_path = os.path.join(session_dir(session_id), f"image_{i:02d}.jpg")
        success  = False

        for attempt in range(3):
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
                    if isinstance(b64, str) and b64.startswith("data:"):
                        b64 = b64.split(",", 1)[1]
                    raw = base64.b64decode(b64)

                if len(raw) < 5000:
                    raise ValueError(f"Image too small ({len(raw)} bytes)")

                with open(img_path, "wb") as f:
                    f.write(raw)
                prev_path = img_path
                success   = True
                logger.info("[%s] Image %d/20 OK (attempt %d)", session_id, i + 1, attempt + 1)
                break
            except Exception as e:
                logger.warning("[%s] Image %d attempt %d failed: %s", session_id, i + 1, attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt)

        if not success:
            logger.error("[%s] Image %d: all attempts failed, copying previous", session_id, i + 1)
            shutil.copy(prev_path, img_path)

        image_paths.append(img_path)

        with _status_lock:
            data = load_session(session_id) or {}
            done = data.get("images_done", [])
            if i not in done:
                done.append(i)
            data["images_done"]  = done
            data["status"]       = "generating"
            data["progress"]     = pct
            data["current_step"] = f"Images {i+1}/20 • Audio {counters.get('aud', 0)}/10 en parallèle..."
            save_session(session_id, data)

    return image_paths


# ---------------------------------------------------------------------------
# Phase 2b — Audio generation (parallel, validated, retry until real speech)
# ---------------------------------------------------------------------------

def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000,
                channels: int = 1, sample_width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _wav_duration(path: str) -> float:
    """Return duration in seconds, or 0 if file is invalid."""
    try:
        with wave.open(path, "r") as wf:
            frames = wf.getnframes()
            rate   = wf.getframerate()
            if rate == 0:
                return 0.0
            return frames / rate
    except Exception:
        return 0.0


def _wav_is_silent(path: str, threshold: float = 50.0) -> bool:
    """Return True if the WAV file is essentially silent (RMS below threshold)."""
    try:
        with wave.open(path, "r") as wf:
            frames = wf.readframes(wf.getnframes())
            sw = wf.getsampwidth()
        if sw == 2:
            samples = array.array("h", frames)
        else:
            return False
        if not samples:
            return True
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        return rms < threshold
    except Exception:
        return True


def _generate_gemini_tts(text: str, max_retries: int = 5) -> bytes:
    """
    Call Gemini TTS with exponential-backoff retry.
    Validates that the returned WAV contains real speech (not silence, not empty).
    Returns valid WAV bytes.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set")

    clean_text = text.strip()
    if not clean_text:
        raise ValueError("Empty text passed to TTS")

    logger.info("[TTS] Generating audio for: %s...", clean_text[:60])

    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash-preview-tts:generateContent")
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": clean_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": "Kore"}
            }},
        },
    }

    last_err = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
            resp.raise_for_status()

            data = resp.json()

            # Navigate Gemini response structure
            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("No candidates in TTS response")

            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                raise ValueError("No parts in TTS response")

            inline = parts[0].get("inlineData", {})
            b64    = inline.get("data", "")
            if not b64:
                raise ValueError("No audio data in TTS response")

            raw = base64.b64decode(b64)
            if len(raw) < 1000:
                raise ValueError(f"Audio data too small ({len(raw)} bytes)")

            # Gemini TTS returns raw PCM — wrap in WAV if needed
            if raw[:4] != b"RIFF":
                wav = _pcm_to_wav(raw, sample_rate=24000, channels=1, sample_width=2)
            else:
                wav = raw

            # Validate duration
            buf = io.BytesIO(wav)
            try:
                with wave.open(buf, "r") as wf:
                    dur = wf.getnframes() / wf.getframerate()
            except Exception as e:
                raise ValueError(f"Invalid WAV: {e}")

            if dur < 0.5:
                raise ValueError(f"Audio too short: {dur:.2f}s")

            logger.info("[TTS] OK (attempt %d) — duration %.2fs for: %s...",
                        attempt + 1, dur, clean_text[:40])
            return wav

        except Exception as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            logger.warning("[TTS] Attempt %d/%d failed: %s — retry in %ds",
                           attempt + 1, max_retries, e, wait)
            if attempt < max_retries - 1:
                time.sleep(wait)

    raise RuntimeError(f"TTS failed after {max_retries} attempts: {last_err}")


def _write_silent_wav(path: str, duration: float = 7.5, sample_rate: int = 24000):
    num_frames = int(sample_rate * duration)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(array.array("h", [0] * num_frames).tobytes())


def _generate_one_audio(session_id: str, i: int, seg: dict,
                        counters: dict, lock: threading.Lock) -> str:
    audio_path = os.path.join(session_dir(session_id), f"audio_{i:02d}.wav")
    text       = seg.get("text", "").strip()

    if not text:
        logger.error("[%s] Segment %d has empty text — writing silence", session_id, i)
        _write_silent_wav(audio_path)
    else:
        try:
            wav_bytes = _generate_gemini_tts(text)
            with open(audio_path, "wb") as f:
                f.write(wav_bytes)

            dur = _wav_duration(audio_path)
            logger.info("[%s] Audio %d/10 saved (%.2fs)", session_id, i + 1, dur)

            # Extra safety: re-check silence
            if _wav_is_silent(audio_path):
                logger.warning("[%s] Audio %d sounds silent — retrying once", session_id, i)
                wav_bytes2 = _generate_gemini_tts(text, max_retries=3)
                with open(audio_path, "wb") as f:
                    f.write(wav_bytes2)
                dur = _wav_duration(audio_path)
                logger.info("[%s] Audio %d re-generated (%.2fs)", session_id, i + 1, dur)

        except Exception as e:
            logger.error("[%s] Audio %d failed all retries: %s — writing silence", session_id, i + 1, e)
            _write_silent_wav(audio_path)

    with lock:
        counters["aud"] = counters.get("aud", 0) + 1

    with _status_lock:
        data = load_session(session_id) or {}
        done = data.get("audio_done", [])
        if i not in done:
            done.append(i)
        data["audio_done"] = done
        save_session(session_id, data)

    return audio_path


def generate_audio(session_id: str, session_data: dict,
                   counters: dict, lock: threading.Lock) -> list[str]:
    segments    = session_data["segments"]
    audio_paths = [None] * len(segments)

    # max_workers=2 avoids Gemini rate limits; segments run in parallel pairs
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_generate_one_audio, session_id, i, seg, counters, lock): i
            for i, seg in enumerate(segments)
        }
        for future in as_completed(futures):
            i              = futures[future]
            audio_paths[i] = future.result()

    return audio_paths


# ---------------------------------------------------------------------------
# Phase 3 — Video assembly via pure ffmpeg
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
    try:
        img = PILImage.open(src_path).convert("RGB")
    except Exception:
        img = PILImage.new("RGB", (VIDEO_W, VIDEO_H), (20, 20, 20))
    img  = img.resize((VIDEO_W, VIDEO_H), PILImage.LANCZOS)
    draw = ImageDraw.Draw(img)
    font = _find_font(34)

    # Word-wrap to max 2 lines
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
        draw.rectangle([x - 8, y - 4, x + tw + 8, y + 40], fill=(0, 0, 0, 140))
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x,     y    ), line, font=font, fill=(255, 255, 255, 255))
        y += 46
    img.save(dst_path, "JPEG", quality=90)


_KB_SCALE = f"{int(VIDEO_W*1.1)}:{int(VIDEO_H*1.1)}"
_KB_DX    = VIDEO_W  * 1.1 - VIDEO_W
_KB_DY    = VIDEO_H  * 1.1 - VIDEO_H


def _ken_burns_vf(duration: float, direction: str = "fwd") -> str:
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
    ff   = _ffmpeg()
    sdir = session_dir(session_id)
    update_status(session_id, "assembling", 72, "Assemblage de la vidéo en cours...")

    segments      = session_data["segments"]
    segment_clips = []

    for seg_i, seg in enumerate(segments):
        update_status(
            session_id, "assembling",
            72 + int((seg_i / 10) * 24),
            f"Rendu segment {seg_i+1}/10...",
        )

        audio_path = audio_paths[seg_i] if seg_i < len(audio_paths) else None
        if not audio_path or not os.path.exists(audio_path):
            audio_path = os.path.join(sdir, f"audio_{seg_i:02d}.wav")
            _write_silent_wav(audio_path)

        audio_dur = _wav_duration(audio_path)
        if audio_dur < 0.5:
            logger.warning("[%s] Segment %d audio too short (%.2fs) — re-generating",
                           session_id, seg_i, audio_dur)
            try:
                wav = _generate_gemini_tts(seg["text"], max_retries=3)
                with open(audio_path, "wb") as f:
                    f.write(wav)
                audio_dur = _wav_duration(audio_path)
            except Exception as e:
                logger.error("[%s] Re-gen failed: %s", session_id, e)
                _write_silent_wav(audio_path, duration=7.0)
                audio_dur = 7.0

        img_dur = max(audio_dur / 2.0, 1.0)

        baked = []
        for offset in range(2):
            img_idx  = seg_i * 2 + offset
            src      = image_paths[img_idx] if img_idx < len(image_paths) else image_paths[-1]
            baked_p  = os.path.join(sdir, f"sub_{img_idx:02d}.jpg")
            _bake_subtitle_to_file(src, seg["text"], baked_p)
            baked.append(baked_p)

        direction      = "fwd" if seg_i % 2 == 0 else "rev"
        vf0            = _ken_burns_vf(img_dur, direction)
        vf1            = _ken_burns_vf(img_dur, "rev" if direction == "fwd" else "fwd")
        seg_clip       = os.path.join(sdir, f"seg_{seg_i:02d}.mp4")
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
            "-map", "[vout]", "-map", "2:a",
            "-t", f"{audio_dur:.4f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1",
            seg_clip,
        ]
        try:
            _run_ff(cmd, timeout=180)
            logger.info("[%s] Segment %d/10 assembled (audio %.2fs)", session_id, seg_i + 1, audio_dur)
        except Exception as e:
            logger.error("[%s] Segment %d assembly failed: %s", session_id, seg_i + 1, e)
            raise

        segment_clips.append(seg_clip)

    concat_txt = os.path.join(sdir, "concat.txt")
    with open(concat_txt, "w") as f:
        for clip in segment_clips:
            f.write(f"file '{clip}'\n")

    out_path = os.path.join(sdir, "final_video.mp4")
    update_status(session_id, "assembling", 97, "Finalisation de la vidéo...")
    _run_ff([
        ff, "-y",
        "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ], timeout=300)

    logger.info("[%s] Final video ready: %s", session_id, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Main production runner
# ---------------------------------------------------------------------------

def run_production(session_id: str, character_image_path: str):
    try:
        session_data = load_session(session_id)
        if not session_data:
            logger.error("[%s] Session not found", session_id)
            return

        lock     = threading.Lock()
        counters = {"img": 0, "aud": 0}

        update_status(session_id, "generating", 5,
                      "Génération images et audio en parallèle...",
                      images_done=[], audio_done=[])

        img_result   = {"paths": None, "error": None}
        audio_result = {"paths": None, "error": None}

        def run_images():
            try:
                img_result["paths"] = generate_images(
                    session_id, character_image_path, session_data, counters, lock)
            except Exception as e:
                img_result["error"] = e
                logger.error("[%s] Image thread: %s", session_id, e, exc_info=True)

        def run_audio():
            try:
                audio_result["paths"] = generate_audio(
                    session_id, session_data, counters, lock)
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

        logger.info("[%s] Production complete", session_id)

    except Exception as e:
        logger.error("[%s] Production failed: %s", session_id, e, exc_info=True)
        update_status(session_id, "error", 0, "Une erreur est survenue.", str(e))
