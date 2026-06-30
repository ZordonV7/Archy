"""Archy — the manager, supervisor, and interpreter.

Responsibilities:
1. Route data between agents (STT → Classifier → [Planner ‖ Mood])
2. Evaluate every agent proposal via `relevancy_score`
3. Veto proposals below threshold; emit ConflictEvents
4. Negotiate with agents (up to `max_negotiation_rounds`)
5. Pass accepted proposals to the rule-based core (final decider)
6. Synthesize user-facing speech via the Interpreter
7. Emit WebSocket events at every step

Invariant: Archy is the ONLY entity that produces natural language for
the user. Agents emit structured JSON only.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from ..agents.classifier_agent import ClassifierAgent
from ..agents.mood_agent import MoodAgent
from ..agents.planner_agent import PlannerAgent
from ..agents.stt_agent import STTAgent
from ..config import Settings
from ..contracts.messages import (
    AgentProposal,
    ClassificationPayload,
    ConflictEvent,
    ConflictResolution,
    DriftEvent,
    MoodAnalysisPayload,
    PlannerPayload,
    ArchyEvaluation,
    Proposal,
    ProposalDecision,
    ProposalStatus,
    RiskLevel,
    Task,
    TaskStatus,
    UserFacingMessage,
    WorkSession,
    SessionStatus,
    utcnow,
)
from ..core.mood_engine import MoodEngine
from ..core.storage import Repository
from ..gemini.client import GeminiClient
from .interpreter import Interpreter
from .persona import PERSONA_PROMPT


EventCallback = Callable[[str, dict], Awaitable[None]]


class Archy:
    """The manager. Wires agents, evaluates proposals, supervises, interprets."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        gemini_client: GeminiClient,
    ):
        self.settings = settings
        self.repo = repo
        self._client = gemini_client

        # Model Router — distributes load across Gemini tiers based on complexity
        from ..gemini.router import ModelRouter
        self._router = ModelRouter(settings)

        # Agents (all share the same Gemini client, different prompts + models)
        self._stt = STTAgent(gemini_client)
        self._classifier = ClassifierAgent(gemini_client)
        self._planner = PlannerAgent(gemini_client, model_router=self._router)
        self._mood = MoodAgent(gemini_client)

        # Rule-based deciders
        self._mood_engine = MoodEngine(settings, repo)

        # v2.1: Energy engine (independent from mood)
        from ..core.energy_engine import EnergyEngine
        self._energy_engine = EnergyEngine(settings, repo)

        # v2.1: Policy engine (formal constraint checks)
        from ..core.policy_engine import PolicyEngine
        self._policy_engine = PolicyEngine(settings, repo, self._mood_engine, self._energy_engine)

        # v2.1: Application layer
        from ..application.context_builder import ContextBuilder
        from ..application.scheduler_v2 import SchedulerV2
        from ..application.proposal_manager import ProposalManager
        from ..application.event_bus import EventBus
        from ..core.scheduler_trainer import SchedulerTrainer
        self._context_builder = ContextBuilder(settings, repo)
        self._scheduler_v2 = SchedulerV2(settings, self._mood_engine, self._energy_engine)
        self._proposal_manager = ProposalManager(repo, self._policy_engine)
        self._scheduler_trainer = SchedulerTrainer(repo)

        # Register executor factories for the actions that go through the
        # ProposalManager. These factories let the manager rehydrate executors
        # after a process restart (action name is persisted in the DB; factory
        # is looked up by name on restore_pending()).
        # Each factory receives the proposal's `parameters` dict and returns
        # an async callable that takes the same dict and performs the action.
        self._proposal_manager.register_executor(
            "task.cancel",
            lambda params: self._execute_task_cancel,
        )
        self._proposal_manager.register_executor(
            "docs.create_note",
            lambda params: self._execute_daily_brief,
        )
        # Restore any pending approvals from a previous run.
        # (Executors are rehydrated via the factories above.)
        try:
            restored = self._proposal_manager.restore_pending()
            if restored:
                logger.info(f"Restored {restored} pending permission requests from previous run")
        except Exception as e:
            logger.warning(f"Could not restore pending permissions: {e}")

        # Wire EmbeddingsClient into ContextBuilder for semantic memory search.
        # In offline mode, this returns an OfflineEmbeddingsClient (hash-based
        # pseudo-vectors — enough to keep the ContextBuilder from crashing).
        try:
            from ..gemini.embeddings import get_embeddings_client
            self._embeddings_client = get_embeddings_client(settings)
            self._context_builder = ContextBuilder(settings, repo, embeddings_client=self._embeddings_client)
        except Exception:
            self._embeddings_client = None

        # v2.1: Unified EventBus — THE single place all business events flow through.
        # Internal backend systems subscribe here.
        # Server's EventHub also subscribes here (for WebSocket → frontend).
        self._event_bus = EventBus()

        # v2.1: Risk engine
        from ..core.risk_engine import RiskEngine
        self._risk_engine = RiskEngine(settings, repo, self._scheduler_trainer)

        # v2.1: Failure simulator
        from ..core.failure_simulator import FailureSimulator
        self._simulator = FailureSimulator(settings, repo, self._risk_engine, self._scheduler_trainer)

        # v2.1: Dynamic Risk Monitor — subscribes to task lifecycle events
        from ..core.dynamic_risk_monitor import DynamicRiskMonitor
        self._risk_monitor = DynamicRiskMonitor(settings, repo, self._risk_engine)
        self._event_bus.subscribe(self._risk_monitor, event_types=[
            "TaskStarted", "TaskCompleted", "TaskCancelled", "TaskMissed",
            "TaskScheduled", "PlanReady", "ConflictDetected",
        ])

        # v2.1: Personal Productivity Model — subscribes to completions + sessions
        from ..core.productivity_model import PersonalProductivityModel
        self._productivity_model = PersonalProductivityModel(repo)
        self._event_bus.subscribe(self._productivity_model, event_types=[
            "TaskCompleted", "TaskMissed",
        ])

        # v2.1: Invalidate ContextBuilder caches on task/mood/energy events
        # so the next build() reflects fresh state. Without this, the 30s TTL
        # on task_context would serve stale data right after a TaskCreated event.
        async def _invalidate_context(event_type: str, payload: dict) -> None:
            try:
                if event_type in ("TaskCreated", "TaskScheduled", "TaskCompleted",
                                  "TaskCancelled", "TaskMissed", "PlanReady"):
                    self._context_builder.invalidate("task_context")
                    self._context_builder.invalidate("conflict_context")
                elif event_type in ("MoodChanged", "MoodAnalyzed"):
                    self._context_builder.invalidate("mood_context")
                elif event_type in ("EnergyChanged",):
                    self._context_builder.invalidate("energy_context")
                elif event_type == "ConflictDetected":
                    self._context_builder.invalidate("conflict_context")
            except Exception as e:
                logger.debug(f"ContextBuilder invalidation skipped: {e}")
        self._event_bus.subscribe(_invalidate_context, event_types=[
            "TaskCreated", "TaskScheduled", "TaskCompleted", "TaskCancelled",
            "TaskMissed", "PlanReady", "MoodChanged", "MoodAnalyzed",
            "EnergyChanged", "ConflictDetected",
        ])

        # v2.1: Adaptive Notification Manager
        from ..core.adaptive_notifications import AdaptiveNotificationManager
        self._notification_manager = AdaptiveNotificationManager(settings, repo, self._productivity_model)

        # v2.1: Decomposer agent — used via /tasks/{id}/decompose endpoint
        # NOT stored as instance — endpoint creates fresh instances per call

        # v2.1: Active work session (Execution Mode)
        self._active_session: WorkSession | None = None

        # Interpreter (Archy's voice)
        self._interpreter = Interpreter(gemini_client)

        # Optional Google Calendar subscriber — registers on EventBus with filtered events
        if settings.calendar_sync_enabled:
            try:
                from ..integrations.auth import GoogleAuth
                from ..integrations.calendar_client import CalendarClient
                from ..integrations.calendar_subscriber import CalendarSubscriber

                self._google_auth = GoogleAuth(settings)
                self._calendar_client = CalendarClient(self._google_auth)
                self._calendar_subscriber = CalendarSubscriber(
                    settings, repo, self._google_auth, self._calendar_client
                )
                # Subscribe to specific events on EventBus
                self._event_bus.subscribe(self._calendar_subscriber, event_types=[
                    "PlanReady", "TaskCompleted", "TaskCancelled", "TaskMissed",
                ])
                logger.info("Google Calendar sync enabled and subscribed on EventBus")
            except ImportError:
                logger.warning(
                    "Calendar sync enabled but google deps not installed. "
                    "Run: pip install google-auth-oauthlib google-api-python-client"
                )
                self._calendar_subscriber = None
        else:
            self._calendar_subscriber = None

    # --- Event system (unified through EventBus) ---

    def subscribe(self, cb: EventCallback, event_types: list[str] | None = None) -> None:
        """Subscribe to business events on the EventBus.
        
        Internal backend systems (RiskMonitor, ProductivityModel, CalendarSubscriber)
        subscribe with filtered event types.
        
        The server's EventHub subscribes without a filter to receive ALL events
        for WebSocket → frontend.
        """
        self._event_bus.subscribe(cb, event_types=event_types)

    async def _emit(self, event_type: str, payload: dict) -> None:
        """Publish a business event through the EventBus.
        
        All subscribers (internal + frontend) receive it in one place.
        After significant task events, also emits RiskChanged.
        """
        # Publish to EventBus — fans out to all subscribers
        # (RiskMonitor, ProductivityModel, CalendarSubscriber, EventHub→WebSocket)
        await self._event_bus.publish(event_type, payload)

        # After task lifecycle events, recalculate risk and emit RiskChanged
        if event_type in ("TaskStarted", "TaskCompleted", "TaskCancelled", "TaskMissed",
                          "TaskScheduled", "PlanReady", "ConflictDetected"):
            try:
                risk_data = self._risk_monitor.get_current_risk()
                await self._event_bus.publish("RiskChanged", risk_data)
            except Exception:
                pass

    # --- Ingest paths ---

    async def ingest_audio(self, audio_bytes: bytes, filename: str, mime: str) -> Task:
        logger.info(f"Archy ingesting audio: {filename} ({len(audio_bytes)} bytes)")
        await self._emit("IngestStarted", {"filename": filename, "bytes": len(audio_bytes)})

        # 1. STT
        stt_proposal = await self._run_with_supervision(
            self._stt, audio_bytes=audio_bytes, mime=mime
        )
        transcript = stt_proposal.payload["transcript"]
        await self._emit("TranscriptReady", {"text": transcript})

        return await self._classify_and_plan(transcript)

    async def ingest_text(self, text: str) -> Task:
        logger.info(f"Archy ingesting text ({len(text)} chars)")
        await self._emit("IngestStarted", {"text": text, "source": "text"})
        return await self._classify_and_plan(text)

    async def _classify_and_plan(self, transcript: str) -> Task:
        # 2. Classifier — single pass (no negotiation unless PolicyEngine rejects)
        cls_proposal = await self._run_single_pass(
            self._classifier, text=transcript
        )
        cls_payload = ClassificationPayload.model_validate(cls_proposal.payload)
        await self._emit("ClassificationReady", cls_payload.model_dump())

        # 3. Create Task + persist
        deadline = (
            datetime.fromisoformat(cls_payload.deadline_iso)
            if cls_payload.deadline_iso
            else None
        )
        task = Task(
            id=str(uuid.uuid4()),
            raw_transcript=transcript,
            title=cls_payload.title,
            category=cls_payload.category,
            priority=cls_payload.priority,
            complexity=cls_payload.complexity,
            deadline=deadline,
            duration_minutes=cls_payload.duration_minutes,
            reasoning=cls_payload.reasoning,
            status=TaskStatus.PENDING,
        )
        self.repo.upsert_task(task)
        await self._emit("TaskCreated", task.model_dump(mode="json"))

        # 4. Run Planner + Mood — single pass for simple tasks, escalate only on conflict
        await self.replan()
        return task

    # --- Replan (runs Planner ‖ Mood, then commits via SchedulerV2) ---

    async def replan(self) -> dict:
        tasks = self.repo.list_schedulable_tasks()

        # Build the 7-layer AgentContext once per replan. This gives agents
        # access to current mood/energy, recent conflicts, user preferences,
        # and (if embeddings are available) semantically retrieved memories.
        # Agents that don't use the context ignore it (they accept **_).
        try:
            agent_context = await self._context_builder.build()
        except Exception as e:
            logger.warning(f"ContextBuilder.build() failed, falling back to no context: {e}")
            agent_context = None

        # Run MoodAgent in parallel with PlannerAgent (single-pass fast path)
        mood_task = self._run_single_pass(self._mood, tasks=tasks, agent_context=agent_context)
        planner_task = self._run_single_pass(self._planner, tasks=tasks, agent_context=agent_context)

        mood_proposal, planner_proposal = await asyncio.gather(mood_task, planner_task)

        mood_payload = MoodAnalysisPayload.model_validate(mood_proposal.payload)
        planner_payload = PlannerPayload.model_validate(planner_proposal.payload)
        await self._emit("MoodAnalyzed", mood_payload.model_dump())
        await self._emit("PlannerAdvisory", planner_payload.model_dump())

        # v2.1: Commit schedule via SchedulerV2 (mood+energy+context aware)
        preferred_order = _extract_preferred_order(planner_payload)
        plan = await self._scheduler_v2.plan(tasks, preferred_order=preferred_order)

        # Apply slots to tasks + persist
        slot_by_id = {s.task_id: s for s in plan.slots if not s.is_break}
        for t in tasks:
            slot = slot_by_id.get(t.id)
            if slot:
                t.scheduled_start = slot.start
                t.scheduled_end = slot.end
                if t.status == TaskStatus.PENDING:
                    t.status = TaskStatus.SCHEDULED
                self.repo.upsert_task(t)
                self._mood_engine.on_task_scheduled(t)
                await self._emit("TaskScheduled", t.model_dump(mode="json"))
            elif t.id in plan.unscheduled_task_ids:
                t.status = TaskStatus.MISSED
                self.repo.upsert_task(t)
                self._mood_engine.on_task_missed(t)
                await self._emit("TaskMissed", t.model_dump(mode="json"))

        score, label, reasons = self._mood_engine.current()
        energy = self._energy_engine.current()
        await self._emit(
            "PlanReady",
            {
                "slots": [s.model_dump(mode="json") for s in plan.slots],
                "unscheduled": plan.unscheduled_task_ids,
                "at_risk": plan.at_risk_task_ids,
                "mood": {"score": score, "label": label, "reasons": reasons},
                "energy": {"score": energy.score, "label": energy.label.value},
                "breaks_inserted": plan.breaks_inserted,
                "context_switches": plan.context_switches,
            },
        )

        # Synthesize Archy's user-facing message
        msg = await self._interpreter.synthesize(
            event_type="PlanReady",
            mood_label=label,
            mood_score=score,
            payload={
                "next_task": (
                    plan.slots[0].model_dump(mode="json") if plan.slots else None
                ),
                "at_risk_count": len(plan.at_risk_task_ids),
                "mood_recommendations": mood_payload.recommendations,
                "energy_label": energy.label.value,
            },
            conflict_happened=any(
                p.constraints_used.get("_feedback") for p in [planner_proposal, mood_proposal]
            ),
        )
        ufm = UserFacingMessage(text=msg, mood_label=label)
        await self._emit("ArchyMessage", ufm.model_dump(mode="json"))

        # v2.1: Assess deadline risk for all scheduled tasks
        assessments = self._risk_engine.assess_all(tasks)
        if assessments:
            risk_data = [a.model_dump(mode="json") for a in assessments]
            await self._emit("RiskAssessed", {"assessments": risk_data})
            # If any task is high-risk, Archy mentions it
            high_risk = [a for a in assessments if a.risk_score >= 60]
            if high_risk:
                top_risk = high_risk[0]
                risk_msg = await self._interpreter.synthesize(
                    event_type="risk_alert",
                    mood_label=label,
                    mood_score=score,
                    payload={
                        "task_title": top_risk.task_title,
                        "risk_score": top_risk.risk_score,
                        "hours_needed": top_risk.hours_needed,
                        "hours_available": top_risk.hours_available,
                        "recovery_actions": top_risk.recovery_plan.actions if top_risk.recovery_plan else [],
                    },
                )
                await self._emit("ArchyMessage", {"text": risk_msg, "mood_label": label, "type": "risk_alert"})

        return {
            "slots": [s.model_dump(mode="json") for s in plan.slots],
            "unscheduled": plan.unscheduled_task_ids,
            "mood": {"score": score, "label": label, "reasons": reasons},
            "energy": {"score": energy.score, "label": energy.label.value},
            "breaks_inserted": plan.breaks_inserted,
            "context_switches": plan.context_switches,
            "assistant_message": msg,
        }

    # --- Lifecycle updates ---

    async def start_task(self, task_id: str) -> Task:
        t = self.repo.get_task(task_id)
        if not t:
            raise KeyError(f"Task {task_id} not found")
        t.status = TaskStatus.IN_PROGRESS
        t.started_at = utcnow()
        self.repo.upsert_task(t)
        # v2.1: deplete energy when starting a task
        energy = self._energy_engine.on_task_started(t)
        await self._emit("EnergyChanged", energy.model_dump(mode="json"))
        await self._emit("TaskStarted", t.model_dump(mode="json"))

        # v2.1: Start a work session (Execution Mode)
        session = WorkSession(
            session_id=str(uuid.uuid4()),
            task_id=t.id,
            task_title=t.title,
            estimated_minutes=t.duration_minutes,
        )
        self._active_session = session
        self.repo.upsert_session(session)
        await self._emit("SessionStarted", session.model_dump(mode="json"))
        logger.info(f"Work session started: '{t.title}' (estimated {t.duration_minutes}min)")
        return t

    async def pause_session(self) -> dict:
        """Pause the active work session (counts as an interruption)."""
        if not self._active_session:
            return {"error": "No active session"}
        s = self._active_session
        if s.status != SessionStatus.ACTIVE:
            return {"error": f"Session is {s.status.value}, cannot pause"}
        s.status = SessionStatus.PAUSED
        s.paused_at = utcnow()
        s.interruption_count += 1
        self.repo.upsert_session(s)
        await self._emit("SessionPaused", s.model_dump(mode="json"))
        logger.info(f"Session paused: '{s.task_title}' (interruption #{s.interruption_count})")
        return s.model_dump(mode="json")

    async def resume_session(self) -> dict:
        """Resume a paused work session."""
        if not self._active_session:
            return {"error": "No active session"}
        s = self._active_session
        if s.status != SessionStatus.PAUSED:
            return {"error": f"Session is {s.status.value}, cannot resume"}
        if s.paused_at:
            s.total_paused_seconds += int((utcnow() - s.paused_at).total_seconds())
        s.paused_at = None
        s.status = SessionStatus.ACTIVE
        self.repo.upsert_session(s)
        await self._emit("SessionResumed", s.model_dump(mode="json"))
        logger.info(f"Session resumed: '{s.task_title}'")
        return s.model_dump(mode="json")

    async def end_session(self, completion_percentage: int = 100, complete_task: bool = True) -> dict:
        """End the active work session.

        Records actual time, feeds data to SchedulerTrainer, optionally completes the task.
        """
        if not self._active_session:
            return {"error": "No active session"}
        s = self._active_session
        s.ended_at = utcnow()
        s.status = SessionStatus.COMPLETED
        s.completion_percentage = max(0, min(100, completion_percentage))

        # Calculate actual work time (excluding pauses)
        total_seconds = (s.ended_at - s.started_at).total_seconds()
        work_seconds = total_seconds - s.total_paused_seconds
        if s.paused_at:  # was still paused when ended
            work_seconds -= (s.ended_at - s.paused_at).total_seconds()
        s.actual_minutes = round(max(0, work_seconds / 60.0), 1)

        self.repo.upsert_session(s)
        await self._emit("SessionEnded", s.model_dump(mode="json"))

        # Feed analytics to SchedulerTrainer
        task = self.repo.get_task(s.task_id)
        if task:
            self._scheduler_trainer.record_completion(
                task, predicted_minutes=s.estimated_minutes, actual_minutes=int(s.actual_minutes)
            )
            # v2.1: Also feed to PersonalProductivityModel — updates dimensions
            # 2 (focus duration), 3 (context-switch cost), 5 (interruption tolerance).
            # We need the previous task's category to detect context switches;
            # look it up from the most recent prior session.
            try:
                prior_sessions = self.repo.list_sessions(limit=5)
                prev_cat = None
                for ps in prior_sessions:
                    if ps.get("task_id") and ps.get("task_id") != s.task_id:
                        prev_task = self.repo.get_task(ps["task_id"])
                        if prev_task:
                            prev_cat = prev_task.category
                            break
                self._productivity_model.record_session(
                    task=task,
                    actual_minutes=int(s.actual_minutes),
                    interruptions=s.interruption_count,
                    prev_category=prev_cat,
                )
            except Exception as e:
                logger.debug(f"record_session skipped: {e}")

        logger.info(
            f"Session ended: '{s.task_title}' — estimated {s.estimated_minutes}min, "
            f"actual {s.actual_minutes}min, {s.interruption_count} interruptions"
        )

        # Optionally complete the task
        if complete_task and task:
            await self.complete_task(s.task_id)

        self._active_session = None
        return s.model_dump(mode="json")

    async def get_active_session(self) -> dict | None:
        """Get the current active session."""
        if self._active_session:
            s = self._active_session
            # Compute elapsed time on the fly
            elapsed = (utcnow() - s.started_at).total_seconds() - s.total_paused_seconds
            if s.paused_at:
                elapsed -= (utcnow() - s.paused_at).total_seconds()
            result = s.model_dump(mode="json")
            result["elapsed_seconds"] = max(0, int(elapsed))
            result["elapsed_minutes"] = round(max(0, elapsed / 60), 1)
            result["progress"] = min(100, int((max(0, elapsed) / 60) / s.estimated_minutes * 100)) if s.estimated_minutes > 0 else 0
            return result
        return None

    async def get_session_history(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        """Get session history (for analytics)."""
        return self.repo.list_sessions(task_id=task_id, limit=limit)

    async def get_session_analytics(self) -> dict:
        """Compute analytics from past work sessions.

        Shows:
        - Completion rate
        - Average interruption count
        - Duration accuracy (estimated vs actual)
        - Per-category breakdown
        - Most/least productive hours
        """
        sessions = self.repo.list_sessions(limit=100)
        completed = [s for s in sessions if s.get("status") == "completed"]

        if not completed:
            return {
                "total_sessions": 0,
                "completion_rate": 0,
                "avg_interruptions": 0,
                "avg_estimated": 0,
                "avg_actual": 0,
                "duration_accuracy": 0,
                "category_stats": {},
                "productivity_by_hour": {},
                "message": "No completed sessions yet. Start a task to begin collecting analytics.",
            }

        total = len(sessions)
        comp_count = len(completed)
        completion_rate = int((comp_count / total) * 100) if total > 0 else 0

        # Interruptions
        total_interruptions = sum(s.get("interruption_count", 0) for s in completed)
        avg_interruptions = round(total_interruptions / comp_count, 1)

        # Duration accuracy
        total_est = sum(s.get("estimated_minutes", 0) for s in completed)
        total_act = sum(s.get("actual_minutes", 0) for s in completed)
        avg_est = round(total_est / comp_count, 1)
        avg_act = round(total_act / comp_count, 1)
        # accuracy = 100% if actual == estimated, lower if over/under
        if avg_est > 0:
            duration_accuracy = max(0, 100 - int(abs(avg_act - avg_est) / avg_est * 100))
        else:
            duration_accuracy = 0

        # Per-category breakdown
        category_stats: dict[str, dict] = {}
        for s in completed:
            # We don't have category in session directly, but can look up task
            task = self.repo.get_task(s.get("task_id", ""))
            cat = task.category if task else "general"
            if cat not in category_stats:
                category_stats[cat] = {
                    "sessions": 0,
                    "total_estimated": 0,
                    "total_actual": 0,
                    "total_interruptions": 0,
                }
            cs = category_stats[cat]
            cs["sessions"] += 1
            cs["total_estimated"] += s.get("estimated_minutes", 0)
            cs["total_actual"] += s.get("actual_minutes", 0)
            cs["total_interruptions"] += s.get("interruption_count", 0)

        # Compute per-category averages + accuracy
        for cat, cs in category_stats.items():
            if cs["sessions"] > 0:
                cs["avg_estimated"] = round(cs["total_estimated"] / cs["sessions"], 1)
                cs["avg_actual"] = round(cs["total_actual"] / cs["sessions"], 1)
                cs["avg_interruptions"] = round(cs["total_interruptions"] / cs["sessions"], 1)
                if cs["avg_estimated"] > 0:
                    cs["accuracy"] = max(0, 100 - int(abs(cs["avg_actual"] - cs["avg_estimated"]) / cs["avg_estimated"] * 100))
                else:
                    cs["accuracy"] = 0

        # Productivity by hour (which hours have most completions)
        from collections import Counter
        from datetime import datetime as _dt
        hour_counts: Counter = Counter()
        for s in completed:
            try:
                started = _dt.fromisoformat(s.get("started_at", ""))
                hour_counts[started.hour] += 1
            except Exception:
                pass

        productivity_by_hour = dict(hour_counts.most_common(5))

        # Best and worst hours
        best_hour = max(productivity_by_hour, key=productivity_by_hour.get) if productivity_by_hour else None

        return {
            "total_sessions": total,
            "completed_sessions": comp_count,
            "completion_rate": completion_rate,
            "avg_interruptions": avg_interruptions,
            "avg_estimated": avg_est,
            "avg_actual": avg_act,
            "duration_accuracy": duration_accuracy,
            "category_stats": category_stats,
            "productivity_by_hour": productivity_by_hour,
            "best_hour": best_hour,
            "message": None,
        }

    async def complete_task(self, task_id: str) -> Task:
        t = self.repo.get_task(task_id)
        if not t:
            raise KeyError(f"Task {task_id} not found")
        t.status = TaskStatus.DONE
        t.completed_at = utcnow()
        self.repo.upsert_task(t)
        # v2.1: Scheduler learning — record actual vs predicted duration
        if t.started_at and t.completed_at:
            try:
                from datetime import timezone
                start = t.started_at
                end = t.completed_at
                if start.tzinfo is None and end.tzinfo is not None:
                    end = end.replace(tzinfo=None)
                elif start.tzinfo is not None and end.tzinfo is None:
                    start = start.replace(tzinfo=None)
                actual_minutes = int((end - start).total_seconds() / 60)
                self._scheduler_trainer.record_completion(
                    t, predicted_minutes=t.duration_minutes, actual_minutes=actual_minutes
                )
            except:
                pass
        # Completing a task NEVER depletes mood — always positive or neutral
        self._mood_engine.on_task_completed(t)
        energy = self._energy_engine.on_task_completed(t)

        # v2.1: Feed the Personal Productivity Model
        actual_min = 0
        if t.started_at and t.completed_at:
            try:
                from datetime import timezone
                start = t.started_at
                end = t.completed_at
                if start.tzinfo is None and end.tzinfo is not None:
                    end = end.replace(tzinfo=None)
                elif start.tzinfo is not None and end.tzinfo is None:
                    start = start.replace(tzinfo=None)
                actual_min = int((end - start).total_seconds() / 60)
            except:
                pass
        session_interruptions = self._active_session.interruption_count if self._active_session else 0
        self._productivity_model.record_completion(t, actual_min, session_interruptions)
        await self._emit("EnergyChanged", energy.model_dump(mode="json"))
        score, label, _ = self._mood_engine.current()
        await self._emit("MoodChanged", {"score": score, "label": label, "reasons": []})
        await self._emit("TaskCompleted", t.model_dump(mode="json"))

        msg = await self._interpreter.synthesize(
            event_type="TaskCompleted",
            mood_label=label,
            mood_score=score,
            payload={"task_title": t.title, "energy": energy.score},
        )
        await self._emit("ArchyMessage", {"text": msg, "mood_label": label})

        await self.replan()
        return t

    async def cancel_task(self, task_id: str) -> Task | dict:
        """Request task cancellation through the ProposalManager.

        If the proposal is auto-approved (LOW risk) or the user has pre-approved
        task.cancel via a registered executor, the cancellation runs immediately.
        Otherwise returns `{"pending_approval": True, ...}` and the actual
        cancellation runs later when the user calls /permissions/{id}/approve.
        """
        t = self.repo.get_task(task_id)
        if not t:
            raise KeyError(f"Task {task_id} not found")

        # --- ProposalManager gate ---
        # The executor is registered in __init__ as `self._execute_task_cancel`.
        # If approved immediately, ProposalManager._execute calls it directly.
        # If pending_approval, it stays attached to the permission and is called
        # when the user approves via /permissions/{id}/approve.
        result = await self._proposal_manager.submit(
            intent=f"Cancel task '{t.title}'",
            action="task.cancel",
            parameters={"task_id": task_id},
            task=t,
            confidence=90,
            reasoning="User explicitly requested cancellation.",
        )
        if result.get("status") == "pending_approval":
            await self._emit("PermissionRequested", result)
            return {"pending_approval": True, **result}  # type: ignore
        if result.get("status") == "rejected":
            return {"rejected": True, **result}  # type: ignore
        if result.get("status") == "needs_revision":
            return {"needs_revision": True, **result}  # type: ignore
        # status == "executed" or "approved_no_executor"
        # The executor already ran the cancellation; just return the task.
        return self.repo.get_task(task_id) or t

    async def _execute_task_cancel(self, parameters: dict) -> dict:
        """Executor for the `task.cancel` action — invoked by ProposalManager.

        Performs the actual cancellation side effects: status change, mood
        depletion, scheduler learning, event emission, replan.
        """
        task_id = parameters.get("task_id")
        if not task_id:
            raise ValueError("task.cancel executor: missing task_id parameter")
        t = self.repo.get_task(task_id)
        if not t:
            raise KeyError(f"Task {task_id} not found")
        t.status = TaskStatus.CANCELLED
        self.repo.upsert_task(t)
        # Cancel DOES deplete mood (user gave up on the task)
        self._mood_engine.on_task_cancelled(t)
        # Scheduler learning — record postponement
        self._scheduler_trainer.record_postponement(t, reason="user_cancelled")
        await self._emit("TaskCancelled", t.model_dump(mode="json"))
        await self.replan()
        return {"cancelled": True, "task_id": task_id}

    async def report_drift(self, event: DriftEvent) -> dict:
        await self._emit("ConflictDetected", event.model_dump(mode="json"))
        self._mood_engine.on_drift(event)
        # v2.1: drift depletes energy too
        energy = self._energy_engine.on_drift(event)
        await self._emit("EnergyChanged", energy.model_dump(mode="json"))
        # v2.1: feed drift to productivity model — lowers recovery_rate dimension
        # (more frequent drifts => user recovers slower from setbacks)
        try:
            self._productivity_model.record_drift(event.minutes_lost)
        except Exception as e:
            logger.debug(f"record_drift skipped: {e}")
        score, label, _ = self._mood_engine.current()

        msg = await self._interpreter.synthesize(
            event_type="ConflictDetected",
            mood_label=label,
            mood_score=score,
            payload={
                "drift_kind": event.kind,
                "minutes_lost": event.minutes_lost,
                "description": event.description,
            },
        )
        await self._emit("ArchyMessage", {"text": msg, "mood_label": label})
        await self.replan()
        return {"mood": {"score": score, "label": label}, "assistant_message": msg}

    async def current_mood(self) -> dict:
        score, label, reasons = self._mood_engine.current()
        return {"score": score, "label": label, "reasons": reasons}

    async def current_energy(self) -> dict:
        """v2.1: Return current energy snapshot."""
        snap = self._energy_engine.current()
        return snap.model_dump(mode="json")

    async def current_risk(self) -> dict:
        """v2.1: Return current dynamic risk + trajectory."""
        return self._risk_monitor.get_current_risk()

    # --- Risk assessment (the "Life Saver" feature) ---

    async def assess_risks(self) -> list[dict]:
        """Assess deadline risk for all scheduled tasks.

        Returns assessments sorted by risk (highest first).
        """
        tasks = self.repo.list_tasks()
        assessments = self._risk_engine.assess_all(tasks)
        result = [a.model_dump(mode="json") for a in assessments]
        if result:
            await self._emit("RiskAssessed", {"assessments": result})
        return result

    async def simulate_failure(self) -> dict:
        """Run a failure simulation — current vs optimized schedule probability.

        This is the signature "Life Saver" feature:
        - Shows current success probability
        - Generates optimized schedule
        - Shows improved probability
        - Lists concrete changes
        """
        tasks = self.repo.list_tasks()
        scheduled = [t for t in tasks if t.status in (TaskStatus.SCHEDULED, TaskStatus.PENDING) and t.deadline]

        if not scheduled:
            return {
                "current_probability": 100,
                "optimized_probability": 100,
                "improvement": 0,
                "summary": "No tasks with deadlines — nothing to simulate.",
                "tasks_analyzed": 0,
                "changes": [],
            }

        result = self._simulator.run(scheduled)
        await self._emit("SimulationComplete", result.model_dump(mode="json"))
        return result.model_dump(mode="json")

    # --- Adaptive learning insights ---

    async def get_insights(self) -> dict:
        """Generate human-readable insights from learned data.

        Shows what Archy has learned about the user's patterns.
        """
        params = self._scheduler_trainer.get_learned_params()
        insights: list[str] = []

        # Duration adjustments
        for cat, mult in params.get("duration_adjustments", {}).items():
            if mult > 1.15:
                pct = int((mult - 1) * 100)
                insights.append(f"✓ You underestimate {cat} tasks by {pct}%.")
            elif mult < 0.9:
                pct = int((1 - mult) * 100)
                insights.append(f"✓ You overestimate {cat} tasks by {pct}%.")

        # Focus windows
        focus_windows = params.get("focus_windows", {})
        if focus_windows:
            best_hour = max(focus_windows, key=focus_windows.get)
            best_rate = focus_windows[best_hour]
            if best_rate > 0.6:
                insights.append(f"✓ You're most productive around {best_hour}:00 ({int(best_rate * 100)}% completion rate).")
            worst_hour = min(focus_windows, key=focus_windows.get)
            worst_rate = focus_windows[worst_hour]
            if worst_rate < 0.4:
                insights.append(f"✓ You struggle with tasks scheduled around {worst_hour}:00 ({int(worst_rate * 100)}% completion rate).")

        # Postponement patterns
        for cat, rate in params.get("postponement_rates", {}).items():
            if rate > 0.4:
                insights.append(f"✓ You postpone {cat} tasks {int(rate * 100)}% of the time.")

        # Sample counts
        counts = params.get("sample_counts", {})
        total = counts.get("completions", 0) + counts.get("postponements", 0)
        insights.append(f"✓ Learned from {total} task(s) so far.")

        return {
            "insights": insights,
            "learned_params": params,
        }

    # --- Daily Briefing ("Good morning, here's your day") ---

    async def generate_briefing(self) -> dict:
        """Generate a rich daily briefing — the "home" view of the dashboard.

        Caching strategy:
        1. Check if we have a cached briefing from the last 5 minutes
        2. If yes, return it immediately (fast load)
        3. Generate fresh briefing in background
        4. Cache the new result for next time
        """
        import json as _json
        from datetime import datetime as _dt, timezone as _tz

        # Check cache first — if we have a briefing from the last 5 min, return it
        cached_raw = self.repo.get_preference("cached_briefing")
        if cached_raw:
            try:
                cached = _json.loads(cached_raw)
                cached_time = _dt.fromisoformat(cached.get("cached_at", ""))
                now_check = _dt.now(_tz.utc)
                if cached_time.tzinfo is None:
                    now_check = now_check.replace(tzinfo=None)
                age_seconds = (now_check - cached_time).total_seconds()
                if age_seconds < 300:  # less than 5 minutes old
                    cached["cached"] = True
                    return cached
            except:
                pass  # cache corrupted — regenerate

        # Generate fresh briefing
        result = await self._generate_fresh_briefing()

        # Cache it
        result["cached_at"] = _dt.now(_tz.utc).isoformat()
        result["cached"] = False
        try:
            self.repo.set_preference(
                "cached_briefing",
                _json.dumps(result, default=str),
                category="briefing",
            )
        except:
            pass

        return result

    async def _generate_fresh_briefing(self) -> dict:
        """Generate a fresh briefing — uses Gemma for greeting (least resource hungry)."""
        from datetime import datetime as _dt, timezone as _tz

        tasks = self.repo.list_tasks()
        # Use timezone-naive now() to match task deadlines from SQLite
        # (SQLite stores datetimes without timezone info)
        now = _dt.now()
        now_ts = now.timestamp()  # convert to number for safe comparison
        hour = now.hour

        # Time-based greeting
        if hour < 12:
            greeting = "Good morning"
        elif hour < 17:
            greeting = "Good afternoon"
        elif hour < 21:
            greeting = "Good evening"
        else:
            greeting = "Good night"

        # Filter to today's scheduled tasks
        today_str = now.strftime("%Y-%m-%d")
        scheduled = [
            t for t in tasks
            if t.status in (TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS)
            and t.scheduled_start
            and t.scheduled_start.strftime("%Y-%m-%d") == today_str
        ]

        # Also include pending tasks (not yet scheduled)
        pending = [t for t in tasks if t.status == TaskStatus.PENDING]

        # Categorize — use timestamp comparison to avoid timezone issues
        focus_blocks = [t for t in scheduled if t.category not in ("work", "general") or t.duration_minutes >= 45]
        meetings = [t for t in scheduled if t.category == "work" and t.duration_minutes < 45]
        deadlines_today = [
            t for t in tasks
            if t.deadline
            and t.deadline.strftime("%Y-%m-%d") == today_str
            and t.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)
        ]
        # Use numeric timestamp comparison for "tomorrow" check — avoids timezone errors
        deadlines_tomorrow = []
        for t in tasks:
            if not t.deadline:
                continue
            if t.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
                continue
            try:
                # Convert both to timestamps (numbers) — works regardless of timezone awareness
                dl_ts = t.deadline.timestamp()
                diff_hours = (dl_ts - now_ts) / 3600.0
                if diff_hours <= 36:  # within 1.5 days
                    deadlines_tomorrow.append(t)
            except Exception:
                continue

        # Top priority task
        all_active = scheduled + pending
        top_priority = max(all_active, key=lambda t: t.priority) if all_active else None

        # Suggested start time for top priority
        suggested_start = None
        if top_priority and top_priority.scheduled_start:
            suggested_start = top_priority.scheduled_start.strftime("%I:%M %p")
        elif top_priority:
            # Suggest based on best focus hour from learned data
            params = self._scheduler_trainer.get_learned_params()
            focus_windows = params.get("focus_windows", {})
            if focus_windows:
                best_hour = max(focus_windows, key=focus_windows.get)
                suggested_start = f"{best_hour}:00"
            else:
                suggested_start = "9:00 AM"

        # Biggest risk
        assessments = self._risk_engine.assess_all(tasks)
        biggest_risk = assessments[0] if assessments else None

        # Current mood + energy
        mood_score, mood_label, mood_reasons = self._mood_engine.current()
        energy = self._energy_engine.current()

        # Learned insights
        insights_result = await self.get_insights()
        insights = insights_result.get("insights", [])

        # Build schedule overview for today
        schedule_overview = []
        for t in sorted(scheduled, key=lambda x: x.scheduled_start or now):
            schedule_overview.append({
                "time": t.scheduled_start.strftime("%I:%M %p") if t.scheduled_start else "TBD",
                "title": t.title,
                "duration": t.duration_minutes,
                "category": t.category,
                "priority": t.priority,
                "is_meeting": t.category == "work" and t.duration_minutes < 45,
            })

        # Generate Archy's intro using Gemma (least resource hungry)
        # Skip the full interpreter chain — use a simple direct call to Gemma
        import json as _json2
        brief_prompt = (
            f"Event: daily_briefing\n"
            f"User's mood: {mood_label} (score {mood_score}/100)\n"
            f"Greeting: {greeting}\n"
            f"Total tasks: {len(scheduled) + len(pending)}\n"
            f"Focus blocks: {len(focus_blocks)}, Meetings: {len(meetings)}\n"
            f"Deadlines today: {len(deadlines_today)}, Tomorrow: {len(deadlines_tomorrow)}\n"
            f"Top priority: {top_priority.title if top_priority else 'None'}\n"
            f"Biggest risk: {biggest_risk.task_title if biggest_risk else 'None'}\n\n"
            f"Respond as Archy. ONE sentence. Max 15 words. Friendly greeting for the day.\n"
            f"Return JSON: {{\"text\": \"your message\"}}"
        )
        try:
            intro_data = await self._client.generate_json_with_fallback(
                agent_name="interpreter",
                system_prompt=PERSONA_PROMPT,
                user_prompt=brief_prompt,
                temperature=0.6,
                max_output_tokens=60,
                operation_name="briefing greeting (Gemma)",
            )
            intro = intro_data.get("text") or intro_data.get("message") or str(intro_data)
            if len(intro) > 200:
                intro = intro[:200]
        except Exception as e:
            logger.warning(f"Briefing intro generation failed: {e}")
            intro = f"{greeting}! You have {len(scheduled) + len(pending)} tasks today."

        return {
            "greeting": greeting,
            "intro": intro,
            "date": now.strftime("%A, %B %d"),
            "schedule_summary": {
                "total_tasks": len(scheduled) + len(pending),
                "scheduled_today": len(scheduled),
                "pending": len(pending),
                "focus_blocks": len(focus_blocks),
                "meetings": len(meetings),
                "deadlines_today": len(deadlines_today),
                "deadlines_tomorrow": len(deadlines_tomorrow),
            },
            "top_priority": {
                "title": top_priority.title if top_priority else None,
                "priority": top_priority.priority if top_priority else 0,
                "suggested_start": suggested_start,
                "duration": top_priority.duration_minutes if top_priority else 0,
                "category": top_priority.category if top_priority else None,
            } if top_priority else None,
            "biggest_risk": {
                "task_title": biggest_risk.task_title,
                "risk_score": biggest_risk.risk_score,
                "risk_label": biggest_risk.risk_label,
                "reason": biggest_risk.reasons[0] if biggest_risk and biggest_risk.reasons else "",
                "recovery_actions": biggest_risk.recovery_plan.actions if biggest_risk and biggest_risk.recovery_plan else [],
            } if biggest_risk else None,
            "current_state": {
                "mood_score": mood_score,
                "mood_label": mood_label,
                "energy_score": energy.score,
                "energy_label": energy.label.value,
            },
            "insights": insights[:3],  # top 3 insights
            "schedule": schedule_overview,
        }

    # --- Daily brief (Google Docs integration) ---

    async def generate_daily_brief(self) -> dict:
        """Generate a daily brief as a Google Doc.

        Goes through ProposalManager because it creates an external resource
        (Google Doc). HIGH risk requires explicit user approval. If the
        user does not approve immediately, returns `{"pending_approval": True}`
        and the actual doc creation runs later when /permissions/{id}/approve
        is called (executor: `self._execute_daily_brief`).
        """
        if not self.settings.daily_brief_enabled:
            return {
                "error": "Daily brief is disabled. Set ARCHY_DAILY_BRIEF_ENABLED=true."
            }

        from ..integrations.auth import GoogleAuth
        auth = GoogleAuth(self.settings)
        if not auth.is_authenticated():
            return {"error": "Not authenticated. Run `archy auth` first."}

        # --- ProposalManager gate (high risk - external write) ---
        # The executor is registered in __init__ as `self._execute_daily_brief`.
        # If approved immediately (e.g., user pre-approved), it runs now.
        # Otherwise it stays attached to the permission and runs on /approve.
        result = await self._proposal_manager.submit(
            intent="Generate daily brief Google Doc",
            action="docs.create_note",
            parameters={"type": "daily_brief"},
            confidence=80,
            reasoning="User requested daily brief generation.",
        )
        if result.get("status") == "pending_approval":
            await self._emit("PermissionRequested", result)
            return {"pending_approval": True, **result}
        if result.get("status") == "rejected":
            return {"rejected": True, **result}
        if result.get("status") == "needs_revision":
            return {"needs_revision": True, **result}
        # status == "executed" -> the executor returned a result dict
        return result.get("result", result)

    async def _execute_daily_brief(self, parameters: dict) -> dict:
        """Executor for the `docs.create_note` action - invoked by ProposalManager.

        Performs the actual Google Doc creation. Decoupled from
        `generate_daily_brief()` so it can run later when the user approves
        a pending permission request.
        """
        from ..integrations.auth import GoogleAuth
        from ..integrations.docs_client import DocsClient

        auth = GoogleAuth(self.settings)
        if not auth.is_authenticated():
            raise RuntimeError("Not authenticated. Run `archy auth` first.")

        docs = DocsClient(auth)
        tasks = self.repo.list_tasks(limit=200)
        score, label, _ = self._mood_engine.current()
        today = utcnow().strftime("%Y-%m-%d")

        # Build sections for the brief
        pending = [t for t in tasks if t.status.value in ("pending", "scheduled")]
        done_today = [
            t for t in tasks
            if t.status == TaskStatus.DONE
            and t.completed_at
            and t.completed_at.strftime("%Y-%m-%d") == today
        ]
        missed = [t for t in tasks if t.status == TaskStatus.MISSED]

        # Compose schedule text
        schedule_lines = []
        for t in pending:
            if t.scheduled_start:
                local = t.scheduled_start.strftime("%I:%M %p")
                schedule_lines.append(f"  {local} - {t.title} ({t.duration_minutes} min)")
            else:
                schedule_lines.append(f"  (unscheduled) - {t.title}")
        schedule_text = "\n".join(schedule_lines) if schedule_lines else "  (no upcoming tasks)"

        # Compose completions text
        done_text = (
            "\n".join(f"  - {t.title}" for t in done_today)
            if done_today
            else "  (none yet - that's okay!)"
        )

        # Compose missed text
        missed_text = (
            "\n".join(f"  - {t.title}" for t in missed)
            if missed
            else "  (none - great job!)"
        )

        # Compose Archy's friendly intro (uses the LLM)
        intro = await self._interpreter.synthesize(
            event_type="daily_brief",
            mood_label=label,
            mood_score=score,
            payload={
                "tasks_done_today": len(done_today),
                "tasks_pending": len(pending),
                "tasks_missed": len(missed),
            },
        )

        title = f"Archy Daily Brief - {utcnow().strftime('%B %d, %Y')}"
        sections = [
            {"heading": "From Archy", "body": intro},
            {"heading": "Today's Schedule", "body": schedule_text},
            {"heading": "Completed Today", "body": done_text},
            {"heading": "Missed (worth revisiting)", "body": missed_text},
            {"heading": "Current Mood", "body": f"Score: {score}/100\nLabel: {label}"},
        ]

        doc_id = docs.create_brief(title, sections)
        url = DocsClient.doc_url(doc_id)
        await self._emit(
            "BriefCreated",
            {"doc_id": doc_id, "doc_url": url, "title": title},
        )
        return {"doc_id": doc_id, "doc_url": url, "title": title}

    # --- Single-pass fast path (v2.1) ---
    # Most tasks don't need negotiation. Run agent once, accept if reasonable.
    # Escalate to full supervision only when PolicyEngine would reject.

    async def _run_single_pass(self, agent, **kwargs) -> AgentProposal:
        """Run an agent ONCE. Accept the result unless it's critically bad.

        This is the fast path — most tasks (schedule dentist, buy groceries)
        don't need the full negotiation cycle. If the proposal's self_confidence
        is very low (< 30), escalate to full supervision.
        """
        proposal = await agent.run(**kwargs)
        self.repo.append_proposal(proposal)
        await self._emit(
            "AgentProposal",
            {
                "proposal_id": proposal.proposal_id,
                "agent_name": proposal.agent_name,
                "proposal_type": proposal.proposal_type,
                "round": 1,
                "self_confidence": proposal.self_confidence,
                "mode": "single_pass",
            },
        )

        # Escalate to full supervision only if confidence is very low
        if proposal.self_confidence < 30:
            logger.info(
                f"Single-pass: {proposal.agent_name} confidence {proposal.self_confidence} < 30, "
                f"escalating to full supervision"
            )
            return await self._run_with_supervision(agent, **kwargs)

        # Accept — record evaluation as APPROVE
        evaluation = ArchyEvaluation(
            proposal_id=proposal.proposal_id,
            decision=ProposalDecision.ACCEPT,
            relevancy_score=proposal.self_confidence,
            feedback="Single-pass accepted (confidence >= 30).",
            constraints={},
            reasoning="Fast path: no negotiation needed.",
        )
        self.repo.append_evaluation(evaluation)
        return proposal

    # --- The supervision + relevancy scoring loop ---

    async def _run_with_supervision(self, agent, **kwargs) -> AgentProposal:
        """Run an agent, evaluate, negotiate if rejected. Returns final proposal."""
        round_num = 1
        proposal = await agent.run(**kwargs)
        self.repo.append_proposal(proposal)
        await self._emit(
            "AgentProposal",
            {
                "proposal_id": proposal.proposal_id,
                "agent_name": proposal.agent_name,
                "proposal_type": proposal.proposal_type,
                "round": round_num,
                "self_confidence": proposal.self_confidence,
            },
        )

        while True:
            evaluation = await self._evaluate_proposal(proposal)
            self.repo.append_evaluation(evaluation)
            await self._emit(
                "ArchyEvaluation",
                {
                    "proposal_id": evaluation.proposal_id,
                    "decision": evaluation.decision.value,
                    "relevancy_score": evaluation.relevancy_score,
                },
            )

            if evaluation.decision == ProposalDecision.ACCEPT:
                return proposal

            if round_num >= self.settings.max_negotiation_rounds:
                # Escalation: log conflict, fall back to rule-based core
                conflict = ConflictEvent(
                    conflict_id=str(uuid.uuid4()),
                    agent_name=proposal.agent_name,
                    proposal_type=proposal.proposal_type,
                    original_proposal=proposal.model_dump(),
                    evaluation=evaluation.model_dump(),
                    rounds=round_num,
                    resolution=ConflictResolution.ESCALATED,
                    final_outcome=(
                        f"Archy vetoed after {round_num} rounds; "
                        f"falling back to rule-based core."
                    ),
                )
                self.repo.append_conflict(conflict)
                await self._emit(
                    "ConflictEvent", conflict.model_dump(mode="json")
                )
                # Return the proposal anyway — caller will use rule-based fallback.
                proposal.constraints_used["_vetoed"] = True
                return proposal

            # Negotiate: ask agent to revise with constraints
            logger.info(
                f"Round {round_num}: Archy rejected {proposal.agent_name} "
                f"(score {evaluation.relevancy_score}); asking for revision"
            )
            round_num += 1
            proposal = await agent.revise(evaluation=evaluation, **kwargs)
            proposal.round = round_num
            self.repo.append_proposal(proposal)
            await self._emit(
                "AgentProposal",
                {
                    "proposal_id": proposal.proposal_id,
                    "agent_name": proposal.agent_name,
                    "proposal_type": proposal.proposal_type,
                    "round": round_num,
                    "self_confidence": proposal.self_confidence,
                    "revision": True,
                },
            )

            # Log a revised resolution (partial conflict)
            conflict = ConflictEvent(
                conflict_id=str(uuid.uuid4()),
                agent_name=proposal.agent_name,
                proposal_type=proposal.proposal_type,
                original_proposal=proposal.model_dump(),
                evaluation=evaluation.model_dump(),
                rounds=round_num,
                resolution=ConflictResolution.REVISED,
                final_outcome="Agent revised proposal per Archy's constraints.",
            )
            self.repo.append_conflict(conflict)
            await self._emit("ConflictEvent", conflict.model_dump(mode="json"))

    async def _evaluate_proposal(self, proposal: AgentProposal) -> ArchyEvaluation:
        """Compute relevancy_score for an agent proposal.

        Heuristics (deterministic v1 — no LLM call needed for speed):
        - Start with the agent's self_confidence as a baseline.
        - Penalize if proposal ignores current mood state (e.g., aggressive plan
          when user is in critical_panic).
        - Penalize if proposal has deadline conflicts.
        - Reward alignment with rule-based core's expectations.

        Future: replace with an LLM-as-judge call for richer reasoning.
        """
        score, label, _ = self._mood_engine.current()
        relevancy = float(proposal.self_confidence)

        # Mood-based adjustments
        if label == "critical_panic":
            # In panic mode, aggressive planner proposals get vetoed hard.
            if proposal.proposal_type == "schedule":
                # Check if proposal has too many slots
                slots = proposal.payload.get("slots", [])
                if len(slots) > 2:
                    relevancy -= 30
                # Check if any slot exceeds 45 min
                for slot in slots:
                    if "end" in slot and "start" in slot:
                        try:
                            start = datetime.fromisoformat(slot["start"])
                            end = datetime.fromisoformat(slot["end"])
                            if (end - start).total_seconds() / 60 > 45:
                                relevancy -= 15
                        except (ValueError, TypeError):
                            pass
                # Constrain next round
                return ArchyEvaluation(
                    proposal_id=proposal.proposal_id,
                    decision=ProposalDecision.REJECT if relevancy <= self.settings.relevancy_threshold else ProposalDecision.ACCEPT,
                    relevancy_score=int(max(0, min(100, relevancy))),
                    feedback=(
                        "User is in critical_panic. Reduce plan to <= 2 slots, "
                        "each <= 45 minutes, with 15-min buffers."
                    ),
                    constraints={
                        "max_slots": 2,
                        "max_slot_minutes": 45,
                        "require_buffer_minutes": 15,
                    },
                    reasoning="Mood-based veto: aggressive plan during panic state.",
                )

        if label == "drift_alert" and proposal.proposal_type == "schedule":
            slots = proposal.payload.get("slots", [])
            if len(slots) > 4:
                relevancy -= 15
                return ArchyEvaluation(
                    proposal_id=proposal.proposal_id,
                    decision=ProposalDecision.REJECT if relevancy <= self.settings.relevancy_threshold else ProposalDecision.ACCEPT,
                    relevancy_score=int(max(0, min(100, relevancy))),
                    feedback="User is drifting. Cap at 4 slots with 10-min buffers.",
                    constraints={"max_slots": 4, "require_buffer_minutes": 10},
                    reasoning="Mood-based adjustment: drifting user needs lighter load.",
                )

        # Classifier: if priority < 30 but no deadline, suspicious
        if proposal.proposal_type == "classification":
            payload = proposal.payload
            if payload.get("priority", 50) < 30 and not payload.get("deadline_iso"):
                relevancy -= 10

        relevancy = int(max(0, min(100, relevancy)))
        decision = (
            ProposalDecision.ACCEPT
            if relevancy > self.settings.relevancy_threshold
            else ProposalDecision.REJECT
        )

        return ArchyEvaluation(
            proposal_id=proposal.proposal_id,
            decision=decision,
            relevancy_score=relevancy,
            feedback="" if decision == ProposalDecision.ACCEPT else "Proposal may not fit current user state.",
            constraints={},
            reasoning=f"Heuristic evaluation (mood={label}, self_conf={proposal.self_confidence}).",
        )


def _extract_preferred_order(planner_payload: PlannerPayload) -> list[str] | None:
    """The PlannerAgent's `ordered_task_ids` lives in its suggestions reasoning.

    Since we didn't model it explicitly in PlannerPayload (to keep schema strict),
    we re-extract from the raw payload dict if present.
    """
    # The agent returns ordered_task_ids in its raw JSON; we stored it under
    # payload but PlannerPayload doesn't have that field. Read from raw payload.
    # For now, return None — the rule-based scheduler will use rank_score.
    # This is a hook for future enhancement.
    return None
