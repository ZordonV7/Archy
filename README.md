# Archy — Adorable Multi-Agent Scheduling Companion

Archy is a chubby, adorable companion who gently helps you follow your schedule. She runs four specialist agents (STT, Classifier, Planner, Mood), supervises their proposals, vetoes the ones that don't fit your current state, and translates everything into warm, motivating speech.

The mascot is a **retro CRT TV character** (`RetroTVMascot`) with 7 canvas-animated moods (happy / sad / panic / upset / thinking / sleeping / waking) — drawn frame-by-frame in vanilla canvas, no Rive or external animation runtime required.

This repo contains:
- **`archy/`** — Python FastAPI backend (multi-agent scheduler, deterministic core, Gemini LLM advisors)
- **`frontend/`** — React + Vite frontend (also ships as a Tauri 2 desktop app)
- **`tests/`** — 67 tests across 7 files
- **`Dockerfile`** + **`render.yaml`** — one-click Render deploy
- **`frontend/vercel.json`** — Vercel deploy config (Root Directory = `frontend`)

## Deployment

**👉 Full guide: [DEPLOYMENT.md](DEPLOYMENT.md)** — covers Vercel + Render + Neon (web), Tauri (desktop), and local dev.

| Target | What | Time to live |
|--------|------|--------------|
| **Web (Vercel)** | Frontend only — talks to Render backend | ~5 min |
| **Web (Render)** | Backend (FastAPI) | ~5 min |
| **Web (Neon)** | Postgres database | ~2 min |
| **Desktop (Tauri)** | Native installer (.msi / .dmg / .AppImage) | ~10 min build |
| **Local dev** | Both servers on localhost | ~2 min |

The Tauri config (`frontend/src-tauri/tauri.conf.json`) opens three OS windows by default: a small mascot window (bottom-left), a speech bubble window above it, and a larger dashboard window on the right.

---

## Quick Start (local dev)

> **Python 3.11+ required.**

### Backend

```bash
# Linux / macOS
cd archy-web-edition
pip install -e ".[dev]"
cp .env.example .env   # or: install -D .env.example .env
# Edit .env: set GEMINI_API_KEY=your-key-from-https://aistudio.google.com/apikey

# Windows (PowerShell)
cd archy-web-edition
pip install -e ".[dev]"
copy .env.example .env
# Edit .env: set GEMINI_API_KEY=your-key-from-https://aistudio.google.com/apikey
```

### Run tests (no API key needed)

```bash
pytest
# → 67 passed across 7 test files (scheduler, mood, energy, risk, policy, application, veto)
```

### Start the server

```bash
archy serve
# Or: python -m archy serve
# Or: python run.py serve
# → open http://127.0.0.1:8000/docs
```

### Try it from another terminal

```bash
archy ingest-text "I have to file my Q2 taxes before midnight tonight"
archy tasks
archy mood
archy energy
archy conflicts
archy drift --kind nap --minutes 60
```

### ⚠️ Do NOT do this

```bash
# ❌ This fails — relative imports require package context:
python archy/server.py

# ✅ Instead, use one of:
archy serve
python -m archy serve
python run.py serve
```

---

## HTTP API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET  | `/health` | — | `{status, mode}` |
| POST | `/ingest/text` | `{"text": "..."}` | Task |
| POST | `/ingest/audio` | multipart `file` | Task |
| GET  | `/tasks?status=...` | — | `{tasks: [...]}` |
| GET  | `/tasks/{id}` | — | Task |
| POST | `/tasks/{id}/start` | — | Task |
| POST | `/tasks/{id}/complete` | — | Task |
| POST | `/tasks/{id}/cancel` | — | Task |
| GET  | `/schedule` | — | `{slots, unscheduled, mood, assistant_message}` |
| GET  | `/mood` | — | `{score, label, reasons}` |
| GET  | `/mood/history` | — | `{snapshots: [...]}` |
| GET  | `/proposals?agent=...` | — | Agent proposal audit trail |
| GET  | `/conflicts` | — | Conflict event audit trail |
| POST | `/drift` | DriftEvent | `{mood, assistant_message}` |
| WS   | `/events` | — | Live event stream |

### WebSocket events

Archy emits 10+ event types. Key ones:

| Event | When |
|---|---|
| `ingest.started` | User submits audio or text |
| `transcript.ready` | STTAgent finished |
| `classification.ready` | ClassifierAgent finished |
| `task.created` | Task persisted to DB |
| `agent.proposal` | Any agent emits a proposal (includes round #) |
| `assistant.evaluation` | Archy scored a proposal (includes relevancy_score) |
| `conflict.event` | Archy vetoed; agent revised or escalated |
| `mood.analysis` | MoodAgent's advisory assessment |
| `planner.advisory` | PlannerAgent's advisory order |
| `plan.ready` | Final schedule committed |
| `assistant.message` | Archy's user-facing speech (the only NL output) |
| `task.started/completed/cancelled` | Lifecycle updates |
| `drift.detected` | User-reported schedule disruption |

---

## Database Schema

Archy uses SQLite by default (`~/.archy/archy.db`) on desktop. For web deployment,
set `ARCHY_DB_URL` to a PostgreSQL connection string and the repository factory
switches to `PostgresRepository` automatically.

Core tables (v2.1):

1. **`tasks`** — user tasks (id, title, priority, complexity, deadline, status, scheduled slots)
2. **`mood_snapshots`** — append-only mood history (score, label, reasons, timestamp)
3. **`energy_snapshots`** — execution-capacity history (separate from mood)
4. **`agent_proposals`** — every proposal from every agent (full audit trail)
5. **`assistant_evaluations`** — Archy's verdict on each proposal (relevancy_score, decision, constraints)
6. **`conflict_events`** — when Archy vetoed (original proposal, evaluation, resolution, outcome)
7. **`proposals`** — formal v2.1 proposals with risk_level + permission flow
8. **`permissions`** — pending permission requests awaiting user approval
9. **`audit_logs`** — every significant state change
10. **`router_logs`** — model router decisions (feeds nightly trainer)
11. **`notifications`** — adaptive notification queue
12. **`preferences`** — key/value user preferences
13. **`memories`** — 5-type memory system with embeddings for semantic search
14. **`work_sessions`** — execution-mode session history (timer, interruptions, actual vs estimated)
15. **`goals`** / **`projects`** / **`task_projects`** — hierarchical planning (Goal → Project → Task)
16. **`users`** — Google OAuth users (web deployment auth)
17. **`summaries`** — daily/weekly generated summaries
18. **`relationships`** — entity graph (people, places, things)

The proposal/evaluation/conflict tables give you complete observability into the
multi-agent negotiation. Query them via `/proposals` and `/conflicts` HTTP endpoints.

---

## Mood Score Logic (rule-based, deterministic)

The `MoodEngine` (in `core/mood_engine.py`) is the source of truth. The `MoodAgent` is advisory.

| Event | Delta |
|---|---|
| Task scheduled | +2 |
| Task scheduled but slot ends after deadline | −4 (in addition to +2) |
| Task completed on time | +5 |
| Task completed late | −3 |
| Task missed | −15 |
| Drift event | −3 per 15 min lost |
| Drift acknowledged manually | +6 (recovery bonus) |
| Each overdue pending task (on recompute) | −3 |

Score is clamped to `[0, 100]`. Labels:
- ≥ 90 → `deep_focus`
- ≥ 70 → `watchful`
- ≥ 50 → `drift_alert`
- < 50 → `critical_panic`

---

## Archy's Persona

Encoded in `assistant/persona.py`. Highlights:

- Warm, encouraging, never harsh
- Simple words, short sentences
- Celebrates small wins
- Reframes setbacks kindly
- Adapts energy to user's mood
- Never exposes technical details (JSON, scores, agent names)
- 1-3 sentences max per message
- A single gentle emoji per message is fine

Tone calibration by mood:
- `deep_focus` → calm, supportive, brief
- `watchful` → cheerful, present
- `drift_alert` → gentle, grounding
- `critical_panic` → soft, slow, reassuring

---

## Configuration

All settings via env vars (prefix `ARCHY_`) or `.env` file:

| Var | Default | Purpose |
|---|---|---|
| `ARCHY_MODE` | `online` | `online` (Gemini APIs) or `offline` (canned stubs) |
| `ARCHY_HOST` | `127.0.0.1` | Server bind host |
| `ARCHY_PORT` | `8000` | Server bind port |
| `ARCHY_DB_PATH` | `~/.archy/archy.db` | SQLite path (desktop) |
| `ARCHY_DB_URL` | _(empty)_ | PostgreSQL URL (web). When set, switches to PostgresRepository |
| `ARCHY_ALLOWED_ORIGINS` | `*` | CORS allowed origins (`*` or comma-separated list) |
| `ARCHY_SECRET_KEY` | `dev-secret-change-me` | Session cookie signing key (CHANGE for prod!) |
| `ARCHY_FRONTEND_URL` | `http://localhost:5173` | OAuth callback redirect target |
| `ARCHY_REQUIRE_AUTH` | `false` | When true, all endpoints (except /health, /auth/*) require login |
| `GEMINI_API_KEY` | (required online) | Get one at https://aistudio.google.com/apikey |
| `ARCHY_LLM_MODEL` | `gemini-2.5-flash` | Default workhorse model (supports thinking) |
| `ARCHY_LLM_MODEL_PREMIUM` | `gemini-2.5-pro` | Premium tier for complex reasoning |
| `ARCHY_LIGHT_MODEL` | `gemma-3-27b-it` | Lightweight text model (briefings, summaries) |
| `ARCHY_EMBEDDING_MODEL` | `gemini-embedding-001` | Semantic search embeddings |
| `ARCHY_TTS_MODEL` | `gemini-2.5-flash-preview-tts` | Spoken audio for Archy's voice |
| `ARCHY_LIVE_STT_MODEL` | `gemini-2.5-flash-native-audio-dialog` | Real-time mic input |
| `ARCHY_LLM_THINKING_BUDGET` | `0` | Thinking budget for Gemini 2.5+ models |
| `ARCHY_BUFFER_MINUTES` | `5` | Gap between scheduled slots |
| `ARCHY_WORKDAY_START` | `09:00` | Scheduler won't book before this |
| `ARCHY_WORKDAY_END` | `22:00` | Scheduler rolls to next day past this |
| `ARCHY_MOOD_BASELINE` | `75` | Starting mood score |
| `ARCHY_RELEVANCY_THRESHOLD` | `65` | Proposals scoring ≤ this get vetoed |
| `ARCHY_MAX_NEGOTIATION_ROUNDS` | `2` | Max revisions before escalation |

---

## What's Next

- **Wire TTS endpoint** — `gemini/tts_client.py` already exists; expose `/tts/speak`
  so the frontend can fetch real Archy voice instead of falling back to browser
  `SpeechSynthesis`. Quota is tight (10 RPD) so reserve for high-value messages.
- **LLM-as-judge for relevancy scoring** — replace the heuristic in `_evaluate_proposal()`
  with a Gemini call for richer reasoning.
- **PlannerAgent's `ordered_task_ids`** — wire through to `SchedulerV2.plan(preferred_order=...)`
  (hook is already there in `_extract_preferred_order`).
- **More retro TV moods** — the canvas mascot (`RetroTVMascot.tsx`) currently
  ships 7 moods. New ones (e.g. `celebrating`, `curious`, `proud`) are pure
  additions — add a new `RetroMood` value, extend `moodDefs`, and add the eye/
  mouth/FX drawing branches.

---

## Google Calendar + Docs Integration (Optional)

Archy can auto-sync scheduled tasks to your Google Calendar and generate daily briefs as Google Docs. Both require a one-time OAuth setup.

### Step 1: Create Google OAuth Credentials

1. Go to **https://console.cloud.google.com/**
2. Create or select a project
3. Enable **Google Calendar API** and **Google Docs API** (APIs & Services → Library)
4. Go to **APIs & Services → OAuth consent screen**
   - User type: **External**
   - Fill in app name (e.g., "Archy"), your email
   - Add scopes: `calendar.events` and `documents`
   - Add yourself as a test user
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: "Archy"
6. Copy the **Client ID** and **Client Secret**

### Step 2: Add to `.env`

```
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
ARCHY_CALENDAR_SYNC_ENABLED=true
ARCHY_DAILY_BRIEF_ENABLED=true
```

### Step 3: Install Google deps

```powershell
pip install google-auth-oauthlib google-api-python-client
```

### Step 4: Run the OAuth flow

```powershell
archy auth
```

A browser opens, you consent to Calendar + Docs access, tokens are saved to `~/.archy/google_tokens.json`. You only need to do this once.

### Step 5: Use it

```powershell
# Normal ingest — scheduled tasks now also appear in Google Calendar
archy ingest-text "I have a meeting tomorrow at 3pm"

# Generate a daily brief Google Doc
archy daily-brief
# → outputs: "Daily brief created! URL: https://docs.google.com/document/d/..."

# Manually push all scheduled tasks to Calendar
archy sync-calendar
```

### What gets synced

| Archy Event | Calendar Action |
|---|---|
| `plan.ready` | Create/update events for each scheduled slot |
| `task.completed` | Event color → green |
| `task.cancelled` | Delete event |
| `task.missed` | Delete event |

Events are color-coded by priority:
- 🔴 Red: priority ≥ 85 (critical)
- 🟡 Yellow: priority ≥ 65 (high)
- 🟢 Green: priority < 65 (normal)

Each event includes:
- Task title as the event title
- Description with priority, complexity, reasoning, and original transcript
- 10-min and 1-min popup reminders
- Source marked as "Archy"

### Daily brief contents

The `archy daily-brief` command creates a Google Doc with:
1. **From Archy** — a friendly intro written by the LLM (Archy's voice)
2. **Today's Schedule** — all upcoming slots with local times
3. **Completed Today** — tasks marked done today
4. **Missed** — tasks that slipped (with kindness)
5. **Current Mood** — score and label

---

## Design Notes

- **One Gemini model, many roles.** All four agents + Archy share the same Gemini client. Different system prompts = different "agents." This is how LangGraph, CrewAI, and AutoGen actually work under the hood.
- **Protocol-based agents.** Every agent implements `Agent` Protocol. Swap any agent without touching others.
- **Deterministic core.** The rule-based scheduler and mood engine are the source of truth. LLM agents are advisory. This keeps the system testable and predictable.
- **Interpreter pattern.** Agents emit JSON; only Archy produces natural language. Token-efficient, persona-consistent, cacheable.
- **Audit trail.** Every proposal, evaluation, and conflict is persisted. Full observability into the multi-agent negotiation.
