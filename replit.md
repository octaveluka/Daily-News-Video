# V-CTRL — Automated Video Production Platform

A platform that automatically generates narrative videos from today's news or a custom topic. It writes a 10-segment documentary script, generates 20 AI images (chained for character consistency), synthesizes 10 audio segments via Gemini TTS, then assembles everything with ffmpeg (Ken Burns + burned subtitles).

## Run & Operate

- `python artifacts/api-server/app.py` — run the Flask API server (port 8080)
- `pnpm --filter @workspace/frontend run dev` — run the React frontend
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- **Frontend:** React + Vite + Tailwind CSS (dark studio theme)
- **Backend:** Python 3.11 + Flask
- **Video pipeline:** pure ffmpeg subprocess (imageio-ffmpeg), Pillow for subtitle baking, numpy
- **AI:** Gemini 2.5 Flash TTS (raw PCM → wrapped as WAV), external image edit API (chained)
- **API codegen:** Orval (from OpenAPI spec)

## Where things live

- `lib/api-spec/openapi.yaml` — API contract (source of truth)
- `artifacts/api-server/app.py` — Flask routes + preview endpoints
- `artifacts/api-server/pipeline.py` — full video generation pipeline
- `artifacts/frontend/src/` — React frontend (dark V-CTRL UI)
- `artifacts/frontend/src/components/GenerationPreview.tsx` — real-time image/audio grid
- `lib/api-client-react/src/generated/` — generated React Query hooks
- `data/sessions/{session_id}/` — persistent session storage (dev); /tmp/v_ctrl_sessions/ (prod fallback)

## Architecture decisions

- **No MoviePy** — replaced entirely by ffmpeg subprocess calls (more reliable, no Python binding issues)
- **Gemini TTS** returns raw PCM (24 kHz, 16-bit, mono) — must wrap in WAV header via `_pcm_to_wav()`
- **Audio validation**: after every TTS call, check duration > 0.5s and silence RMS; retry up to 5× with backoff
- **Assembly**: assembling step also re-generates audio if a segment is silent/short (triple safety net)
- **Character in every image**: prompts include "the person from the reference photo" so the character appears in the right location
- **Documentary narrative style**: prompts instruct Al Jazeera/Vice News style — EVENTS not character movement descriptions
- **Parallel generation**: images sequential (chained), audio parallel ThreadPoolExecutor max_workers=2
- **Real-time preview**: `/api/preview/<sid>/manifest` polled every 2s; images/audio served individually
- **Sessions**: `data/sessions/` (dev, persistent); auto-fallback to `/tmp/v_ctrl_sessions/` if not writable

## Product

- **Step 1:** Choose "Actualité du jour" (auto-fetch) or "Mon sujet" (custom topic) → generates 10-segment narrative + 20 image prompts
- **Step 2:** Upload a character image (PNG/JPG) → triggers full production pipeline
- **Step 3:** Watch real-time images/audio preview → download final MP4 with title, description, and hashtags
- **History:** Session history panel always visible, shows past productions with Voir/Télécharger buttons

## Required Environment Variables

- `GEMINI_API_KEY` — Google Gemini API key (for TTS audio generation)
- `PORT` — auto-set by Replit workflows (8080 for API, 18130 for frontend)
- `SESSIONS_DIR` — (optional) override session storage path

## User preferences

- Narrative style: journalistic documentary (Al Jazeera / Vice News / France 24), NOT character movement fiction
- Image prompts: every prompt must explicitly include "the person from the reference photo" + location matching narration
- Image variety: each of the 20 prompts must use a different shot type (WIDE, CLOSE-UP, AERIAL, SILHOUETTE, etc.)
- Audio: zero tolerance for silent/empty segments — retry + re-generate at assembly if needed
- Step 1 UI: two modes — "Actualité du jour" (auto) and "Mon sujet" (custom text input)
- Real-time preview: images and audio must appear in UI as they're generated

## Gotchas

- **vite.config.ts**: PORT/BASE_PATH are required in dev but must NOT throw during `vite build` (isBuild guard added)
- **Gemini TTS**: returns raw PCM not WAV — always run `_pcm_to_wav()` before saving; validate duration after
- **ffmpeg binary**: located via `imageio_ffmpeg.get_ffmpeg_exe()` — always use this, not system `ffmpeg`
- **Ken Burns**: scale to 110% first, then slow crop — calculated by ffmpeg filter_complex, not Python
- **Image chaining**: each image API call gets previous image as base64 — character stays consistent
- **SESSIONS_DIR in production**: uses write-test fallback to `/tmp/v_ctrl_sessions/` if default path not writable
- **Audio silence detection**: check both duration (< 0.5s) and RMS (< 50) to catch all silent cases
- **React Query hooks**: second arg must be `{ query: { queryKey: ..., enabled: ..., ... } }` (not flat options)

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
