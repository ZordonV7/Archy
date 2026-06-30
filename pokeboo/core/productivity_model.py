"""Personal Productivity Model — the evolving user profile.

This is the single source of truth for everything PoKeBoo learns about
the user. Every other system (RiskEngine, SchedulerV2, Notifications,
FailureSimulator) queries this model.

9 Dimensions Learned:
1. Preferred work hours     — when the user completes tasks fastest
2. Focus duration           — average session length before interruption
3. Context-switch cost      — energy drop when switching categories
4. Underestimation bias     — per-category duration multiplier
5. Interruption tolerance   — how many pauses before productivity drops
6. Recovery after missed    — how mood/energy recover after a miss
7. Deadline reliability     — P(completed before deadline) per category
8. Notification effectiveness — which hours get ignored vs acted on
9. Best task ordering       — which category sequences work best

The model is stored as JSON in the preferences table and updated after
every task completion, session end, notification interaction, and drift event.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from ..contracts.messages import Task, TaskStatus, utcnow
from .storage import Repository


class ProductivityProfile(BaseModel):
    """The complete productivity profile — persisted as JSON."""

    # 1. Preferred work hours (hour → completion rate 0-1)
    preferred_hours: dict[int, float] = Field(default_factory=dict)

    # 2. Focus duration (minutes)
    avg_focus_duration: int = 45
    focus_durations: list[int] = Field(default_factory=list)  # last 20 sessions

    # 3. Context-switch cost (energy drop per category transition)
    context_switch_costs: dict[str, float] = Field(default_factory=dict)  # "from->to" → 0-1

    # 4. Underestimation bias (category → multiplier)
    duration_multipliers: dict[str, float] = Field(default_factory=dict)

    # 5. Interruption tolerance (avg interruptions before productivity drops)
    interruption_tolerance: int = 3
    interruption_history: list[int] = Field(default_factory=list)  # last 20 sessions

    # 6. Recovery after missed work (mood delta recovery rate per hour)
    recovery_rate: float = 2.0  # mood points recovered per hour after a miss

    # 7. Deadline reliability (category → P(completed on time))
    deadline_reliability: dict[str, float] = Field(default_factory=dict)

    # 8. Notification effectiveness (hour → response rate 0-1)
    notification_effectiveness: dict[int, float] = Field(default_factory=dict)
    notification_history: list[dict] = Field(default_factory=list)  # last 50 notifications

    # 9. Best task ordering (category pair → success rate)
    task_ordering_scores: dict[str, float] = Field(default_factory=dict)  # "cat1->cat2" → 0-1

    # Metadata
    total_completions: int = 0
    total_sessions: int = 0
    total_notifications: int = 0
    last_updated: str = ""


class PersonalProductivityModel:
    """Learns and stores the user's productivity profile.

    Usage:
        model = PersonalProductivityModel(repo)
        model.record_completion(task, actual_minutes, session_interruptions)
        model.record_notification(hour=9, acted=True)
        profile = model.get_profile()
        # profile.preferred_hours = {10: 0.85, 14: 0.72, 16: 0.65, ...}
    """

    PREF_KEY = "productivity_profile"

    def __init__(self, repo: Repository):
        self._repo = repo
        self._cache: ProductivityProfile | None = None

    async def __call__(self, event_type: str, payload: dict) -> None:
        """EventBus subscriber — routes events to the right learning method."""
        try:
            if event_type == "TaskCompleted":
                # Reconstruct minimal task info from event payload
                task = Task(
                    id=payload.get("id", ""),
                    raw_transcript=payload.get("raw_transcript", ""),
                    title=payload.get("title", ""),
                    category=payload.get("category", "general"),
                    priority=payload.get("priority", 50),
                    complexity=payload.get("complexity", 50),
                    deadline=None,
                    duration_minutes=payload.get("duration_minutes", 30),
                    status=TaskStatus.DONE,
                    created_at=datetime.fromisoformat(payload["created_at"]) if "created_at" in payload else utcnow(),
                    completed_at=datetime.fromisoformat(payload["completed_at"]) if "completed_at" in payload else None,
                    started_at=datetime.fromisoformat(payload["started_at"]) if "started_at" in payload else None,
                )
                # Try to get actual minutes from the payload or the work session
                actual_min = payload.get("actual_minutes", 0)
                if not actual_min and task.started_at and task.completed_at:
                    try:
                        start = task.started_at
                        end = task.completed_at
                        if start.tzinfo is None and end.tzinfo is not None:
                            end = end.replace(tzinfo=None)
                        elif start.tzinfo is not None and end.tzinfo is None:
                            start = start.replace(tzinfo=None)
                        actual_min = int((end - start).total_seconds() / 60)
                    except:
                        actual_min = 0
                self.record_completion(task, actual_min, 0)

            elif event_type == "TaskMissed":
                task = Task(
                    id=payload.get("id", ""),
                    raw_transcript=payload.get("raw_transcript", ""),
                    title=payload.get("title", ""),
                    category=payload.get("category", "general"),
                    priority=payload.get("priority", 50),
                    complexity=payload.get("complexity", 50),
                    deadline=None,
                    duration_minutes=payload.get("duration_minutes", 30),
                    status=TaskStatus.MISSED,
                    created_at=datetime.fromisoformat(payload["created_at"]) if "created_at" in payload else utcnow(),
                )
                self.record_miss(task)

        except Exception as e:
            logger.error(f"[ProductivityModel] Event handler failed for {event_type}: {e}")

    def get_profile(self) -> ProductivityProfile:
        """Get the current profile (cached)."""
        if self._cache is not None:
            return self._cache
        raw = self._repo.get_preference(self.PREF_KEY)
        if raw:
            try:
                data = json.loads(raw)
                self._cache = ProductivityProfile.model_validate(data)
                return self._cache
            except Exception:
                pass
        self._cache = ProductivityProfile()
        return self._cache

    def save(self) -> None:
        """Persist the profile to DB."""
        if self._cache is None:
            return
        self._cache.last_updated = utcnow().isoformat()
        self._cache.total_completions = min(self._cache.total_completions, 9999)
        self._cache.total_sessions = min(self._cache.total_sessions, 9999)
        try:
            self._repo.set_preference(
                self.PREF_KEY,
                self._cache.model_dump_json(),
                category="productivity",
            )
        except Exception as e:
            logger.error(f"Failed to save productivity profile: {e}")

    # --- Recording events ---

    def record_completion(
        self,
        task: Task,
        actual_minutes: int,
        session_interruptions: int = 0,
    ) -> None:
        """Record a task completion — updates dimensions 1,2,4,5,7,9."""
        p = self.get_profile()
        p.total_completions += 1

        # 1. Preferred work hours
        if task.completed_at:
            hour = task.completed_at.hour
            # Increase this hour's completion rate
            current = p.preferred_hours.get(hour, 0.5)
            # Exponential moving average — weight recent completions more
            p.preferred_hours[hour] = current * 0.8 + 1.0 * 0.2

            # Decay other hours slightly (so the best hours stand out)
            for h in list(p.preferred_hours.keys()):
                if h != hour:
                    p.preferred_hours[h] = p.preferred_hours[h] * 0.95

        # 2. Focus duration
        if actual_minutes > 0:
            p.focus_durations.append(actual_minutes)
            p.focus_durations = p.focus_durations[-20:]  # keep last 20
            p.avg_focus_duration = int(sum(p.focus_durations) / len(p.focus_durations))

        # 4. Underestimation bias
        if task.duration_minutes > 0 and actual_minutes > 0:
            ratio = actual_minutes / task.duration_minutes
            cat = task.category or "general"
            current_mult = p.duration_multipliers.get(cat, 1.0)
            # EMA toward the observed ratio
            p.duration_multipliers[cat] = current_mult * 0.7 + ratio * 0.3

        # 5. Interruption tolerance
        if session_interruptions >= 0:
            p.interruption_history.append(session_interruptions)
            p.interruption_history = p.interruption_history[-20:]
            if p.interruption_history:
                p.interruption_tolerance = int(
                    sum(p.interruption_history) / len(p.interruption_history)
                )

        # 7. Deadline reliability
        if task.deadline and task.completed_at:
            cat = task.category or "general"
            try:
                dl = task.deadline
                comp = task.completed_at
                if dl.tzinfo is None and comp.tzinfo is not None:
                    comp = comp.replace(tzinfo=None)
                elif dl.tzinfo is not None and comp.tzinfo is None:
                    dl = dl.replace(tzinfo=None)
                on_time = 1.0 if comp <= dl else 0.0
                current = p.deadline_reliability.get(cat, 0.5)
                p.deadline_reliability[cat] = current * 0.7 + on_time * 0.3
            except:
                pass

        # 9. Best task ordering — track if this task followed another
        # (simplified: just track category → success)
        cat = task.category or "general"
        current_score = p.task_ordering_scores.get(f"{cat}:complete", 0.5)
        p.task_ordering_scores[f"{cat}:complete"] = current_score * 0.8 + 1.0 * 0.2

        self.save()
        logger.info(
            f"[ProductivityModel] Recorded completion: '{task.title}' "
            f"(actual {actual_minutes}min, {session_interruptions} interruptions) "
            f"— profile updated"
        )

    def record_session(
        self,
        task: Task,
        actual_minutes: int,
        interruptions: int,
        prev_category: str | None = None,
    ) -> None:
        """Record a work session end — updates dimensions 2,3,5."""
        p = self.get_profile()
        p.total_sessions += 1

        # 2. Focus duration
        if actual_minutes > 0:
            p.focus_durations.append(actual_minutes)
            p.focus_durations = p.focus_durations[-20:]
            p.avg_focus_duration = int(sum(p.focus_durations) / len(p.focus_durations))

        # 3. Context-switch cost
        if prev_category and prev_category != task.category:
            key = f"{prev_category}->{task.category}"
            # Higher interruptions = higher switch cost
            cost = min(1.0, interruptions / 5.0)
            current = p.context_switch_costs.get(key, 0.3)
            p.context_switch_costs[key] = current * 0.7 + cost * 0.3

        # 5. Interruption tolerance
        p.interruption_history.append(interruptions)
        p.interruption_history = p.interruption_history[-20:]
        if p.interruption_history:
            p.interruption_tolerance = int(
                sum(p.interruption_history) / len(p.interruption_history)
            )

        self.save()

    def record_notification(self, hour: int, acted: bool, notification_type: str = "deadline") -> None:
        """Record whether the user acted on a notification — updates dimension 8."""
        p = self.get_profile()
        p.total_notifications += 1

        # 8. Notification effectiveness
        current = p.notification_effectiveness.get(hour, 0.5)
        response = 1.0 if acted else 0.0
        p.notification_effectiveness[hour] = current * 0.7 + response * 0.3

        # Store history
        p.notification_history.append({
            "hour": hour,
            "acted": acted,
            "type": notification_type,
            "timestamp": utcnow().isoformat(),
        })
        p.notification_history = p.notification_history[-50:]

        self.save()
        logger.info(
            f"[ProductivityModel] Notification at {hour}:00 — {'acted' if acted else 'ignored'} "
            f"(effectiveness: {p.notification_effectiveness.get(hour, 0.5):.2f})"
        )

    def record_miss(self, task: Task) -> None:
        """Record a missed task — updates dimensions 6,7."""
        p = self.get_profile()
        cat = task.category or "general"

        # 7. Deadline reliability — missed = 0
        current = p.deadline_reliability.get(cat, 0.5)
        p.deadline_reliability[cat] = current * 0.7 + 0.0 * 0.3

        # 9. Task ordering
        current_score = p.task_ordering_scores.get(f"{cat}:complete", 0.5)
        p.task_ordering_scores[f"{cat}:complete"] = current_score * 0.8 + 0.0 * 0.2

        self.save()

    def record_drift(self, minutes_lost: int) -> None:
        """Record a drift event — affects recovery rate estimation."""
        p = self.get_profile()
        # More frequent drifts → lower recovery rate
        p.recovery_rate = max(0.5, p.recovery_rate - 0.1)
        self.save()

    # --- Querying the model ---

    def get_best_hours(self, top_n: int = 3) -> list[int]:
        """Return the top N most productive hours."""
        p = self.get_profile()
        if not p.preferred_hours:
            return [9, 10, 14]  # defaults
        sorted_hours = sorted(p.preferred_hours.items(), key=lambda x: x[1], reverse=True)
        return [h for h, _ in sorted_hours[:top_n]]

    def get_worst_hours(self, top_n: int = 2) -> list[int]:
        """Return the least productive hours."""
        p = self.get_profile()
        if not p.preferred_hours:
            return [15, 16]
        sorted_hours = sorted(p.preferred_hours.items(), key=lambda x: x[1])
        return [h for h, _ in sorted_hours[:top_n]]

    def get_duration_multiplier(self, category: str) -> float:
        """Get the learned duration multiplier for a category."""
        p = self.get_profile()
        return p.duration_multipliers.get(category, 1.0)

    def get_deadline_reliability(self, category: str) -> float:
        """Get P(completed before deadline) for a category."""
        p = self.get_profile()
        return p.deadline_reliability.get(category, 0.5)

    def get_context_switch_cost(self, from_cat: str, to_cat: str) -> float:
        """Get the energy cost of switching between categories."""
        if from_cat == to_cat:
            return 0.0
        p = self.get_profile()
        return p.context_switch_costs.get(f"{from_cat}->{to_cat}", 0.3)

    def get_notification_score(self, hour: int) -> float:
        """Get the effectiveness score for sending a notification at this hour."""
        p = self.get_profile()
        return p.notification_effectiveness.get(hour, 0.5)

    def get_recommended_notification_hour(self, preferred_hours: list[int] = None) -> int:
        """Get the best hour to send a notification based on past effectiveness."""
        p = self.get_profile()
        if not p.notification_effectiveness:
            # Fall back to preferred work hours
            best = self.get_best_hours(1)
            return best[0] if best else 9
        # Find the hour with highest effectiveness
        best_hour = max(p.notification_effectiveness, key=p.notification_effectiveness.get)
        return best_hour

    def get_profile_summary(self) -> dict:
        """Return a human-readable summary of the entire profile."""
        p = self.get_profile()
        best = self.get_best_hours(3)
        worst = self.get_worst_hours(2)
        best_hour_str = ", ".join(f"{h}:00" for h in best) if best else "unknown"
        worst_hour_str = ", ".join(f"{h}:00" for h in worst) if worst else "unknown"

        # Build bias descriptions
        biases = []
        for cat, mult in sorted(p.duration_multipliers.items(), key=lambda x: x[1], reverse=True):
            if mult > 1.15:
                biases.append(f"Underestimates {cat} by {int((mult-1)*100)}%")
            elif mult < 0.85:
                biases.append(f"Overestimates {cat} by {int((1-mult)*100)}%")

        # Deadline reliability
        reliability = []
        for cat, rate in sorted(p.deadline_reliability.items(), key=lambda x: x[1]):
            reliability.append(f"{cat}: {int(rate*100)}% on-time")

        return {
            "total_completions": p.total_completions,
            "total_sessions": p.total_sessions,
            "best_hours": best_hour_str,
            "worst_hours": worst_hour_str,
            "avg_focus_duration": f"{p.avg_focus_duration} min",
            "interruption_tolerance": f"{p.interruption_tolerance} per session",
            "recovery_rate": f"{p.recovery_rate} mood points/hr after miss",
            "biases": biases,
            "deadline_reliability": reliability,
            "best_notification_hour": f"{self.get_recommended_notification_hour()}:00",
            "notification_responsiveness": {
                str(h): f"{int(r*100)}%" for h, r in sorted(p.notification_effectiveness.items())
            },
            "context_switch_costs": {
                k: f"{int(v*100)}% energy drop" for k, v in p.context_switch_costs.items()
            },
        }
