"""RiskEngine tests — deadline risk prediction."""
from __future__ import annotations

from datetime import timedelta

import pytest

from archy.config import Settings
from archy.contracts.messages import Task, TaskStatus, utcnow
from archy.core.risk_engine import RiskEngine
from archy.core.scheduler_trainer import SchedulerTrainer
from archy.core.storage import Repository


pytestmark = pytest.mark.asyncio


def _task(
    *,
    id: str = "t1",
    title: str = "Test Task",
    priority: int = 70,
    complexity: int = 50,
    category: str = "general",
    duration_minutes: int = 60,
    deadline=None,
    status: TaskStatus = TaskStatus.SCHEDULED,
) -> Task:
    return Task(
        id=id, raw_transcript=title, title=title,
        priority=priority, complexity=complexity, category=category,
        duration_minutes=duration_minutes, deadline=deadline, status=status,
    )


async def test_no_risk_without_deadline(tmp_settings: Settings, repo: Repository):
    """Tasks without deadlines return None."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(deadline=None)
    result = engine.assess_task(t)
    assert result is None


async def test_low_risk_with_plenty_of_time(tmp_settings: Settings, repo: Repository):
    """Task with far deadline and short duration = low risk."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=30,
        deadline=utcnow() + timedelta(days=7),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert result.risk_score < 30
    assert result.risk_label == "low"


async def test_high_risk_with_tight_deadline(tmp_settings: Settings, repo: Repository):
    """Task needing 4h with only 1h available = high risk."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=240,  # 4 hours
        deadline=utcnow() + timedelta(hours=3),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert result.risk_score >= 60
    assert result.risk_label in ("high", "critical")


async def test_critical_risk_past_deadline(tmp_settings: Settings, repo: Repository):
    """Task whose deadline has passed = 100% risk."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=60,
        deadline=utcnow() - timedelta(hours=1),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert result.risk_score == 100
    assert result.risk_label == "critical"


async def test_recovery_plan_generated_for_high_risk(tmp_settings: Settings, repo: Repository):
    """High-risk tasks get a recovery plan."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=240,  # 4 hours
        complexity=80,
        deadline=utcnow() + timedelta(hours=5),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert result.recovery_plan is not None
    assert len(result.recovery_plan.actions) > 0
    assert result.recovery_plan.probability_before >= result.recovery_plan.probability_after


async def test_no_recovery_plan_for_low_risk(tmp_settings: Settings, repo: Repository):
    """Low-risk tasks don't get a recovery plan."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=30,
        deadline=utcnow() + timedelta(days=14),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert result.recovery_plan is None


async def test_reasons_generated(tmp_settings: Settings, repo: Repository):
    """Risk assessment includes human-readable reasons."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=180,
        deadline=utcnow() + timedelta(hours=4),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert len(result.reasons) > 0
    # Should mention hours needed vs available
    assert any("h " in r for r in result.reasons)


async def test_assess_all_sorts_by_risk(tmp_settings: Settings, repo: Repository):
    """assess_all returns tasks sorted by risk (highest first)."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    tasks = [
        _task(id="low", duration_minutes=30, deadline=utcnow() + timedelta(days=7)),
        _task(id="high", duration_minutes=240, deadline=utcnow() + timedelta(hours=3)),
        _task(id="med", duration_minutes=120, deadline=utcnow() + timedelta(days=2)),
    ]
    results = engine.assess_all(tasks)
    assert len(results) >= 2
    # High risk should be first
    assert results[0].risk_score >= results[-1].risk_score


async def test_recovery_plan_splits_large_tasks(tmp_settings: Settings, repo: Repository):
    """Recovery plan suggests splitting tasks > 2 hours into sessions."""
    trainer = SchedulerTrainer(repo)
    engine = RiskEngine(tmp_settings, repo, trainer)
    t = _task(
        duration_minutes=300,  # 5 hours
        complexity=85,
        deadline=utcnow() + timedelta(hours=6),
    )
    result = engine.assess_task(t)
    assert result is not None
    assert result.recovery_plan is not None
    assert len(result.recovery_plan.subtasks) >= 2
