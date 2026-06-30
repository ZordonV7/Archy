"""Dynamic Risk Monitor — continuously recalculates deadline risk.

Every action the user takes (skip, complete, drift, replan) changes their
risk trajectory. This module hooks into Archy's event system and recalculates
risk after every significant event, emitting RiskChanged events.

The user should feel that every action changes their trajectory:
  9:00 AM  → Risk: 32%  (started the day well)
  ↓ skipped work session
  11:30 AM → Risk: 51%  (falling behind)
  ↓ completed focus block
  2:00 PM  → Risk: 21%  (back on track)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Awaitable

from loguru import logger
from pydantic import BaseModel, Field

from ..config import Settings
from ..contracts.messages import Task, TaskStatus, utcnow
from ..core.risk_engine import RiskAssessment, RiskEngine
from ..core.storage import Repository


EventCallback = Callable[[str, dict], Awaitable[None]]


class RiskSnapshot(BaseModel):
    """A point-in-time risk reading — stored as history."""
    timestamp: datetime = Field(default_factory=utcnow)
    overall_risk: int = Field(ge=0, le=100)
    highest_task_risk: int = Field(ge=0, le=100)
    highest_task_title: str = ""
    task_count: int = 0
    trigger: str = ""  # what event caused this recalculation


class DynamicRiskMonitor:
    """Monitors risk continuously by hooking into Archy's event system.

    Usage:
        monitor = DynamicRiskMonitor(settings, repo, risk_engine)
        assistant.subscribe(monitor)
        # Now every event triggers a risk recalculation
    """

    # Events that should trigger a risk recalculation
    TRIGGER_EVENTS = {
        "TaskStarted", "TaskCompleted", "TaskCancelled", "TaskMissed",
        "TaskScheduled", "PlanReady", "ConflictDetected",
    }

    def __init__(self, settings: Settings, repo: Repository, risk_engine: RiskEngine):
        self._s = settings
        self._repo = repo
        self._risk = risk_engine
        self._last_snapshot: RiskSnapshot | None = None

    async def __call__(self, event_type: str, payload: dict) -> None:
        """Event callback — recalculates risk after significant events."""
        if event_type not in self.TRIGGER_EVENTS:
            return

        try:
            snapshot = self.recalculate(trigger=event_type)
            if snapshot:
                # Check if risk changed significantly (more than 3 points)
                old = self._last_snapshot.overall_risk if self._last_snapshot else -1
                new = snapshot.overall_risk

                if abs(new - old) >= 3 or event_type == "PlanReady":
                    self._last_snapshot = snapshot
                    self._store_snapshot(snapshot)
                    logger.info(
                        f"[RiskMonitor] Risk {old}% → {new}% "
                        f"(trigger: {event_type}, highest: {snapshot.highest_task_title} "
                        f"@ {snapshot.highest_task_risk}%)"
                    )
                    # The caller (Archy) will emit the event — we just return the data
                    # Archy checks _last_snapshot after each event
        except Exception:
            logger.exception("[RiskMonitor] Failed to recalculate risk")

    def recalculate(self, trigger: str = "manual") -> RiskSnapshot | None:
        """Recalculate risk for all tasks. Returns a snapshot."""
        tasks = self._repo.list_tasks()
        assessments = self._risk.assess_all(tasks)

        if not assessments:
            snapshot = RiskSnapshot(
                overall_risk=0,
                highest_task_risk=0,
                highest_task_title="",
                task_count=0,
                trigger=trigger,
            )
            self._last_snapshot = snapshot
            return snapshot

        # Overall risk = weighted average (highest-risk task dominates)
        highest = assessments[0]
        avg_risk = sum(a.risk_score for a in assessments) / len(assessments)
        # Weight: 70% highest + 30% average (highest task dominates)
        overall = int(0.7 * highest.risk_score + 0.3 * avg_risk)
        overall = max(0, min(100, overall))

        snapshot = RiskSnapshot(
            overall_risk=overall,
            highest_task_risk=highest.risk_score,
            highest_task_title=highest.task_title,
            task_count=len(assessments),
            trigger=trigger,
        )
        self._last_snapshot = snapshot
        return snapshot

    def get_current_risk(self) -> dict:
        """Get the current risk snapshot + trajectory."""
        if not self._last_snapshot:
            snap = self.recalculate(trigger="initial")
            if not snap:
                return {"overall_risk": 0, "trajectory": []}
        else:
            snap = self._last_snapshot

        # Get history from DB
        history = self._get_history(limit=10)

        return {
            "overall_risk": snap.overall_risk,
            "highest_task_risk": snap.highest_task_risk,
            "highest_task_title": snap.highest_task_title,
            "task_count": snap.task_count,
            "trigger": snap.trigger,
            "timestamp": snap.timestamp.isoformat(),
            "trajectory": history,
            # Human-readable assessment
            "label": self._risk_label(snap.overall_risk),
            "message": self._risk_message(snap.overall_risk, snap.highest_task_title),
        }

    def _store_snapshot(self, snapshot: RiskSnapshot) -> None:
        """Store risk snapshot in DB for trajectory tracking."""
        try:
            import json
            self._repo.append_audit_log(
                event_type="RiskSnapshot",
                actor="risk_monitor",
                action="recalculate",
                target=None,
                details={
                    "overall_risk": snapshot.overall_risk,
                    "highest_task_risk": snapshot.highest_task_risk,
                    "highest_task_title": snapshot.highest_task_title,
                    "task_count": snapshot.task_count,
                    "trigger": snapshot.trigger,
                    "timestamp": snapshot.timestamp.isoformat(),
                },
            )
        except Exception:
            pass

    def _get_history(self, limit: int = 10) -> list[dict]:
        """Get recent risk snapshots for trajectory chart."""
        try:
            logs = self._repo.list_audit_logs(limit=100)
            snapshots = [
                {
                    "risk": log["details"].get("overall_risk", 0),
                    "timestamp": log["timestamp"],
                    "trigger": log["details"].get("trigger", ""),
                    "highest_task": log["details"].get("highest_task_title", ""),
                }
                for log in logs
                if log.get("event_type") == "RiskSnapshot"
            ]
            return snapshots[:limit]
        except:
            return []

    @staticmethod
    def _risk_label(risk: int) -> str:
        if risk >= 80:
            return "critical"
        elif risk >= 60:
            return "high"
        elif risk >= 35:
            return "moderate"
        return "low"

    @staticmethod
    def _risk_message(risk: int, highest_task: str) -> str:
        if risk >= 80:
            return f"⚠️ Critical risk — '{highest_task}' is very likely to be missed."
        elif risk >= 60:
            return f"⚠️ High risk — '{highest_task}' needs attention soon."
        elif risk >= 35:
            return f"Moderate risk — '{highest_task}' is manageable but monitor progress."
        return "Low risk — you're on track."
