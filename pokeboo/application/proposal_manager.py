"""ProposalManager — formal proposal flow with permission gates.

Every non-trivial action goes through:
  Intent → Proposal → Validation (PolicyEngine) → Permission (if needed) → Execution

Risk levels:
- LOW: auto-approve (reads, list operations, memory retrieval)
- MEDIUM: requires confirmation (task updates, schedule changes)
- HIGH: requires explicit approval (external messages, medical reschedules, deletions)

The ProposalManager:
1. Creates a Proposal from an agent's intent
2. Runs it through the PolicyEngine for validation
3. If LOW risk + APPROVE → execute immediately
4. If MEDIUM/HIGH risk + APPROVE → request user permission
5. If REJECT_WITH_CONSTRAINTS → return constraints to agent for revision
6. If REJECT_HARD → log to audit, return failure
7. After execution, logs to audit trail

Persistence:
- Pending proposals + their executors are persisted to the `permissions` table.
- On process restart, `restore_pending()` reloads them from the DB so
  approvals submitted before the restart can still be executed.
- Executors themselves (Python callables) are NOT persisted — they must be
  re-registered by the caller via `register_executor()` after restore. The
  ProposalManager keeps an in-memory map of action_name → executor factory.
  When a permission is approved, the factory for that action is invoked to
  produce the actual callable. If no factory is registered, the proposal is
  marked "approved_no_executor" (the audit trail records the approval, but
  no side effect runs — caller must handle this case).

This is one of the most important v2.1 additions.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable

from loguru import logger

from ..contracts.messages import (
    Proposal,
    ProposalStatus,
    RiskLevel,
    Task,
    utcnow,
)
from ..core.policy_engine import PolicyDecision, PolicyDecisionType, PolicyEngine
from ..core.storage import Repository


# Actions that are always LOW risk (auto-approve)
LOW_RISK_ACTIONS = {
    "memory.retrieve", "memory.search", "memory.list",
    "task.list", "task.get", "task.read",
    "calendar.list", "calendar.get",
    "context.build",
}

# Actions that are always HIGH risk (explicit approval)
HIGH_RISK_ACTIONS = {
    "communication.send_email", "communication.send_message",
    "calendar.delete_event", "calendar.reschedule_event",
    "task.delete", "memory.delete",
    "external_api.write",
}


# Type alias for executor factories: given the proposal's parameters dict,
# returns the actual async callable that will be invoked with those parameters.
# This indirection lets us persist the *action name* (string) to the DB and
# rehydrate the executor on restart by looking up the factory.
ExecutorFactory = Callable[[dict[str, Any]], Callable[[dict[str, Any]], Awaitable[Any]] | None]


class ProposalManager:
    """Manages the Intent → Proposal → Validation → Execution flow.

    Usage:
        pm = ProposalManager(repo, policy_engine)
        # Register executor factories for each action (do this once at startup)
        pm.register_executor("task.cancel", lambda params: pm_default_executor)
        pm.register_executor("docs.create_note", lambda params: docs_factory(params))

        result = await pm.submit(
            intent="schedule tax filing",
            action="calendar.create_event",
            parameters={"title": "File Taxes", "start": "..."},
            task=task,
            executor=lambda params: calendar.create_event(**params),
        )
    """

    def __init__(self, repo: Repository, policy_engine: PolicyEngine):
        self._repo = repo
        self._policy = policy_engine
        # Pending permission requests awaiting user approval.
        # Keyed by permission_id. Value = (proposal, executor_or_None).
        self._pending: dict[str, tuple[Proposal, Callable[[dict[str, Any]], Awaitable[Any]] | None]] = {}
        # Executor factories — registered at startup so we can rehydrate
        # executors after a process restart by looking up the action name.
        self._executor_factories: dict[str, ExecutorFactory] = {}

    # --- Executor factory registration ---

    def register_executor(self, action: str, factory: ExecutorFactory) -> None:
        """Register an executor factory for a given action name.

        The factory receives the proposal's `parameters` dict and returns
        the actual async callable to invoke (or None if no executor applies).

        This is what makes approvals work across process restarts: the
        action name is persisted in the DB, and on restart we rehydrate
        the executor by calling the registered factory.
        """
        self._executor_factories[action] = factory

    def restore_pending(self) -> int:
        """Reload pending permission requests from the DB after a restart.

        Rehydrates executors by looking up each proposal's action in the
        registered executor factories. Returns the count of restored entries.
        Call this once at startup AFTER all executor factories are registered.

        Parameters are loaded from the `proposals` table (which persists them
        as JSON), so executors receive the same parameters dict after a
        restart that they would have received at submit time.
        """
        restored = 0
        try:
            pending_perms = self._repo.list_permissions(status="pending", limit=500)
        except Exception as e:
            logger.warning(f"ProposalManager.restore_pending: failed to load from DB: {e}")
            return 0
        # Build a lookup of proposal_id -> parameters from the proposals table
        proposal_params: dict[str, dict] = {}
        try:
            for p in self._repo.list_proposals_v2(limit=500):
                pid = p.get("proposal_id")
                raw_params = p.get("parameters", "{}")
                if pid:
                    try:
                        proposal_params[pid] = json.loads(raw_params) if isinstance(raw_params, str) else (raw_params or {})
                    except (json.JSONDecodeError, TypeError):
                        proposal_params[pid] = {}
        except Exception as e:
            logger.warning(f"ProposalManager.restore_pending: failed to load proposals: {e}")
        for perm in pending_perms:
            perm_id = perm.get("id")
            proposal_id = perm.get("proposal_id")
            action = perm.get("action", "")
            if not perm_id or not proposal_id:
                continue
            # Look up the persisted parameters dict
            params = proposal_params.get(proposal_id, {})
            try:
                proposal = Proposal(
                    proposal_id=proposal_id,
                    intent=perm.get("reason", ""),
                    action=action,
                    parameters=params,
                    risk_level=RiskLevel(perm.get("risk_level", "medium")),
                    confidence=75,
                    reasoning=perm.get("reason", ""),
                    expected_impact=perm.get("impact", ""),
                )
            except Exception:
                continue
            # Rehydrate executor from factory
            executor = None
            factory = self._executor_factories.get(action)
            if factory:
                try:
                    executor = factory(proposal.parameters)
                except Exception as e:
                    logger.warning(f"ProposalManager.restore: factory for '{action}' failed: {e}")
            self._pending[perm_id] = (proposal, executor)
            restored += 1
        if restored:
            logger.info(f"ProposalManager: restored {restored} pending permission requests from DB")
        return restored

    # --- Risk classification ---

    def classify_risk(self, action: str) -> RiskLevel:
        """Classify the risk level of an action."""
        if action in HIGH_RISK_ACTIONS:
            return RiskLevel.HIGH
        if action in LOW_RISK_ACTIONS:
            return RiskLevel.LOW
        # Default: anything that modifies state is MEDIUM
        if any(action.startswith(p) for p in ("calendar.create", "calendar.update",
                                                "task.create", "task.update", "task.start",
                                                "task.complete", "task.cancel",
                                                "memory.store", "docs.create")):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    # --- Submit flow ---

    async def submit(
        self,
        intent: str,
        action: str,
        parameters: dict[str, Any],
        task: Task | None = None,
        executor: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        confidence: int = 75,
        reasoning: str = "",
    ) -> dict[str, Any]:
        """Submit a proposal through the full flow.

        Returns:
            {
                "proposal_id": str,
                "status": "executed" | "pending_approval" | "rejected" | "needs_revision",
                "result": Any (if executed),
                "constraints": dict (if needs_revision),
                "reason": str,
            }
        """
        risk_level = self.classify_risk(action)
        proposal = Proposal(
            proposal_id=str(uuid.uuid4()),
            intent=intent,
            action=action,
            parameters=parameters,
            risk_level=risk_level,
            confidence=confidence,
            reasoning=reasoning,
            expected_impact=f"Execute {action}",
            # Set expires_at on the Proposal itself so approve() can check it
            # without needing to load the PermissionRequest.
            expires_at=utcnow() + timedelta(minutes=30 if risk_level == RiskLevel.HIGH else 5),
        )
        self._repo.append_proposal_v2(proposal)
        self._repo.append_audit_log(
            "ProposalCreated", "agent", "submit_proposal",
            target=proposal.proposal_id,
            details={"intent": intent, "action": action, "risk": risk_level.value},
        )

        # If an executor factory is registered for this action, prefer it
        # (ensures consistency across restarts). Fall back to the passed executor.
        effective_executor = executor
        factory = self._executor_factories.get(action)
        if factory is not None and effective_executor is None:
            try:
                effective_executor = factory(parameters)
            except Exception as e:
                logger.warning(f"ProposalManager.submit: factory for '{action}' failed: {e}")

        # Step 1: Policy validation
        decision = self._policy.evaluate_proposal(proposal, task)

        if decision.decision == PolicyDecisionType.REJECT_HARD:
            proposal.status = ProposalStatus.REJECTED
            self._repo.update_proposal_status(proposal.proposal_id, "rejected")
            self._repo.append_audit_log(
                "ProposalRejected", "policy_engine", "hard_reject",
                target=proposal.proposal_id,
                details={"reason": decision.reason, "violated": decision.violated_rules},
            )
            logger.warning(f"Proposal {proposal.proposal_id} hard rejected: {decision.reason}")
            return {
                "proposal_id": proposal.proposal_id,
                "status": "rejected",
                "reason": decision.reason,
                "violated_rules": decision.violated_rules,
            }

        if decision.decision == PolicyDecisionType.REJECT_WITH_CONSTRAINTS:
            proposal.status = ProposalStatus.REJECTED
            self._repo.update_proposal_status(proposal.proposal_id, "rejected")
            self._repo.append_audit_log(
                "ProposalRejected", "policy_engine", "soft_reject",
                target=proposal.proposal_id,
                details={"reason": decision.reason, "constraints": decision.constraints},
            )
            logger.info(f"Proposal {proposal.proposal_id} needs revision: {decision.reason}")
            return {
                "proposal_id": proposal.proposal_id,
                "status": "needs_revision",
                "reason": decision.reason,
                "constraints": decision.constraints,
            }

        # Step 2: Permission gate (LOW risk auto-approves)
        if risk_level == RiskLevel.LOW:
            return await self._execute(proposal, effective_executor)

        # MEDIUM/HIGH risk — need user approval
        if risk_level == RiskLevel.HIGH:
            # High risk requires explicit approval — queue it
            return await self._request_approval(proposal, effective_executor, explicit=True)
        else:
            # MEDIUM — could auto-approve with confirmation, or queue
            return await self._request_approval(proposal, effective_executor, explicit=False)

    async def _request_approval(
        self,
        proposal: Proposal,
        executor: Callable[[dict[str, Any]], Awaitable[Any]] | None,
        explicit: bool,
    ) -> dict[str, Any]:
        """Request user approval for a medium/high risk action."""
        from ..contracts.messages import PermissionRequest
        perm_id = str(uuid.uuid4())
        # expires_at already set on the Proposal in submit(); reuse it for the PermissionRequest
        expires = proposal.expires_at or (utcnow() + timedelta(minutes=30 if explicit else 5))
        perm = PermissionRequest(
            id=perm_id,
            proposal_id=proposal.proposal_id,
            action=proposal.action,
            reason=proposal.reasoning or f"Execute {proposal.action}",
            impact=proposal.expected_impact,
            risk_level=proposal.risk_level,
            expires_at=expires,
        )
        self._repo.append_permission(perm)
        # Persist the executor alongside the pending proposal so approve() can call it.
        # Note: the executor itself is in-memory only — on restart, restore_pending()
        # rehydrates it via the registered factory for this action.
        self._pending[perm_id] = (proposal, executor)
        self._repo.append_audit_log(
            "PermissionRequested", "proposal_manager", "request_approval",
            target=perm_id,
            details={"action": proposal.action, "risk": proposal.risk_level.value},
        )
        logger.info(
            f"Permission requested ({perm_id}) for {proposal.action} "
            f"[{proposal.risk_level.value}] — executor {'attached' if executor else 'none'}"
        )
        return {
            "proposal_id": proposal.proposal_id,
            "permission_id": perm_id,
            "status": "pending_approval",
            "risk_level": proposal.risk_level.value,
            "reason": proposal.reasoning,
            "expires_at": expires.isoformat(),
        }

    async def approve(
        self,
        permission_id: str,
        approved_by: str = "user",
        executor: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        """Approve a pending proposal and execute it.

        The `executor` parameter is optional — if not provided, the executor
        stored at submit() time (or rehydrated by restore_pending()) is used.
        Callers may override it for one-off cases.
        """
        if permission_id not in self._pending:
            return {"status": "error", "reason": "Permission request not found or expired"}
        proposal, stored_executor = self._pending.pop(permission_id)
        # Check expiry (proposal.expires_at is now set in submit())
        if proposal.expires_at and utcnow() > proposal.expires_at:
            self._repo.update_permission_status(permission_id, "expired")
            return {"status": "expired", "reason": "Permission request expired"}

        self._repo.update_permission_status(permission_id, "granted", approved_by=approved_by)
        self._repo.append_audit_log(
            "PermissionGranted", approved_by, "approve",
            target=permission_id,
            details={"proposal_id": proposal.proposal_id},
        )
        # Prefer the explicitly-passed executor, fall back to the stored one
        effective_executor = executor if executor is not None else stored_executor
        return await self._execute(proposal, effective_executor)

    async def deny(self, permission_id: str, denied_by: str = "user") -> dict[str, Any]:
        """Deny a pending proposal."""
        if permission_id not in self._pending:
            return {"status": "error", "reason": "Permission request not found"}
        proposal, _ = self._pending.pop(permission_id)
        self._repo.update_permission_status(permission_id, "denied")
        proposal.status = ProposalStatus.REJECTED
        self._repo.update_proposal_status(proposal.proposal_id, "rejected")
        self._repo.append_audit_log(
            "PermissionDenied", denied_by, "deny",
            target=permission_id,
            details={"proposal_id": proposal.proposal_id},
        )
        logger.info(f"Permission {permission_id} denied by {denied_by}")
        return {"status": "denied", "proposal_id": proposal.proposal_id}

    async def _execute(
        self,
        proposal: Proposal,
        executor: Callable[[dict[str, Any]], Awaitable[Any]] | None,
    ) -> dict[str, Any]:
        """Execute the proposal's action."""
        if executor is None:
            # No executor — mark as approved so the audit trail records the
            # approval. This is the normal path for LOW-risk reads (task.list,
            # memory.retrieve) where there's no side effect to run.
            # For MEDIUM/HIGH actions that *should* have an executor, the
            # caller must register one via register_executor() — otherwise
            # the approval is recorded but the side effect silently won't run.
            proposal.status = ProposalStatus.APPROVED
            self._repo.update_proposal_status(proposal.proposal_id, "approved")
            # Only log a warning if this was a MEDIUM/HIGH action (those
            # SHOULD have had an executor). LOW-risk reads are fine without.
            if proposal.risk_level != RiskLevel.LOW:
                self._repo.append_audit_log(
                    "ProposalApprovedNoExecutor", "executor", "execute",
                    target=proposal.proposal_id,
                    details={"action": proposal.action, "note": "No executor registered for this action"},
                )
                logger.warning(
                    f"Proposal {proposal.proposal_id} approved but no executor registered "
                    f"for action '{proposal.action}'. Marking approved; no side effect will run."
                )
            return {
                "proposal_id": proposal.proposal_id,
                "status": "approved",
                "result": None,
            }
        try:
            result = await executor(proposal.parameters)
            proposal.status = ProposalStatus.EXECUTED
            proposal.executed_at = utcnow()
            self._repo.update_proposal_status(proposal.proposal_id, "executed")
            self._repo.append_audit_log(
                "ProposalExecuted", "executor", "execute",
                target=proposal.proposal_id,
                details={"action": proposal.action, "success": True},
            )
            logger.success(f"Proposal {proposal.proposal_id} executed: {proposal.action}")
            return {
                "proposal_id": proposal.proposal_id,
                "status": "executed",
                "result": result,
            }
        except Exception as e:
            proposal.status = ProposalStatus.REJECTED
            self._repo.update_proposal_status(proposal.proposal_id, "rejected")
            self._repo.append_audit_log(
                "ProposalExecutionFailed", "executor", "execute",
                target=proposal.proposal_id,
                details={"action": proposal.action, "error": str(e)},
            )
            logger.error(f"Proposal {proposal.proposal_id} execution failed: {e}")
            return {
                "proposal_id": proposal.proposal_id,
                "status": "execution_failed",
                "error": str(e),
            }

    def list_pending(self) -> list[dict[str, Any]]:
        """List all pending permission requests."""
        return [
            {
                "permission_id": pid,
                "proposal_id": p.proposal_id,
                "action": p.action,
                "risk_level": p.risk_level.value,
                "reason": p.reasoning,
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                "has_executor": executor is not None,
            }
            for pid, (p, executor) in self._pending.items()
        ]
