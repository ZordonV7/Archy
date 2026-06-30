"""Base Agent protocol + shared utilities.

Every agent implements `run()` which takes a typed input and returns an
`AgentProposal`. Agents NEVER call each other; only Archy (the manager)
routes data between them.

Agents may also implement `revise()` which takes Archy's feedback +
constraints and produces a new proposal. This is the negotiation loop.
"""
from __future__ import annotations

import uuid
from typing import Any, Protocol

from ..contracts.messages import AgentProposal, ArchyEvaluation


class Agent(Protocol):
    """Interface every agent implements.

    `name` and `proposal_type` are used for routing, logging, and audit trail.
    """

    name: str
    proposal_type: str

    async def run(self, **kwargs: Any) -> AgentProposal:
        """Initial proposal. Returns structured payload + self_confidence."""
        ...

    async def revise(
        self, evaluation: ArchyEvaluation, **original_kwargs: Any
    ) -> AgentProposal:
        """Re-run with Archy's constraints. Override if agent supports negotiation."""
        # Default: agents can't revise, so re-run with no changes.
        return await self.run(**original_kwargs)


def new_proposal_id() -> str:
    return str(uuid.uuid4())
