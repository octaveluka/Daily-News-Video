# V-CTRL — Automated Video Production Platform

A platform that automatically generates 75-second narrative videos from today's news. It fetches a news topic, writes a 10-segment script, generates 20 AI images (chained for character consistency), synthesizes 10 audio segments via Gemini TTS, then assembles everything with MoviePy (Ken Burns effect + burned-in subtitles).

## Run & Operate

- `python artifacts/api-server/app.py` — run the Flask API server (port 8080)
- `pnpm --filter @workspace/frontend run dev` — run the React frontend
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- **Frontend:** React + Vite + Tailwind CSS (dark studio theme)
- **Backend:** Python 3.11 + Flask (replaces Node.js api-server)
- **Video pipeline:** MoviePy 1.0.3, Pillow, numpy
- **AI:** Gemini 2.5 Flash TTS, external image edit API
- **API codegen:** Orval (from OpenAPI spec)

## Where things live

- `lib/api-spec/openapi.yaml` — API contract (source of truth)
- `artifacts/api-server/app.py` — Flask routes
- `artifacts/api-server/pipeline.py` — full video generation pipeline
- `artifacts/frontend/src/` — React frontend (dark V-CTRL UI)
- `lib/api-client-react/src/generated/` — generated React Query hooks
- `/tmp/temp_sessions/{session_id}/` — runtime session storage (images, audio, video)

## Architecture decisions

- Python Flask replaces the Node.js api-server entirely (MoviePy is Python-only)
- Character consistency maintained by chaining: each image edit receives the previous image as base64 context
- 10 audio files × 2 images each = 20 images + 10 audios = ~75s video
- Production runs in a background thread; frontend polls `/api/status/{id}` every 2s
- Sessions stored in `/tmp/temp_sessions/` — ephemeral, cleared on restart

## Product

- **Step 1:** Click "Lancer la recherche" → fetches today's news → generates 10-segment script + 20 image prompts
- **Step 2:** Upload a character image (PNG/JPG) → triggers full production pipeline
- **Step 3:** Watch real-time progress → download final MP4 with title, description, and hashtags

## Required Environment Variables

- `GEMINI_API_KEY` — Google Gemini API key (for TTS audio generation)
- `PORT` — auto-set by Replit workflows (8080)

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- The Flask server runs from `artifacts/api-server/` directory — paths in artifact.toml must be relative to that directory
- MoviePy's TextClip requires ImageMagick (`convert` binary); if subtitles fail, they are silently skipped
- Gemini TTS model: `gemini-2.5-flash-preview-tts` — check for model name updates
- The text API (`delfaapiai.vercel.app`) may return JSON in various shapes; pipeline has fallback handling

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
