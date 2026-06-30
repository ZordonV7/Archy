"""Rule-based final scheduler (the decider).

The PlannerAgent proposes an ordering; THIS module actually commits datetime
slots. Deterministic, testable, no LLM.

Strategy (v1): greedy, rank-based.
- Sort by `rank_score` (priority + deadline urgency + inverse complexity).
- Pack from "now" forward, respecting per-task duration, inter-task buffer,
  and workday window (e.g. 09:00-22:00).
- Tasks already in progress keep their existing slot.
- Tasks whose slot would end after their deadline → flagged `at_risk`.
- Tasks already past deadline + tolerance → unschedulable (effectively missed).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Protocol

from loguru import logger
from pydantic import BaseModel, Field

from ..config import Settings
from ..contracts.messages import Task, TaskStatus, utcnow


class ScheduledSlot(BaseModel):
    task_id: str
    start: datetime
    end: datetime
    at_risk: bool = False


class PlanResult(BaseModel):
    slots: list[ScheduledSlot]
    unscheduled_task_ids: list[str] = Field(default_factory=list)
    at_risk_task_ids: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


class Scheduler(Protocol):
    async def plan(
        self, tasks: list[Task], preferred_order: list[str] | None = None
    ) -> PlanResult: ...


class GreedyScheduler:
    """Concrete scheduler — see module docstring.

    `preferred_order` is an optional hint from the PlannerAgent. If provided,
    we use it as the sort key (still respecting in-progress anchor + workday).
    """

    def __init__(self, settings: Settings):
        self.buffer = timedelta(minutes=settings.buffer_minutes)
        self.workday_start = _parse_hhmm(settings.workday_start)
        self.workday_end = _parse_hhmm(settings.workday_end)

    async def plan(
        self, tasks: list[Task], preferred_order: list[str] | None = None
    ) -> PlanResult:
        schedulable = [
            t for t in tasks if t.status in (TaskStatus.PENDING, TaskStatus.SCHEDULED)
        ]
        in_progress = [
            t for t in tasks
            if t.status == TaskStatus.IN_PROGRESS and t.scheduled_start
        ]

        # Sort: prefer LLM-suggested order if provided; else by rank_score.
        if preferred_order:
            order_index = {tid: i for i, tid in enumerate(preferred_order)}
            schedulable.sort(
                key=lambda t: order_index.get(t.id, 10_000 + (-t.rank_score))
            )
        else:
            schedulable.sort(key=lambda t: t.rank_score, reverse=True)

        slots: list[ScheduledSlot] = []
        at_risk_ids: list[str] = []

        # Anchor in-progress task slots.
        for t in in_progress:
            if t.scheduled_start and t.scheduled_end:
                slot = ScheduledSlot(
                    task_id=t.id,
                    start=t.scheduled_start,
                    end=t.scheduled_end,
                    at_risk=_is_at_risk(t),
                )
                slots.append(slot)
                if slot.at_risk:
                    at_risk_ids.append(t.id)

        cursor = max((s.end for s in slots), default=utcnow())
        unscheduled: list[str] = []

        for t in schedulable:
            start = _next_available_start(cursor, self.workday_start, self.workday_end)
            duration = timedelta(minutes=t.duration_minutes)
            end = start + duration

            # Roll past end-of-workday.
            if end.time() > self.workday_end and end.date() == start.date():
                start = datetime.combine(
                    start.date() + timedelta(days=1),
                    self.workday_start,
                    tzinfo=start.tzinfo,
                )
                end = start + duration

            at_risk = bool(t.deadline and end > t.deadline)

            # Past deadline + tolerance → unschedulable.
            if t.deadline and start > t.deadline + timedelta(minutes=15):
                unscheduled.append(t.id)
                logger.warning(
                    f"Task {t.id} ({t.title}) cannot be scheduled before deadline "
                    f"{t.deadline.isoformat()}; marked unscheduled."
                )
                continue

            slots.append(ScheduledSlot(task_id=t.id, start=start, end=end, at_risk=at_risk))
            if at_risk:
                at_risk_ids.append(t.id)
            cursor = end + self.buffer

        slots.sort(key=lambda s: s.start)
        logger.info(
            f"Scheduler produced {len(slots)} slots, {len(unscheduled)} unscheduled, "
            f"{len(at_risk_ids)} at-risk"
        )
        return PlanResult(
            slots=slots,
            unscheduled_task_ids=unscheduled,
            at_risk_task_ids=at_risk_ids,
        )


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m), second=0)


def _next_available_start(
    cursor: datetime, workday_start: time, workday_end: time
) -> datetime:
    candidate = cursor
    if candidate.time() > workday_end:
        candidate = datetime.combine(
            candidate.date() + timedelta(days=1), workday_start, tzinfo=candidate.tzinfo
        )
    elif candidate.time() < workday_start:
        candidate = datetime.combine(candidate.date(), workday_start, tzinfo=candidate.tzinfo)
    return candidate


def _is_at_risk(task: Task) -> bool:
    if not (task.deadline and task.scheduled_end):
        return False
    return task.scheduled_end > task.deadline
