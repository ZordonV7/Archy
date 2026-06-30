"""Risk Engine — deadline risk prediction with recovery plans.

This is the core "Life Saver" feature. For each task with a deadline, the
RiskEngine answers:

  "Will this user miss the deadline?"

With:
  - A risk score (0-100, probability of missing)
  - A confidence score (0-100, how sure we are)
  - Human-readable reasons
  - A concrete recovery plan (if risk is high)

Deterministic — no LLM needed for the core computation. The LLM is only
used to phrase the recovery plan in PokeBo's voice (optional, via interpreter).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from ..config import Settings
from ..contracts.messages import Task, TaskStatus, utcnow
from .scheduler_trainer import SchedulerTrainer
from .storage import Repository


class RecoveryMode(StrEnum):
    """User-selectable recovery strategy aggressiveness."""
    CONSERVATIVE = "conservative"  # Don't touch meetings, only use free time
    BALANCED = "balanced"          # Move low-priority tasks, keep evenings free
    EMERGENCY = "emergency"        # Use evenings, weekend sessions, compress breaks


class SubTaskSuggestion(BaseModel):
    """A suggested sub-task in a recovery plan."""
    title: str
    estimated_minutes: int
    suggested_time: str = ""  # "Thursday 2pm" etc.


class RecoveryPlan(BaseModel):
    """A concrete plan to reduce deadline risk."""
    actions: list[str] = Field(default_factory=list)
    subtasks: list[SubTaskSuggestion] = Field(default_factory=list)
    tasks_to_move: list[str] = Field(default_factory=list)  # titles of lower-priority tasks to move
    probability_before: int = 0  # 0-100
    probability_after: int = 0   # 0-100


class RiskAssessment(BaseModel):
    """Per-task deadline risk assessment."""
    task_id: str
    task_title: str
    deadline: datetime | None = None
    hours_needed: float = 0.0
    hours_available: float = 0.0
    risk_score: int = Field(ge=0, le=100, description="Probability of missing deadline")
    risk_label: str  # low | moderate | high | critical
    confidence: int = Field(ge=0, le=100, description="Confidence in this prediction")
    reasons: list[str] = Field(default_factory=list)
    recovery_plan: RecoveryPlan | None = None


class RiskEngine:
    """Deterministic deadline risk predictor.

    Uses learned parameters from SchedulerTrainer to make accurate predictions.
    Improves over time as more completion data is collected.
    """

    def __init__(self, settings: Settings, repo: Repository, trainer: SchedulerTrainer):
        self._s = settings
        self._repo = repo
        self._trainer = trainer

    def assess_all(self, tasks: list[Task] | None = None) -> list[RiskAssessment]:
        """Assess risk for all tasks with deadlines.

        Returns assessments sorted by risk_score (highest first).
        """
        if tasks is None:
            tasks = self._repo.list_tasks(status=TaskStatus.SCHEDULED)
            tasks = [t for t in tasks if t.deadline]

        assessments = []
        for task in tasks:
            if task.deadline and task.status not in (TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.MISSED):
                assessment = self.assess_task(task, tasks)
                if assessment:
                    assessments.append(assessment)

        assessments.sort(key=lambda a: a.risk_score, reverse=True)
        return assessments

    def assess_task(self, task: Task, all_tasks: list[Task] | None = None, recovery_mode: RecoveryMode = RecoveryMode.BALANCED) -> RiskAssessment | None:
        """Assess deadline risk for a single task.

        Returns None if the task has no deadline.
        """
        if not task.deadline:
            return None

        if all_tasks is None:
            all_tasks = self._repo.list_tasks()

        # --- Hours needed (adjusted by learned duration multiplier) ---
        params = self._trainer.get_learned_params()
        duration_multiplier = params["duration_adjustments"].get(task.category, 1.0)
        hours_needed = (task.duration_minutes * duration_multiplier) / 60.0

        # --- Hours available (free focus time between now and deadline) ---
        hours_available = self._compute_available_hours(task.deadline, all_tasks, exclude_task_id=task.id)

        # --- Risk score: probability of missing deadline ---
        if hours_available <= 0:
            risk_score = 100  # past deadline = always 100
        elif hours_needed <= hours_available * 0.5:
            risk_score = 10  # plenty of time
        else:
            ratio = hours_needed / hours_available
            risk_score = min(100, int(ratio * 55))

        # Adjust by historical completion rate (but not for past deadlines)
        completion_rate = self._get_completion_rate(task, params)
        if hours_available > 0:
            risk_score = int(risk_score * (1.3 - completion_rate * 0.5))
            risk_score = max(0, min(100, risk_score))

        # --- Risk label ---
        if risk_score >= 80:
            risk_label = "critical"
        elif risk_score >= 60:
            risk_label = "high"
        elif risk_score >= 35:
            risk_label = "moderate"
        else:
            risk_label = "low"

        # --- Confidence in our prediction ---
        sample_count = params.get("sample_counts", {}).get("completions", 0)
        confidence = min(95, 25 + sample_count * 4)  # more data = more confidence

        # --- Reasons (human-readable) ---
        reasons = self._generate_reasons(
            task, hours_needed, hours_available, completion_rate, risk_score, params
        )

        # --- Recovery plan (only if risk is moderate or higher) ---
        recovery_plan = None
        if risk_score >= 35:
            recovery_plan = self._generate_recovery_plan(
                task, hours_needed, hours_available, all_tasks, risk_score, params, recovery_mode
            )

        return RiskAssessment(
            task_id=task.id,
            task_title=task.title,
            deadline=task.deadline,
            hours_needed=round(hours_needed, 1),
            hours_available=round(hours_available, 1),
            risk_score=risk_score,
            risk_label=risk_label,
            confidence=confidence,
            reasons=reasons,
            recovery_plan=recovery_plan,
        )

    def _compute_available_hours(
        self, deadline: datetime, all_tasks: list[Task], exclude_task_id: str | None = None
    ) -> float:
        """Compute available focus hours between now and deadline.

        Subtracts time already allocated to other scheduled tasks.
        Respects workday window (e.g., 9am-10pm).
        """
        now = utcnow()
        # Normalize timezone: if deadline is naive, make now naive too
        if deadline.tzinfo is None:
            now = now.replace(tzinfo=None)
        if deadline <= now:
            return 0.0

        # Total calendar hours
        total_hours = (deadline - now).total_seconds() / 3600.0

        # Workday fraction (e.g., 9am-10pm = 13 hours out of 24 = 0.54)
        wh_start = self._parse_hhmm(self._s.workday_start)
        wh_end = self._parse_hhmm(self._s.workday_end)
        workday_hours = wh_end.hour - wh_start.hour
        workday_fraction = workday_hours / 24.0

        # Available work hours
        available = total_hours * workday_fraction

        # Subtract time already allocated to other scheduled tasks
        for t in all_tasks:
            if t.id == exclude_task_id:
                continue
            if t.status in (TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS) and t.scheduled_start:
                # Only count tasks that fall before this deadline
                if t.scheduled_start < deadline:
                    available -= t.duration_minutes / 60.0

        return max(0.0, available)

    def _get_completion_rate(self, task: Task, params: dict) -> float:
        """Get historical completion rate for this task's category+complexity."""
        # Look up learned completion likelihood
        complexity = task.complexity
        bucket = f"{complexity // 25 * 25}-{complexity // 25 * 25 + 24}"
        key = f"{task.category}:{bucket}"
        rate = params.get("completion_likelihood", {}).get(key)
        if rate is not None:
            return rate
        # Fallback: use category-level postponement rate
        postponement_rate = params.get("postponement_rates", {}).get(task.category, 0.2)
        return 1.0 - postponement_rate

    def _generate_reasons(
        self,
        task: Task,
        hours_needed: float,
        hours_available: float,
        completion_rate: float,
        risk_score: int,
        params: dict,
    ) -> list[str]:
        """Generate human-readable reasons for the risk assessment."""
        reasons = []

        if hours_available <= 0:
            reasons.append("Deadline has already passed — no time remaining.")
        elif hours_needed > hours_available:
            reasons.append(
                f"Needs {hours_needed:.1f}h but only {hours_available:.1f}h available before deadline."
            )
        else:
            reasons.append(
                f"Needs {hours_needed:.1f}h, {hours_available:.1f}h available — sufficient time."
            )

        if completion_rate < 0.5:
            reasons.append(
                f"Historical completion rate for {task.category} tasks is only {int(completion_rate * 100)}%."
            )

        # Check if duration is underestimated
        duration_multiplier = params.get("duration_adjustments", {}).get(task.category, 1.0)
        if duration_multiplier > 1.15:
            reasons.append(
                f"You typically underestimate {task.category} tasks by {int((duration_multiplier - 1) * 100)}%."
            )

        # Check if there are many other tasks competing for time
        if hours_needed > 0 and hours_available > 0:
            utilization = hours_needed / hours_available if hours_available > 0 else 1.0
            if utilization > 0.7:
                reasons.append("Schedule is heavily loaded — little room for delays.")

        if risk_score >= 80:
            reasons.append("Without intervention, you are very likely to miss this deadline.")
        elif risk_score >= 60:
            reasons.append("Significant risk of missing deadline without adjustments.")
        elif risk_score >= 35:
            reasons.append("Moderate risk — monitor progress closely.")

        return reasons

    def _generate_recovery_plan(
        self,
        task: Task,
        hours_needed: float,
        hours_available: float,
        all_tasks: list[Task],
        current_risk: int,
        params: dict,
        mode: RecoveryMode = RecoveryMode.BALANCED,
    ) -> RecoveryPlan:
        """Generate a concrete recovery plan based on the selected mode.

        Modes:
        - CONSERVATIVE: Don't change meetings. Only use free time.
        - BALANCED: Move low-priority tasks. Keep evenings free.
        - EMERGENCY: Use evenings. Weekend sessions. Compress breaks.
        """
        actions: list[str] = []
        subtasks: list[SubTaskSuggestion] = []
        tasks_to_move: list[str] = []
        new_risk = current_risk

        # 1. Split large tasks (all modes)
        if hours_needed > 2.0:
            num_sessions = min(5, max(2, int(hours_needed / 1.5)))
            per_session = int((task.duration_minutes * params.get("duration_adjustments", {}).get(task.category, 1.0)) / num_sessions)
            for i in range(num_sessions):
                subtasks.append(SubTaskSuggestion(
                    title=f"{task.title} — Session {i+1}/{num_sessions}",
                    estimated_minutes=per_session,
                ))
            actions.append(f"Split '{task.title}' into {num_sessions} sessions of ~{per_session}min.")
            new_risk = max(10, new_risk - 20)

        # 2. Mode-specific actions
        if mode == RecoveryMode.CONSERVATIVE:
            # Don't touch meetings — only use free time
            actions.append("Conservative mode: keeping all meetings in place.")
            actions.append("Using only free time slots between existing commitments.")
            # Smaller risk reduction — we're being gentle
            new_risk = max(15, new_risk - 5)

        elif mode == RecoveryMode.BALANCED:
            # Move low-priority tasks, keep evenings free
            competing = [
                t for t in all_tasks
                if t.id != task.id
                and t.status in (TaskStatus.SCHEDULED, TaskStatus.PENDING)
                and t.scheduled_start
                and t.priority < task.priority
            ]
            competing.sort(key=lambda t: t.priority)
            for t in competing[:3]:
                tasks_to_move.append(t.title)
            if tasks_to_move:
                actions.append(f"Balanced mode: moving {len(tasks_to_move)} low-priority task(s) after deadline: {', '.join(tasks_to_move[:2])}.")
                new_risk = max(5, new_risk - 15)
            actions.append("Keeping evenings free for rest.")

        elif mode == RecoveryMode.EMERGENCY:
            # Use evenings, weekend sessions, compress breaks
            actions.append("🚨 Emergency mode: activating evening work sessions.")
            actions.append("Scheduling weekend catch-up sessions.")
            actions.append("Compressing all breaks to 5 minutes.")
            # Move ALL lower-priority tasks
            competing = [
                t for t in all_tasks
                if t.id != task.id
                and t.status in (TaskStatus.SCHEDULED, TaskStatus.PENDING)
                and t.priority < task.priority
            ]
            for t in competing[:5]:
                tasks_to_move.append(t.title)
            if tasks_to_move:
                actions.append(f"Moving ALL {len(tasks_to_move)} lower-priority tasks to next week.")
            # Big risk reduction — we're going all-out
            new_risk = max(5, new_risk - 30)

        # 3. Extend focus blocks (all modes, but more aggressive in emergency)
        if mode == RecoveryMode.EMERGENCY:
            actions.append("Extending focus blocks to 2 hours — push through.")
            new_risk = max(5, new_risk - 10)
        elif hours_needed > hours_available * 0.8:
            actions.append("Consider extending focus blocks slightly.")
            new_risk = max(5, new_risk - 5)

        # 4. Deadline urgency (all modes)
        if task.deadline:
            dl = task.deadline
            now = utcnow()
            if dl.tzinfo is None:
                now = now.replace(tzinfo=None)
            days_left = (dl - now).days
            if days_left <= 2 and hours_needed > 3:
                actions.append(f"⚠️ Only {days_left} day(s) left — start immediately!")

        return RecoveryPlan(
            actions=actions,
            subtasks=subtasks,
            tasks_to_move=tasks_to_move,
            probability_before=current_risk,
            probability_after=new_risk,
        )

    @staticmethod
    def _parse_hhmm(s: str):
        from datetime import time
        h, m = s.split(":")
        return time(hour=int(h), minute=int(m), second=0)
