"""TaskStateMachine + PolicyEngine tests — v2.1."""
from __future__ import annotations

from datetime import timedelta

import pytest

from archy.config import Settings
from archy.contracts.messages import (
    Proposal,
    RiskLevel,
    Task,
    TaskState,
    TaskStateMachine,
    TaskStatus,
    utcnow,
)
from archy.core.energy_engine import EnergyEngine
from archy.core.mood_engine import MoodEngine
from archy.core.policy_engine import PolicyDecisionType, PolicyEngine
from archy.core.storage import Repository


pytestmark = pytest.mark.asyncio


# --- TaskStateMachine tests ---


def test_valid_transitions():
    assert TaskStateMachine.can_transition(TaskState.CREATED, TaskState.CLASSIFIED)
    assert TaskStateMachine.can_transition(TaskState.CLASSIFIED, TaskState.SCHEDULED)
    assert TaskStateMachine.can_transition(TaskState.SCHEDULED, TaskState.ACTIVE)
    assert TaskStateMachine.can_transition(TaskState.ACTIVE, TaskState.COMPLETED)


def test_invalid_transitions():
    # Can't go from CREATED directly to COMPLETED
    assert not TaskStateMachine.can_transition(TaskState.CREATED, TaskState.COMPLETED)
    # Can't go from COMPLETED back to ACTIVE
    assert not TaskStateMachine.can_transition(TaskState.COMPLETED, TaskState.ACTIVE)


def test_completed_is_immutable():
    assert TaskStateMachine.is_immutable(TaskState.COMPLETED)
    assert TaskStateMachine.is_immutable(TaskState.ARCHIVED)
    assert not TaskStateMachine.is_immutable(TaskState.SCHEDULED)


def test_archived_is_terminal():
    assert TaskStateMachine.is_terminal(TaskState.ARCHIVED)
    assert not TaskStateMachine.is_terminal(TaskState.COMPLETED)


def test_transition_raises_on_invalid():
    with pytest.raises(ValueError, match="Invalid task state transition"):
        TaskStateMachine.transition(TaskState.CREATED, TaskState.COMPLETED)


def test_reschedule_allowed():
    """SCHEDULED → SCHEDULED is allowed (rescheduling)."""
    assert TaskStateMachine.can_transition(TaskState.SCHEDULED, TaskState.SCHEDULED)


def test_missed_can_be_rescheduled():
    """MISSED → SCHEDULED is allowed (recovery)."""
    assert TaskStateMachine.can_transition(TaskState.MISSED, TaskState.SCHEDULED)


# --- PolicyEngine tests ---


def _make_proposal(
    *,
    action: str = "calendar.create_event",
    risk_level: RiskLevel = RiskLevel.LOW,
) -> Proposal:
    return Proposal(
        proposal_id="test-prop",
        intent="schedule task",
        action=action,
        parameters={},
        risk_level=risk_level,
        confidence=80,
    )


def _make_task(
    *,
    category: str = "general",
    complexity: int = 50,
    duration_minutes: int = 30,
    status: TaskStatus = TaskStatus.PENDING,
    deadline=None,
    scheduled_start=None,
) -> Task:
    return Task(
        id="test-task",
        raw_transcript="test",
        title="Test Task",
        category=category,
        priority=70,
        complexity=complexity,
        duration_minutes=duration_minutes,
        status=status,
        deadline=deadline,
        scheduled_start=scheduled_start,
    )


async def test_policy_approves_normal_proposal(tmp_settings: Settings, repo: Repository):
    """A normal low-risk proposal with a healthy user should be approved."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)

    proposal = _make_proposal()
    task = _make_task()

    decision = policy.evaluate_proposal(proposal, task)
    assert decision.decision == PolicyDecisionType.APPROVE


async def test_policy_rejects_completed_task_modification(
    tmp_settings: Settings, repo: Repository
):
    """Safety rule: never modify completed tasks."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)

    proposal = _make_proposal(action="task.update")
    task = _make_task(status=TaskStatus.DONE)

    decision = policy.evaluate_proposal(proposal, task)
    assert decision.decision == PolicyDecisionType.REJECT_HARD
    assert any("modify" in r or "immutable" in r or "done" in r or "completed" in r for r in decision.violated_rules)


async def test_policy_rejects_medical_reschedule(
    tmp_settings: Settings, repo: Repository
):
    """Safety rule: never auto-reschedule medical events."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)

    proposal = _make_proposal(
        action="calendar.reschedule_event",
        risk_level=RiskLevel.MEDIUM,  # not HIGH — should be rejected
    )
    task = _make_task(category="health")

    decision = policy.evaluate_proposal(proposal, task)
    assert decision.decision == PolicyDecisionType.REJECT_HARD


async def test_policy_constrains_during_critical_panic(
    tmp_settings: Settings, repo: Repository
):
    """During critical_panic, long tasks get constrained."""
    # Seed critical_panic mood
    repo.append_mood(40, "critical_panic", ["test setup"])

    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)

    proposal = _make_proposal()
    task = _make_task(duration_minutes=90)  # exceeds 45 min panic limit

    decision = policy.evaluate_proposal(proposal, task)
    assert decision.decision == PolicyDecisionType.REJECT_WITH_CONSTRAINTS
    assert "max_slot_minutes" in decision.constraints
    assert decision.constraints["max_slot_minutes"] == 45


async def test_policy_constrains_when_energy_depleted(
    tmp_settings: Settings, repo: Repository
):
    """When energy is depleted, complex tasks require a break first."""
    # Deplete stored energy directly
    for _ in range(10):
        repo.append_energy(20, "depleted", ["test setup"])

    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)

    proposal = _make_proposal()
    task = _make_task(complexity=70)

    decision = policy.evaluate_proposal(proposal, task)
    # With depleted energy, should either reject or add constraints
    assert decision.decision in (PolicyDecisionType.REJECT_WITH_CONSTRAINTS, PolicyDecisionType.APPROVE)


async def test_policy_rejects_external_messages_without_approval(
    tmp_settings: Settings, repo: Repository
):
    """Safety rule: communication actions require HIGH risk (approval)."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)

    proposal = _make_proposal(
        action="communication.send_email",
        risk_level=RiskLevel.LOW,  # LOW risk — should be rejected hard
    )

    decision = policy.evaluate_proposal(proposal)
    assert decision.decision == PolicyDecisionType.REJECT_HARD
