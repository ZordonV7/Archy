"""Mood engine tests — deterministic, no network."""
from __future__ import annotations

from datetime import timedelta

import pytest

from archy.config import Settings
from archy.contracts.messages import DriftEvent, Task, TaskStatus, utcnow
from archy.core.mood_engine import MoodEngine
from archy.core.storage import Repository


pytestmark = pytest.mark.asyncio


def _task(
    *,
    id: str = "t1",
    priority: int = 70,
    status: TaskStatus = TaskStatus.SCHEDULED,
    scheduled_start=None,
    scheduled_end=None,
    completed_at=None,
) -> Task:
    return Task(
        id=id,
        raw_transcript=id,
        title=id,
        priority=priority,
        complexity=50,
        duration_minutes=30,
        status=status,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        completed_at=completed_at,
    )


async def test_baseline_when_no_history(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    score, label, _ = m.current()
    assert score == tmp_settings.mood_baseline
    assert label == "watchful"


async def test_completed_on_time_increases(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    m.current()
    now = utcnow()
    end = now - timedelta(minutes=5)
    # Deadline-based task, completed before deadline
    t = _task(
        status=TaskStatus.DONE,
        scheduled_start=now - timedelta(minutes=35),
        scheduled_end=end,
        completed_at=end,
    )
    t.deadline = now + timedelta(hours=1)  # deadline is 1 hour in the future
    score, _, _ = m.on_task_completed(t)
    assert score > tmp_settings.mood_baseline  # should increase (beat deadline)


async def test_completed_late_decreases(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    m.current()
    now = utcnow()
    # Deadline-based task completed AFTER deadline
    t = _task(
        status=TaskStatus.DONE,
        scheduled_start=now - timedelta(minutes=60),
        scheduled_end=now - timedelta(minutes=30),
        completed_at=now,
    )
    t.deadline = now - timedelta(minutes=5)  # deadline was 5 min ago
    score, _, _ = m.on_task_completed(t)
    assert score < tmp_settings.mood_baseline  # should decrease


async def test_missed_decreases(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    m.current()
    score, _, _ = m.on_task_missed(_task(status=TaskStatus.MISSED))
    assert score == tmp_settings.mood_baseline - 15


async def test_drift_scales_with_minutes(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    m.current()
    event = DriftEvent(kind="nap", description="slept 1h", minutes_lost=60)
    score, _, _ = m.on_drift(event)
    # 60 min = 4 units × -3 = -12
    assert score == tmp_settings.mood_baseline - 12


async def test_manual_drift_adds_recovery_bonus(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    m.current()
    event = DriftEvent(kind="manual", description="ack", minutes_lost=15)
    score, _, _ = m.on_drift(event)
    # -3 (drift) + +6 (recovery) = +3
    assert score == tmp_settings.mood_baseline + 3


async def test_clamping(tmp_settings: Settings, repo: Repository):
    tmp_settings.mood_baseline = 95
    m = MoodEngine(tmp_settings, repo)
    m.current()
    now = utcnow()
    end = now - timedelta(minutes=5)
    for _ in range(5):
        t = _task(
            status=TaskStatus.DONE,
            scheduled_start=now - timedelta(minutes=35),
            scheduled_end=end,
            completed_at=end,
        )
        score, _, _ = m.on_task_completed(t)
        assert score <= tmp_settings.mood_max


async def test_recompute_from_state(tmp_settings: Settings, repo: Repository):
    m = MoodEngine(tmp_settings, repo)
    now = utcnow()  # single timestamp to avoid microsecond drift
    missed = _task(id="missed", status=TaskStatus.MISSED)
    on_time = _task(
        id="ontime",
        status=TaskStatus.DONE,
        scheduled_start=now - timedelta(minutes=30),
        scheduled_end=now - timedelta(minutes=5),
        completed_at=now - timedelta(minutes=5),  # exactly on time
    )
    score, _, _ = m.recompute_from_state([missed, on_time])
    assert score == tmp_settings.mood_baseline - 15 + 5
