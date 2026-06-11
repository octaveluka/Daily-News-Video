"""
V-CTRL — Video generation pipeline
  Phase 1 : fetch news / custom topic → 10-segment DOCUMENTARY script + 20 image prompts
  Phase 2 : 2 parallel threads:
              • Thread A — 20 images via external API (4 sections × 5 parallel)
              • Thread B — 10 audio segments via Gemini TTS
  Phase 3 : ffmpeg assembly — each audio covers EXACTLY 2 images, equal time per image

Format:  '16:9'  →  1280×720   (paysage)
         '9:16'  →  720×1280   (portrait / TikTok / Reels)

Audio validation: min 2 s, RMS silence check, up to 5 retries.
Ken Burns: 1.03× zoom (subtle).
"""

import io, os, json, uuid, wave, base64, array, shutil, zipfile, asyncio
import logging, subprocess, threading, time, re
from concurrent.futures import ThreadPoolExecutor, as_completed
import edge_tts

import requests
import psycopg2
import psycopg2.extras
from PIL import Image as PILImage, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_default_sessions_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "sessions",
)
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", _default_sessions_dir)
try:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    _t = os.path.join(SESSIONS_DIR, ".write_test")
    open(_t, "w").write("ok"); os.remove(_t)
except OSError:
    SESSIONS_DIR = "/tmp/v_ctrl_sessions"
    os.makedirs(SESSIONS_DIR, exist_ok=True)

TEXT_API_URL   = "https://delfaapiai.vercel.app/ai/copilot"
IMAGE_EDIT_URL = "https://gem-tw6a.onrender.com/edit"
TTS_VOICE = "fr-FR-HenriNeural"

FPS          = 24
TOTAL_IMGS   = 20
SECTION_SIZE = 5    # images per section
N_SECTIONS   = 4    # sections run in parallel

# Ken Burns zoom factor — keep subtle
_KB_FACTOR = 1.03

_status_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _dims(video_format: str) -> tuple[int, int]:
    """Return (width, height) for the given format string."""
    if video_format == "9:16":
        return 720, 1280
    return 1280, 720   # default 16:9


def _format_hint(video_format: str) -> str:
    if video_format == "9:16":
        return "vertical portrait format 9:16 for TikTok/Reels/Shorts"
    return "horizontal landscape format 16:9 for YouTube"


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

def _get_db_conn():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable not set")
    return psycopg2.connect(db_url)


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
    r = subprocess.run(args, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg rc={r.returncode}:\n{r.stderr.decode(errors='replace')[-2000:]}")
    return r


# ---------------------------------------------------------------------------
# Session helpers — PostgreSQL backed
# ---------------------------------------------------------------------------

def session_dir(session_id: str) -> str:
    p = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(p, exist_ok=True)
    return p


def load_session(session_id: str) -> dict | None:
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT data FROM sessions WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
                return dict(row["data"]) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error("[DB] load_session %s: %s", session_id, e)
        return None


def save_session(session_id: str, data: dict):
    conn = None
    try:
        conn = _get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sessions (session_id, topic, title, status, progress,
                                     current_step, error, video_path, created_at,
                                     last_heartbeat, data)
                VALUES (%(sid)s, %(topic)s, %(title)s, %(status)s, %(progress)s,
                        %(step)s, %(error)s, %(vpath)s,
                        COALESCE(%(cat)s::timestamptz, NOW()),
                        %(hb)s, %(data)s)
                ON CONFLICT (session_id) DO UPDATE SET
                    topic          = EXCLUDED.topic,
                    title          = EXCLUDED.title,
                    status         = EXCLUDED.status,
                    progress       = EXCLUDED.progress,
                    current_step   = EXCLUDED.current_step,
                    error          = EXCLUDED.error,
                    video_path     = EXCLUDED.video_path,
                    last_heartbeat = EXCLUDED.last_heartbeat,
                    data           = EXCLUDED.data
            """, {
                "sid":     session_id,
                "topic":   data.get("topic", ""),
                "title":   data.get("title", ""),
                "status":  data.get("status", "pending"),
                "progress": data.get("progress", 0),
                "step":    data.get("current_step", ""),
                "error":   data.get("error"),
                "vpath":   data.get("video_path"),
                "cat":     data.get("created_at"),
                "hb":      float(data.get("last_heartbeat") or 0),
                "data":    psycopg2.extras.Json(data),
            })
        conn.commit()
    except Exception as e:
        logger.error("[DB] save_session %s: %s", session_id, e, exc_info=True)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def list_sessions() -> list[dict]:
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT session_id, topic, title, status, progress,
                           current_step, error, video_path, created_at
                    FROM sessions ORDER BY created_at DESC
                """)
                rows = cur.fetchall()
                return [{
                    "session_id":   r["session_id"],
                    "topic":        r["topic"],
                    "title":        r["title"],
                    "status":       r["status"],
                    "progress":     r["progress"],
                    "current_step": r["current_step"],
                    "error":        r["error"],
                    "video_url":    f"/api/download/{r['session_id']}" if r["status"] == "done" else None,
                    "created_at":   r["created_at"].isoformat() if r["created_at"] else "",
                } for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.error("[DB] list_sessions: %s", e)
        return []


def update_status(session_id: str, status: str, progress: int,
                  current_step: str, error: str | None = None, **extra):
    with _status_lock:
        data = load_session(session_id) or {}
        data["status"]         = status
        data["progress"]       = progress
        data["current_step"]   = current_step
        data["error"]          = error
        data["last_heartbeat"] = time.time()
        data.update(extra)
        save_session(session_id, data)


# ---------------------------------------------------------------------------
# Auto-resume stalled sessions on server start
# ---------------------------------------------------------------------------

def auto_resume_stalled_sessions():
    try:
        conn = _get_db_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT session_id, data FROM sessions
                    WHERE status IN ('generating', 'assembling', 'pending')
                """)
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[auto-resume] DB error: %s", e)
        return

    now = time.time()
    for row in rows:
        try:
            sid  = row["session_id"]
            data = dict(row["data"]) if row["data"] else {}
            hb   = data.get("last_heartbeat", 0)
            if now - hb < 300:
                continue
            char_img = data.get("character_image_path")
            if not char_img or not os.path.exists(char_img):
                continue
            logger.info("[auto-resume] Restarting stalled session %s", sid)
            t = threading.Thread(target=run_production, args=(sid, char_img), daemon=True)
            t.start()
        except Exception as e:
            logger.warning("[auto-resume] Error checking session %s: %s", row.get("session_id"), e)


def find_character_image(session_id: str) -> str | None:
    data = load_session(session_id)
    if not data:
        return None
    p = data.get("character_image_path")
    return p if p and os.path.exists(p) else None


# ---------------------------------------------------------------------------
# Phase 1 — Script generation
# ---------------------------------------------------------------------------

def _call_text_api(message: str, timeout: int = 90) -> str:
    resp = requests.get(TEXT_API_URL,
                        params={"message": message, "model": "default"},
                        timeout=timeout)
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

_NARRATIVE_PROMPT_TEMPLATE = """Tu es le narrateur d'un documentaire journalistique (style Al Jazeera / Vice News / France 24).
Tu écris en français un récit en 10 segments pour une vidéo virale sur : "{topic}"

═══ RÈGLE ABSOLUE — STYLE DOCUMENTAIRE ═══
• Chaque segment = FAITS, ÉVÉNEMENTS, CONTEXTE HISTORIQUE, CITATIONS de personnes impliquées
• INTERDIT : décrire les gestes, mouvements ou sensations physiques d'un personnage fictif
• INTERDIT : "elle ouvre les yeux", "il respire profondément", "son cœur bat"
• OBLIGATOIRE : "Un mouvement prend de l'ampleur", citations directes, enjeux géopolitiques/sociaux
• Chaque segment = 30 à 45 mots, assez long pour une narration audio fluide

═══ EXEMPLE PARFAIT ═══
"Depuis quelque temps, un mouvement inédit prend de l'ampleur. À la suite du vote d'une loi historique accordant la nationalité béninoise aux afro-descendants, des milliers de personnes venues d'Haïti et des Caraïbes font le voyage pour renouer avec leurs racines."

═══ RÈGLE PROMPTS IMAGE ═══
Chaque prompt DOIT :
1. Mentionner "the person from the reference photo"
2. Décrire exactement le LIEU et le DÉCOR correspondant aux paroles du segment
3. Être très différent des autres (type de plan, lumière, décor différents)
Types : WIDE SHOT | CLOSE-UP | EXTREME CLOSE-UP | AERIAL VIEW | LOW ANGLE | SILHOUETTE | DUTCH ANGLE | OVER-THE-SHOULDER | MEDIUM SHOT | TWO-SHOT

FORMAT JSON STRICT (sans markdown, sans commentaires) :
{{"title":"...","description":"...","hashtags":["#...","#...","#...","#...","#..."],"segments":[{{"index":0,"text":"...30-45 mots...","image_prompts":["TYPE - the person from the reference photo [action] in [décor précis], [lumière], photorealistic, 8k, cinematic","TYPE - the person from the reference photo [autre action] in [autre décor], [lumière], photorealistic, 8k, cinematic"]}},{{"index":1,...}},{{"index":2,...}},{{"index":3,...}},{{"index":4,...}},{{"index":5,...}},{{"index":6,...}},{{"index":7,...}},{{"index":8,...}},{{"index":9,"text":"...chute émotionnelle ou espoir...","image_prompts":["...","..."]}}]}}"""

def _fallback_script(topic: str) -> dict:
    t = topic[:60]
    return {
        "title": f"Reportage : {t}", "description": f"Un documentaire sur {t}.",
        "hashtags": ["#reportage", "#documentaire", "#info", "#actualité", "#viral"],
        "segments": [{"index": i, "text": f"Au cœur de cette réalité mondiale, des milliers de personnes témoignent d'une transformation profonde qui touche directement leur quotidien et l'avenir de leurs communautés : {topic[:30]}.",
            "image_prompts": [
                f"WIDE SHOT - the person from the reference photo in a meaningful scene about {t}, golden hour, photorealistic, 8k, cinematic",
                f"CLOSE-UP PORTRAIT - the person from the reference photo facing camera, dramatic context of {t}, candlelight, photorealistic, 8k, cinematic"
            ]} for i in range(10)],
    }

def fetch_news_and_generate_script(custom_topic: str | None = None) -> dict:
    session_id = str(uuid.uuid4())

    if custom_topic and custom_topic.strip():
        topic = custom_topic.strip()
    else:
        try:
            raw = _call_text_api(
                "Donne-moi en une phrase courte (max 15 mots) le fait d'actualité le plus marquant "
                "et humain d'aujourd'hui, en français. Uniquement la phrase, sans ponctuation finale.", 30)
            topic = raw.strip().strip('"\'').rstrip(".")
        except Exception as e:
            logger.warning("News fetch failed: %s", e)
            topic = "Le réchauffement climatique force des communautés entières à quitter leurs terres ancestrales"

    for attempt in range(3):
        try:
            raw  = _call_text_api(_NARRATIVE_PROMPT_TEMPLATE.format(topic=topic), 120)
            data = _extract_json(raw)
            if data.get("segments") and len(data["segments"]) >= 5:
                break
        except Exception as e:
            logger.warning("Script attempt %d: %s", attempt + 1, e)
            data = {}
    else:
        data = _fallback_script(topic)

    segments = data.get("segments", [])
    while len(segments) < 10:
        i = len(segments)
        segments.append({"index": i,
            "text": f"Ce phénomène mondial révèle les enjeux profonds d'une réalité que peu osent aborder et qui touche directement des millions de personnes à travers le monde : {topic[:40]}.",
            "image_prompts": [
                f"MEDIUM SHOT - the person from the reference photo in a key scene about {topic[:35]}, warm light, photorealistic, 8k, cinematic",
                f"DUTCH ANGLE - the person from the reference photo in dramatic context of {topic[:35]}, blue hour, photorealistic, 8k, cinematic"
            ]})
    segments = segments[:10]
    for i, seg in enumerate(segments):
        seg["index"] = i
        prompts = seg.get("image_prompts", [])
        while len(prompts) < 2:
            prompts.append(f"WIDE SHOT - the person from the reference photo related to {topic[:35]}, photorealistic, 8k, cinematic")
        seg["image_prompts"] = prompts[:2]

    import datetime
    result = {
        "session_id":    session_id,
        "created_at":    datetime.datetime.utcnow().isoformat(),
        "topic":         topic,
        "title":         data.get("title", f"Reportage : {topic}")[:80],
        "description":   data.get("description", "Un documentaire sur l'actualité du monde.")[:200],
        "hashtags":      data.get("hashtags", ["#reportage","#documentaire","#info","#actualité","#viral"])[:5],
        "segments":      segments,
        "status":        "pending",
        "progress":      0,
        "current_step":  "Script généré — en attente de l'image du personnage.",
        "images_done":   [],
        "audio_done":    [],
        "last_heartbeat": 0,
        "error":         None,
        "video_format":  "16:9",   # default, overwritten by produce endpoint
    }
    save_session(session_id, result)
    return result


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _encode_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# ---------------------------------------------------------------------------
# Phase 2a — External API: ALL 20 images
# 4 parallel sections of 5 — original character image as reference always
# ---------------------------------------------------------------------------

def _generate_one_image(session_id: str, i: int, prompt: str,
                         character_image_path: str, video_format: str,
                         counters: dict, lock: threading.Lock,
                         image_paths: list) -> None:
    img_path = os.path.join(session_dir(session_id), f"image_{i:02d}.jpg")
    fmt_hint = _format_hint(video_format)
    full_prompt = f"{prompt}, {fmt_hint}"
    success = False

    for attempt in range(3):
        try:
            resp = requests.post(IMAGE_EDIT_URL,
                json={"prompt": full_prompt, "image": _encode_b64(character_image_path)},
                timeout=120)
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
            success = True
            logger.info("[%s] Image %02d OK (attempt %d)", session_id, i, attempt + 1)
            break
        except Exception as e:
            logger.warning("[%s] Image %02d attempt %d: %s", session_id, i, attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    if not success:
        logger.error("[%s] Image %02d failed — copying character image", session_id, i)
        shutil.copy(character_image_path, img_path)

    with lock:
        image_paths[i] = img_path
        counters["img"] = counters.get("img", 0) + 1
        total_done = counters["img"]

    with _status_lock:
        data = load_session(session_id) or {}
        done = data.get("images_done", [])
        if i not in done:
            done.append(i)
        data["images_done"]    = done
        data["last_heartbeat"] = time.time()
        data["status"]         = "generating"
        data["progress"]       = 5 + int((total_done / TOTAL_IMGS) * 35)
        data["current_step"]   = f"Images {total_done}/{TOTAL_IMGS} • Audio {counters.get('aud', 0)}/10"
        save_session(session_id, data)


def generate_all_images(session_id: str, character_image_path: str,
                         all_prompts: list[str], video_format: str,
                         counters: dict, lock: threading.Lock,
                         image_paths: list) -> None:
    """4 sections of 5 images each, all running simultaneously."""
    sections = [
        list(range(s * SECTION_SIZE, s * SECTION_SIZE + SECTION_SIZE))
        for s in range(N_SECTIONS)
    ]

    def run_section(indices: list[int]) -> None:
        with ThreadPoolExecutor(max_workers=SECTION_SIZE) as pool:
            futures = [
                pool.submit(_generate_one_image, session_id, idx, all_prompts[idx],
                            character_image_path, video_format, counters, lock, image_paths)
                for idx in indices
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logger.error("[%s] Section image error: %s", session_id, e)

    threads = [threading.Thread(target=run_section, args=(sec,), daemon=True) for sec in sections]
    for t in threads: t.start()
    for t in threads: t.join()


# ---------------------------------------------------------------------------
# Phase 2b — Audio: edge-tts (fr-FR-HenriNeural)
# ---------------------------------------------------------------------------

def _wav_duration(wav_bytes_or_path) -> float:
    try:
        if isinstance(wav_bytes_or_path, str):
            with wave.open(wav_bytes_or_path, "r") as wf:
                return wf.getnframes() / (wf.getframerate() or 1)
        else:
            with wave.open(io.BytesIO(wav_bytes_or_path), "r") as wf:
                return wf.getnframes() / (wf.getframerate() or 1)
    except Exception:
        return 0.0

def _write_silent_wav(path: str, duration: float = 7.5, rate: int = 24000):
    n = int(rate * duration)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(array.array("h", [0] * n).tobytes())

MIN_AUDIO_DURATION = 2.0  # seconds — safety floor used during assembly


async def _edge_tts_to_mp3(text: str, mp3_path: str) -> None:
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(mp3_path)


def _generate_one_audio(session_id: str, i: int, seg: dict,
                         counters: dict, lock: threading.Lock) -> str:
    audio_path = os.path.join(session_dir(session_id), f"audio_{i:02d}.wav")
    text = seg.get("text", "").strip()

    if not text:
        logger.warning("[%s] Audio %d: empty text → silent placeholder", session_id, i)
        _write_silent_wav(audio_path, 7.5)
    else:
        mp3_path = audio_path.replace(".wav", ".mp3")
        try:
            asyncio.run(_edge_tts_to_mp3(text, mp3_path))
            _run_ff([_ffmpeg(), "-y", "-i", mp3_path,
                     "-ar", "24000", "-ac", "1", audio_path], timeout=60)
            try:
                os.remove(mp3_path)
            except OSError:
                pass
            logger.info("[%s] Audio %02d ✓", session_id, i)
        except Exception as e:
            logger.error("[%s] Audio %02d failed: %s → silent placeholder", session_id, i, e)
            _write_silent_wav(audio_path, 7.5)

    with lock:
        counters["aud"] = counters.get("aud", 0) + 1
    with _status_lock:
        data = load_session(session_id) or {}
        done = data.get("audio_done", [])
        if i not in done:
            done.append(i)
        data["audio_done"]     = done
        data["last_heartbeat"] = time.time()
        save_session(session_id, data)
    return audio_path


def generate_audio(session_id: str, session_data: dict,
                   counters: dict, lock: threading.Lock) -> list[str]:
    segments    = session_data["segments"]
    audio_paths = [None] * len(segments)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_generate_one_audio, session_id, i, seg, counters, lock): i
                   for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            i = futures[future]
            audio_paths[i] = future.result()
    return audio_paths


# ---------------------------------------------------------------------------
# Audio repair — scan all audio files and regenerate any without real speech
# ---------------------------------------------------------------------------

def repair_audio_files(session_id: str) -> dict:
    """
    Regenerate any missing audio files for a session via edge-tts.
    Returns {"repaired": [indices], "ok": [indices], "failed": [indices]}
    """
    session_data = load_session(session_id)
    if not session_data:
        return {"error": "session not found"}

    segments = session_data.get("segments", [])
    sdir     = session_dir(session_id)

    bad_indices = []
    ok_indices  = []

    for i in range(10):
        ap = os.path.join(sdir, f"audio_{i:02d}.wav")
        if os.path.exists(ap) and _wav_duration(ap) >= MIN_AUDIO_DURATION:
            ok_indices.append(i)
        else:
            bad_indices.append(i)

    if not bad_indices:
        logger.info("[%s] Audio repair: all %d files OK", session_id, len(ok_indices))
        return {"repaired": [], "ok": ok_indices, "failed": []}

    logger.info("[%s] Audio repair: %d files to regenerate: %s",
                session_id, len(bad_indices), bad_indices)

    repaired = []
    failed   = []

    def _repair_one(i: int):
        ap      = os.path.join(sdir, f"audio_{i:02d}.wav")
        mp3_path = ap.replace(".wav", ".mp3")
        text    = segments[i]["text"].strip() if i < len(segments) else ""
        if not text:
            _write_silent_wav(ap, 7.5)
            failed.append(i)
            return
        try:
            asyncio.run(_edge_tts_to_mp3(text, mp3_path))
            _run_ff([_ffmpeg(), "-y", "-i", mp3_path,
                     "-ar", "24000", "-ac", "1", ap], timeout=60)
            try:
                os.remove(mp3_path)
            except OSError:
                pass
            repaired.append(i)
            logger.info("[%s] Audio %02d repaired OK", session_id, i)
        except Exception as e:
            logger.error("[%s] Audio %02d repair failed: %s", session_id, i, e)
            failed.append(i)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(_repair_one, bad_indices))

    return {"repaired": sorted(repaired), "ok": sorted(ok_indices), "failed": sorted(failed)}


# ---------------------------------------------------------------------------
# Phase 3 — Video assembly
# Each audio covers EXACTLY 2 images; each image gets dur/2 time.
# Ken Burns zoom: 1.03× (subtle).
# ---------------------------------------------------------------------------

def _find_font(size: int = 34):
    for c in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(c):
            try: return ImageFont.truetype(c, size)
            except Exception: pass
    return ImageFont.load_default()


def _prepare_image(src: str, dst: str, w: int, h: int):
    """Resize image to (w×h) — no subtitle overlay."""
    try:
        img = PILImage.open(src).convert("RGB")
    except Exception:
        img = PILImage.new("RGB", (w, h), (20, 20, 20))
    img = img.resize((w, h), PILImage.LANCZOS)
    img.save(dst, "JPEG", quality=90)


def _kb_vf(w: int, h: int, dur: float, direction: str = "fwd") -> str:
    """Ken Burns ffmpeg filter string. Zoom = KB_FACTOR (1.03)."""
    sw = int(w * _KB_FACTOR)
    sh = int(h * _KB_FACTOR)
    dx = sw - w
    dy = sh - h
    if direction == "fwd":
        xe = f"'trunc({dx:.1f}*t/{dur:.4f})'"
        ye = f"'trunc({dy:.1f}*t/{dur:.4f})'"
    else:
        xe = f"'trunc({dx:.1f}*(1-t/{dur:.4f}))'"
        ye = f"'trunc({dy:.1f}*(1-t/{dur:.4f}))'"
    return (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}:{xe}:{ye},setsar=1")


def assemble_video(session_id: str, image_paths: list[str],
                   audio_paths: list[str], session_data: dict) -> str:
    """
    Assemble 10 segments. Each segment:
      - audio  = audio_paths[si]
      - image0 = image_paths[si*2]     → dur/2 seconds
      - image1 = image_paths[si*2+1]   → dur/2 seconds
    Total: 10 × (2 images) = 20 images, exactly matching the 10 audio tracks.

    Audio is trimmed inside filter_complex via atrim to guarantee sync.
    """
    video_format = session_data.get("video_format", "16:9")
    vw, vh = _dims(video_format)

    ff      = _ffmpeg()
    sdir    = session_dir(session_id)
    segments = session_data["segments"]

    update_status(session_id, "assembling", 72, "Montage des segments vidéo…")
    clips = []

    for si in range(10):
        update_status(session_id, "assembling", 72 + int((si / 10) * 24),
                      f"Rendu segment {si+1}/10…", last_heartbeat=time.time())

        # ── Resolve audio path ──
        ap = (audio_paths[si]
              if si < len(audio_paths) and audio_paths[si]
              else os.path.join(sdir, f"audio_{si:02d}.wav"))
        if not os.path.exists(ap):
            ap = os.path.join(sdir, f"audio_{si:02d}.wav")
            _write_silent_wav(ap, 7.5)

        dur = _wav_duration(ap)
        if dur < MIN_AUDIO_DURATION:
            logger.warning("[%s] Segment %d: audio still too short (%.2fs) — silent fallback",
                           session_id, si, dur)
            _write_silent_wav(ap, 7.5)
            dur = 7.5

        # ── Each image gets exactly half the audio duration ──
        img_dur = dur / 2.0

        # ── Prepare the 2 images ──
        baked = []
        for off in range(2):
            idx = si * 2 + off
            idx = max(0, min(idx, TOTAL_IMGS - 1))
            src = (image_paths[idx]
                   if idx < len(image_paths) and image_paths[idx]
                   else image_paths[0])
            dst = os.path.join(sdir, f"frm_{si:02d}_{off}.jpg")
            _prepare_image(src, dst, vw, vh)
            baked.append(dst)

        # ── Ken Burns — alternate direction per segment ──
        d   = "fwd" if si % 2 == 0 else "rev"
        vf0 = _kb_vf(vw, vh, img_dur, d)
        vf1 = _kb_vf(vw, vh, img_dur, "rev" if d == "fwd" else "fwd")

        clip = os.path.join(sdir, f"seg_{si:02d}.mp4")

        # Audio is trimmed INSIDE filter_complex (atrim) — avoids the ffmpeg
        # quirk where output-level -t can silently drop the audio stream when
        # combined with -filter_complex + -map.
        filt = (
            f"[0:v]{vf0}[v0];"
            f"[1:v]{vf1}[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[vout];"
            f"[2:a]atrim=0:{dur:.4f},asetpts=PTS-STARTPTS[aout]"
        )

        _run_ff([ff, "-y",
            "-loop", "1", "-t", f"{img_dur:.4f}", "-i", baked[0],
            "-loop", "1", "-t", f"{img_dur:.4f}", "-i", baked[1],
            "-i", ap,
            "-filter_complex", filt,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "1",
            clip
        ], timeout=180)
        clips.append(clip)
        logger.info("[%s] Segment %d/10 OK — audio=%.2fs each_img=%.2fs",
                    session_id, si + 1, dur, img_dur)

    concat_txt = os.path.join(sdir, "concat.txt")
    with open(concat_txt, "w") as f:
        for c in clips: f.write(f"file '{c}'\n")

    out = os.path.join(sdir, "final_video.mp4")
    update_status(session_id, "assembling", 97, "Finalisation…")
    _run_ff([ff, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k", out], timeout=300)
    logger.info("[%s] Final video → %s", session_id, out)
    return out


# ---------------------------------------------------------------------------
# Bundle ZIP — everything produced during the session
# ---------------------------------------------------------------------------

def create_bundle_zip(session_id: str) -> str:
    """
    Create a ZIP archive containing:
      - original character image
      - all generated images (image_00..19.jpg)
      - all audio files (audio_00..09.wav)
      - final video (final_video.mp4)
      - session metadata (session_data.json)
    Returns the path to the ZIP file.
    """
    sdir     = session_dir(session_id)
    zip_path = os.path.join(sdir, f"bundle_{session_id[:8]}.zip")

    data = load_session(session_id) or {}

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Session metadata
        zf.writestr("session_data.json", json.dumps(data, ensure_ascii=False, indent=2))

        # Original character image
        char_img = data.get("character_image_path")
        if char_img and os.path.exists(char_img):
            zf.write(char_img, f"originals/{os.path.basename(char_img)}")

        # Generated images
        for i in range(TOTAL_IMGS):
            p = os.path.join(sdir, f"image_{i:02d}.jpg")
            if os.path.exists(p):
                zf.write(p, f"images/image_{i:02d}.jpg")

        # Audio files
        for i in range(10):
            p = os.path.join(sdir, f"audio_{i:02d}.wav")
            if os.path.exists(p):
                zf.write(p, f"audio/audio_{i:02d}.wav")

        # Final video
        video_path = data.get("video_path")
        if video_path and os.path.exists(video_path):
            zf.write(video_path, "final_video.mp4")

    logger.info("[%s] Bundle ZIP created → %s", session_id, zip_path)
    return zip_path


# ---------------------------------------------------------------------------
# Main production runner
# ---------------------------------------------------------------------------

def run_production(session_id: str, character_image_path: str,
                   video_format: str = "16:9"):
    try:
        session_data = load_session(session_id)
        if not session_data:
            logger.error("[%s] Session not found", session_id)
            return

        with _status_lock:
            data = load_session(session_id) or {}
            data["character_image_path"] = character_image_path
            data["video_format"]         = video_format
            save_session(session_id, data)

        all_prompts = [p for seg in session_data["segments"] for p in seg["image_prompts"]]
        while len(all_prompts) < TOTAL_IMGS:
            all_prompts.append("WIDE SHOT - the person from the reference photo, photorealistic, 8k, cinematic")
        all_prompts = all_prompts[:TOTAL_IMGS]

        lock        = threading.Lock()
        counters    = {"img": 0, "aud": 0}
        image_paths = [None] * TOTAL_IMGS

        update_status(session_id, "generating", 5,
                      f"2 threads démarrés : images ({video_format}) • audio TTS",
                      images_done=[], audio_done=[])

        results = {"audio": None, "err_img": None, "err_aud": None}

        def run_img():
            try:
                generate_all_images(session_id, character_image_path, all_prompts,
                                    video_format, counters, lock, image_paths)
            except Exception as e:
                results["err_img"] = e
                logger.error("[%s] Images thread: %s", session_id, e, exc_info=True)

        def run_aud():
            try:
                results["audio"] = generate_audio(session_id, session_data, counters, lock)
            except Exception as e:
                results["err_aud"] = e
                logger.error("[%s] Audio thread: %s", session_id, e, exc_info=True)

        t1 = threading.Thread(target=run_img, daemon=True)
        t2 = threading.Thread(target=run_aud, daemon=True)
        t1.start(); t2.start()
        t1.join();  t2.join()

        audio_paths = results["audio"] or []

        # Fill any None image slots
        last_valid = character_image_path
        for i in range(TOTAL_IMGS):
            if image_paths[i] and os.path.exists(image_paths[i]):
                last_valid = image_paths[i]
            else:
                logger.warning("[%s] Image %d missing — using fallback", session_id, i)
                image_paths[i] = last_valid

        logger.info("[%s] All threads done — %d images, %d audio",
                    session_id, TOTAL_IMGS, len(audio_paths))

        fresh_data  = load_session(session_id) or session_data
        video_path  = assemble_video(session_id, image_paths, audio_paths, fresh_data)

        with _status_lock:
            data = load_session(session_id) or session_data
            data.update({
                "status":           "done",
                "progress":         100,
                "current_step":     "Vidéo prête !",
                "video_path":       video_path,
                "video_url":        f"/api/download/{session_id}",
                "duration_seconds": 75.0,
                "error":            None,
                "last_heartbeat":   time.time(),
            })
            save_session(session_id, data)

        logger.info("[%s] Production complete", session_id)

    except Exception as e:
        logger.error("[%s] Production failed: %s", session_id, e, exc_info=True)
        update_status(session_id, "error", 0, "Une erreur est survenue.", str(e))
