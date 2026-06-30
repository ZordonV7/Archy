"""Scheduler tests — deterministic, no network."""
from __future__ import annotations

from datetime import timedelta

import pytest

from archy.config import Settings
from archy.contracts.messages import Task, TaskStatus, utcnow
from archy.core.scheduler import GreedyScheduler


pytestmark = pytest.mark.asyncio


def _task(
    *,
    id: str = "t1",
    title: str = "T",
    priority: int = 50,
    complexity: int = 50,
    duration_minutes: int = 30,
    deadline=None,
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    return Task(
        id=id,
        raw_transcript=title,
        title=title,
        priority=priority,
        complexity=complexity,
        deadline=deadline,
        duration_minutes=duration_minutes,
        status=status,
    )


async def test_empty_tasks(tmp_settings: Settings):
    sched = GreedyScheduler(tmp_settings)
    plan = await sched.plan([])
    assert plan.slots == []
    assert plan.unscheduled_task_ids == []


async def test_single_task_gets_slot(tmp_settings: Settings):
    sched = GreedyScheduler(tmp_settings)
    plan = await sched.plan([_task(id="a", priority=70)])
    assert len(plan.slots) == 1
    assert plan.slots[0].task_id == "a"
    assert plan.slots[0].end - plan.slots[0].start == timedelta(minutes=30)


async def test_higher_priority_first(tmp_settings: Settings):
    sched = GreedyScheduler(tmp_settings)
    plan = await sched.plan([
        _task(id="low", priority=20),
        _task(id="high", priority=95),
    ])
    assert plan.slots[0].task_id == "high"
    assert plan.slots[1].task_id == "low"


async def test_buffer_between_slots(tmp_settings: Settings):
    tmp_settings.workday_start = "00:00"
    tmp_settings.workday_end = "23:59"  # prevent workday rollover
    sched = GreedyScheduler(tmp_settings)
    plan = await sched.plan([
        _task(id="a", priority=80),
        _task(id="b", priority=70),
    ])
    gap = plan.slots[1].start - plan.slots[0].end
    assert gap == timedelta(minutes=5)


async def test_at_risk_flagged(tmp_settings: Settings):
    """Task whose scheduled end exceeds its deadline is flagged at_risk."""
    tmp_settings.workday_start = "00:00"
    tmp_settings.workday_end = "23:59"  # prevent workday rollover
    sched = GreedyScheduler(tmp_settings)
    t = _task(
        id="risky",
        priority=50,
        duration_minutes=120,
        deadline=utcnow() + timedelta(minutes=30),
    )
    plan = await sched.plan([t])
    assert len(plan.slots) == 1
    assert plan.slots[0].at_risk is True
    assert "risky" in plan.at_risk_task_ids


async def test_unscheduled_past_deadline(tmp_settings: Settings):
    sched = GreedyScheduler(tmp_settings)
    t = _task(
        id="missed",
        priority=50,
        duration_minutes=30,
        deadline=utcnow() - timedelta(hours=2),
    )
    plan = await sched.plan([t])
    assert plan.unscheduled_task_ids == ["missed"]
    assert plan.slots == []


async def test_preferred_order_overrides_rank(tmp_settings: Settings):
    """If PlannerAgent provides preferred_order, scheduler respects it."""
    sched = GreedyScheduler(tmp_settings)
    plan = await sched.plan(
        [
            _task(id="a", priority=90),
            _task(id="b", priority=20),
        ],
        preferred_order=["b", "a"],  # b first despite lower priority
    )
    assert plan.slots[0].task_id == "b"
    assert plan.slots[1].task_id == "a"
