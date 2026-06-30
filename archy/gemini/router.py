"""Model Router — picks the right model based on task complexity.

Distributes load across Google's Gemini free-tier models to maximize the
daily quota usage. Based on Google's published free-tier limits:

| Model                          | RPD  | RPM | Best use                          |
|--------------------------------|------|-----|-----------------------------------|
| gemini-2.5-flash               | 250  | 10  | Default workhorse (most agents)   |
| gemini-2.5-pro                 | 50   | 5   | Complex reasoning (sparingly)     |
| gemini-2.0-flash               | 1500 | 15  | Cheap fallback                    |
| gemma-3-27b-it                 | 1000 | 15  | Lightweight text ops / summaries  |
| gemini-embedding-001           | 1500 | 100 | Semantic search / similarity      |
| gemini-2.5-flash-preview-tts   | 10   | 3   | Archy's voice (TTS)              |
| Live API (audio dialog)        | ∞    | ∞   | Real-time STT                     |

Strategy:
- Default → Gemini 2.5 Flash (250 RPD)
- High complexity (>=80) → Gemini 2.5 Pro (50 RPD)
- Low complexity (<=30) → Gemma 3 (1000 RPD, frees up Flash quota)
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Literal

from loguru import logger

from ..config import Settings


ModelTier = Literal["default", "premium", "light", "embedding", "tts", "live_stt"]


class ModelRouter:
    """Picks the right Gemini model for a given task.

    Tracks daily quota usage per model so we can fall back gracefully
    when limits approach.
    """

    def __init__(self, settings: Settings):
        self._s = settings
        self._lock = threading.Lock()
        # Quota tracking: {model_name: {date: {count: int, last_reset: datetime}}}
        self._usage: dict[str, dict] = {}
        # Round-robin counter for premium models (3.5 ↔ 3 Flash)
        self._premium_counter = 0

    def pick_model_for_complexity(self, complexity: int) -> str:
        """Pick the best model for a task with the given complexity score."""
        with self._lock:
            # High complexity → premium model (rotate 3.5 ↔ 3 Flash)
            if complexity >= self._s.router_complexity_threshold:
                model = self._pick_premium_model()
                logger.info(
                    f"Router: complexity={complexity} → premium model '{model}'"
                )
                return model
            # Low complexity → light model (Gemma 4)
            if complexity <= self._s.router_light_threshold:
                logger.info(
                    f"Router: complexity={complexity} → light model '{self._s.light_model}'"
                )
                return self._s.light_model
            # Default → workhorse (3.1 Flash Lite)
            logger.info(
                f"Router: complexity={complexity} → default model '{self._s.llm_model}'"
            )
            return self._s.llm_model

    def pick_model_for_agent(self, agent_name: str, complexity_hint: int = 50) -> str:
        """Pick model based on agent role + complexity hint.

        Agent-specific routing:
        - classifier: complexity-driven (mostly default, premium for ambiguous)
        - planner: always premium (complex reasoning about ordering)
        - mood: default (simple analysis)
        - interpreter (Archy voice): default (conversational)
        - stt: live_stt_model (if streaming) or default (if file upload)
        - tts: tts_model (always)
        """
        if agent_name == "planner":
            # Planner always needs strong reasoning — use premium if quota available
            preferred = self._pick_premium_model()
            if self.is_quota_available(preferred):
                return preferred
            # Premium exhausted — fall back to default (better than failing)
            logger.warning("Premium model quota exhausted; planner using default model")
            return self._s.llm_model
        if agent_name == "mood":
            return self._s.llm_model
        if agent_name == "interpreter":
            return self._s.llm_model
        if agent_name == "classifier":
            return self.pick_model_for_complexity(complexity_hint)
        if agent_name == "stt":
            return self._s.llm_model
        if agent_name == "tts":
            return self._s.tts_model
        return self._s.llm_model

    def _pick_premium_model(self) -> str:
        """Pick the premium model. Gemini 2.5 Pro is the only premium tier now."""
        self._premium_counter += 1
        # Always use 2.5 Pro — there's only one premium model in the real API.
        # Counter retained for future round-robin if Google ships multiple pro tiers.
        return self._s.llm_model_premium  # gemini-2.5-pro

    def record_usage(self, model: str) -> None:
        """Record that we made a request to the given model."""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if model not in self._usage:
                self._usage[model] = {"date": today, "count": 0}
            entry = self._usage[model]
            if entry["date"] != today:
                # Reset for new day
                entry["date"] = today
                entry["count"] = 0
            entry["count"] += 1

    def get_usage_report(self) -> dict[str, int]:
        """Get today's usage count per model."""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            return {
                model: entry["count"]
                for model, entry in self._usage.items()
                if entry["date"] == today
            }

    # --- Quota limits (for graceful fallback) ---
    # Approximate Google free-tier daily limits (requests per day).
    QUOTA_LIMITS = {
        "gemini-2.5-flash": 250,
        "gemini-2.5-pro": 50,
        "gemini-2.0-flash": 1500,
        "gemma-3-27b-it": 1000,
        "gemini-embedding-001": 1500,
        "gemini-2.5-flash-preview-tts": 10,
    }

    def is_quota_available(self, model: str) -> bool:
        """Check if a model still has daily quota remaining."""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            used = self._usage.get(model, {}).get("count", 0)
            if self._usage.get(model, {}).get("date") != today:
                used = 0
            limit = self.QUOTA_LIMITS.get(model, 100)
            return used < limit

    def pick_model_safe(self, complexity: int) -> str:
        """Like pick_model_for_complexity, but falls back if quota is exhausted."""
        preferred = self.pick_model_for_complexity(complexity)
        if self.is_quota_available(preferred):
            return preferred
        # Fallback: premium → default; light → default; default → light
        logger.warning(
            f"Router: model '{preferred}' quota exhausted, falling back to '{self._s.llm_model}'"
        )
        return self._s.llm_model
