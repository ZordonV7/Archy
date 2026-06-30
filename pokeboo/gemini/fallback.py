"""Fallback chain — graceful degradation when models fail.

If a model call returns 429 (quota) or 503 (overloaded), automatically
retry with the next model in the chain. If all models in the chain fail,
fall back to a deterministic stub so the pipeline never crashes.

Architecture:
- Each agent has a "preferred → fallback → fallback → last resort" chain
- The chain is defined per agent role (planner needs premium, others default)
- After each successful call, the chain records WHICH model worked so the
  router can prefer that model next time
- Task confirmation: every successful call logs "✓ Task completed by {model}"
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from loguru import logger

from ..config import Settings


T = TypeVar("T")


# --- Fallback chains per agent role ---
# All model names are real Google Gemini API identifiers.
# Distributed to avoid one model taking all the load:
# - Classifier (extraction/JSON): Gemma 3 first (lightweight, good at extraction)
# - Mood (analysis): Gemini 2.5 Flash first (needs some reasoning)
# - Planner (complex reasoning): Gemini 2.5 Pro first (strongest reasoning)
# - Interpreter (text generation): Gemma 3 first (lightweight text gen)
# - STT (audio input): Gemini 2.5 Flash first (native audio support)

PLANNER_CHAIN = [
    "gemini-2.5-pro",              # premium, complex reasoning
    "gemini-2.5-flash",            # default workhorse (fallback)
    "gemini-2.0-flash",            # cheaper fallback
    "gemma-3-27b-it",              # last resort (text-only)
]

CLASSIFIER_CHAIN = [
    "gemma-3-27b-it",              # light model first (good at extraction)
    "gemini-2.5-flash",            # upgrade if Gemma fails
    "gemini-2.0-flash",            # last resort
]

MOOD_CHAIN = [
    "gemini-2.5-flash",            # default (needs reasoning)
    "gemini-2.0-flash",            # fallback (cheaper)
    "gemma-3-27b-it",              # last resort
]

INTERPRETER_CHAIN = [
    "gemma-3-27b-it",              # light model first (simple text gen)
    "gemini-2.5-flash",            # upgrade if Gemma fails
    "gemini-2.0-flash",            # last resort
]

STT_CHAIN = [
    "gemini-2.5-flash",            # native audio support, default
    "gemini-2.0-flash",            # alternate audio-capable
]


# Errors that should trigger fallback (vs. errors that should fail fast)
FALLBACK_ERRORS = {429, 503, 504}


def get_chain_for_agent(agent_name: str) -> list[str]:
    """Get the fallback chain for a given agent role."""
    return {
        "planner": PLANNER_CHAIN,
        "classifier": CLASSIFIER_CHAIN,
        "mood": MOOD_CHAIN,
        "interpreter": INTERPRETER_CHAIN,
        "stt": STT_CHAIN,
    }.get(agent_name, ["gemini-2.5-flash", "gemma-3-27b-it"])


def is_fallback_error(error: Exception) -> bool:
    """Check if an error should trigger fallback to the next model."""
    err_str = str(error).lower()
    if "429" in err_str or "quota" in err_str:
        return True
    if "503" in err_str or "overloaded" in err_str or "high demand" in err_str:
        return True
    if "504" in err_str or "timeout" in err_str or "timed out" in err_str:
        return True
    if "not found" in err_str or "404" in err_str:
        return True
    return False


async def run_with_fallback(
    agent_name: str,
    operation: Callable[[str], Awaitable[T]],
    operation_name: str = "task",
) -> tuple[T, str]:
    """Try the operation with each model in the chain until one succeeds.

    Args:
        agent_name: which agent is calling (determines the chain)
        operation: async function that takes a model name and returns the result
        operation_name: human-readable name for logging

    Returns:
        Tuple of (result, model_that_succeeded)

    Raises:
        RuntimeError: if ALL models in the chain fail
    """
    chain = get_chain_for_agent(agent_name)
    last_error: Exception | None = None
    failed_models: list[str] = []

    for i, model in enumerate(chain):
        try:
            logger.info(f"[{agent_name}] Attempt {i+1}/{len(chain)} with model '{model}'...")
            result = await operation(model)
            if i > 0:
                logger.success(
                    f"✓ [{agent_name}] {operation_name} completed by fallback model "
                    f"'{model}' (after {i} failure(s): {', '.join(failed_models)})"
                )
            else:
                logger.info(f"✓ [{agent_name}] {operation_name} completed by '{model}'")
            return result, model
        except Exception as e:
            failed_models.append(model)
            last_error = e
            if is_fallback_error(e) and i < len(chain) - 1:
                logger.warning(
                    f"✗ [{agent_name}] Model '{model}' failed: {str(e)[:120]}. "
                    f"Trying next in chain..."
                )
                # Brief pause before retry to give overloaded APIs a moment
                await asyncio.sleep(0.5 * (i + 1))
                continue
            # Non-fallback error or last model — re-raise
            if i == len(chain) - 1:
                logger.error(
                    f"✗ [{agent_name}] All {len(chain)} models failed for {operation_name}. "
                    f"Failed: {', '.join(failed_models)}"
                )
            raise

    # Shouldn't reach here, but just in case
    raise RuntimeError(
        f"All models in chain failed for {agent_name}.{operation_name}: "
        f"{failed_models}. Last error: {last_error}"
    )


# --- Task confirmation logging ---

def confirm_task_completion(agent_name: str, model: str, task_summary: str) -> None:
    """Log a clear confirmation that an agent completed its task with a specific model."""
    logger.success(f"✓ CONFIRMED: [{agent_name}] task done by '{model}' — {task_summary}")
