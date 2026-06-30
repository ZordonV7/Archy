"""Calendar subscriber — listens to PokeBo events and syncs them to Calendar.

This is an optional subscriber. Enable via `POKEBOO_CALENDAR_SYNC_ENABLED=true`.
If Google auth isn't set up, the subscriber logs warnings but never breaks
the rest of the pipeline.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from ..config import Settings
from ..contracts.messages import TaskStatus
from ..core.storage import Repository
from .auth import GoogleAuth
from .calendar_client import CalendarClient


EventCallback = Callable[[str, dict], Awaitable[None]]


class CalendarSubscriber:
    """Subscribes to PokeBo events and pushes them to Google Calendar.

    Events handled:
    - plan.ready: sync all scheduled slots to Calendar
    - task.completed: mark event green
    - task.cancelled: delete Calendar event
    - task.missed: mark event red (via delete + re-create with red color)
    """

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        auth: GoogleAuth,
        calendar: CalendarClient,
    ):
        self._settings = settings
        self._repo = repo
        self._auth = auth
        self._calendar = calendar

    async def __call__(self, event_type: str, payload: dict) -> None:
        """Event callback — handles individual events."""
        if not self._settings.calendar_sync_enabled:
            return
        if not self._auth.is_authenticated():
            logger.warning(
                "Calendar sync enabled but not authenticated. "
                "Run `pokeboo auth` to enable."
            )
            return

        try:
            if event_type == "PlanReady":
                await self._handle_plan_ready(payload)
            elif event_type == "TaskCompleted":
                await self._handle_task_completed(payload)
            elif event_type in ("TaskCancelled", "TaskMissed"):
                await self._handle_task_removed(payload)
        except Exception:
            logger.exception(f"Calendar sync failed for {event_type}")

    async def _handle_plan_ready(self, payload: dict) -> None:
        """Sync all scheduled slots from the latest plan."""
        slots = payload.get("slots", [])
        calendar_id = self._settings.calendar_id
        for slot in slots:
            task = self._repo.get_task(slot["task_id"])
            if task:
                self._calendar.sync_task(task, calendar_id=calendar_id)

    async def _handle_task_completed(self, payload: dict) -> None:
        """Mark the Calendar event green when a task is completed."""
        task_id = payload.get("id")
        if not task_id:
            return
        task = self._repo.get_task(task_id)
        if task:
            self._calendar.mark_task_completed(task, calendar_id=self._settings.calendar_id)

    async def _handle_task_removed(self, payload: dict) -> None:
        """Delete the Calendar event when a task is cancelled/missed."""
        task_id = payload.get("id")
        if task_id:
            self._calendar.delete_task_event(task_id, calendar_id=self._settings.calendar_id)
