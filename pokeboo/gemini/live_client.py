"""Gemini Live API client — real-time streaming Speech-to-Text.

Uses `gemini-2.5-flash-native-audio-dialog` (unlimited RPM, 1M TPM).
The Live API uses WebSocket (not REST) for bidirectional streaming.

This is a STUB implementation. The actual Live API requires:
1. WebSocket connection to `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent`
2. Setup message with model + config
3. Audio chunks streamed as `realtimeInput` messages
4. Transcription streamed back as `serverContent` messages

For now, the file-based `GeminiClient.transcribe_audio()` remains the
default. This Live client will be wired in when the Rust overlay ships
real-time mic capture.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Callable

from loguru import logger

from ..config import Settings


class LiveSTTClient:
    """Real-time streaming STT via Gemini Live API.

    Usage (when fully implemented):
        client = LiveSTTClient(settings)
        async with client.stream() as session:
            async for audio_chunk in mic_source:
                session.send_audio(audio_chunk)
            async for transcript in session.transcripts():
                print(transcript)
    """

    WS_URL = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY required for LiveSTTClient")
        self._api_key = settings.gemini_api_key
        self._model = settings.live_stt_model
        self._ws = None
        self._connected = False

    async def connect(self) -> None:
        """Open the WebSocket connection and send setup message."""
        try:
            import websockets
        except ImportError:
            raise RuntimeError(
                "websockets package not installed. Run: pip install websockets"
            )

        url = f"{self.WS_URL}?key={self._api_key}"
        self._ws = await websockets.connect(url)
        self._connected = True

        # Send setup message
        setup_msg = {
            "setup": {
                "model": f"models/{self._model}",
                "generation_config": {
                    "response_modalities": ["TEXT"],
                },
            }
        }
        await self._ws.send(json.dumps(setup_msg))
        # Wait for setup complete
        response = await self._ws.recv()
        logger.info(f"Live API connected: {response[:100]}")

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Send a chunk of audio (PCM 16-bit, 16kHz, mono)."""
        if not self._connected or not self._ws:
            raise RuntimeError("Not connected. Call connect() first.")
        import base64

        msg = {
            "realtimeInput": {
                "mediaChunks": [
                    {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(audio_chunk).decode("ascii"),
                    }
                ]
            }
        }
        await self._ws.send(json.dumps(msg))

    async def transcripts(self) -> AsyncIterator[str]:
        """Yield transcript chunks as they arrive from the model."""
        if not self._connected or not self._ws:
            raise RuntimeError("Not connected. Call connect() first.")
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                # Extract text from serverContent
                server_content = msg.get("serverContent", {})
                parts = server_content.get("modelTurn", {}).get("parts", [])
                for part in parts:
                    text = part.get("text", "")
                    if text:
                        yield text
                # Check for turn complete
                if server_content.get("turnComplete"):
                    break
        except Exception as e:
            logger.error(f"Live API stream error: {e}")
            raise

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
            self._connected = False
            logger.info("Live API connection closed")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()
