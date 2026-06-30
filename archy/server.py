"""HTTP + WebSocket server for Archy.

Exposes REST endpoints + a WebSocket event stream. The Rust overlay UI
will talk to this.

Endpoints:
  GET    /health
  GET    /tasks
  GET    /tasks/{id}
  POST   /ingest/text            body: {"text": "..."}
  POST   /ingest/audio           multipart: file
  POST   /tasks/{id}/start
  POST   /tasks/{id}/complete
  POST   /tasks/{id}/cancel
  POST   /drift                  body: DriftEvent
  GET    /schedule               current plan
  GET    /mood                   latest mood
  GET    /mood/history
  GET    /proposals              agent proposal audit trail
  GET    /conflicts              conflict event audit trail
  WS     /events                 live event stream
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .contracts.messages import DriftEvent, TaskStatus
from .core.storage import Repository, get_repository
from .gemini.client import GeminiClient, get_gemini_client
from .gemini.embeddings import get_embeddings_client
from .integrations.auth import GoogleAuth
from .assistant.manager import Archy


class EventHub:
    """In-memory pub/sub for Archy events → WebSocket clients."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._queues.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        msg = {"type": event_type, "payload": payload}
        async with self._lock:
            targets = list(self._queues)
        for q in targets:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("Event queue full, dropping message")


class IngestTextRequest(BaseModel):
    text: str


def create_app(
    settings: Settings | None = None,
    assistant: Archy | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_db_dir()
    repo: Repository = get_repository(settings)
    hub = EventHub()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal assistant
        if assistant is None:
            gemini = get_gemini_client(settings)
            assistant = Archy(settings, repo, gemini)
        assistant.subscribe(hub.publish)
        app.state.assistant = assistant
        app.state.repo = repo
        app.state.hub = hub
        logger.info(
            f"Archy server up on {settings.host}:{settings.port} ({settings.mode} mode)"
        )
        yield
        repo.close()

    app = FastAPI(title="Archy", version="0.2.0", lifespan=lifespan)

    # Parse allowed_origins: "*" or comma-separated list of URLs
    origins_str = settings.allowed_origins.strip()
    if origins_str == "*":
        allowed_origins_list = ["*"]
        allow_credentials = False  # Wildcard can't be used with credentials
    else:
        allowed_origins_list = [o.strip() for o in origins_str.split(",") if o.strip()]
        allow_credentials = True   # Specific origins can use credentials

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins_list,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Session middleware — signs cookies with the secret_key.
    # Used by /auth/* endpoints to store the logged-in user's id + email.
    # Even when require_auth=False (desktop), this is harmless — the session
    # just stays empty if no login happens.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="archy_session",
        max_age=30 * 24 * 60 * 60,  # 30 days
        same_site="lax",
        https_only=not settings.allowed_origins.startswith("http://localhost"),  # secure in prod
    )

    def _assistant() -> Archy:
        return app.state.assistant

    # --- Health ---

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "mode": settings.mode}

    @app.get("/keep-alive")
    async def keep_alive() -> dict:
        """Lightweight ping endpoint to prevent Render free-tier from sleeping.

        Set up an external cron (e.g. cron-job.org, UptimeRobot, or Vercel Cron)
        to hit this endpoint every 10 minutes. Returns 200 fast so the request
        is cheap.
        """
        return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

    # --- Ingest ---

    @app.post("/ingest/text")
    async def ingest_text(req: IngestTextRequest) -> dict:
        task = await _assistant().ingest_text(req.text)
        return task.model_dump(mode="json")

    @app.post("/ingest/audio")
    async def ingest_audio(file: UploadFile) -> dict:
        audio = await file.read()
        if not audio:
            raise HTTPException(400, "Empty audio payload")
        task = await _assistant().ingest_audio(
            audio, file.filename or "audio", file.content_type or "audio/wav"
        )
        return task.model_dump(mode="json")

    # --- Tasks ---

    @app.get("/tasks")
    async def list_tasks(status: TaskStatus | None = None) -> dict:
        return {"tasks": [t.model_dump(mode="json") for t in repo.list_tasks(status=status)]}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        t = repo.get_task(task_id)
        if not t:
            raise HTTPException(404, f"Task {task_id} not found")
        return t.model_dump(mode="json")

    @app.post("/tasks/{task_id}/start")
    async def start_task(task_id: str) -> dict:
        try:
            t = await _assistant().start_task(task_id)
        except KeyError:
            raise HTTPException(404, f"Task {task_id} not found")
        return t.model_dump(mode="json")

    # v2.1: Work Session endpoints (Execution Mode)
    @app.get("/sessions/active")
    async def get_active_session() -> dict:
        session = await _assistant().get_active_session()
        return session or {}

    @app.post("/sessions/pause")
    async def pause_session() -> dict:
        return await _assistant().pause_session()

    @app.post("/sessions/resume")
    async def resume_session() -> dict:
        return await _assistant().resume_session()

    @app.post("/sessions/end")
    async def end_session(completion_percentage: int = 100, complete_task: bool = True) -> dict:
        return await _assistant().end_session(completion_percentage, complete_task)

    @app.get("/sessions/history")
    async def session_history(task_id: str | None = None, limit: int = 20) -> dict:
        sessions = await _assistant().get_session_history(task_id=task_id, limit=limit)
        return {"sessions": sessions}

    @app.get("/sessions/analytics")
    async def session_analytics() -> dict:
        return await _assistant().get_session_analytics()

    @app.post("/tasks/{task_id}/complete")
    async def complete_task(task_id: str) -> dict:
        try:
            t = await _assistant().complete_task(task_id)
        except KeyError:
            raise HTTPException(404, f"Task {task_id} not found")
        return t.model_dump(mode="json")

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        try:
            t = await _assistant().cancel_task(task_id)
            # cancel_task may return a Task or a dict (pending_approval)
            if isinstance(t, dict):
                return t
            return t.model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"Task {task_id} not found")

    # v2.1: Edit task schedule (time + duration)
    @app.patch("/tasks/{task_id}")
    async def edit_task(task_id: str, body: dict) -> dict:
        """Edit a task's scheduled time and/or duration.

        Body: { "scheduled_start": "ISO", "scheduled_end": "ISO", "duration_minutes": int }
        """
        t = repo.get_task(task_id)
        if not t:
            raise HTTPException(404, f"Task {task_id} not found")
        from datetime import datetime as _dt
        if "scheduled_start" in body and body["scheduled_start"]:
            try:
                t.scheduled_start = _dt.fromisoformat(body["scheduled_start"])
            except:
                pass
        if "scheduled_end" in body and body["scheduled_end"]:
            try:
                t.scheduled_end = _dt.fromisoformat(body["scheduled_end"])
            except:
                pass
        if "duration_minutes" in body and body["duration_minutes"]:
            t.duration_minutes = int(body["duration_minutes"])
        if "title" in body:
            t.title = body["title"]
        if "priority" in body:
            t.priority = int(body["priority"])
        repo.upsert_task(t)
        return t.model_dump(mode="json")

    # v2.1: Restore task from trash (only if deadline is in the future)
    @app.post("/tasks/{task_id}/restore")
    async def restore_task(task_id: str) -> dict:
        """Restore a cancelled/missed task. Only works if deadline is still in the future."""
        t = repo.get_task(task_id)
        if not t:
            raise HTTPException(404, f"Task {task_id} not found")
        from datetime import datetime as _dt
        from ..contracts.messages import utcnow as _utcnow
        now = _utcnow()
        # Check deadline — normalize timezone
        if t.deadline:
            dl = t.deadline
            now_check = now
            if dl.tzinfo is None:
                now_check = now.replace(tzinfo=None)
            if dl < now_check:
                return {
                    "error": "Cannot restore — deadline has already passed.",
                    "deadline": t.deadline.isoformat() if t.deadline else None,
                }
        # Restore: set status back to pending
        t.status = TaskStatus.PENDING
        t.scheduled_start = None
        t.scheduled_end = None
        repo.upsert_task(t)
        # Replan to reschedule it
        await _assistant().replan()
        return t.model_dump(mode="json")

    # v2.1: Empty trash — permanently DELETE from database
    @app.delete("/trash/empty")
    async def empty_trash() -> dict:
        """Permanently delete all completed/cancelled/missed tasks from the database."""
        from ..contracts.messages import TaskStatus
        deleted = repo.delete_tasks_by_status([
            TaskStatus.DONE.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.MISSED.value,
        ])
        return {"deleted": deleted, "message": f"Permanently deleted {deleted} task(s)."}

    # v2.1: Delete a single task permanently (from trash)
    @app.delete("/tasks/{task_id}")
    async def delete_task(task_id: str) -> dict:
        """Permanently delete a task from the database. No mood effect."""
        success = repo.delete_task(task_id)
        if not success:
            raise HTTPException(404, f"Task {task_id} not found")
        return {"deleted": True, "task_id": task_id}

    # v2.1: AI-powered reschedule — natural language rescheduling
    @app.post("/tasks/{task_id}/ai-reschedule")
    async def ai_reschedule(task_id: str, body: dict = None) -> dict:
        """Use AI to reschedule a task based on natural language instructions.

        Body: { "instruction": "move it to tomorrow afternoon" }

        The AI parses the instruction and updates the task's scheduled time.
        """
        instruction = (body or {}).get("instruction", "")
        if not instruction:
            return {"error": "No instruction provided."}

        t = repo.get_task(task_id)
        if not t:
            raise HTTPException(404, f"Task {task_id} not found")

        # Build context for the AI
        from datetime import datetime as _dt
        import json as _json

        task_context = {
            "task_title": t.title,
            "category": t.category,
            "priority": t.priority,
            "duration_minutes": t.duration_minutes,
            "current_scheduled_start": t.scheduled_start.isoformat() if t.scheduled_start else None,
            "current_scheduled_end": t.scheduled_end.isoformat() if t.scheduled_end else None,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "workday_start": get_settings().workday_start,
            "workday_end": get_settings().workday_end,
            "current_time_utc": datetime.now(timezone.utc).isoformat(),
            "user_instruction": instruction,
        }

        ai_prompt = f"""You are a scheduling assistant. The user wants to reschedule a task.

Current task info:
{_json.dumps(task_context, indent=2)}

User instruction: "{instruction}"

Determine the new scheduled_start and scheduled_end times (ISO 8601 format).
Consider:
- The workday window ({get_settings().workday_start} to {get_settings().workday_end})
- The task's deadline (if any) — don't schedule past it
- The task duration ({t.duration_minutes} minutes)
- Current time

Return STRICT JSON:
{{
  "scheduled_start": "ISO 8601 datetime",
  "scheduled_end": "ISO 8601 datetime",
  "explanation": "One sentence explaining why you chose this time."
}}
"""

        try:
            gemini = get_gemini_client()
            result = await gemini.generate_json_with_fallback(
                agent_name="classifier",
                system_prompt="You are a scheduling assistant. Return only valid JSON.",
                user_prompt=ai_prompt,
                temperature=0.2,
                max_output_tokens=300,
                operation_name="AI reschedule",
            )

            new_start = result.get("scheduled_start")
            new_end = result.get("scheduled_end")
            explanation = result.get("explanation", "Rescheduled by AI.")

            if new_start:
                t.scheduled_start = _dt.fromisoformat(new_start)
            if new_end:
                t.scheduled_end = _dt.fromisoformat(new_end)

            repo.upsert_task(t)

            return {
                "status": "ok",
                "task": t.model_dump(mode="json"),
                "explanation": explanation,
            }
        except Exception as e:
            return {"error": f"AI reschedule failed: {str(e)}"}

    # --- Schedule & Mood ---

    @app.get("/schedule")
    async def get_schedule() -> dict:
        return await _assistant().replan()

    @app.get("/mood")
    async def get_mood() -> dict:
        return await _assistant().current_mood()

    @app.get("/mood/history")
    async def mood_history(limit: int = 50) -> dict:
        return {"snapshots": repo.mood_history(limit=limit)}

    # v2.1: Energy
    @app.get("/energy")
    async def get_energy() -> dict:
        return await _assistant().current_energy()

    @app.get("/energy/history")
    async def energy_history(limit: int = 50) -> dict:
        return {"snapshots": repo.energy_history(limit=limit)}

    # v2.1: Schedule density — how busy is today?
    @app.get("/schedule/density")
    async def schedule_density() -> dict:
        """Returns how full the schedule is + upcoming deadlines."""
        from .core.energy_engine import EnergyEngine
        engine = EnergyEngine(get_settings(), repo)
        return engine.get_schedule_density()

    # v2.1: Deadline notifications — check for tasks due soon
    @app.get("/notifications/deadlines")
    async def deadline_notifications() -> dict:
        """Check all scheduled tasks for upcoming deadlines.

        Delegates to AdaptiveNotificationManager.check_deadlines() which:
        - Determines notification tier based on priority (30/15/5 for critical,
          15/5 for high, 5 for normal)
        - Considers user's notification effectiveness per hour (learned)
        - Delays notifications during meetings (if calendar synced)
        - Falls back to best-hour if current hour has low effectiveness
        """
        assistant = _assistant()
        tasks = repo.list_tasks()
        smart_notifications = assistant._notification_manager.check_deadlines(tasks)
        # Convert SmartNotification objects to dicts for the API response
        notifications = [
            n.model_dump(mode="json") if hasattr(n, "model_dump") else n
            for n in smart_notifications
        ]
        return {
            "notifications": notifications,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    # v2.1: Audit logs
    @app.get("/audit")
    async def audit_logs(limit: int = 100) -> dict:
        return {"logs": repo.list_audit_logs(limit=limit)}

    # v2.1: Router logs
    @app.get("/router/logs")
    async def router_logs(limit: int = 100) -> dict:
        return {"logs": repo.list_router_logs(limit=limit)}

    # v2.1: Preferences
    @app.get("/preferences")
    async def list_prefs() -> dict:
        return {"preferences": repo.list_preferences()}

    @app.post("/preferences/{key}")
    async def set_pref(key: str, value: str = "", category: str = "general") -> dict:
        repo.set_preference(key, value, category)
        return {"status": "ok", "key": key, "value": value}

    # v2.1: Summaries
    @app.get("/summaries")
    async def list_summaries(summary_type: str | None = None, limit: int = 30) -> dict:
        return {"summaries": repo.list_summaries(summary_type=summary_type, limit=limit)}

    # v2.1: Scheduler learned parameters
    @app.get("/scheduler/params")
    async def scheduler_params() -> dict:
        from .core.scheduler_trainer import SchedulerTrainer
        trainer = SchedulerTrainer(repo)
        return trainer.get_learned_params()

    @app.post("/scheduler/retrain")
    async def scheduler_retrain() -> dict:
        from .core.scheduler_trainer import SchedulerTrainer
        trainer = SchedulerTrainer(repo)
        return trainer.retrain()

    # v2.1: Adaptive learning insights (human-readable)
    @app.get("/scheduler/insights")
    async def scheduler_insights() -> dict:
        return await _assistant().get_insights()

    # v2.1: Risk assessment ("Will this user miss the deadline?")
    @app.get("/risk/assess")
    async def assess_risks() -> dict:
        assessments = await _assistant().assess_risks()
        return {"assessments": assessments}

    # v2.1: Dynamic risk — current risk + trajectory (updates in real-time)
    @app.get("/risk/current")
    async def current_risk() -> dict:
        return await _assistant().current_risk()

    # v2.1: Recovery modes
    @app.get("/risk/recovery-modes")
    async def recovery_modes() -> dict:
        """List available recovery modes with descriptions."""
        from .core.risk_engine import RecoveryMode
        return {
            "modes": [
                {
                    "id": RecoveryMode.CONSERVATIVE.value,
                    "name": "Conservative",
                    "description": "Don't change meetings. Only use free time.",
                    "icon": "🟢",
                },
                {
                    "id": RecoveryMode.BALANCED.value,
                    "name": "Balanced",
                    "description": "Move low-priority tasks. Keep evenings free.",
                    "icon": "🟡",
                },
                {
                    "id": RecoveryMode.EMERGENCY.value,
                    "name": "Emergency",
                    "description": "Use evenings. Weekend sessions. Compress breaks.",
                    "icon": "🔴",
                },
            ]
        }

    @app.post("/risk/set-mode")
    async def set_recovery_mode(body: dict) -> dict:
        """Set the recovery mode for risk assessment."""
        mode = body.get("mode", "balanced")
        repo.set_preference("recovery_mode", mode, category="risk")
        return {"status": "ok", "mode": mode}

    # v2.1: Learned biases — human-readable
    @app.get("/scheduler/biases")
    async def learned_biases() -> dict:
        """Show what Archy has learned about the user's estimation biases."""
        from .core.scheduler_trainer import SchedulerTrainer
        trainer = SchedulerTrainer(repo)
        return {"biases": trainer.get_bias_summary()}

    # v2.1: Personal Productivity Model — the evolving user profile
    @app.get("/productivity/profile")
    async def productivity_profile() -> dict:
        """Get the complete productivity profile — 9 dimensions."""
        return _assistant()._productivity_model.get_profile_summary()

    @app.get("/productivity/profile/raw")
    async def productivity_profile_raw() -> dict:
        """Get the raw productivity profile data."""
        return _assistant()._productivity_model.get_profile().model_dump()

    # v2.1: Adaptive Notifications
    @app.get("/notifications/strategy")
    async def notification_strategy() -> dict:
        """Get the current adaptive notification strategy."""
        return _assistant()._notification_manager.get_recommended_strategy()

    @app.get("/notifications/check")
    async def check_notifications() -> dict:
        """Check for deadline notifications that should be delivered now."""
        tasks = repo.list_tasks()
        notifications = _assistant()._notification_manager.check_deadlines(tasks)
        return {
            "notifications": [n.model_dump(mode="json") for n in notifications],
            "count": len(notifications),
        }

    @app.post("/notifications/{notification_id}/action")
    async def record_notification_action(notification_id: str, acted: bool = True) -> dict:
        """Record whether the user acted on a notification."""
        _assistant()._notification_manager.record_action(notification_id, acted)
        return {"status": "ok", "acted": acted}

    # v2.1: Goal → Project → Task hierarchy

    @app.get("/goals")
    async def list_goals(status: str | None = None) -> dict:
        return {"goals": repo.list_goals(status=status)}

    @app.post("/goals")
    async def create_goal(body: dict) -> dict:
        import uuid
        goal_id = body.get("id", str(uuid.uuid4()))
        repo.upsert_goal(
            goal_id=goal_id,
            title=body.get("title", "Untitled Goal"),
            description=body.get("description", ""),
            target_date=body.get("target_date"),
            status=body.get("status", "active"),
        )
        return {"status": "ok", "goal_id": goal_id}

    @app.get("/projects")
    async def list_projects(goal_id: str | None = None) -> dict:
        return {"projects": repo.list_projects(goal_id=goal_id)}

    @app.post("/projects")
    async def create_project(body: dict) -> dict:
        import uuid
        project_id = body.get("id", str(uuid.uuid4()))
        repo.upsert_project(
            project_id=project_id,
            goal_id=body.get("goal_id"),
            title=body.get("title", "Untitled Project"),
            description=body.get("description", ""),
            status=body.get("status", "active"),
        )
        return {"status": "ok", "project_id": project_id}

    @app.post("/tasks/{task_id}/link-project")
    async def link_task_to_project(task_id: str, body: dict) -> dict:
        repo.link_task_to_project(
            task_id=task_id,
            project_id=body.get("project_id"),
            goal_id=body.get("goal_id"),
            subtask_order=body.get("subtask_order"),
        )
        return {"status": "ok"}

    # v2.1: Failure simulation (current vs optimized schedule probability)
    @app.post("/simulation/run")
    async def run_simulation() -> dict:
        return await _assistant().simulate_failure()

    # v2.1: Daily briefing ("Good morning, here's your day")
    @app.get("/briefing")
    async def get_briefing() -> dict:
        return await _assistant().generate_briefing()

    # v2.1: Task decomposition (auto-split complex tasks)
    @app.post("/tasks/{task_id}/decompose")
    async def decompose_task(task_id: str) -> dict:
        """Auto-split a complex task into subtasks."""
        from .agents.decomposer_agent import DecomposerAgent
        task = repo.get_task(task_id)
        if not task:
            raise HTTPException(404, f"Task {task_id} not found")
        gemini = get_gemini_client()
        decomposer = DecomposerAgent(gemini)
        result = await decomposer.decompose(task)
        return result.model_dump(mode="json")

    # v2.1: Memory endpoints (uses EmbeddingsClient for semantic search)
    @app.get("/memories")
    async def list_memories(memory_type: str | None = None, limit: int = 100) -> dict:
        return {"memories": repo.list_memories(memory_type=memory_type, limit=limit)}

    @app.post("/memories")
    async def store_memory(body: dict) -> dict:
        """Store a new memory with embedding."""
        import uuid
        from .contracts.messages import Memory, MemoryType, utcnow
        try:
            memory = Memory(
                id=str(uuid.uuid4()),
                memory_type=MemoryType(body.get("memory_type", "episodic")),
                content=body.get("content", ""),
                metadata=body.get("metadata", {}),
                importance=body.get("importance", 50),
                created_at=utcnow(),
            )
            try:
                embed_client = get_embeddings_client(get_settings())
                memory.embedding = await embed_client.embed(memory.content)
            except Exception as e:
                logger.warning(f"Could not generate embedding: {e}")
            repo.append_memory(memory)
            return {"status": "ok", "memory_id": memory.id}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @app.post("/memories/search")
    async def search_memories(body: dict) -> dict:
        """Semantic search over memories."""
        query = body.get("query", "")
        top_k = body.get("top_k", 10)
        if not query:
            return {"results": []}
        try:
            embed_client = get_embeddings_client(get_settings())
            query_vec = await embed_client.embed(query)
        except Exception as e:
            logger.warning(f"Embedding search failed, falling back to text: {e}")
            return {"results": repo.list_memories(limit=top_k), "fallback": True}
        all_memories = repo.list_memories(limit=500)
        scored = []
        for m in all_memories:
            if m.get("embedding"):
                sim = embed_client.cosine_similarity(query_vec, m["embedding"])
                scored.append((sim, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [m for _, m in scored[:top_k]]
        for m in results:
            repo.increment_memory_access(m["id"])
        return {"results": results}

    # --- Audit trails ---

    @app.get("/proposals")
    async def list_proposals(agent: str | None = None, limit: int = 100) -> dict:
        return {"proposals": repo.list_proposals(agent_name=agent, limit=limit)}

    @app.get("/conflicts")
    async def list_conflicts(limit: int = 100) -> dict:
        return {"conflicts": repo.list_conflicts(limit=limit)}

    # --- Drift ---

    @app.post("/drift")
    async def report_drift(event: DriftEvent) -> dict:
        return await _assistant().report_drift(event)

    # --- Permissions (ProposalManager gate) ---

    @app.get("/permissions/pending")
    async def list_pending_permissions() -> dict:
        """List all pending permission requests awaiting user approval."""
        return {"permissions": _assistant()._proposal_manager.list_pending()}

    @app.post("/permissions/{permission_id}/approve")
    async def approve_permission(permission_id: str) -> dict:
        """Approve a pending permission request and execute the action."""
        assistant = _assistant()
        # Find the pending proposal
        pending = assistant._proposal_manager.list_pending()
        perm = next((p for p in pending if p["permission_id"] == permission_id), None)
        if not perm:
            raise HTTPException(404, "Permission request not found")

        # For now, just mark as approved — the action was already gated
        # In a full implementation, we'd store the executor and call it here
        result = await assistant._proposal_manager.approve(permission_id, approved_by="user")
        return result

    @app.post("/permissions/{permission_id}/deny")
    async def deny_permission(permission_id: str) -> dict:
        """Deny a pending permission request."""
        assistant = _assistant()
        result = await assistant._proposal_manager.deny(permission_id)
        return result

    # --- Auth (Google OAuth web flow) ---

    def _get_current_user(request: Request) -> dict | None:
        """Read the logged-in user from the session cookie. Returns None if not logged in."""
        session = request.scope.get("session", {})
        if not session.get("user_id"):
            return None
        return {
            "id": session.get("user_id"),
            "email": session.get("user_email", ""),
            "name": session.get("user_name", ""),
            "picture": session.get("user_picture", ""),
        }

    def _require_user(request: Request) -> dict:
        """If require_auth is True, return 401 for unauthenticated requests.
        If require_auth is False (desktop), return a None-like user (no enforcement)."""
        if not settings.require_auth:
            return _get_current_user(request) or {"id": "desktop", "email": "", "name": "Desktop", "picture": ""}
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated. Visit /auth/google/login to sign in.")
        return user

    @app.get("/auth/google/login")
    async def google_auth_login(request: Request) -> RedirectResponse:
        """Start the Google OAuth web flow.

        Redirects the user to Google's consent screen. After consent, Google
        redirects back to /auth/google/callback on this backend.

        The redirect_uri is derived from the incoming request's host, so it
        works on any deployment URL. Make sure to register it in the Google
        Cloud Console under APIs & Services → Credentials → OAuth client →
        Authorized redirect URIs.
        """
        auth = GoogleAuth(settings)
        if not auth.is_configured():
            raise HTTPException(500, "Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        # Build the callback URL from the request's base URL
        # (e.g. "https://archy-backend.onrender.com/auth/google/callback")
        redirect_uri = str(request.url.replace(path="/auth/google/callback", query=""))
        # Simple state token for CSRF protection (timestamp-based)
        import time, hashlib
        state = hashlib.sha256(f"{settings.secret_key}:{int(time.time()//300)}".encode()).hexdigest()[:16]
        request.session["oauth_state"] = state
        request.session["oauth_redirect_uri"] = redirect_uri
        auth_url = auth.get_auth_url(redirect_uri=redirect_uri, state=state)
        logger.info(f"Starting Google OAuth flow, redirect_uri={redirect_uri}")
        return RedirectResponse(url=auth_url)

    @app.get("/auth/google/callback")
    async def google_auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Google OAuth callback — exchanges the auth code for tokens, logs the user in.

        After successful login, redirects to the frontend URL (settings.frontend_url).
        On failure, redirects to the frontend with ?auth_error=xxx.
        """
        frontend = settings.frontend_url.rstrip("/")
        if error:
            logger.warning(f"Google OAuth error: {error}")
            return RedirectResponse(url=f"{frontend}/?auth_error={error}")
        if not code:
            return RedirectResponse(url=f"{frontend}/?auth_error=no_code")

        # Verify state (CSRF protection)
        expected_state = request.session.pop("oauth_state", "")
        redirect_uri = request.session.pop("oauth_redirect_uri", "")
        if state and expected_state and state != expected_state:
            logger.warning(f"OAuth state mismatch: expected={expected_state}, got={state}")
            return RedirectResponse(url=f"{frontend}/?auth_error=state_mismatch")

        try:
            auth = GoogleAuth(settings)
            creds = auth.exchange_code(code=code, redirect_uri=redirect_uri)
            user_info = auth.get_user_info(creds)
            # Upsert user in DB
            user = repo.upsert_user(
                google_id=user_info.get("google_id", ""),
                email=user_info.get("email", ""),
                name=user_info.get("name", ""),
                picture=user_info.get("picture", ""),
            )
            # Store minimal user info in the session cookie
            request.session["user_id"] = user["id"]
            request.session["user_email"] = user["email"]
            request.session["user_name"] = user["name"]
            request.session["user_picture"] = user["picture"]
            logger.info(f"User logged in: {user['email']} (id={user['id'][:8]}...)")
            return RedirectResponse(url=f"{frontend}/?auth_success=1")
        except Exception as e:
            logger.error(f"Google OAuth callback failed: {e}")
            return RedirectResponse(url=f"{frontend}/?auth_error={str(e)[:200]}")

    @app.get("/auth/me")
    async def auth_me(request: Request) -> dict:
        """Return the current logged-in user, or null if not authenticated."""
        user = _get_current_user(request)
        if not user:
            return {"user": None, "require_auth": settings.require_auth}
        return {"user": user, "require_auth": settings.require_auth}

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> dict:
        """Clear the session cookie — logs the user out."""
        request.session.clear()
        return {"status": "logged_out"}

    # Legacy endpoint kept for backward compatibility with the old frontend
    # (which called POST /auth/google). New code should use GET /auth/google/login.
    @app.post("/auth/google")
    async def google_auth_legacy() -> dict:
        return {
            "message": "Use GET /auth/google/login to start the OAuth flow.",
            "login_url": "/auth/google/login",
        }

    @app.post("/calendar/sync")
    async def sync_calendar() -> dict:
        """Manually trigger calendar sync."""
        return await _assistant().replan()

    @app.post("/brief/daily")
    async def generate_daily_brief() -> dict:
        """Generate a daily brief Google Doc."""
        return await _assistant().generate_daily_brief()

    # --- WebSocket ---

    @app.websocket("/events")
    async def events_ws(ws: WebSocket) -> None:
        await ws.accept()
        q = await hub.subscribe()
        try:
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg, default=str))
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        finally:
            await hub.unsubscribe(q)

    return app


# For `uvicorn archy.server:app`
app = create_app()
