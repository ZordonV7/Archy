"""Google Calendar API wrapper.

Creates/updates/deletes events from Archy's scheduled tasks.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from ..contracts.messages import Task, TaskStatus


class CalendarClient:
    """Wraps Google Calendar API for Archy's use.

    All events go to a single calendar (default: 'primary').
    Event IDs are stored as task IDs prefixed with 'archy-' to avoid collisions
    with user's manually created events.
    """

    def __init__(self, auth):
        self._auth = auth
        self._service: Any = None

    def _get_service(self):
        if self._service is None:
            from googleapiclient.discovery import build
            creds = self._auth.get_credentials()
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    @staticmethod
    def _event_id(task_id: str) -> str:
        return f"archy-{task_id}"

    def sync_task(self, task: Task, calendar_id: str = "primary") -> str | None:
        """Create or update a Calendar event for the given task.

        Returns the event ID, or None if the task has no scheduled slot.
        """
        if not (task.scheduled_start and task.scheduled_end):
            return None

        service = self._get_service()
        event_body = self._build_event(task)

        try:
            # Try to fetch existing event; if 404, create new.
            existing = (
                service.events()
                .get(calendarId=calendar_id, eventId=self._event_id(task.id))
                .execute()
            )
            # Update existing
            updated = (
                service.events()
                .update(calendarId=calendar_id, eventId=existing["id"], body=event_body)
                .execute()
            )
            logger.info(f"Updated Calendar event for task '{task.title}'")
            return updated["id"]
        except Exception:
            # Event doesn't exist yet — create it.
            # Force the ID so we can idempotently update later.
            event_body["id"] = self._event_id(task.id)
            created = (
                service.events()
                .insert(calendarId=calendar_id, body=event_body)
                .execute()
            )
            logger.info(f"Created Calendar event for task '{task.title}'")
            return created["id"]

    def delete_task_event(self, task_id: str, calendar_id: str = "primary") -> None:
        """Delete the Calendar event for a cancelled task."""
        service = self._get_service()
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=self._event_id(task_id)
            ).execute()
            logger.info(f"Deleted Calendar event for task {task_id}")
        except Exception as e:
            logger.warning(f"Could not delete Calendar event for {task_id}: {e}")

    def mark_task_completed(self, task: Task, calendar_id: str = "primary") -> None:
        """Change event color to green when task is completed."""
        if not (task.scheduled_start and task.scheduled_end):
            return
        service = self._get_service()
        try:
            existing = (
                service.events()
                .get(calendarId=calendar_id, eventId=self._event_id(task.id))
                .execute()
            )
            # Color ID 2 = green in Google Calendar
            existing["colorId"] = "2"
            service.events().update(
                calendarId=calendar_id, eventId=existing["id"], body=existing
            ).execute()
            logger.info(f"Marked Calendar event green for completed task '{task.title}'")
        except Exception as e:
            logger.warning(f"Could not update color for {task.id}: {e}")

    def _build_event(self, task: Task) -> dict:
        """Build a Calendar event body from a task."""
        # Color by priority: red=high, yellow=medium, green=low
        if task.priority >= 85:
            color_id = "11"  # red
        elif task.priority >= 65:
            color_id = "5"   # yellow
        else:
            color_id = "10"  # green
        return {
            "summary": task.title,
            "description": (
                f"Archy task\n"
                f"Priority: {task.priority}/100 ({task.priority_label.value})\n"
                f"Complexity: {task.complexity}/100\n"
                f"Category: {task.category}\n"
                f"Duration: {task.duration_minutes} min\n"
                f"\n{task.reasoning}\n"
                f"\nOriginal transcript: \"{task.raw_transcript}\""
            ),
            "start": {
                "dateTime": task.scheduled_start.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": task.scheduled_end.isoformat(),
                "timeZone": "UTC",
            },
            "colorId": color_id,
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "popup", "minutes": 1},
                ],
            },
            "source": {
                "title": "Archy",
                "url": "https://archy.local",
            },
        }
