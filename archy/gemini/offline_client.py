"""Offline Gemini client — canned responses for development without an API key.

When `ARCHY_MODE=offline` is set, `get_gemini_client()` returns an instance
of this class instead of the real `GeminiClient`. The offline client:

- Requires no `GEMINI_API_KEY`.
- Makes zero network calls.
- Returns plausible, deterministic JSON for each agent role.
- Lets the entire pipeline (ingest → classify → plan → mood → interpret → schedule)
  run end-to-end so you can develop the UI / test the deterministic core without
  hitting Google's API.

The deterministic core (SchedulerV2, MoodEngine, EnergyEngine, RiskEngine,
PolicyEngine) does all the *real* decisions — the LLM agents are advisory.
So offline mode is genuinely useful: the schedule you get back is real, the
mood/energy scores are real, only the natural-language framing is canned.

This is NOT a mock of `GeminiClient` for unit tests. For unit tests, inject a
custom fake (see `tests/test_veto.py::FakeGeminiClient`). This is a
production-mode fallback for "I want to run the server but don't have a key".
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from loguru import logger

from ..config import Settings


# --- Helpers for shaping offline responses ---

def _extract_title(text: str, max_words: int = 6) -> str:
    """Derive a reasonable task title from the user's free-form text."""
    # Strip common leading verbs ("I have to", "need to", "gotta", "remind me to")
    stripped = re.sub(
        r"^\s*(i\s+(have to|need to|gotta|want to)|remind me to|please|got to)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Take the first sentence-ish chunk
    first = re.split(r"[.!?;\n]", stripped, maxsplit=1)[0].strip()
    words = first.split()
    if not words:
        return "Untitled Task"
    title = " ".join(words[:max_words])
    # Title-case the first letter of each word, preserving short prepositions
    return title.title()


def _detect_category(text: str) -> str:
    """Crude keyword-based category detection."""
    t = text.lower()
    if any(w in t for w in ["tax", "invoice", "bill", "pay", "bank", "money", "finance"]):
        return "finance"
    if any(w in t for w in ["thesis", "essay", "homework", "study", "exam", "lecture", "class", "assignment"]):
        return "academics"
    if any(w in t for w in ["meeting", "deadline", "project", "report", "email", "boss", "client", "slides"]):
        return "work"
    if any(w in t for w in ["doctor", "medicine", "gym", "run", "workout", "sleep", "meditate", "dentist"]):
        return "health"
    if any(w in t for w in ["call mom", "call dad", "birthday", "anniversary", "groceries", "laundry", "clean"]):
        return "personal"
    return "general"


def _detect_urgency(text: str) -> int:
    """Crude urgency detection — higher = more urgent."""
    t = text.lower()
    score = 50
    if any(w in t for w in ["asap", "urgent", "emergency", "critical"]):
        score += 30
    if any(w in t for w in ["tonight", "today", "now", "immediately"]):
        score += 20
    if any(w in t for w in ["tomorrow"]):
        score += 10
    if any(w in t for w in ["next week", "eventually", "someday", "later"]):
        score -= 20
    return max(10, min(100, score))


def _detect_complexity(text: str) -> int:
    """Crude complexity detection."""
    t = text.lower()
    score = 35
    if any(w in t for w in ["write", "draft", "design", "build", "implement", "create"]):
        score += 30
    if any(w in t for w in ["file", "submit", "fill", "form"]):
        score -= 5
    if any(w in t for w in ["thesis", "report", "presentation", "chapter"]):
        score += 25
    if len(text.split()) > 15:
        score += 10
    return max(15, min(95, score))


def _hash_embedding(text: str, dim: int = 256) -> list[float]:
    """Deterministic pseudo-embedding from text — for offline semantic search.

    Uses SHA-256 of the text to seed a deterministic vector. The same text
    always yields the same vector; similar texts are not actually similar
    (this is not a real embedding), but the cosine-similarity machinery can
    still run without crashing.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Repeat hash to fill `dim` bytes
    while len(h) < dim:
        h += hashlib.sha256(h).digest()
    return [((b - 128) / 128.0) for b in h[:dim]]


# --- Per-agent offline response builders ---

def _classifier_response(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Build a ClassificationPayload-shaped dict from the user utterance."""
    # The classifier agent puts the user text after a "User utterance:" marker
    m = re.search(r"User utterance:\s*(.+)$", user_prompt, flags=re.DOTALL)
    user_text = m.group(1).strip() if m else user_prompt.strip()
    # Strip any manager-constraints section if appended
    user_text = re.split(r"\n\nManager constraints:", user_text, maxsplit=1)[0].strip()

    return {
        "title": _extract_title(user_text),
        "category": _detect_category(user_text),
        "priority": _detect_urgency(user_text),
        "complexity": _detect_complexity(user_text),
        "deadline_iso": None,  # offline mode doesn't parse relative deadlines
        "duration_minutes": 30,
        "reasoning": "Offline mode: heuristic classification (no LLM call).",
    }


def _mood_response(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Return the baseline mood — the MoodEngine will override anyway."""
    return {
        "score": 75,
        "label": "watchful",
        "causes": [],
        "recommendations": [
            "Offline mode: mood agent disabled. MoodEngine rule-based score is the source of truth."
        ],
        "reasoning": "Offline mode: returning baseline mood. MoodEngine will compute the real score.",
    }


def _planner_response(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Return an empty advisory plan — SchedulerV2 does the real scheduling."""
    return {
        "ordered_task_ids": [],  # advisory only; SchedulerV2 uses rank_score
        "at_risk_task_ids": [],
        "suggestions": [
            "Offline mode: planner agent disabled. SchedulerV2 (rule-based) is the source of truth."
        ],
        "reasoning": "Offline mode: returning empty advisory plan.",
    }


def _interpreter_response(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Generate a friendly canned Archy message.

    Tries to be context-aware by sniffing the event_type in the user_prompt,
    so the message at least matches the kind of thing that happened.
    """
    p = user_prompt.lower()
    if "planready" in p or "plan_ready" in p:
        text = "Here's your updated plan. Take it one step at a time. 💜"
    elif "taskcompleted" in p or "task_completed" in p:
        text = "Nice work finishing that! Small wins add up. ✨"
    elif "taskmissed" in p or "task_missed" in p:
        text = "That one slipped past — it's okay. Let's pick it back up gently."
    elif "taskcancelled" in p or "task_cancelled" in p:
        text = "Got it, I've cancelled that one for you."
    elif "driftdetected" in p or "drift_detected" in p:
        text = "Sounds like the plan shifted. Let's adjust together."
    elif "risk_alert" in p or "riskalert" in p:
        text = "Heads up — this one's getting tight. Let's find a way through."
    elif "conflictdetected" in p or "conflict_detected" in p:
        text = "I tweaked the plan a little to make it easier for you."
    elif "daily_brief" in p or "briefing" in p:
        text = "Here's your daily brief. You've got this."
    else:
        text = "I'm here. Take your time."
    return {"text": text}


# Dispatch table — agent_name → offline response builder
_OFFLINE_BUILDERS: dict[str, callable] = {
    "classifier": _classifier_response,
    "mood": _mood_response,
    "planner": _planner_response,
    "interpreter": _interpreter_response,
}


class OfflineGeminiClient:
    """Drop-in replacement for `GeminiClient` when running in offline mode.

    Same public method signatures as `GeminiClient`, but no network calls.
    All responses are deterministic and shaped to match the production
    payload schemas so the rest of the pipeline runs unchanged.
    """

    def __init__(self, settings: Settings):
        # No API key required. We keep a reference to settings for parity.
        self._settings = settings
        self._model = settings.llm_model
        self._model_pro = settings.llm_model_premium
        logger.info(
            "OfflineGeminiClient initialized — no network calls will be made. "
            "Set ARCHY_MODE=online and GEMINI_API_KEY to enable real LLM calls."
        )

    # --- JSON generation (sync signatures matching GeminiClient) ---

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 800,
    ) -> dict[str, Any]:
        # Without an agent_name, we can't dispatch — default to interpreter-shaped output.
        return _interpreter_response(system_prompt, user_prompt)

    async def generate_json_pro(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.4,
        max_output_tokens: int = 1200,
    ) -> dict[str, Any]:
        return _interpreter_response(system_prompt, user_prompt)

    async def generate_json_with_fallback(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_output_tokens: int = 800,
        operation_name: str = "JSON generation",
    ) -> dict[str, Any]:
        builder = _OFFLINE_BUILDERS.get(agent_name, _interpreter_response)
        result = builder(system_prompt, user_prompt)
        logger.debug(
            f"[offline:{agent_name}] returning canned response for '{operation_name}'."
        )
        return result

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 800,
    ) -> str:
        return _interpreter_response(system_prompt, user_prompt)["text"]

    async def generate_text_with_fallback(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.4,
        max_output_tokens: int = 800,
        operation_name: str = "text generation",
    ) -> str:
        return _interpreter_response(system_prompt, user_prompt)["text"]

    # --- Audio (returns a placeholder transcript) ---

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime: str,
        *,
        language_hint: str = "en",
    ) -> str:
        return "[offline: audio transcription unavailable without GEMINI_API_KEY]"

    async def transcribe_audio_with_fallback(
        self,
        audio_bytes: bytes,
        mime: str,
        language_hint: str = "en",
    ) -> str:
        return await self.transcribe_audio(audio_bytes, mime, language_hint=language_hint)

    async def close(self) -> None:
        # No httpx client to close.
        pass


class OfflineEmbeddingsClient:
    """Drop-in replacement for `EmbeddingsClient` when running in offline mode.

    Returns deterministic hash-based pseudo-embeddings. These are NOT real
    semantic embeddings — two texts with similar meaning will NOT have similar
    vectors. The cosine-similarity machinery still runs, but results are
    effectively random. Good enough to keep the /memories endpoints from
    crashing; not good enough for real semantic search.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._dim = 256
        logger.info(
            "OfflineEmbeddingsClient initialized — returning hash-based pseudo-embeddings. "
            "Semantic search will not produce meaningful results."
        )

    async def embed(self, text: str) -> list[float]:
        return _hash_embedding(text, dim=self._dim)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_embedding(t, dim=self._dim) for t in texts]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        # Same implementation as the real EmbeddingsClient
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def close(self) -> None:
        pass
