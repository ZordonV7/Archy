# Archy — Adorable Multi-Agent Scheduling Backend

Archy is a chubby, adorable companion who gently helps you follow your schedule. She runs four specialist agents (STT, Classifier, Planner, Mood), supervises their proposals, vetoes the ones that don't fit your current state, and translates everything into warm, motivating speech.

This is the Python backend. The Rust overlay UI (the actual chubby sprite that waddles across your screen) will live in a sibling project and talk to this server over HTTP/WebSocket.

---

## Architecture

```
                         ┌──────────────────────────┐
                         │          USER            │
                         │  voice / text / UI tap   │
                         └────────────┬─────────────┘
                                      │
                                      ▼
                         ┌──────────────────────────┐
                         │     ARCHY (Manager)      │
                         │  ──────────────────────  │
                         │  • Persona: chubby,      │
                         │    adorable, motivational│
                         │  • Interpreter: turns    │
                         │    structured agent data │
                         │    → friendly user speech│
                         │  • Supervisor: relevancy │
                         │    score + veto power    │
                         │  • Single source of      │
                         │    personality           │
                         └─┬──────┬──────┬──────┬──┘
                           │      │      │      │
              ┌────────────┘      │      │      └────────────┐
              ▼                   ▼      ▼                   ▼
   ┌─────────────────┐  ┌──────────────┐  ┌──────────────┐
   │  STT Agent      │  │ Classifier   │  │  Mood Agent  │
   │  (audio→text    │  │ + Priorit.   │  │ (analyst +   │
   │   via Gemini    │  │ Agent        │  │  advisor)    │
   │   native audio) │  │              │  │              │
   └─────────────────┘  └──────┬───────┘  └──────┬───────┘
                               │                 │
                               ▼                 │
                        ┌──────────────┐         │
                        │ Planning     │ ◀───────┘
                        │ Agent        │  (mood influences plan)
                        │ (advisory)   │
                        └──────┬───────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  DETERMINISTIC CORE │  ← no LLM, fully testable
                    │  • Rule-based       │
                    │    scheduler (final │
                    │    decider)         │
                    │  • Rule-based mood  │
                    │    engine (final    │
                    │    decider)         │
                    │  • Energy engine    │
                    │  • Risk engine      │
                    │  • SQLite / Postgres│
                    │    storage + full   │
                    │    audit trail      │
                    └─────────────────────┘
```

### Key invariants

1. **Agents never speak English to each other** — only structured JSON via `AgentProposal`
2. **Archy is the ONLY entity that produces user-facing natural language** — via the Interpreter
3. **LLM agents are advisory; rule-based core is the final decider** — deterministic, testable
4. **Every proposal is evaluated** — Archy assigns a `relevancy_score`; if it exceeds threshold, accept; else reject with constraints and ask for revision
5. **Every conflict is persisted + published** — full audit trail in SQLite + WebSocket events

---

## Project Layout

```
archy/
├── pyproject.toml
├── .env.example
├── run.py                       # `python run.py serve`
├── archy/
│   ├── __main__.py              # `python -m archy serve`
│   ├── config.py                # pydantic-settings
│   ├── server.py                # FastAPI + WebSocket hub
│   ├── cli.py                   # `archy` CLI
│   │
│   ├── contracts/
│   │   └── messages.py          # ALL inter-agent message types (the protocol)
│   │
│   ├── gemini/
│   │   ├── client.py            # Shared async Gemini client (text + audio + JSON)
│   │   ├── tts_client.py        # Gemini TTS client (Archy's voice)
│   │   ├── live_client.py       # Live STT (real-time mic input)
│   │   ├── embeddings.py        # Semantic search embeddings
│   │   └── router.py            # Model router (light/premium tiers)
│   │
│   ├── agents/                  ← One file per agent
│   │   ├── base.py              # Agent protocol
│   │   ├── stt_agent.py         # SpeechToTextAgent (Gemini native audio)
│   │   ├── classifier_agent.py  # ClassifierAgent (priority/complexity/deadline)
│   │   ├── planner_agent.py     # PlannerAgent (advisory ordering)
│   │   ├── mood_agent.py        # MoodAgent (analyst + advisor)
│   │   └── decomposer_agent.py  # DecomposerAgent (auto-split complex tasks)
│   │
│   ├── core/                    ← Deterministic deciders (no LLM)
│   │   ├── scheduler.py         # GreedyScheduler (final slot committer)
│   │   ├── mood_engine.py       # MoodEngine (final mood scorer)
│   │   ├── energy_engine.py     # EnergyEngine (execution capacity)
│   │   ├── risk_engine.py       # RiskEngine (deadline miss probability)
│   │   ├── policy_engine.py     # PolicyEngine (formal constraints)
│   │   ├── failure_simulator.py # Simulate current vs optimized schedule
│   │   ├── scheduler_trainer.py # Learns user patterns over time
│   │   ├── adaptive_notifications.py
│   │   ├── productivity_model.py
│   │   ├── storage.py           # SQLite repo (full audit, 16+ tables)
│   │   └── storage_postgres.py  # PostgreSQL repo (web deployment)
│   │
│   ├── application/             ← Application layer
│   │   ├── event_bus.py         # Unified event bus
│   │   ├── context_builder.py   # 7-layer AgentContext builder
│   │   ├── scheduler_v2.py      # Mood + energy + context-switch aware
│   │   └── proposal_manager.py  # Permissioned action gate
│   │
│   ├── integrations/            ← External services
│   │   ├── auth.py              # Google OAuth flow
│   │   ├── calendar_client.py   # Google Calendar sync
│   │   ├── calendar_subscriber.py
│   │   └── docs_client.py       # Google Docs daily briefs
│   │
│   └── assistant/               ← THE MANAGER
│       ├── persona.py           # Chubby adorable personality prompt
│       ├── interpreter.py       # JSON → user speech (only NL source)
│       └── manager.py           # Orchestrator + relevancy scoring + veto
│
└── tests/
    ├── test_scheduler.py        # 7 tests — scheduler rules
    ├── test_mood_engine.py      # 8 tests — mood delta rules
    ├── test_energy_engine.py    # 9 tests — energy deltas
    ├── test_risk_engine.py      # 9 tests — deadline risk
    ├── test_policy_engine.py    # 13 tests — policy constraints
    ├── test_application.py      # 18 tests — application layer
    └── test_veto.py             # 3 tests — Archy's veto + conflict logging
```

---

## The Veto Mechanism

Every agent proposal flows through this loop:

```
Agent.run() → AgentProposal (with self_confidence)
                    │
                    ▼
        Archy._evaluate_proposal()
                    │
                    ▼
        ┌───── relevancy_score > threshold? ─────┐
        │                                         │
       YES                                       NO
        │                                         │
        ▼                                         ▼
     ACCEPT                              REJECT + constraints
        │                                         │
        │                                         ▼
        │                              Agent.revise() with constraints
        │                                         │
        │                                         ▼
        │                              (loop, max_negotiation_rounds)
        │                                         │
        │                                         ▼
        │                              ESCALATE → ConflictEvent persisted
        │
        ▼
  Rule-based core commits the decision
```

### Current veto rules (in `assistant/manager.py`)

| User mood | Agent | Proposal shape | Veto? |
|---|---|---|---|
| critical_panic | Planner | > 2 slots | YES — constrain to 2 slots |
| critical_panic | Planner | any slot > 45 min | YES — constrain to 45 min max |
| drift_alert | Planner | > 4 slots | YES — constrain to 4 slots |
| any | Classifier | priority < 30 + no deadline | Penalize (−10) |
| any | any | self_confidence low | Penalize |

Threshold is configurable: `ARCHY_RELEVANCY_THRESHOLD=65` (default).

### Conflict events

Every rejection produces a `ConflictEvent` that's:
1. Persisted to SQLite (`conflict_events` table)
2. Published via WebSocket (`conflict.event` event type)
3. Surfaced to Archy's interpreter so she can gently mention the adjustment

Example WebSocket event:
```json
{
  "type": "conflict.event",
  "payload": {
    "agent_name": "planner",
    "proposal_type": "schedule",
    "rounds": 2,
    "resolution": "revised",
    "final_outcome": "Agent revised proposal per Archy's constraints."
  }
}
```

The Rust overlay can later show these as "Archy tweaked the plan to make it easier for you" — full transparency into the multi-agent negotiation.

---

## Quick Start

> **Python 3.11+ required.**

### Setup

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
- **Rive mascot** — the frontend ships a canvas-based RetroTVMascot today; a
  `.riv` Rive file (with proper state machine for the 7 moods) would let
  designers iterate on the animation without touching code.

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
