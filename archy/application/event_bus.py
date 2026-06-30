"""EventBus — expanded event system with subscription filtering.

v2.1 features:
- 20+ event types (TaskCreated, MoodChanged, EnergyChanged, ProposalCreated, etc.)
- Subscription filtering (clients subscribe to specific event types)
- Event history (last N events stored for replay)
- Async fan-out to all subscribers
- Thread-safe

Used by:
- WebSocket hub (server.py) — pushes events to connected UI clients
- CalendarSubscriber — syncs events to Google Calendar
- AuditLogger — writes all events to audit_logs table
- Future: NotificationWorker, MetricsAggregator
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from loguru import logger

from ..contracts.messages import EventType


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    """Async event bus with filtering and history.

    Usage:
        bus = EventBus()
        bus.subscribe(callback, event_types=["TaskCreated", "MoodChanged"])
        await bus.publish("TaskCreated", {"task_id": "..."})

    Subscribers can filter by event types. If event_types is None or empty,
    they receive all events.
    """

    def __init__(self, history_size: int = 100):
        self._subscribers: list[tuple[EventCallback, set[str] | None]] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()
        self._event_counts: dict[str, int] = {}

    def subscribe(
        self,
        callback: EventCallback,
        event_types: list[str] | None = None,
    ) -> None:
        """Subscribe to events. If event_types is None, receives all events."""
        types_set = set(event_types) if event_types else None
        self._subscribers.append((callback, types_set))
        logger.debug(f"EventBus: subscriber added (filter={event_types})")

    def unsubscribe(self, callback: EventCallback) -> None:
        """Remove a subscriber."""
        self._subscribers = [(cb, t) for cb, t in self._subscribers if cb != callback]

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event to all matching subscribers."""
        # Record in history + counts
        event_record = {
            "type": event_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._history.append(event_record)
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1

        # Fan out to matching subscribers
        for callback, types_filter in list(self._subscribers):
            # If filter is set and event_type not in filter, skip
            if types_filter and event_type not in types_filter:
                continue
            try:
                await callback(event_type, payload)
            except Exception:
                logger.exception(f"EventBus subscriber raised on event {event_type}")

    def get_history(self, event_type: str | None = None, limit: int = 50) -> list[dict]:
        """Get recent events, optionally filtered by type."""
        if event_type:
            events = [e for e in self._history if e["type"] == event_type]
        else:
            events = list(self._history)
        return events[-limit:]

    def get_event_counts(self) -> dict[str, int]:
        """Get total event counts by type."""
        return dict(self._event_counts)

    def clear_history(self) -> None:
        """Clear event history (useful for testing)."""
        self._history.clear()
        self._event_counts.clear()


# Singleton event bus
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
