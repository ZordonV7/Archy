"""MoodAgent — LLM analyst + advisor.

Analyzes recent events (task completions, drift, missed deadlines) and
produces a mood score + label + causes + recommendations.

This is ADVISORY. The rule-based `core.mood_engine.MoodEngine` is the
final decider (deterministic, testable). The MoodAgent adds nuance —
explaining *why* the user feels a certain way and *what to do about it*.

PokeBo consumes this to:
1. Decide whether to veto aggressive planner proposals
2. Match its user-facing tone to the mood label
3. Show recommendations as actionable suggestions
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ..contracts.messages import (
    AgentContext,
    AgentProposal,
    MoodAnalysisPayload,
    PokeBoEvaluation,
    Task,
    utcnow,
)
from ..gemini.client import GeminiClient
from .base import Agent, new_proposal_id


SYSTEM_PROMPT = """You are the MoodAgent in a personal scheduling system.

Analyze the user's recent task history and current schedule. Produce a mood assessment.

Rules:
- `score` (0-100): your estimate of the user's current stress/focus state. 100 = deep productive focus. 50 = drifting. 0 = panic.
- `label`: one of "deep_focus" (>=90), "watchful" (>=70), "drift_alert" (>=50), "critical_panic" (<50).
- `causes`: list of short strings explaining what's driving the score (e.g., "2 missed deadlines in past hour").
- `recommendations`: list of short actionable strings for the manager (PokeBo) to consider (e.g., "reduce next slot to 25 minutes", "acknowledge progress before next task").
- `reasoning`: one sentence, internal — never shown to user.

Consider:
- Recent completions (on-time vs late vs missed)
- Pending task load vs. time available
- Whether deadlines are clustering
- Drift events in the past hour

Return STRICT JSON. Schema:
{
  "score": integer,
  "label": string,
  "causes": [string, ...],
  "recommendations": [string, ...],
  "reasoning": string
}
"""


class MoodAgent:
    name = "mood"
    proposal_type = "mood_analysis"

    def __init__(self, client: GeminiClient):
        self._client = client

    async def run(
        self,
        tasks: list[Task],
        recent_drift_minutes: int = 0,
        constraints: dict | None = None,
        agent_context: AgentContext | None = None,
        **_: Any,
    ) -> AgentProposal:
        constraints = constraints or {}
        # Build compact summary for LLM
        summary = {
            "total_tasks": len(tasks),
            "by_status": {},
            "overdue_pending": 0,
            "at_risk_scheduled": 0,
            "recent_drift_minutes": recent_drift_minutes,
        }
        for t in tasks:
            s = t.status.value
            summary["by_status"][s] = summary["by_status"].get(s, 0) + 1
            if t.status.value in ("pending", "scheduled") and t.deadline:
                if t.deadline < utcnow():
                    summary["overdue_pending"] += 1
            if t.status.value == "scheduled" and t.scheduled_end and t.deadline:
                if t.scheduled_end > t.deadline:
                    summary["at_risk_scheduled"] += 1

        user_prompt = f"Current state summary:\n{json.dumps(summary, indent=2)}\n"
        # NEW: include AgentContext summary if provided (recent mood history,
        # recent conflicts, recalled memories — helps the LLM reason about
        # what's been happening recently)
        if agent_context is not None:
            ctx_lines = []
            mood_ctx = agent_context.mood_context or {}
            if mood_ctx:
                history = mood_ctx.get("history", [])
                if history:
                    ctx_lines.append(f"Recent mood history: {history}")
            conflict_ctx = agent_context.conflict_context or {}
            if conflict_ctx.get("recent"):
                ctx_lines.append(f"Recent conflicts: {len(conflict_ctx['recent'])} in last hour")
            memory_ctx = agent_context.memory_context or {}
            if memory_ctx.get("memories"):
                ctx_lines.append(f"Recalled {len(memory_ctx['memories'])} relevant memories")
            if ctx_lines:
                user_prompt += f"\nAgent context:\n" + "\n".join(f"  - {l}" for l in ctx_lines) + "\n"
        if constraints:
            user_prompt += f"\nManager constraints: {constraints}\n"

        logger.info(f"MoodAgent analyzing {len(tasks)} tasks")
        data = await self._client.generate_json_with_fallback(
            agent_name="mood",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            operation_name="mood analysis",
        )
        payload = MoodAnalysisPayload.model_validate(data)

        confidence = 80
        if summary["total_tasks"] < 3:
            confidence -= 15  # not enough signal

        return AgentProposal(
            proposal_id=new_proposal_id(),
            agent_name=self.name,
            proposal_type=self.proposal_type,
            payload=payload.model_dump(),
            self_confidence=confidence,
            internal_reasoning=payload.reasoning,
            constraints_used=constraints,
        )

    async def revise(
        self,
        evaluation: PokeBoEvaluation,
        tasks: list[Task],
        recent_drift_minutes: int = 0,
        agent_context: AgentContext | None = None,
        **_: Any,
    ) -> AgentProposal:
        new_constraints = {**evaluation.constraints}
        new_constraints["_feedback"] = evaluation.feedback
        return await self.run(
            tasks=tasks,
            recent_drift_minutes=recent_drift_minutes,
            constraints=new_constraints,
            agent_context=agent_context,
        )
