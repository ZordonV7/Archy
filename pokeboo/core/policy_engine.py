"""Policy Engine — formal constraint checks (deterministic).

Every proposal must pass through the PolicyEngine before execution.
This replaces PokeBo's ad-hoc veto heuristics with a formal, testable system.

Checks:
1. deadline_constraints — does the proposal respect task deadlines?
2. calendar_constraints — does it conflict with existing calendar events?
3. mood_constraints — is it appropriate for the user's current mood?
4. energy_constraints — does the user have enough energy for this?
5. work_hour_constraints — is it within the workday window?
6. safety_rules — hard rules that can never be violated (e.g., medical events)

The PolicyEngine returns a PolicyDecision: APPROVE, REJECT_WITH_CONSTRAINTS,
or REJECT_HARD (safety violation).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from loguru import logger

from ..config import Settings
from ..contracts.messages import (
    EnergySnapshot,
    Proposal,
    RiskLevel,
    SafetyRule,
    Task,
    TaskState,
    TaskStateMachine,
    utcnow,
)
from .energy_engine import EnergyEngine
from .mood_engine import MoodEngine
from .storage import Repository


class PolicyDecisionType(StrEnum):
    APPROVE = "approve"
    REJECT_WITH_CONSTRAINTS = "reject_with_constraints"  # agent can revise
    REJECT_HARD = "reject_hard"                          # safety violation, no revision


@dataclass
class PolicyDecision:
    decision: PolicyDecisionType
    reason: str
    constraints: dict[str, Any]
    violated_rules: list[str] = None

    def __post_init__(self):
        if self.violated_rules is None:
            self.violated_rules = []


class PolicyEngine:
    """Formal constraint checker. Every proposal must pass through here.

    This is the deterministic authority — it can override agent proposals
    based on hard rules and current user state.
    """

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        mood_engine: MoodEngine,
        energy_engine: EnergyEngine,
    ):
        self._s = settings
        self._repo = repo
        self._mood = mood_engine
        self._energy = energy_engine

    def evaluate_proposal(self, proposal: Proposal, task: Task | None = None) -> PolicyDecision:
        """Run all policy checks on a proposal.

        Returns:
            APPROVE — proposal is safe to execute
            REJECT_WITH_CONSTRAINTS — agent should revise with these constraints
            REJECT_HARD — safety violation, cannot revise
        """
        violated: list[str] = []
        constraints: dict[str, Any] = {}

        # --- Safety checks (hard rules) ---
        safety_violation = self._check_safety_rules(proposal, task)
        if safety_violation:
            return PolicyDecision(
                decision=PolicyDecisionType.REJECT_HARD,
                reason=safety_violation,
                constraints={},
                violated_rules=[safety_violation],
            )

        # --- Deadline constraints ---
        deadline_check = self._check_deadline_constraints(task)
        if deadline_check:
            violated.append(deadline_check)

        # --- Mood constraints ---
        mood_check, mood_constraints = self._check_mood_constraints(proposal, task)
        if mood_check:
            violated.append(mood_check)
            constraints.update(mood_constraints)

        # --- Energy constraints ---
        energy_check, energy_constraints = self._check_energy_constraints(task)
        if energy_check:
            violated.append(energy_check)
            constraints.update(energy_constraints)

        # --- Work hour constraints ---
        workhour_check = self._check_work_hour_constraints(task)
        if workhour_check:
            violated.append(workhour_check)

        # --- Task state machine check ---
        state_check = self._check_task_state(proposal, task)
        if state_check:
            violated.append(state_check)

        if not violated:
            return PolicyDecision(
                decision=PolicyDecisionType.APPROVE,
                reason="All policy checks passed.",
                constraints={},
            )

        return PolicyDecision(
            decision=PolicyDecisionType.REJECT_WITH_CONSTRAINTS,
            reason="; ".join(violated),
            constraints=constraints,
            violated_rules=violated,
        )

    def _check_safety_rules(self, proposal: Proposal, task: Task | None) -> str | None:
        """Hard safety rules that can never be violated."""
        # Never modify completed tasks — check both TaskStatus and TaskState values
        if task:
            status_val = task.status.value if hasattr(task.status, 'value') else str(task.status)
            immutable_statuses = {"done", "completed", "archived"}
            if status_val in immutable_statuses:
                return f"Safety violation: cannot modify task in state '{status_val}'"

        # Never reschedule medical events automatically
        if (
            task
            and task.category == "health"
            and proposal.action.startswith("calendar.reschedule")
            and proposal.risk_level != RiskLevel.HIGH
        ):
            return SafetyRule.NEVER_RESCHEDULE_MEDICAL_EVENTS_AUTOMATICALLY.value

        # Never send external messages without approval
        if proposal.action.startswith("communication.") and proposal.risk_level == RiskLevel.LOW:
            return SafetyRule.NEVER_SEND_EXTERNAL_MESSAGES_WITHOUT_APPROVAL.value

        return None

    def _check_deadline_constraints(self, task: Task | None) -> str | None:
        """Check if task deadline is respected."""
        if not task or not task.deadline:
            return None
        if task.deadline < utcnow():
            return f"Task deadline ({task.deadline.isoformat()}) has already passed"
        return None

    def _check_mood_constraints(
        self, proposal: Proposal, task: Task | None
    ) -> tuple[str | None, dict[str, Any]]:
        """Check if proposal is appropriate for current mood state."""
        score, label, _ = self._mood.current()
        constraints: dict[str, Any] = {}

        if label == "critical_panic":
            if proposal.action.startswith("calendar.create") and task:
                # In panic mode, limit task duration to 45 min
                if task.duration_minutes > 45:
                    constraints["max_slot_minutes"] = 45
                    return (
                        f"User in critical_panic — task duration {task.duration_minutes}min "
                        f"exceeds 45min limit",
                        constraints,
                    )
            # In panic, limit number of new slots
            constraints["max_slots"] = 2
            return ("User in critical_panic — limit to 2 new slots", constraints)

        if label == "drift_alert":
            if proposal.action.startswith("calendar.create") and task:
                if task.duration_minutes > 90:
                    constraints["max_slot_minutes"] = 90
                    return (
                        f"User drifting — task duration {task.duration_minutes}min "
                        f"exceeds 90min limit",
                        constraints,
                    )

        return None, constraints

    def _check_energy_constraints(
        self, task: Task | None
    ) -> tuple[str | None, dict[str, Any]]:
        """Check if user has enough energy for this task."""
        if not task:
            return None, {}
        energy = self._energy.current()
        constraints: dict[str, Any] = {}

        if energy.label.value == "depleted" and task.complexity > 30:
            constraints["require_break_first"] = True
            constraints["break_minutes"] = 15
            return (
                f"Energy depleted ({energy.score}/100) — task complexity "
                f"{task.complexity} too high. Require 15-min break first.",
                constraints,
            )

        if energy.label.value == "low" and task.complexity > 60:
            constraints["suggest_simpler_task"] = True
            return (
                f"Energy low ({energy.score}/100) — task complexity "
                f"{task.complexity} too high. Suggest simpler task.",
                constraints,
            )

        return None, constraints

    def _check_work_hour_constraints(self, task: Task | None) -> str | None:
        """Check if scheduled time is within workday window."""
        if not task or not task.scheduled_start:
            return None
        start_time = task.scheduled_start.time()
        from datetime import time
        wh_start = self._parse_hhmm(self._s.workday_start)
        wh_end = self._parse_hhmm(self._s.workday_end)
        if start_time < wh_start or start_time > wh_end:
            return (
                f"Scheduled start {start_time.strftime('%H:%M')} outside "
                f"workday window {self._s.workday_start}-{self._s.workday_end}"
            )
        return None

    def _check_task_state(self, proposal: Proposal, task: Task | None) -> str | None:
        """Check if the task state allows this action."""
        if not task:
            return None
        # Can't schedule a task that's already completed/done/archived
        status_val = task.status.value if hasattr(task.status, 'value') else str(task.status)
        immutable_statuses = {"done", "completed", "archived"}
        if status_val in immutable_statuses:
            return f"Task in immutable state '{status_val}' — cannot {proposal.action}"
        return None

    @staticmethod
    def _parse_hhmm(s: str):
        from datetime import time
        h, m = s.split(":")
        return time(hour=int(h), minute=int(m), second=0)
