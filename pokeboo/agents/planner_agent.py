"""PlannerAgent — LLM advisory schedule suggestion.

This is ADVISORY only. The rule-based `core.scheduler.GreedyScheduler`
remains the final decider. The PlannerAgent proposes an ordering and
flags at-risk tasks; the rule-based scheduler actually commits slots.

PokeBo can reject this agent's proposal (e.g., "plan too aggressive for
current mood") and ask for a revision with tighter constraints.

Model routing: ALWAYS uses the premium tier (3.5 Flash / 3 Flash) because
planning requires complex reasoning about deadlines, priorities, and trade-offs.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ..contracts.messages import (
    AgentContext,
    AgentProposal,
    MoodAnalysisPayload,
    PlannerPayload,
    PokeBoEvaluation,
    ScheduledSlot,
    Task,
)
from ..gemini.client import GeminiClient
from .base import Agent, new_proposal_id


SYSTEM_PROMPT = """You are the PlannerAgent in a personal scheduling system.

Given a list of pending tasks (with priority, complexity, deadline, duration), propose an execution order. You are ADVISORY — the rule-based scheduler is the final decider, but your suggestions influence it.

Rules:
- Order tasks by composite urgency: priority first, then deadline proximity, then inverse complexity.
- Identify tasks whose slot would end after their deadline (`at_risk_task_ids`).
- Provide `suggestions`: short actionable strings (e.g., "consolidate tax filing into a single 25-min sprint").
- `reasoning`: one sentence, internal — never shown to user.
- If `constraints` from manager are provided (e.g., `max_slots`, `max_slot_minutes`, `require_buffer_minutes`), respect them strictly.
- If an AgentContext is provided, use it: prefer the user's best focus hours for high-complexity tasks, avoid scheduling during low-energy periods, and respect learned preferences.

Return STRICT JSON. Schema:
{
  "ordered_task_ids": [string, ...],
  "at_risk_task_ids": [string, ...],
  "suggestions": [string, ...],
  "reasoning": string
}

Do NOT generate actual datetime slots — the rule-based scheduler does that.
"""


def _summarize_context_for_planner(ctx: AgentContext) -> str:
    """Compact summary of AgentContext for inclusion in the planner prompt."""
    lines = []
    # Mood + energy
    mood = ctx.mood_context or {}
    if mood:
        lines.append(
            f"Current mood: score={mood.get('score')}, label={mood.get('label')}"
        )
    energy = ctx.energy_context or {}
    if energy:
        lines.append(
            f"Current energy: score={energy.get('score')}, label={energy.get('label')}"
        )
    # User preferences (best/worst hours)
    prefs = ctx.user_preferences or {}
    best_hours = prefs.get("best_hours")
    if best_hours:
        lines.append(f"User's best focus hours: {best_hours}")
    worst_hours = prefs.get("worst_hours")
    if worst_hours:
        lines.append(f"User's worst hours (avoid): {worst_hours}")
    # Recent conflicts (informs what went wrong last time)
    conflicts = ctx.conflict_context or {}
    recent = conflicts.get("recent", [])
    if recent:
        lines.append(f"Recent agent conflicts ({len(recent)}): plan was revised {len(recent)}x recently")
    # Memories (semantic recall)
    memories = ctx.memory_context or {}
    recalled = memories.get("memories", [])
    if recalled:
        lines.append(f"Recalled {len(recalled)} relevant memories (e.g., {recalled[0].get('content', '')[:60]!r})")
    return "\n".join(lines) if lines else ""


class PlannerAgent:
    name = "planner"
    proposal_type = "schedule"

    def __init__(self, client: GeminiClient, model_router=None):
        self._client = client
        self._router = model_router

    async def run(
        self,
        tasks: list[Task],
        mood: MoodAnalysisPayload | None = None,
        constraints: dict | None = None,
        agent_context: AgentContext | None = None,
        **_: Any,
    ) -> AgentProposal:
        constraints = constraints or {}
        compact = [
            {
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "complexity": t.complexity,
                "deadline": t.deadline.isoformat() if t.deadline else None,
                "duration_minutes": t.duration_minutes,
            }
            for t in tasks
        ]
        user_prompt = (
            f"Pending tasks ({len(compact)}):\n"
            f"{json.dumps(compact, indent=2)}\n\n"
        )
        if mood:
            user_prompt += (
                f"User's current mood: score={mood.score}, label={mood.label}, "
                f"causes={mood.causes}, recommendations={mood.recommendations}\n\n"
            )
        # NEW: include AgentContext summary if provided
        if agent_context is not None:
            ctx_summary = _summarize_context_for_planner(agent_context)
            if ctx_summary:
                user_prompt += f"Agent context:\n{ctx_summary}\n\n"
        if constraints:
            user_prompt += f"Manager constraints: {constraints}\n"

        # Use fallback chain — planner tries premium first, falls back to default on 503/429
        logger.info(f"PlannerAgent proposing order for {len(tasks)} tasks")
        data = await self._client.generate_json_with_fallback(
            agent_name="planner",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            operation_name=f"planning for {len(tasks)} tasks",
        )

        payload = PlannerPayload(
            slots=[],
            at_risk_task_ids=data.get("at_risk_task_ids", []),
            suggestions=data.get("suggestions", []),
            reasoning=data.get("reasoning", ""),
        )

        confidence = 80
        if mood and mood.score < 50:
            confidence -= 10

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
        mood: MoodAnalysisPayload | None = None,
        agent_context: AgentContext | None = None,
        **_: Any,
    ) -> AgentProposal:
        new_constraints = {**evaluation.constraints}
        new_constraints["_feedback"] = evaluation.feedback
        return await self.run(tasks=tasks, mood=mood, constraints=new_constraints, agent_context=agent_context)
