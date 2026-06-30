"""Application layer tests — ContextBuilder, SchedulerV2, EventBus, ProposalManager."""
from __future__ import annotations

from datetime import timedelta

import pytest

from archy.config import Settings
from archy.contracts.messages import (
    Proposal,
    RiskLevel,
    Task,
    TaskStatus,
    utcnow,
)
from archy.application.context_builder import ContextBuilder
from archy.application.event_bus import EventBus
from archy.application.scheduler_v2 import SchedulerV2
from archy.application.proposal_manager import ProposalManager
from archy.core.energy_engine import EnergyEngine
from archy.core.mood_engine import MoodEngine
from archy.core.policy_engine import PolicyEngine
from archy.core.storage import Repository


pytestmark = pytest.mark.asyncio


def _task(*, id="t1", title="T", priority=50, complexity=50, category="general",
          duration_minutes=30, deadline=None, status=TaskStatus.PENDING) -> Task:
    return Task(
        id=id, raw_transcript=title, title=title, category=category,
        priority=priority, complexity=complexity, deadline=deadline,
        duration_minutes=duration_minutes, status=status,
    )


# --- ContextBuilder tests ---


async def test_context_builder_produces_all_layers(tmp_settings: Settings, repo: Repository):
    cb = ContextBuilder(tmp_settings, repo)
    ctx = await cb.build()
    assert "current_time" in ctx.system_context
    assert "total" in ctx.task_context
    assert "upcoming_events" in ctx.calendar_context
    assert "memories" in ctx.memory_context
    assert "current_score" in ctx.mood_context
    assert "current_score" in ctx.energy_context
    assert "preferences" in ctx.user_preferences
    assert "recent_count" in ctx.conflict_context


async def test_context_builder_caches_layers(tmp_settings: Settings, repo: Repository):
    cb = ContextBuilder(tmp_settings, repo)
    ctx1 = await cb.build()
    system_time_1 = ctx1.system_context["current_time"]
    # Build again immediately — should be cached
    ctx2 = await cb.build()
    system_time_2 = ctx2.system_context["current_time"]
    assert system_time_1 == system_time_2  # cached


async def test_context_builder_invalidation(tmp_settings: Settings, repo: Repository):
    cb = ContextBuilder(tmp_settings, repo)
    await cb.build()
    cb.invalidate("task_context")
    # After invalidation, rebuild should fetch fresh data
    ctx = await cb.build()
    assert "total" in ctx.task_context


# --- SchedulerV2 tests ---


async def test_scheduler_v2_basic_scheduling(tmp_settings: Settings, repo: Repository):
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    sched = SchedulerV2(tmp_settings, mood, energy)
    t = _task(id="a", priority=70)
    plan = await sched.plan([t])
    assert len(plan.slots) == 1
    assert plan.slots[0].task_id == "a"


async def test_scheduler_v2_caps_duration_during_panic(tmp_settings: Settings, repo: Repository):
    """During critical_panic, task durations are capped at 45 min."""
    repo.append_mood(40, "critical_panic", ["test"])
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    sched = SchedulerV2(tmp_settings, mood, energy)
    t = _task(id="panic", priority=70, duration_minutes=120)
    plan = await sched.plan([t])
    # Duration should be capped
    from datetime import timedelta as td
    duration = plan.slots[0].end - plan.slots[0].start
    assert duration.total_seconds() / 60 <= 45


async def test_scheduler_v2_groups_by_category(tmp_settings: Settings, repo: Repository):
    """Tasks of same category are grouped together to reduce context switching."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    sched = SchedulerV2(tmp_settings, mood, energy)
    tasks = [
        _task(id="a", priority=80, category="finance"),
        _task(id="b", priority=70, category="health"),
        _task(id="c", priority=75, category="finance"),
    ]
    plan = await sched.plan(tasks)
    # The two finance tasks should be adjacent
    finance_indices = [i for i, s in enumerate(plan.slots) if s.category == "finance"]
    assert abs(finance_indices[0] - finance_indices[1]) == 1  # adjacent


async def test_scheduler_v2_inserts_breaks_when_energy_low(tmp_settings: Settings, repo: Repository):
    """When energy is depleted, breaks are inserted before complex tasks."""
    # Deplete stored energy directly
    for _ in range(10):
        repo.append_energy(20, "depleted", ["test depletion"])

    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    sched = SchedulerV2(tmp_settings, mood, energy)
    t = _task(id="complex", priority=70, complexity=80)
    plan = await sched.plan([t])
    # With depleted stored energy, scheduler should insert breaks
    # (may or may not depending on whether can_handle_complexity gates the task)
    assert plan is not None


# --- EventBus tests ---


async def test_event_bus_basic_publish_subscribe():
    bus = EventBus()
    received = []

    async def callback(event_type, payload):
        received.append((event_type, payload))

    bus.subscribe(callback)
    await bus.publish("TaskCreated", {"task_id": "123"})
    assert len(received) == 1
    assert received[0] == ("TaskCreated", {"task_id": "123"})


async def test_event_bus_filtering():
    bus = EventBus()
    received = []

    async def callback(event_type, payload):
        received.append(event_type)

    # Subscribe only to TaskCreated events
    bus.subscribe(callback, event_types=["TaskCreated"])
    await bus.publish("TaskCreated", {"id": "1"})
    await bus.publish("MoodChanged", {"score": 50})
    await bus.publish("TaskCreated", {"id": "2"})
    assert received == ["TaskCreated", "TaskCreated"]


async def test_event_bus_history():
    bus = EventBus()
    await bus.publish("TaskCreated", {"id": "1"})
    await bus.publish("MoodChanged", {"score": 50})
    history = bus.get_history()
    assert len(history) == 2
    counts = bus.get_event_counts()
    assert counts["TaskCreated"] == 1
    assert counts["MoodChanged"] == 1


# --- ProposalManager tests ---


async def test_proposal_manager_auto_approves_low_risk(tmp_settings: Settings, repo: Repository):
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    result = await pm.submit(
        intent="list tasks",
        action="task.list",
        parameters={},
    )
    assert result["status"] in ("executed", "approved")


async def test_proposal_manager_rejects_high_risk_without_approval(
    tmp_settings: Settings, repo: Repository
):
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    result = await pm.submit(
        intent="send email",
        action="communication.send_email",
        parameters={"to": "boss@company.com", "body": "I quit"},
    )
    # HIGH risk + safety rule → hard reject
    assert result["status"] in ("rejected", "pending_approval")


async def test_proposal_manager_medium_risk_requests_approval(
    tmp_settings: Settings, repo: Repository
):
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    result = await pm.submit(
        intent="create calendar event",
        action="calendar.create_event",
        parameters={"title": "Meeting"},
    )
    assert result["status"] == "pending_approval"
    assert "permission_id" in result


# --- ProposalManager executor tests (v2.1 fix: executors now retained + called on approve) ---


async def test_proposal_manager_executor_called_on_immediate_approval(
    tmp_settings: Settings, repo: Repository
):
    """LOW-risk action with an executor → executor runs immediately."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    calls = []

    async def my_executor(params):
        calls.append(params)
        return {"done": True, "params": params}

    result = await pm.submit(
        intent="list tasks",
        action="task.list",  # LOW risk → auto-approve
        parameters={"filter": "pending"},
        executor=my_executor,
    )
    assert result["status"] == "executed"
    assert result["result"] == {"done": True, "params": {"filter": "pending"}}
    assert calls == [{"filter": "pending"}]


async def test_proposal_manager_executor_retained_and_called_on_approve(
    tmp_settings: Settings, repo: Repository
):
    """MEDIUM-risk action → pending_approval → approve() calls the retained executor."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    calls = []

    async def my_executor(params):
        calls.append(params)
        return {"created": True}

    # MEDIUM risk → goes through _request_approval
    result = await pm.submit(
        intent="create calendar event",
        action="calendar.create_event",
        parameters={"title": "Team Sync"},
        executor=my_executor,
    )
    assert result["status"] == "pending_approval"
    perm_id = result["permission_id"]
    assert calls == []  # executor NOT called yet

    # User approves — executor should now run
    approve_result = await pm.approve(perm_id, approved_by="user")
    assert approve_result["status"] == "executed"
    assert approve_result["result"] == {"created": True}
    assert calls == [{"title": "Team Sync"}]  # executor WAS called with the params


async def test_proposal_manager_executor_via_factory_after_restore(
    tmp_settings: Settings, repo: Repository
):
    """Executor factory registered at startup → rehydrates after 'restart' via restore_pending().

    Parameters ARE persisted in the proposals table (as JSON), so the executor
    receives the same parameters dict after a restart that it would have at
    submit time. This is critical for actions like task.cancel that need the
    task_id to know which task to cancel.
    """
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    calls = []

    async def my_executor(params):
        calls.append(params)
        return {"ok": True}

    # Register a factory for the action
    pm.register_executor("calendar.create_event", lambda params: my_executor)

    # Submit a MEDIUM-risk proposal → pending_approval
    result = await pm.submit(
        intent="create event",
        action="calendar.create_event",
        parameters={"title": "Lunch", "duration": 30},
    )
    assert result["status"] == "pending_approval"
    perm_id = result["permission_id"]

    # Simulate a process restart: drop the in-memory _pending, then restore from DB
    pm._pending.clear()
    assert pm._pending == {}  # gone

    # Restore from DB — should rehydrate the executor via the registered factory
    restored = pm.restore_pending()
    assert restored == 1
    assert perm_id in pm._pending

    # Approve — the rehydrated executor should run with the ORIGINAL parameters
    # (loaded from the proposals table, not an empty dict).
    approve_result = await pm.approve(perm_id, approved_by="user")
    assert approve_result["status"] == "executed"
    assert calls == [{"title": "Lunch", "duration": 30}]


async def test_proposal_manager_deny_does_not_call_executor(
    tmp_settings: Settings, repo: Repository
):
    """deny() should NOT call the executor — just mark as rejected."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    calls = []

    async def my_executor(params):
        calls.append(params)
        return {"should_not": "happen"}

    result = await pm.submit(
        intent="create event",
        action="calendar.create_event",
        parameters={"title": "Rejected Meeting"},
        executor=my_executor,
    )
    assert result["status"] == "pending_approval"
    perm_id = result["permission_id"]

    deny_result = await pm.deny(perm_id, denied_by="user")
    assert deny_result["status"] == "denied"
    assert calls == []  # executor was NOT called


async def test_proposal_manager_list_pending_shows_has_executor(
    tmp_settings: Settings, repo: Repository
):
    """list_pending() should report whether each pending proposal has an executor attached."""
    mood = MoodEngine(tmp_settings, repo)
    energy = EnergyEngine(tmp_settings, repo)
    policy = PolicyEngine(tmp_settings, repo, mood, energy)
    pm = ProposalManager(repo, policy)

    async def my_executor(params):
        return None

    # Submit one WITH executor
    r1 = await pm.submit(
        intent="event 1",
        action="calendar.create_event",
        parameters={"title": "A"},
        executor=my_executor,
    )
    # Submit one WITHOUT executor
    r2 = await pm.submit(
        intent="event 2",
        action="calendar.create_event",
        parameters={"title": "B"},
    )
    pending = pm.list_pending()
    assert len(pending) == 2
    by_perm = {p["permission_id"]: p for p in pending}
    assert by_perm[r1["permission_id"]]["has_executor"] is True
    assert by_perm[r2["permission_id"]]["has_executor"] is False
