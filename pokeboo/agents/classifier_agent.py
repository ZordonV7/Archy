"""ClassifierAgent — text → structured task metadata (priority/complexity/deadline)."""
from __future__ import annotations

from typing import Any

from loguru import logger

from ..contracts.messages import AgentProposal, ClassificationPayload, PokeBoEvaluation
from ..gemini.client import GeminiClient
from .base import Agent, new_proposal_id


SYSTEM_PROMPT = """You are the ClassifierAgent in a personal scheduling system.

Given a short informal user utterance (often transcribed speech), produce structured JSON describing the task.

Rules:
- `title`: imperative, <= 8 words, title-case.
- `category`: one of "finance", "academics", "work", "personal", "health", "general".
- `priority` (0-100): higher = more urgent. Consider deadline proximity, financial/legal consequences, social impact.
- `complexity` (0-100): higher = more cognitive/execution effort. Filing a form = 30. Writing a thesis chapter = 90.
- `deadline_iso`: ISO 8601 datetime in UTC. If relative ("tonight", "tomorrow 9am", "in 4h"), resolve against current time. If no deadline, return null.
- `duration_minutes`: realistic focused-work estimate. Minimum 15, maximum 240.
- `reasoning`: ONE short sentence explaining the priority. This is internal — never shown to user.

Return STRICT JSON only. Schema:
{
  "title": string,
  "category": string,
  "priority": integer,
  "complexity": integer,
  "deadline_iso": string | null,
  "duration_minutes": integer,
  "reasoning": string
}

You may receive `constraints` from the manager (PokeBo) asking you to adjust.
Respect them: if `max_priority` is set, do not exceed it. If `force_category` is set, use it.
"""


class ClassifierAgent:
    name = "classifier"
    proposal_type = "classification"

    def __init__(self, client: GeminiClient):
        self._client = client

    async def run(self, text: str, constraints: dict | None = None, **_: Any) -> AgentProposal:
        constraints = constraints or {}
        user_prompt = f"Current UTC time: {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}\n\nUser utterance:\n{text}"

        if constraints:
            user_prompt += f"\n\nManager constraints: {constraints}"

        logger.info(f"ClassifierAgent classifying: {text[:80]!r}")
        data = await self._client.generate_json_with_fallback(
            agent_name="classifier",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
            operation_name="task classification",
        )
        payload = ClassificationPayload.model_validate(data)

        # Self-confidence: higher if priority/deadline are extreme (clear signal)
        # or lower if ambiguous.
        confidence = 75
        if payload.priority >= 90 or payload.priority <= 20:
            confidence = 90
        if not payload.deadline_iso:
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

    async def revise(self, evaluation: PokeBoEvaluation, text: str, **_: Any) -> AgentProposal:
        # Re-run with PokeBo's constraints merged in.
        new_constraints = {**evaluation.constraints}
        new_constraints["_feedback"] = evaluation.feedback
        return await self.run(text=text, constraints=new_constraints)
