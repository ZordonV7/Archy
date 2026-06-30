"""Gemini TTS client — generates spoken audio for Archy's messages.

Uses `gemini-3.1-flash-tts-preview` (10 RPD, 3 RPM).
Very limited quota — only use for high-value messages (Archy's voice).

The TTS model returns base64-encoded PCM audio. We decode to raw bytes
that can be played by the frontend.
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
from loguru import logger

from ..config import Settings


# Archy's voice preset — warm, friendly, slightly higher pitch
ARCHY_VOICE_STYLE = "Warm, friendly, slightly soft-spoken, gentle pace, encouraging tone"


class TTSClient:
    """Generates spoken audio for Archy's user-facing messages."""

    URL_TMPL = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY required for TTSClient")
        self._api_key = settings.gemini_api_key
        self._model = settings.tts_model
        self._client = httpx.AsyncClient(timeout=60.0)

    async def synthesize(
        self,
        text: str,
        voice_style: str = ARCHY_VOICE_STYLE,
    ) -> bytes:
        """Convert text to spoken audio bytes.

        Args:
            text: The text to speak (Archy's message)
            voice_style: Style description for the voice

        Returns: PCM audio bytes (24kHz, 16-bit, mono)
        """
        url = self.URL_TMPL.format(model=self._model)
        params = {"key": self._api_key}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Say the following in a {voice_style} voice: {text}"
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
            },
        }
        resp = await self._client.post(url, params=params, json=payload)
        if resp.status_code >= 400:
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            raise RuntimeError(f"TTS API error {resp.status_code}: {err_msg}")

        data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            for part in parts:
                if "inlineData" in part or "inline_data" in part:
                    inline = part.get("inlineData") or part.get("inline_data")
                    audio_b64 = inline.get("data", "")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        logger.info(
                            f"TTS generated {len(audio_bytes)} bytes of audio "
                            f"for text: {text[:50]!r}"
                        )
                        return audio_bytes
            raise RuntimeError("TTS response had no audio data")
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected TTS response shape: {data}") from e

    async def close(self) -> None:
        await self._client.aclose()
