"""Adaptive Notification Manager — learns when reminders work.

Instead of fixed 30/15/5 minute reminders, this system learns:
- Which hours the user actually responds to notifications
- Which notification types get ignored
- When to delay reminders (e.g., during meetings)

The user should experience:
- Morning reminders ignored → PoKeBoo switches to lunch reminders
- Notifications during meetings → PoKeBoo delays them automatically
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from ..config import Settings
from ..contracts.messages import Task, TaskStatus, utcnow
from .productivity_model import PersonalProductivityModel
from .storage import Repository


class SmartNotification(BaseModel):
    """A notification that's been scheduled with adaptive timing."""
    notification_id: str
    task_id: str
    task_title: str
    message: str
    scheduled_for: datetime
    priority: str = "normal"  # low, normal, high, critical
    tier: int = 5  # minutes before deadline (5, 15, 30)
    status: str = "scheduled"  # scheduled, delivered, acted, ignored, delayed
    original_time: datetime | None = None  # if delayed, when was it originally for
    delay_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class AdaptiveNotificationManager:
    """Manages notifications with adaptive timing based on the productivity model.

    Strategy:
    1. Check deadline proximity (30/15/5 min tiers, scaled by priority)
    2. Check notification effectiveness for current hour
    3. If effectiveness < 0.3 → delay to next best hour
    4. If user is in a meeting (scheduled task in progress) → delay
    5. Record whether user acted on each notification
    """

    def __init__(self, settings: Settings, repo: Repository, model: PersonalProductivityModel):
        self._s = settings
        self._repo = repo
        self._model = model
        self._pending: dict[str, SmartNotification] = {}

    def check_deadlines(self, tasks: list[Task]) -> list[SmartNotification]:
        """Check all tasks for upcoming deadlines and generate smart notifications.

        Returns notifications that should be delivered NOW.
        """
        now = utcnow()
        notifications_to_deliver: list[SmartNotification] = []

        for task in tasks:
            if not task.deadline or task.status.value not in ("scheduled", "in_progress"):
                continue

            # Calculate minutes to deadline
            try:
                dl = task.deadline
                now_check = now
                if dl.tzinfo is None and now_check.tzinfo is not None:
                    now_check = now_check.replace(tzinfo=None)
                elif dl.tzinfo is not None and now_check.tzinfo is None:
                    dl = dl.replace(tzinfo=None)
                minutes_left = (dl - now_check).total_seconds() / 60
            except:
                continue

            if minutes_left <= 0 or minutes_left > 35:
                continue  # too far or already passed

            # Determine notification tier based on priority
            if task.priority >= 85:
                tiers = [30, 15, 5]
            elif task.priority >= 65:
                tiers = [15, 5]
            else:
                tiers = [5]

            for tier in tiers:
                if tier - 2 <= minutes_left <= tier + 2:
                    # Check if we already sent this tier
                    notif_id = f"{task.id}-{tier}"
                    if notif_id in self._pending:
                        continue

                    # Check notification effectiveness for current hour
                    current_hour = now.hour
                    effectiveness = self._model.get_notification_score(current_hour)

                    # Check if user is in a meeting (another task in progress)
                    in_meeting = any(
                        t.status.value == "in_progress" and t.id != task.id
                        for t in tasks
                    )

                    notification = SmartNotification(
                        notification_id=notif_id,
                        task_id=task.id,
                        task_title=task.title,
                        message=f"⏰ '{task.title}' deadline in ~{int(minutes_left)} min!",
                        scheduled_for=now,
                        priority="critical" if task.priority >= 85 else "high" if task.priority >= 65 else "normal",
                        tier=tier,
                    )

                    if effectiveness < 0.3 or in_meeting:
                        # Delay — find next best hour
                        best_hour = self._model.get_recommended_notification_hour()
                        if best_hour != current_hour and minutes_left > 10:
                            # Schedule for the best hour instead
                            delayed_time = now.replace(hour=best_hour, minute=0, second=0)
                            if delayed_time > now and delayed_time < (dl if 'dl' in dir() else now + timedelta(hours=1)):
                                notification.scheduled_for = delayed_time
                                notification.original_time = now
                                notification.delay_count = 1
                                notification.status = "delayed"
                                notification.message = f"⏰ '{task.title}' — deadline approaching!"
                                self._pending[notif_id] = notification
                                logger.info(
                                    f"[Notifications] Delayed notification for '{task.title}' "
                                    f"from {current_hour}:00 to {best_hour}:00 "
                                    f"(effectiveness: {effectiveness:.2f}, in_meeting: {in_meeting})"
                                )
                                continue

                    # Deliver now
                    notification.status = "delivered"
                    self._pending[notif_id] = notification
                    notifications_to_deliver.append(notification)
                    break

        return notifications_to_deliver

    def record_action(self, notification_id: str, acted: bool) -> None:
        """Record whether the user acted on a notification."""
        notif = self._pending.get(notification_id)
        if not notif:
            return

        notif.status = "acted" if acted else "ignored"
        hour = notif.scheduled_for.hour
        self._model.record_notification(
            hour=hour,
            acted=acted,
            notification_type=f"deadline_tier_{notif.tier}",
        )

        if acted:
            logger.info(f"[Notifications] User ACTED on notification '{notif.task_title}' at {hour}:00")
        else:
            logger.info(f"[Notifications] User IGNORED notification '{notif.task_title}' at {hour}:00")

    def get_pending(self) -> list[dict]:
        """Get all pending/delayed notifications."""
        return [
            n.model_dump(mode="json")
            for n in self._pending.values()
            if n.status in ("scheduled", "delayed")
        ]

    def get_recommended_strategy(self) -> dict:
        """Return the current recommended notification strategy based on learning."""
        p = self._model.get_profile()
        best_hour = self._model.get_recommended_notification_hour()

        # Find ignored hours
        ignored_hours = [
            h for h, score in p.notification_effectiveness.items()
            if score < 0.3
        ]

        # Find effective hours
        effective_hours = [
            h for h, score in p.notification_effectiveness.items()
            if score > 0.6
        ]

        return {
            "best_hour": f"{best_hour}:00",
            "effective_hours": [f"{h}:00" for h in sorted(effective_hours)],
            "avoid_hours": [f"{h}:00" for h in sorted(ignored_hours)],
            "total_notifications_sent": p.total_notifications,
            "strategy": self._generate_strategy_text(best_hour, ignored_hours, effective_hours),
        }

    @staticmethod
    def _generate_strategy_text(best: int, ignored: list[int], effective: list[int]) -> str:
        parts = []
        if effective:
            parts.append(f"Reminders work best at {', '.join(f'{h}:00' for h in effective[:3])}.")
        if ignored:
            parts.append(f"User ignores reminders at {', '.join(f'{h}:00' for h in ignored[:3])} — avoiding those times.")
        if not effective and not ignored:
            parts.append("Still learning optimal notification times.")
        return " ".join(parts)
