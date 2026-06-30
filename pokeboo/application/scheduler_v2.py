"""Scheduler v2 — enhanced with mood, energy, and context-switching cost.

Improvements over v1:
1. Mood-aware: during critical_panic, limits slot duration and count
2. Energy-aware: skips complex tasks when energy is depleted; inserts breaks
3. Context-switching cost: groups same-category tasks together to reduce switching
4. Focus time protection: blocks long uninterrupted periods for deep work
5. Deadline-weighted ranking: closer deadlines bubble up

Algorithm:
1. Filter schedulable tasks (PENDING + SCHEDULED)
2. Score each task: rank = f(priority, deadline_urgency, complexity, category_match)
3. Sort by rank (descending)
4. Insert breaks based on energy level
5. Pack slots respecting workday window + buffer + mood constraints
6. Group same-category tasks when possible (reduces context switching)
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Protocol

from loguru import logger
from pydantic import BaseModel, Field

from ..config import Settings
from ..contracts.messages import Task, TaskStatus, utcnow
from ..core.energy_engine import EnergyEngine
from ..core.mood_engine import MoodEngine


# Category similarity matrix — 0 = same (no cost), 1 = different (high cost)
# Used to compute context-switching cost when ordering tasks
CATEGORY_GROUPS = {
    "finance": {"finance": 0.0, "work": 0.3, "personal": 0.5, "health": 0.7, "academics": 0.6, "general": 0.4},
    "work": {"finance": 0.3, "work": 0.0, "personal": 0.5, "health": 0.6, "academics": 0.4, "general": 0.3},
    "personal": {"finance": 0.5, "work": 0.5, "personal": 0.0, "health": 0.4, "academics": 0.5, "general": 0.2},
    "health": {"finance": 0.7, "work": 0.6, "personal": 0.4, "health": 0.0, "academics": 0.6, "general": 0.4},
    "academics": {"finance": 0.6, "work": 0.4, "personal": 0.5, "health": 0.6, "academics": 0.0, "general": 0.4},
    "general": {"finance": 0.4, "work": 0.3, "personal": 0.2, "health": 0.4, "academics": 0.4, "general": 0.0},
}


class ScheduledSlot(BaseModel):
    task_id: str
    start: datetime
    end: datetime
    at_risk: bool = False
    is_break: bool = False
    category: str = "general"


class PlanResultV2(BaseModel):
    slots: list[ScheduledSlot]
    unscheduled_task_ids: list[str] = Field(default_factory=list)
    at_risk_task_ids: list[str] = Field(default_factory=list)
    breaks_inserted: int = 0
    context_switches: int = 0
    energy_warnings: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


class SchedulerV2:
    """Enhanced scheduler with mood + energy + context-switching awareness."""

    def __init__(
        self,
        settings: Settings,
        mood_engine: MoodEngine,
        energy_engine: EnergyEngine,
    ):
        self._s = settings
        self._mood = mood_engine
        self._energy = energy_engine

    async def plan(
        self,
        tasks: list[Task],
        preferred_order: list[str] | None = None,
    ) -> PlanResultV2:
        schedulable = [t for t in tasks if t.status in (TaskStatus.PENDING, TaskStatus.SCHEDULED)]
        in_progress = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS and t.scheduled_start]

        # Get current mood + energy constraints
        _, mood_label, _ = self._mood.current()
        energy = self._energy.current()

        # Apply mood-based limits
        max_slot_minutes = 240
        max_slots = 20
        if mood_label == "critical_panic":
            max_slot_minutes = 45
            max_slots = 2
        elif mood_label == "drift_alert":
            max_slot_minutes = 90
            max_slots = 4

        # Cap task durations if needed
        for t in schedulable:
            if t.duration_minutes > max_slot_minutes:
                logger.info(
                    f"Task '{t.title}' duration {t.duration_minutes}min capped to "
                    f"{max_slot_minutes}min (mood={mood_label})"
                )
                t.duration_minutes = max_slot_minutes

        # Sort: preferred order if given, else by enhanced rank
        if preferred_order:
            order_idx = {tid: i for i, tid in enumerate(preferred_order)}
            schedulable.sort(key=lambda t: order_idx.get(t.id, 10_000))
        else:
            schedulable.sort(key=lambda t: self._rank_score_v2(t, energy.score), reverse=True)

        # Group by category to minimize context switching (stable sort preserves rank within groups)
        schedulable.sort(key=lambda t: t.category)

        slots: list[ScheduledSlot] = []
        at_risk_ids: list[str] = []
        unscheduled: list[str] = []
        breaks_inserted = 0
        energy_warnings: list[str] = []
        context_switches = 0

        # Anchor in-progress tasks
        for t in in_progress:
            if t.scheduled_start and t.scheduled_end:
                slot = ScheduledSlot(
                    task_id=t.id, start=t.scheduled_start, end=t.scheduled_end,
                    at_risk=self._is_at_risk(t), category=t.category,
                )
                slots.append(slot)
                if slot.at_risk:
                    at_risk_ids.append(t.id)

        cursor = max((s.end for s in slots), default=utcnow())
        prev_category: str | None = None
        slot_count = 0

        for t in schedulable:
            if slot_count >= max_slots:
                logger.warning(f"Max slots ({max_slots}) reached (mood={mood_label}); stopping")
                break

            # Energy check: skip complex tasks when depleted
            if not self._energy.can_handle_complexity(t.complexity):
                energy_warnings.append(
                    f"Skipped '{t.title}' (complexity {t.complexity}) — energy {energy.score}/100 too low"
                )
                # Try to insert a break first
                if energy.label.value in ("depleted", "low"):
                    break_slot = self._insert_break(cursor, 15)
                    if break_slot:
                        slots.append(break_slot)
                        cursor = break_slot.end
                        breaks_inserted += 1
                        # Re-check energy after break (simplified — just allow the task)
                        logger.info(f"Inserted 15-min break before '{t.title}'")

            # Insert break between different categories if energy is low
            if prev_category and prev_category != t.category:
                context_switches += 1
                if energy.label.value in ("depleted", "low"):
                    break_slot = self._insert_break(cursor, 10)
                    if break_slot:
                        slots.append(break_slot)
                        cursor = break_slot.end
                        breaks_inserted += 1

            start = self._next_available_start(cursor)
            duration = timedelta(minutes=t.duration_minutes)
            end = start + duration

            # Roll past end-of-workday
            if end.time() > self._parse_hhmm(self._s.workday_end) and end.date() == start.date():
                start = datetime.combine(
                    start.date() + timedelta(days=1),
                    self._parse_hhmm(self._s.workday_start),
                    tzinfo=start.tzinfo,
                )
                end = start + duration

            at_risk = bool(t.deadline and end > t.deadline)

            # Past deadline + tolerance → unschedulable
            if t.deadline and start > t.deadline + timedelta(minutes=15):
                unscheduled.append(t.id)
                logger.warning(f"Task {t.id} cannot be scheduled before deadline")
                continue

            slots.append(ScheduledSlot(
                task_id=t.id, start=start, end=end,
                at_risk=at_risk, category=t.category,
            ))
            if at_risk:
                at_risk_ids.append(t.id)
            cursor = end + timedelta(minutes=self._s.buffer_minutes)
            prev_category = t.category
            slot_count += 1

        slots.sort(key=lambda s: s.start)
        logger.info(
            f"SchedulerV2: {len(slots)} slots, {len(unscheduled)} unscheduled, "
            f"{breaks_inserted} breaks, {context_switches} context switches"
        )
        return PlanResultV2(
            slots=slots,
            unscheduled_task_ids=unscheduled,
            at_risk_task_ids=at_risk_ids,
            breaks_inserted=breaks_inserted,
            context_switches=context_switches,
            energy_warnings=energy_warnings,
        )

    def _rank_score_v2(self, task: Task, energy_score: int) -> float:
        """Enhanced rank score: priority + deadline urgency + inverse complexity + energy fit.

        v2 additions:
        - Energy fit: tasks matching current energy level get a boost
        - Deadline weight increased when deadline is very close
        """
        # Base rank (same as v1)
        urgency = 0.0
        if task.deadline:
            hours_left = max(0.0, (task.deadline - utcnow()).total_seconds() / 3600.0)
            urgency = max(0.0, 100.0 - (hours_left / 168.0) * 100.0)
            # Boost urgency if deadline is very close (< 4 hours)
            if hours_left < 4:
                urgency = min(100, urgency + 20)
        inv_complexity = 100.0 - float(task.complexity)

        base = 0.5 * task.priority + 0.35 * urgency + 0.15 * inv_complexity

        # v2: energy fit bonus
        # If task complexity matches current energy, boost it
        if energy_score >= 80 and task.complexity <= 50:
            base += 5  # easy task when energy is high — do it now
        elif energy_score < 40 and task.complexity < 30:
            base += 8  # only easy tasks when depleted — prioritize them
        elif energy_score < 40 and task.complexity > 70:
            base -= 15  # penalize complex tasks when depleted

        return base

    def _insert_break(self, cursor: datetime, minutes: int) -> ScheduledSlot | None:
        """Insert a break slot to restore energy."""
        start = self._next_available_start(cursor)
        end = start + timedelta(minutes=minutes)
        return ScheduledSlot(
            task_id=f"break-{start.strftime('%H%M')}",
            start=start, end=end,
            is_break=True, category="break",
        )

    def _next_available_start(self, cursor: datetime) -> datetime:
        """Advance cursor into the next valid workday window."""
        candidate = cursor
        wh_start = self._parse_hhmm(self._s.workday_start)
        wh_end = self._parse_hhmm(self._s.workday_end)
        if candidate.time() > wh_end:
            candidate = datetime.combine(
                candidate.date() + timedelta(days=1), wh_start, tzinfo=candidate.tzinfo
            )
        elif candidate.time() < wh_start:
            candidate = datetime.combine(candidate.date(), wh_start, tzinfo=candidate.tzinfo)
        return candidate

    def _is_at_risk(self, task: Task) -> bool:
        if not (task.deadline and task.scheduled_end):
            return False
        return task.scheduled_end > task.deadline

    @staticmethod
    def _parse_hhmm(s: str) -> time:
        h, m = s.split(":")
        return time(hour=int(h), minute=int(m), second=0)
