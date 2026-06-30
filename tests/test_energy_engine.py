"""Energy Engine tests — v2.1 deterministic energy tracking."""
from __future__ import annotations

from datetime import timedelta

import pytest

from archy.config import Settings
from archy.contracts.messages import DriftEvent, Task, TaskStatus, utcnow
from archy.core.energy_engine import EnergyEngine
from archy.core.storage import Repository


pytestmark = pytest.mark.asyncio


def _task(
    *,
    id: str = "t1",
    complexity: int = 50,
    duration_minutes: int = 30,
    status: TaskStatus = TaskStatus.SCHEDULED,
    completed_at=None,
) -> Task:
    return Task(
        id=id, raw_transcript=id, title=id,
        priority=70, complexity=complexity,
        duration_minutes=duration_minutes,
        status=status, completed_at=completed_at,
    )


async def test_baseline_when_no_history(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    snap = m.current()
    # With no schedule, density=0 → density_score=100 → blended high
    assert snap.score >= 60  # should be high (empty schedule)
    assert snap.label.value in ("full", "steady")


async def test_task_completed_depletes_energy(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    m.current()  # establish baseline
    t = _task(complexity=50, status=TaskStatus.DONE, completed_at=utcnow())
    snap = m.on_task_completed(t)
    # base -8 + complexity -15 + satisfaction +3 = -20 → 60
    assert snap.score == 60
    assert snap.label.value == "steady"  # 60 >= 60 threshold


async def test_complex_task_depletes_more(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    m.current()
    t = _task(complexity=90, status=TaskStatus.DONE, completed_at=utcnow())
    snap = m.on_task_completed(t)
    # -8 - 27 + 3 = -32 → 48
    assert snap.score == 48
    assert snap.label.value == "low"


async def test_drift_depletes_energy(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    m.current()
    event = DriftEvent(kind="nap", description="slept 1h", minutes_lost=60)
    snap = m.on_drift(event)
    # 4 units × -5 = -20 → 60
    assert snap.score == 60


async def test_break_restores_energy(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    # Deplete first
    t = _task(complexity=90, status=TaskStatus.DONE, completed_at=utcnow())
    m.on_task_completed(t)  # 48
    # Take a 30-min break
    snap = m.on_break(30)
    # 2 units × +8 = +16 → 64
    assert snap.score == 64


async def test_can_handle_complexity_when_depleted(tmp_settings: Settings, repo: Repository):
    """When energy is depleted, only low-complexity tasks are allowed."""
    m = EnergyEngine(tmp_settings, repo)
    # Deplete heavily — call _apply directly to bypass density computation
    for i in range(10):
        m._apply(-15, [f"deplete {i}"])
    snap = m._repo.latest_energy()
    assert snap is not None
    assert snap.score <= 40
    # can_handle_complexity uses stored energy, not display score
    assert m.can_handle_complexity(50) is False
    assert m.can_handle_complexity(20) is True


async def test_can_handle_complexity_when_full(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    assert m.can_handle_complexity(100) is True


async def test_energy_clamped_to_0_100(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    # Take many breaks to try exceeding 100
    for _ in range(10):
        m.on_break(60)
    snap = m.current()
    assert snap.score <= 100


async def test_recompute_from_state(tmp_settings: Settings, repo: Repository):
    m = EnergyEngine(tmp_settings, repo)
    now = utcnow()
    t = _task(
        complexity=50,
        status=TaskStatus.DONE,
        completed_at=now,
    )
    snap = m.recompute_from_state([t])
    # 80 - 8 - 15 + 3 = 60, minus fatigue (0.5h * 3 = 1.5 → int=1) = 59
    assert snap.score == 59
