"""Rule-based final mood engine (the decider).

The MoodAgent proposes a score; THIS module applies deterministic delta
rules from real events. The two are blended: agent score is advisory,
rule-based engine is the source of truth.

Strategy:
- Maintain a baseline score (default 75).
- Each event (task completed/missed, drift) applies a delta.
- Clamp to [mood_min, mood_max].
- Persist every snapshot for history.
"""
from __future__ import annotations

from datetime import timedelta

from loguru import logger

from ..config import Settings
from ..contracts.messages import DriftEvent, Task, TaskStatus, utcnow
from .storage import Repository


# --- Delta rules ---
DELTA_TASK_COMPLETED_ON_TIME = +5
DELTA_TASK_COMPLETED_LATE = -3
DELTA_TASK_MISSED = -15          # missing deadline = big penalty
DELTA_TASK_CANCELLED = -2        # cancelling = tiny penalty (user chose to)
DELTA_TASK_SCHEDULED = +2
DELTA_TASK_AT_RISK = -4
DELTA_DRIFT_PER_15_MIN = -3
DELTA_DRIFT_RECOVERY_BONUS = +6
DELTA_OVERDUE_PENDING_PER_TASK = -3

# v2.1: Productivity-based task deltas (practice, study, exercise)
DELTA_PRODUCTIVITY_PARTIAL = -8     # did less than estimated (e.g., 4hr piano, did 2hr)
DELTA_PRODUCTIVITY_OVERCOMPLETION = +3  # did more than estimated

# v2.1: Deadline-based task deltas
DELTA_DEADLINE_EARLY = +8           # finished before deadline
DELTA_DEADLINE_EARLY_BONUS = +5     # finished way before deadline (< 40% of estimated time)


class MoodEngine:
    """Deterministic mood scorer. Source of truth — the MoodAgent is advisory."""

    def __init__(self, settings: Settings, repo: Repository):
        self.baseline = settings.mood_baseline
        self.minimum = settings.mood_min
        self.maximum = settings.mood_max
        self._repo = repo

    def current(self) -> tuple[int, str, list[str]]:
        snap = self._repo.latest_mood()
        if snap is None:
            self._repo.append_mood(
                self.baseline,
                _label_from_score(self.baseline),
                ["Baseline — system initialized."],
            )
            return (self.baseline, _label_from_score(self.baseline), ["Baseline — system initialized."])
        return snap

    def on_task_scheduled(self, task: Task) -> tuple[int, str, list[str]]:
        delta = DELTA_TASK_SCHEDULED
        reason = f"Task '{task.title}' scheduled."
        if task.scheduled_end and task.deadline and task.scheduled_end > task.deadline:
            delta += DELTA_TASK_AT_RISK
            reason += " (slot ends after deadline — at risk)"
        return self._apply(delta, [reason])

    def on_task_completed(self, task: Task) -> tuple[int, str, list[str]]:
        """Mood delta depends on task type:
        
        - DEADLINE-based (has deadline, e.g., "file taxes"):
          * Finished before deadline → +8 (or +13 if way early)
          * Finished after deadline → -3
        
        - PRODUCTIVITY-based (no deadline, e.g., "practice piano 4hr"):
          * Completed on time (did what was planned) → +5
          * Did LESS than estimated (4hr planned, 2hr done) → -8
          * Did MORE than estimated → +8
        """
        if task.status != TaskStatus.DONE or not task.completed_at:
            return self.current()

        # Calculate actual time spent
        actual_minutes = 0
        if task.started_at and task.completed_at:
            try:
                from datetime import timezone
                start = task.started_at
                end = task.completed_at
                # Normalize timezone
                if start.tzinfo is None and end.tzinfo is not None:
                    end = end.replace(tzinfo=None)
                elif start.tzinfo is not None and end.tzinfo is None:
                    start = start.replace(tzinfo=None)
                actual_minutes = max(1, (end - start).total_seconds() / 60)
            except:
                actual_minutes = task.duration_minutes

        estimated = task.duration_minutes

        # Determine task type
        is_deadline_based = task.deadline is not None

        if is_deadline_based:
            # DEADLINE-BASED task
            # Check if finished before deadline
            try:
                dl = task.deadline
                comp = task.completed_at
                if dl.tzinfo is None and comp.tzinfo is not None:
                    comp = comp.replace(tzinfo=None)
                elif dl.tzinfo is not None and comp.tzinfo is None:
                    dl = dl.replace(tzinfo=None)

                if comp <= dl:
                    # Finished before deadline — mood goes UP
                    # Check if way early (< 40% of estimated time)
                    if estimated > 0 and actual_minutes < estimated * 0.4:
                        total = DELTA_DEADLINE_EARLY + DELTA_DEADLINE_EARLY_BONUS
                        return self._apply(total, [
                            f"Task '{task.title}' finished WAY before deadline (+{total})! Energy boost!"
                        ])
                    return self._apply(DELTA_DEADLINE_EARLY, [
                        f"Task '{task.title}' beat the deadline (+{DELTA_DEADLINE_EARLY})!"
                    ])
                else:
                    # Finished after deadline — mood goes down
                    return self._apply(DELTA_TASK_COMPLETED_LATE, [
                        f"Task '{task.title}' completed but past deadline ({DELTA_TASK_COMPLETED_LATE})."
                    ])
            except:
                # Fallback if timezone comparison fails
                return self._apply(DELTA_TASK_COMPLETED_ON_TIME, [
                    f"Task '{task.title}' completed (+{DELTA_TASK_COMPLETED_ON_TIME})."
                ])
        else:
            # PRODUCTIVITY-BASED task (no deadline — practice, study, exercise)
            # Completing is ALWAYS positive — the user did it, that's an achievement
            if estimated > 0 and actual_minutes > 0:
                ratio = actual_minutes / estimated
                if ratio > 1.2:
                    # Did more than planned — extra bonus
                    total = DELTA_TASK_COMPLETED_ON_TIME + DELTA_PRODUCTIVITY_OVERCOMPLETION
                    return self._apply(total, [
                        f"Task '{task.title}' — did {int(actual_minutes)}min, more than planned {estimated}min (+{total})!"
                    ])
                else:
                    # Completed — even if did less than planned, it's still positive
                    # NO penalty for under-completion. The user completed the task.
                    return self._apply(DELTA_TASK_COMPLETED_ON_TIME, [
                        f"Task '{task.title}' — completed (+{DELTA_TASK_COMPLETED_ON_TIME})."
                    ])
            else:
                return self._apply(DELTA_TASK_COMPLETED_ON_TIME, [
                    f"Task '{task.title}' completed (+{DELTA_TASK_COMPLETED_ON_TIME})."
                ])

    def on_task_missed(self, task: Task) -> tuple[int, str, list[str]]:
        return self._apply(
            DELTA_TASK_MISSED,
            [f"Task '{task.title}' missed deadline ({DELTA_TASK_MISSED})."],
        )

    def on_task_cancelled(self, task: Task) -> tuple[int, str, list[str]]:
        """Cancelling a task = small penalty (user chose to, not a failure)."""
        return self._apply(
            DELTA_TASK_CANCELLED,
            [f"Task '{task.title}' cancelled ({DELTA_TASK_CANCELLED})."],
        )

    def on_drift(self, event: DriftEvent) -> tuple[int, str, list[str]]:
        units = max(1, event.minutes_lost // 15)
        delta = DELTA_DRIFT_PER_15_MIN * units
        reasons = [
            f"Drift event '{event.kind}': {event.description} "
            f"({event.minutes_lost} min lost, {delta})."
        ]
        snap = self._apply(delta, reasons)
        if event.kind == "manual":
            snap = self._apply(
                DELTA_DRIFT_RECOVERY_BONUS, ["Recovery acknowledged (+bonus)."]
            )
        return snap

    def recompute_from_state(self, tasks: list[Task]) -> tuple[int, str, list[str]]:
        """Rebuild mood from current task state — used on startup."""
        reasons: list[str] = []
        score = self.baseline
        overdue_pending = 0

        for t in tasks:
            if t.status == TaskStatus.MISSED:
                score += DELTA_TASK_MISSED
                reasons.append(f"Missed: {t.title} ({DELTA_TASK_MISSED})")
            elif t.status == TaskStatus.DONE and t.completed_at and t.scheduled_end:
                if t.completed_at <= t.scheduled_end:
                    score += DELTA_TASK_COMPLETED_ON_TIME
                    reasons.append(f"On-time: {t.title} (+{DELTA_TASK_COMPLETED_ON_TIME})")
                else:
                    score += DELTA_TASK_COMPLETED_LATE
                    reasons.append(f"Late: {t.title} ({DELTA_TASK_COMPLETED_LATE})")
            elif t.status in (TaskStatus.PENDING, TaskStatus.SCHEDULED):
                if t.deadline and t.deadline < utcnow():
                    overdue_pending += 1
            if t.status == TaskStatus.SCHEDULED and t.scheduled_end and t.deadline:
                if t.scheduled_end > t.deadline:
                    score += DELTA_TASK_AT_RISK
                    reasons.append(f"At-risk slot: {t.title} ({DELTA_TASK_AT_RISK})")

        if overdue_pending:
            delta = DELTA_OVERDUE_PENDING_PER_TASK * overdue_pending
            score += delta
            reasons.append(f"{overdue_pending} overdue pending task(s) ({delta})")

        score = max(self.minimum, min(self.maximum, score))
        label = _label_from_score(score)
        if not reasons:
            reasons = ["Baseline — no significant events."]
        self._repo.append_mood(score, label, reasons)
        logger.info(f"Mood recomputed from state: {score} ({label})")
        return (score, label, reasons)

    def _apply(self, delta: int, reasons: list[str]) -> tuple[int, str, list[str]]:
        prev = self._repo.latest_mood()
        prev_score = prev[0] if prev else self.baseline
        new_score = max(self.minimum, min(self.maximum, prev_score + delta))
        label = _label_from_score(new_score)
        self._repo.append_mood(new_score, label, reasons)
        logger.info(
            f"Mood {prev_score} -> {new_score} (delta={delta:+d}) — {'; '.join(reasons)}"
        )
        return (new_score, label, reasons)


def _label_from_score(score: int) -> str:
    if score >= 90:
        return "deep_focus"
    if score >= 70:
        return "watchful"
    if score >= 50:
        return "drift_alert"
    return "critical_panic"
