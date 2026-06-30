"""SpeechToTextAgent — audio → text via Gemini native audio input."""
from __future__ import annotations

from typing import Any

from loguru import logger

from ..contracts.messages import AgentProposal, ArchyEvaluation, STTPayload
from ..gemini.client import GeminiClient
from .base import Agent, new_proposal_id


class STTAgent:
    """Transcribes audio using Gemini's native audio input."""

    name = "stt"
    proposal_type = "transcription"

    def __init__(self, client: GeminiClient):
        self._client = client

    async def run(self, audio_bytes: bytes, mime: str, **_: Any) -> AgentProposal:
        logger.info(f"STTAgent transcribing {len(audio_bytes)} bytes of {mime}")
        transcript = await self._client.transcribe_audio_with_fallback(audio_bytes, mime)
        logger.info(f"STTAgent transcript: {transcript[:120]!r}")

        payload = STTPayload(
            transcript=transcript,
            language="en",
            confidence=0.9,  # Gemini doesn't return confidence; fixed for now
        )
        return AgentProposal(
            proposal_id=new_proposal_id(),
            agent_name=self.name,
            proposal_type=self.proposal_type,
            payload=payload.model_dump(),
            self_confidence=85,
            internal_reasoning="Gemini native audio transcription.",
        )

    async def revise(self, evaluation: ArchyEvaluation, **original_kwargs: Any) -> AgentProposal:
        # STT is factual — no revision possible. Re-run as-is.
        return await self.run(**original_kwargs)
