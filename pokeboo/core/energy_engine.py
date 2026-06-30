"""Energy Engine — execution capacity tracking (separate from mood).

Energy = how much work you CAN do.
Mood = how you FEEL about doing it.

These are independent dimensions:
- High mood + high energy → peak productivity (deep focus tasks)
- High mood + low energy → motivated but tired (light tasks only)
- Low mood + high energy → physically capable but stressed (routine tasks)
- Low mood + low energy → burnout risk (rest needed)

Energy depletes with:
- Task completion (proportional to complexity)
- Drift events (stress drains energy)
- Long work sessions without breaks

Energy restores with:
- Breaks (scheduled between tasks)
- Task completion satisfaction (+5)
- Sleep/overnight reset (full restore)
"""
from __future__ import annotations

from datetime import timedelta

from loguru import logger

from ..config import Settings
from ..contracts.messages import DriftEvent, EnergyLabel, EnergySnapshot, Task, TaskStatus, utcnow
from .storage import Repository


# --- Delta rules ---
ENERGY_BASELINE = 80  # start each day at 80 (not 100 — realistic)

# Depletion
DELTA_PER_TASK_COMPLETED = -8        # completing a task costs energy
DELTA_PER_COMPLEXITY_POINT = -0.3    # complex tasks cost more (complexity 80 → -24)
DELTA_PER_DRIFT_15_MIN = -5          # drift events drain energy
DELTA_PER_HOUR_WORKING = -3          # sustained work depletes gradually

# Restoration
DELTA_PER_BREAK_15_MIN = +8          # breaks restore energy
DELTA_PER_TASK_COMPLETED_SATISFACTION = +3  # small boost from completion
DELTA_OVERNIGHT_RESET = 100          # full restore on new day


class EnergyEngine:
    """Deterministic energy scorer. Independent from MoodEngine.

    Tracks execution capacity over time. The scheduler consults this
    to avoid scheduling complex tasks when energy is depleted.
    """

    def __init__(self, settings: Settings, repo: Repository):
        self.baseline = ENERGY_BASELINE
        self._repo = repo

    def current(self) -> EnergySnapshot:
        """Return the latest energy snapshot, or baseline if none exists.

        The displayed energy score now reflects SCHEDULE DENSITY:
        - Empty schedule → high energy (100)
        - Packed schedule → low energy (20-40)
        - Deadline approaching → drops further

        This gives the user an instant visual sense of how busy their day is
        just by looking at the energy bar next to the mascot.
        """
        # Get base energy from storage (tracks actual work depletion)
        stored = self._repo.latest_energy()
        base_score = stored.score if stored else self.baseline

        # Compute schedule density
        density_info = self.get_schedule_density()
        density = density_info["density"]  # 0-100

        # Invert density: empty schedule (0%) = high energy (100), packed (100%) = low (20)
        density_score = max(20, 100 - density)

        # Blend: 60% density-based + 40% stored energy
        # This means: a packed day with no work done yet still shows low energy
        # But an empty day where you've been working hard shows slightly lower
        display_score = int(0.6 * density_score + 0.4 * base_score)

        # If deadlines are approaching, drop further
        upcoming = density_info.get("upcoming_deadlines", [])
        urgent = [d for d in upcoming if d.get("hours_left", 999) < 6]
        if urgent:
            display_score -= len(urgent) * 5

        display_score = max(5, min(100, display_score))

        if display_score >= 80:
            label = EnergyLabel.FULL
        elif display_score >= 60:
            label = EnergyLabel.STEADY
        elif display_score >= 40:
            label = EnergyLabel.LOW
        else:
            label = EnergyLabel.DEPLETED

        # Build causes from density info
        causes = [density_info["description"]]
        if stored and stored.causes:
            causes.extend(stored.causes[-2:])  # keep last 2 stored causes

        return EnergySnapshot(
            score=display_score,
            label=label,
            causes=causes,
            recommendations=density_info.get("description", "").split(". ")[:2],
        )

    def on_task_started(self, task: Task) -> EnergySnapshot:
        """Starting a complex task costs a small upfront energy commitment."""
        delta = -int(task.complexity * 0.1)  # 10% of complexity as upfront cost
        return self._apply(delta, [f"Started task '{task.title}' (complexity {task.complexity})"])

    def on_task_completed(self, task: Task) -> EnergySnapshot:
        """Completing a task costs energy based on ACTUAL time spent.

        Task type matters:
        - DEADLINE-based + finished before deadline → energy BOOST (satisfaction + relief)
        - PRODUCTIVITY-based + did less than planned → small energy loss (guilt)
        - PRODUCTIVITY-based + did more than planned → small energy boost
        """
        # Calculate actual work time
        if task.started_at and task.completed_at:
            try:
                from datetime import timezone
                start = task.started_at
                end = task.completed_at
                if start.tzinfo is None and end.tzinfo is not None:
                    end = end.replace(tzinfo=None)
                elif start.tzinfo is not None and end.tzinfo is None:
                    start = start.replace(tzinfo=None)
                actual_minutes = max(5, int((end - start).total_seconds() / 60))
            except:
                actual_minutes = task.duration_minutes
        else:
            actual_minutes = task.duration_minutes

        estimated = task.duration_minutes
        is_deadline_based = task.deadline is not None

        # Base energy cost = actual time spent × complexity factor
        actual_complexity_cost = int(task.complexity * abs(DELTA_PER_COMPLEXITY_POINT) * (actual_minutes / max(estimated, 1)))
        total_delta = DELTA_PER_TASK_COMPLETED - actual_complexity_cost + DELTA_PER_TASK_COMPLETED_SATISFACTION

        if is_deadline_based:
            # Check if beat the deadline
            try:
                dl = task.deadline
                comp = task.completed_at
                if dl.tzinfo is None and comp.tzinfo is not None:
                    comp = comp.replace(tzinfo=None)
                elif dl.tzinfo is not None and comp.tzinfo is None:
                    dl = dl.replace(tzinfo=None)

                if comp <= dl:
                    # Beat deadline — energy boost (relief + satisfaction)
                    if estimated > 0 and actual_minutes < estimated * 0.4:
                        # Way early — big boost
                        total_delta += 8
                        return self._apply(total_delta, [
                            f"'{task.title}' crushed before deadline — energy surge!"
                        ])
                    total_delta += 4
                    return self._apply(total_delta, [
                        f"'{task.title}' beat the deadline — energy boost!"
                    ])
                else:
                    # Past deadline — extra drain (stress)
                    total_delta -= 3
                    return self._apply(total_delta, [
                        f"'{task.title}' past deadline — stress drain."
                    ])
            except:
                pass

        # PRODUCTIVITY-based task
        if estimated > 0 and actual_minutes > 0:
            ratio = actual_minutes / estimated
            if ratio < 0.6:
                # Did less than planned — slight guilt drain
                total_delta -= 2
                return self._apply(total_delta, [
                    f"'{task.title}' — only {actual_minutes}min of {estimated}min planned."
                ])
            elif ratio > 1.2:
                # Did more than planned — satisfaction boost
                total_delta += 3
                return self._apply(total_delta, [
                    f"'{task.title}' — did {actual_minutes}min, more than planned!"
                ])

        return self._apply(total_delta, [
            f"Completed '{task.title}' (actual {actual_minutes}min)"
        ])

    def on_task_missed(self, task: Task) -> EnergySnapshot:
        """Missing a task drains energy (stress + lost effort)."""
        return self._apply(-10, [f"Missed task '{task.title}' (-10)"])

    def on_drift(self, event: DriftEvent) -> EnergySnapshot:
        """Drift events drain energy (stress + disruption)."""
        units = max(1, event.minutes_lost // 15)
        delta = DELTA_PER_DRIFT_15_MIN * units
        return self._apply(delta, [
            f"Drift '{event.kind}': {event.minutes_lost} min lost ({delta})"
        ])

    def on_break(self, minutes: int) -> EnergySnapshot:
        """Scheduled breaks restore energy."""
        units = max(1, minutes // 15)
        delta = DELTA_PER_BREAK_15_MIN * units
        return self._apply(delta, [f"Break: {minutes} min (+{delta})"])

    def recompute_from_state(self, tasks: list[Task]) -> EnergySnapshot:
        """Rebuild energy from current task state — used on startup."""
        score = self.baseline
        reasons: list[str] = []
        today = utcnow().date()

        for t in tasks:
            # Only count today's activity
            if t.completed_at and t.completed_at.date() == today:
                complexity_cost = int(t.complexity * abs(DELTA_PER_COMPLEXITY_POINT))
                delta = DELTA_PER_TASK_COMPLETED - complexity_cost + DELTA_PER_TASK_COMPLETED_SATISFACTION
                score += delta
                reasons.append(f"Completed: {t.title} ({delta})")
            elif t.status == TaskStatus.MISSED and t.created_at.date() == today:
                score -= 10
                reasons.append(f"Missed: {t.title} (-10)")
            elif t.status == TaskStatus.IN_PROGRESS:
                # Active tasks have an upfront cost already paid
                pass

        # Count hours worked today (rough estimate from completed tasks)
        completed_today = [
            t for t in tasks
            if t.status == TaskStatus.DONE and t.completed_at and t.completed_at.date() == today
        ]
        total_minutes = sum(t.duration_minutes for t in completed_today)
        hours_worked = total_minutes / 60
        fatigue = int(hours_worked * abs(DELTA_PER_HOUR_WORKING))
        score -= fatigue
        if fatigue > 0:
            reasons.append(f"Fatigue: {hours_worked:.1f}h worked (-{fatigue})")

        score = max(0, min(100, score))
        snap = EnergySnapshot.from_score(score, reasons or ["Baseline — start of day"])
        self._repo.append_energy(snap.score, snap.label.value, snap.causes)
        logger.info(f"Energy recomputed from state: {score} ({snap.label.value})")
        return snap

    def can_handle_complexity(self, complexity: int) -> bool:
        """Check if current energy can handle a task of given complexity.

        Uses STORED energy (actual depletion), not display score (which includes
        schedule density). This ensures the scheduler makes decisions based on
        actual capacity, not how busy the day looks.
        """
        snap = self._repo.latest_energy()
        stored_score = snap.score if snap else self.baseline

        if stored_score >= 80:
            return complexity <= 100
        elif stored_score >= 60:
            return complexity <= 80
        elif stored_score >= 40:
            return complexity <= 60
        else:
            return complexity <= 30

    def get_schedule_density(self) -> dict:
        """Describe how busy the day is — used by UI to show schedule state.

        Returns:
            - density: 0-100 (how full the schedule is)
            - density_label: "empty" | "light" | "moderate" | "packed"
            - upcoming_deadlines: list of tasks with deadlines in next 24h
            - free_hours: estimated free focus hours today
            - description: human-readable summary
        """
        tasks = self._repo.list_tasks()
        from ..contracts.messages import TaskStatus, utcnow
        from datetime import datetime as _dt

        now = utcnow()
        today_str = now.strftime("%Y-%m-%d")

        # Scheduled tasks today
        scheduled_today = [
            t for t in tasks
            if t.status in (TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS)
            and t.scheduled_start
            and t.scheduled_start.strftime("%Y-%m-%d") == today_str
        ]

        # Total scheduled minutes today
        total_minutes = sum(t.duration_minutes for t in scheduled_today)

        # Workday capacity (e.g., 9am-10pm = 13 hours = 780 min)
        wh_start = self._parse_hhmm(self._s.workday_start) if hasattr(self, '_s') else None
        wh_end = self._parse_hhmm(self._s.workday_end) if hasattr(self, '_s') else None
        if wh_start and wh_end:
            workday_minutes = (wh_end.hour - wh_start.hour) * 60
        else:
            workday_minutes = 780  # default 13h

        density = min(100, int((total_minutes / workday_minutes) * 100)) if workday_minutes > 0 else 0

        if density == 0:
            density_label = "empty"
            description = "Schedule is empty — plenty of time available."
        elif density < 30:
            density_label = "light"
            description = f"Light day — {total_minutes}min scheduled, lots of free time."
        elif density < 60:
            density_label = "moderate"
            description = f"Moderate load — {total_minutes}min scheduled."
        elif density < 85:
            density_label = "busy"
            description = f"Busy day — {total_minutes}min scheduled. Watch your energy."
        else:
            density_label = "packed"
            description = f"Packed schedule — {total_minutes}min. Consider reducing load."

        # Upcoming deadlines (next 24h)
        upcoming_deadlines = []
        for t in tasks:
            if not t.deadline or t.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
                continue
            try:
                dl = t.deadline
                now_check = now
                if dl.tzinfo is None:
                    now_check = now.replace(tzinfo=None)
                hours_left = (dl - now_check).total_seconds() / 3600
                if 0 < hours_left <= 24:
                    upcoming_deadlines.append({
                        "task_title": t.title,
                        "hours_left": round(hours_left, 1),
                        "priority": t.priority,
                    })
            except:
                continue

        # Free focus hours
        free_hours = max(0, (workday_minutes - total_minutes) / 60)

        # If deadlines are approaching, boost urgency
        if upcoming_deadlines:
            urgent = [d for d in upcoming_deadlines if d["hours_left"] < 6]
            if urgent:
                description += f" ⚠️ {len(urgent)} deadline(s) within 6 hours!"

        return {
            "density": density,
            "density_label": density_label,
            "description": description,
            "scheduled_minutes": total_minutes,
            "free_hours": round(free_hours, 1),
            "upcoming_deadlines": upcoming_deadlines,
            "task_count": len(scheduled_today),
        }

    def _parse_hhmm(self, s: str):
        from datetime import time
        h, m = s.split(":")
        return time(hour=int(h), minute=int(m), second=0)

    def _baseline_snapshot(self) -> EnergySnapshot:
        return EnergySnapshot.from_score(self.baseline, ["Baseline — system initialized"])

    def _apply(self, delta: int, reasons: list[str]) -> EnergySnapshot:
        prev = self._repo.latest_energy()
        prev_score = prev.score if prev else self.baseline
        new_score = max(0, min(100, prev_score + delta))
        snap = EnergySnapshot.from_score(new_score, reasons)
        self._repo.append_energy(snap.score, snap.label.value, snap.causes)
        logger.info(
            f"Energy {prev_score} -> {new_score} (delta={delta:+d}) — {'; '.join(reasons)}"
        )
        return snap
