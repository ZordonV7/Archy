"""PokeBo's interpreter — structured agent data → user-facing speech.

This is the ONLY place natural language for the user is generated. All
agents emit JSON; only PokeBo (via this module) produces prose.

Uses the Pro model for richer reasoning — PokeBo's voice is the product.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ..gemini.client import GeminiClient
from .persona import PERSONA_PROMPT


class Interpreter:
    """Translates structured agent outputs into PokeBo's user-facing speech."""

    def __init__(self, client: GeminiClient):
        self._client = client

    async def synthesize(
        self,
        event_type: str,
        mood_label: str,
        mood_score: int,
        payload: dict[str, Any],
        *,
        conflict_happened: bool = False,
    ) -> str:
        """Produce 1-3 sentences of PokeBo's speech for the given event.

        Uses the fallback chain so it NEVER crashes — if Gemini 3.5 Flash
        is overloaded, falls back to 3.1 Flash Lite, then Gemma 4, then
        a deterministic fallback message.
        """
        user_prompt = (
            f"Event: {event_type}\n"
            f"User's current mood: {mood_label} (score {mood_score}/100)\n"
            f"Conflict happened (you adjusted an agent's proposal): {conflict_happened}\n\n"
            f"Structured data from agents:\n{json.dumps(payload, indent=2, default=str)}\n\n"
            f"Respond as PokeBo. ONE sentence. Max 15 words. Match tone to mood. No technical terms. "
            f"Return JSON: {{\"text\": \"your one-sentence message here\"}}"
        )

        try:
            data = await self._client.generate_json_with_fallback(
                agent_name="interpreter",
                system_prompt=PERSONA_PROMPT,
                user_prompt=user_prompt,
                temperature=0.6,
                max_output_tokens=60,  # very short — 1 sentence max
                operation_name="PokeBo speech synthesis",
            )
            text = data.get("text") or data.get("message") or ""
            if not text:
                for v in data.values():
                    if isinstance(v, str) and len(v) > 5:
                        text = v
                        break
            if not text:
                text = _fallback_message(event_type, mood_label)
            return text.strip()
        except Exception as e:
            logger.error(f"Interpreter synthesis failed (all fallbacks exhausted): {e}")
            return _fallback_message(event_type, mood_label)


def _fallback_message(event_type: str, mood_label: str) -> str:
    """Deterministic fallback if the LLM call fails — never leave the user hanging."""
    if mood_label == "critical_panic":
        return "Hey. Breathe. One tiny step at a time — I'm right here with you."
    if event_type == "task.created":
        return "Got it. I'll find a good spot for that. You're doing great."
    if event_type == "task.completed":
        return "Yay! That's done. I'm so proud of you. On to the next when you're ready."
    if event_type == "task.missed":
        return "That one slipped past. It happens — let's pick it back up together."
    if event_type == "drift.detected":
        return "Hey, things shifted a little. I adjusted the plan. We're okay."
    return "I'm here. Take your time."
