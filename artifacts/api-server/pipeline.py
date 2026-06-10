"""
Video generation pipeline.
Handles: news fetch -> script -> images -> audio -> MoviePy assembly
"""

import os
import json
import uuid
import base64
import time
import tempfile
import logging
import threading
import requests

logger = logging.getLogger(__name__)

SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "temp_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

TEXT_API_URL = "https://delfaapiai.vercel.app/ai/copilot"
IMAGE_EDIT_URL = "https://gem-tw6a.onrender.com/edit"
IMAGE_GENERATE_URL = "https://gem-tw6a.onrender.com/generate"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def session_dir(session_id: str) -> str:
    path = os.path.join(SESSIONS_DIR, session_id)
    os.makedirs(path, exist_ok=True)
    return path


def load_session(session_id: str) -> dict | None:
    path = os.path.join(session_dir(session_id), "data.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_session(session_id: str, data: dict):
    path = os.path.join(session_dir(session_id), "data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_status(session_id: str, status: str, progress: int, current_step: str, error: str | None = None):
    data = load_session(session_id) or {}
    data["status"] = status
    data["progress"] = progress
    data["current_step"] = current_step
    data["error"] = error
    save_session(session_id, data)


# ---------------------------------------------------------------------------
# Phase 1: Narrative brain — fetch news + generate script
# ---------------------------------------------------------------------------

def _call_text_api(message: str, timeout: int = 60) -> str:
    """Call the narrative AI API."""
    resp = requests.get(
        TEXT_API_URL,
        params={"message": message, "model": "default"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    # API returns {"answer": "...", "operator": "...", ...}
    if isinstance(data, dict):
        for key in ("answer", "response", "text", "content", "result"):
            val = data.get(key)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
        # Last resort: concatenate all string values
        return " ".join(v for v in data.values() if isinstance(v, str))
    return str(data).strip()


def fetch_news_and_generate_script() -> dict:
    """
    Step 1: Fetch today's news topic.
    Step 2: Turn it into a 10-segment narrative script + 20 image prompts + metadata.
    Returns a dict compatible with SessionInit schema.
    """
    session_id = str(uuid.uuid4())

    # --- Fetch news topic ---
    try:
        topic_raw = _call_text_api(
            "Donne-moi l'actualité la plus importante d'aujourd'hui en une seule phrase courte, "
            "en français. Réponds UNIQUEMENT avec la phrase, sans explication."
        )
        topic = topic_raw.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning("News API failed, using fallback: %s", e)
        topic = "L'intelligence artificielle transforme le monde du travail"

    # --- Generate full script + prompts + metadata ---
    script_prompt = f"""Tu es un scénariste. Sujet: "{topic}"
Réponds UNIQUEMENT avec un JSON valide, sans markdown, sans explication.
Format exact:
{{"title":"Titre court","description":"Description courte","hashtags":["#tag1","#tag2","#tag3","#tag4","#tag5"],"segments":[{{"index":0,"text":"Texte narratif segment 1 en français (15 mots max)","image_prompts":["cinematic scene of person related to {topic[:30]}, photorealistic","close-up dramatic portrait related to {topic[:30]}, studio lighting"]}},{{"index":1,"text":"Texte narratif segment 2","image_prompts":["wide shot scene about {topic[:30]}, cinematic","person reacting to news, dramatic lighting"]}},{{"index":2,"text":"Texte narratif segment 3","image_prompts":["scene 5","scene 6"]}},{{"index":3,"text":"Texte narratif segment 4","image_prompts":["scene 7","scene 8"]}},{{"index":4,"text":"Texte narratif segment 5","image_prompts":["scene 9","scene 10"]}},{{"index":5,"text":"Texte narratif segment 6","image_prompts":["scene 11","scene 12"]}},{{"index":6,"text":"Texte narratif segment 7","image_prompts":["scene 13","scene 14"]}},{{"index":7,"text":"Texte narratif segment 8","image_prompts":["scene 15","scene 16"]}},{{"index":8,"text":"Texte narratif segment 9","image_prompts":["scene 17","scene 18"]}},{{"index":9,"text":"Texte narratif segment 10","image_prompts":["scene 19","scene 20"]}}]}}
Remplace tous les champs par du contenu réel sur le sujet. JSON uniquement."""

    try:
        script_raw = _call_text_api(script_prompt, timeout=90)
        script_data = _extract_json(script_raw)
    except Exception as e:
        logger.warning("Script generation failed, using fallback: %s", e)
        script_data = _fallback_script(topic)

    # Validate structure
    segments = script_data.get("segments", [])
    if len(segments) < 10:
        segments = _pad_segments(segments, topic)
    segments = segments[:10]
    for i, seg in enumerate(segments):
        seg["index"] = i
        if len(seg.get("image_prompts", [])) < 2:
            seg["image_prompts"] = [
                f"A person in a scene related to {topic}, photorealistic, cinematic lighting, scene {i*2+1}",
                f"A person in a scene related to {topic}, photorealistic, cinematic lighting, scene {i*2+2}",
            ]
        seg["image_prompts"] = seg["image_prompts"][:2]

    result = {
        "session_id": session_id,
        "topic": topic,
        "title": script_data.get("title", f"Actualité: {topic}")[:80],
        "description": script_data.get("description", "Une vidéo sur l'actualité du jour.")[:150],
        "hashtags": script_data.get("hashtags", ["#actu", "#news", "#viral", "#info", "#tendance"])[:5],
        "segments": segments,
        "status": "pending",
        "progress": 0,
        "current_step": "Script généré. En attente de l'image du personnage.",
        "error": None,
    }

    save_session(session_id, result)
    return result


def _extract_json(text: str) -> dict:
    """Robustly extract a JSON object from a potentially messy response string."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except Exception:
                continue
    # Try direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try to find the outermost {...}
    import re
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No valid JSON found in response: {text[:200]}")


def _fallback_script(topic: str) -> dict:
    return {
        "title": f"L'actualité du jour : {topic[:50]}",
        "description": "Découvrez l'essentiel de l'actualité en 75 secondes.",
        "hashtags": ["#actu", "#news", "#viral", "#info", "#tendance"],
        "segments": [
            {
                "index": i,
                "text": f"Segment {i+1} sur : {topic}",
                "image_prompts": [
                    f"A reporter presenting news about {topic}, professional studio, cinematic, scene {i*2+1}",
                    f"Close up of person reacting to news about {topic}, dramatic lighting, scene {i*2+2}",
                ],
            }
            for i in range(10)
        ],
    }


def _pad_segments(segments: list, topic: str) -> list:
    while len(segments) < 10:
        i = len(segments)
        segments.append({
            "index": i,
            "text": f"Un aspect crucial de cette actualité : {topic}",
            "image_prompts": [
                f"A person in a cinematic scene about {topic}, photorealistic, scene {i*2+1}",
                f"A person gesturing dramatically about {topic}, studio lighting, scene {i*2+2}",
            ],
        })
    return segments


# ---------------------------------------------------------------------------
# Phase 2: Production — images → audio → video
# ---------------------------------------------------------------------------

def _encode_image_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def generate_images(session_id: str, character_image_path: str, session_data: dict) -> list[str]:
    """Generate 20 images using the edit API with chained context."""
    segments = session_data["segments"]
    all_prompts = []
    for seg in segments:
        all_prompts.extend(seg["image_prompts"])

    image_paths = []
    prev_image_path = character_image_path

    for i, prompt in enumerate(all_prompts):
        update_status(
            session_id,
            "generating_images",
            5 + int((i / 20) * 40),
            f"Génération de l'image {i+1}/20...",
        )

        img_path = os.path.join(session_dir(session_id), f"image_{i:02d}.jpg")

        try:
            prev_b64 = _encode_image_base64(prev_image_path)
            resp = requests.post(
                IMAGE_EDIT_URL,
                json={"prompt": prompt, "image": prev_b64},
                timeout=120,
            )
            resp.raise_for_status()

            # Response may be raw image bytes or JSON with base64
            content_type = resp.headers.get("content-type", "")
            if "image" in content_type:
                with open(img_path, "wb") as f:
                    f.write(resp.content)
            else:
                data = resp.json()
                img_b64 = data.get("image") or data.get("data") or data.get("result", "")
                if img_b64.startswith("data:"):
                    img_b64 = img_b64.split(",", 1)[1]
                with open(img_path, "wb") as f:
                    f.write(base64.b64decode(img_b64))

            prev_image_path = img_path
            logger.info("Image %d generated: %s", i, img_path)

        except Exception as e:
            logger.error("Image %d generation failed: %s", i, e)
            # Use a fallback: copy previous image
            import shutil
            shutil.copy(prev_image_path, img_path)

        image_paths.append(img_path)

    return image_paths


def generate_audio(session_id: str, session_data: dict) -> list[str]:
    """Generate 10 audio files via Gemini TTS. Each covers 2 image segments."""
    segments = session_data["segments"]
    audio_paths = []

    for i, seg in enumerate(segments):
        update_status(
            session_id,
            "generating_audio",
            45 + int((i / 10) * 25),
            f"Synthèse vocale {i+1}/10...",
        )

        audio_path = os.path.join(session_dir(session_id), f"audio_{i:02d}.mp3")

        try:
            audio_data = _generate_gemini_tts(seg["text"])
            with open(audio_path, "wb") as f:
                f.write(audio_data)
            logger.info("Audio %d generated: %s", i, audio_path)
        except Exception as e:
            logger.error("Audio %d generation failed: %s", i, e)
            # Create a silent audio file as fallback (7.5s of silence)
            _create_silent_audio(audio_path, duration=7.5)

        audio_paths.append(audio_path)

    return audio_paths


def _generate_gemini_tts(text: str) -> bytes:
    """Call Gemini API for text-to-speech. Returns raw audio bytes (MP3/WAV)."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": "Kore"}
                }
            },
        },
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Extract audio from response
    audio_b64 = (
        data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    )
    return base64.b64decode(audio_b64)


def _create_silent_audio(path: str, duration: float = 7.5):
    """Create a silent MP3 using wave module as fallback."""
    try:
        import wave
        import struct
        import array

        # Write a minimal silent WAV file then convert
        wav_path = path.replace(".mp3", ".wav")
        sample_rate = 22050
        num_frames = int(sample_rate * duration)
        num_channels = 1

        with wave.open(wav_path, "w") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            data = array.array("h", [0] * num_frames)
            wf.writeframes(data.tobytes())

        # Copy as-is (moviepy can handle wav)
        import shutil
        shutil.copy(wav_path, path.replace(".mp3", ".wav"))
        # Rename for consistency
        os.rename(wav_path, path)
    except Exception as e:
        logger.error("Silent audio creation failed: %s", e)
        # Write empty file as last resort
        with open(path, "wb") as f:
            f.write(b"")


def assemble_video(session_id: str, image_paths: list[str], audio_paths: list[str], session_data: dict) -> str:
    """
    Assemble the final video using MoviePy.
    - 20 images paired with 10 audio files (2 images per audio)
    - Ken Burns effect (zoom) on images
    - Subtitles burned in
    - Total ~75 seconds
    """
    update_status(session_id, "assembling", 70, "Assemblage vidéo en cours...")

    try:
        from moviepy.editor import (
            ImageClip,
            AudioFileClip,
            concatenate_videoclips,
            CompositeVideoClip,
            TextClip,
        )
        import numpy as np
        from PIL import Image

        VIDEO_SIZE = (1280, 720)
        fps = 24

        video_clips = []

        for audio_idx, audio_path in enumerate(audio_paths):
            update_status(
                session_id,
                "assembling",
                70 + int((audio_idx / 10) * 20),
                f"Assemblage segment {audio_idx+1}/10...",
            )

            # Load audio and get duration
            try:
                audio_clip = AudioFileClip(audio_path)
                audio_duration = audio_clip.duration
            except Exception:
                audio_duration = 7.5
                audio_clip = None

            img_duration = audio_duration / 2.0  # each image gets half the audio duration

            for img_offset in range(2):
                img_idx = audio_idx * 2 + img_offset
                if img_idx >= len(image_paths):
                    break

                img_path = image_paths[img_idx]
                try:
                    pil_img = Image.open(img_path).convert("RGB")
                    pil_img = pil_img.resize(VIDEO_SIZE, Image.LANCZOS)

                    # Ken Burns effect: gentle zoom from 1.0 to 1.05
                    def make_ken_burns_frame(t, pil_img=pil_img, duration=img_duration):
                        zoom = 1.0 + 0.05 * (t / max(duration, 0.001))
                        arr = np.array(pil_img)
                        h, w = arr.shape[:2]
                        new_w = int(w / zoom)
                        new_h = int(h / zoom)
                        x_start = (w - new_w) // 2
                        y_start = (h - new_h) // 2
                        cropped = arr[y_start:y_start+new_h, x_start:x_start+new_w]
                        from PIL import Image as PILImage
                        resized = np.array(PILImage.fromarray(cropped).resize((w, h), PILImage.LANCZOS))
                        return resized

                    img_clip = ImageClip(np.array(pil_img), duration=img_duration)
                    img_clip = img_clip.fl(lambda gf, t: make_ken_burns_frame(t), apply_to="mask")

                except Exception as e:
                    logger.error("Error processing image %d: %s", img_idx, e)
                    # Solid black frame fallback
                    black = np.zeros((VIDEO_SIZE[1], VIDEO_SIZE[0], 3), dtype=np.uint8)
                    img_clip = ImageClip(black, duration=img_duration)

                img_clip = img_clip.set_fps(fps)

                # Subtitle for this segment
                try:
                    segment_text = session_data["segments"][audio_idx]["text"]
                    # Split long text into 2 lines
                    words = segment_text.split()
                    mid = len(words) // 2
                    line1 = " ".join(words[:mid])
                    line2 = " ".join(words[mid:])
                    subtitle_text = f"{line1}\n{line2}" if line2 else line1

                    txt_clip = (
                        TextClip(
                            subtitle_text,
                            fontsize=32,
                            color="white",
                            stroke_color="black",
                            stroke_width=2,
                            method="caption",
                            size=(VIDEO_SIZE[0] - 80, None),
                            font="DejaVu-Sans-Bold",
                        )
                        .set_position(("center", VIDEO_SIZE[1] - 120))
                        .set_duration(img_duration)
                    )
                    clip_with_sub = CompositeVideoClip([img_clip, txt_clip], size=VIDEO_SIZE)
                except Exception as e:
                    logger.warning("Subtitle failed, skipping: %s", e)
                    clip_with_sub = img_clip

                # Assign audio to first image of this audio segment
                if audio_clip is not None and img_offset == 0:
                    clip_with_sub = clip_with_sub.set_audio(audio_clip)

                video_clips.append(clip_with_sub)

        if not video_clips:
            raise ValueError("No video clips were created")

        final_video = concatenate_videoclips(video_clips, method="compose")

        video_path = os.path.join(session_dir(session_id), "final_video.mp4")
        final_video.write_videofile(
            video_path,
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=os.path.join(session_dir(session_id), "temp_audio.m4a"),
            remove_temp=True,
            logger=None,
        )

        final_video.close()
        return video_path

    except Exception as e:
        logger.error("Video assembly failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# Main production runner (runs in background thread)
# ---------------------------------------------------------------------------

def run_production(session_id: str, character_image_path: str):
    """Full production pipeline run in a background thread."""
    try:
        session_data = load_session(session_id)
        if not session_data:
            logger.error("Session not found: %s", session_id)
            return

        # Step 1: Generate images
        logger.info("[%s] Starting image generation", session_id)
        image_paths = generate_images(session_id, character_image_path, session_data)

        # Reload session data (status may have been updated)
        session_data = load_session(session_id)

        # Step 2: Generate audio
        logger.info("[%s] Starting audio generation", session_id)
        audio_paths = generate_audio(session_id, session_data)

        # Step 3: Assemble video
        logger.info("[%s] Starting video assembly", session_id)
        video_path = assemble_video(session_id, image_paths, audio_paths, session_data)

        # Done
        session_data = load_session(session_id)
        session_data["status"] = "done"
        session_data["progress"] = 100
        session_data["current_step"] = "Vidéo prête !"
        session_data["video_path"] = video_path
        session_data["video_url"] = f"/api/download/{session_id}"
        session_data["duration_seconds"] = 75.0
        session_data["error"] = None
        save_session(session_id, session_data)
        logger.info("[%s] Production complete: %s", session_id, video_path)

    except Exception as e:
        logger.error("[%s] Production failed: %s", session_id, e, exc_info=True)
        update_status(session_id, "error", 0, "Une erreur est survenue.", str(e))
