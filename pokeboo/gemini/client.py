"""Shared async Gemini client.

Supports:
- Per-call model override (so Model Router can route to different tiers)
- Fallback chain (auto-retry with next model on 429/503/504)
- Gemini 2.5+ "thinking" models via thinkingConfig
- Native audio input (file-based STT)
- Structured JSON output (responseMimeType: application/json)
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from ..config import Settings
from .fallback import confirm_task_completion, run_with_fallback

if TYPE_CHECKING:
    # Type-only import to keep the offline client out of the runtime import graph
    # (avoids a circular import: offline_client imports Settings from ..config).
    from .offline_client import OfflineGeminiClient


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


class GeminiClient:
    """Async Gemini REST client with thinking-model + per-call model support."""

    URL_TMPL = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY is required for online mode. "
                "Get one at https://aistudio.google.com/apikey"
            )
        self._api_key = settings.gemini_api_key
        self._model = settings.llm_model
        self._model_pro = settings.llm_model_premium
        self._thinking_budget = settings.llm_thinking_budget
        self._client = httpx.AsyncClient(timeout=60.0)

    def _build_generation_config(
        self,
        temperature: float,
        max_output_tokens: int,
        json_output: bool = True,
        model: str | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": temperature,
            "topP": 0.9,
            "maxOutputTokens": max_output_tokens,
        }
        if json_output:
            config["responseMimeType"] = "application/json"
        # thinkingConfig is ONLY for Gemini 2.5+ thinking models.
        # Gemma models and Gemini 2.0 reject it with 400 error.
        actual_model = model or self._model
        if actual_model.startswith("gemini-2.5") or actual_model.startswith("gemini-3"):
            config["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        return config

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 800,
    ) -> dict[str, Any]:
        """Text-in, JSON-out. Model can be overridden per call (for Model Router)."""
        actual_model = model or self._model
        url = self.URL_TMPL.format(model=actual_model)
        params = {"key": self._api_key}
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": self._build_generation_config(
                temperature, max_output_tokens, json_output=True, model=actual_model
            ),
        }
        resp = await self._client.post(url, params=params, json=payload)
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise RuntimeError(
                f"Gemini API error {resp.status_code} for model '{model or self._model}': {err_msg}"
            )
        data = resp.json()
        text = _extract_text(data)
        parsed = _safe_load_json(text)
        if parsed is None:
            raise RuntimeError(f"Gemini returned unparseable JSON: {text[:200]}")
        return parsed

    async def generate_json_pro(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.4,
        max_output_tokens: int = 1200,
    ) -> dict[str, Any]:
        """Use the premium model — for complex reasoning."""
        return await self.generate_json(
            system_prompt,
            user_prompt,
            model=self._model_pro,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

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
        """Generate JSON with automatic fallback to next model on failure.

        Tries the agent's preferred model first; if it returns 429/503/504,
        automatically retries with the next model in the agent's fallback chain.
        """
        async def _try(model: str) -> dict[str, Any]:
            return await self.generate_json(
                system_prompt, user_prompt,
                model=model, temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

        result, model_used = await run_with_fallback(
            agent_name, _try, operation_name=operation_name
        )
        confirm_task_completion(agent_name, model_used, operation_name)
        return result

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
        """Generate text with automatic fallback. For lightweight Gemma calls."""
        async def _try(model: str) -> str:
            return await self.generate_text(
                system_prompt, user_prompt,
                model=model, temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

        result, model_used = await run_with_fallback(
            agent_name, _try, operation_name=operation_name
        )
        confirm_task_completion(agent_name, model_used, operation_name)
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
        """Text-in, text-out (no JSON). For lightweight generation with Gemma 4."""
        actual_model = model or self._model
        url = self.URL_TMPL.format(model=actual_model)
        params = {"key": self._api_key}
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": self._build_generation_config(
                temperature, max_output_tokens, json_output=False, model=actual_model
            ),
        }
        resp = await self._client.post(url, params=params, json=payload)
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise RuntimeError(
                f"Gemini API error {resp.status_code} for model '{model or self._model}': {err_msg}"
            )
        data = resp.json()
        return _extract_text(data)

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime: str,
        *,
        language_hint: str = "en",
    ) -> str:
        """Native audio input — send audio bytes inline, get transcript."""
        import base64

        encoded = base64.b64encode(audio_bytes).decode("ascii")
        url = self.URL_TMPL.format(model=self._model)
        params = {"key": self._api_key}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"Transcribe this audio faithfully. Language hint: {language_hint}. Return only the transcript text, no commentary."},
                        {"inline_data": {"mime_type": mime, "data": encoded}},
                    ],
                }
            ],
            "generationConfig": self._build_generation_config(
                temperature=0.0, max_output_tokens=800, json_output=False, model=self._model
            ),
        }
        resp = await self._client.post(url, params=params, json=payload)
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise RuntimeError(
                f"Gemini API error {resp.status_code} for transcription: {err_msg}"
            )
        data = resp.json()
        return _extract_text(data).strip()

    async def transcribe_audio_with_fallback(
        self,
        audio_bytes: bytes,
        mime: str,
        language_hint: str = "en",
    ) -> str:
        """Transcribe audio with fallback chain — STT doesn't fail hard on 429/503."""
        from .fallback import run_with_fallback, confirm_task_completion, is_fallback_error

        async def _try(model: str) -> str:
            import base64
            encoded = base64.b64encode(audio_bytes).decode("ascii")
            url = self.URL_TMPL.format(model=model)
            params = {"key": self._api_key}
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": f"Transcribe this audio faithfully. Language hint: {language_hint}. Return only the transcript text."},
                            {"inline_data": {"mime_type": mime, "data": encoded}},
                        ],
                    }
                ],
                "generationConfig": self._build_generation_config(
                    temperature=0.0, max_output_tokens=800, json_output=False, model=model
                ),
            }
            resp = await self._client.post(url, params=params, json=payload)
            if resp.status_code >= 400:
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("error", {}).get("message", resp.text[:300])
                except Exception:
                    err_msg = resp.text[:300]
                raise RuntimeError(
                    f"Gemini API error {resp.status_code} for model '{model}': {err_msg}"
                )
            data = resp.json()
            return _extract_text(data).strip()

        result, model_used = await run_with_fallback(
            "stt", _try, operation_name="audio transcription"
        )
        confirm_task_completion("stt", model_used, "audio transcription")
        return result

    async def close(self) -> None:
        await self._client.aclose()


def _extract_text(data: dict) -> str:
    """Extract text from Gemini response, filtering out thinking parts."""
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text_parts = [
            p["text"] for p in parts
            if "text" in p and not p.get("thought", False)
        ]
        return "".join(text_parts)
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Gemini response shape: {data}") from e


def _safe_load_json(text: str) -> dict[str, Any] | list[Any] | None:
    text = text.strip()
    m = _JSON_BLOCK_RE.search(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None


_client: GeminiClient | OfflineGeminiClient | None = None


def get_gemini_client(settings: Settings | None = None) -> GeminiClient | OfflineGeminiClient:
    """Get the shared Gemini client instance.

    Dispatches on `settings.mode`:
    - `"online"` (default): returns a real `GeminiClient` that hits the Gemini
      REST API. Requires `GEMINI_API_KEY`.
    - `"offline"`: returns an `OfflineGeminiClient` that returns canned
      responses and makes zero network calls. No API key required. Useful for
      local development, UI testing, and CI.

    The returned object has the same public method signatures either way, so
    callers (agents, interpreter, manager) don't need to care which mode is
    active.
    """
    global _client
    if _client is not None:
        return _client
    from ..config import get_settings

    s = settings or get_settings()

    if s.mode == "offline":
        # Imported lazily to avoid circular import (offline_client imports Settings).
        from .offline_client import OfflineGeminiClient
        _client = OfflineGeminiClient(s)
        return _client

    _client = GeminiClient(s)
    return _client
