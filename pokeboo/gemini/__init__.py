"""Gemini client subpackage."""
from .client import GeminiClient, get_gemini_client
from .embeddings import EmbeddingsClient, get_embeddings_client
from .fallback import confirm_task_completion, run_with_fallback
from .live_client import LiveSTTClient
from .offline_client import OfflineEmbeddingsClient, OfflineGeminiClient
from .router import ModelRouter
from .tts_client import POKEBO_VOICE_STYLE, TTSClient

__all__ = [
    "EmbeddingsClient",
    "GeminiClient",
    "LiveSTTClient",
    "ModelRouter",
    "OfflineEmbeddingsClient",
    "OfflineGeminiClient",
    "POKEBO_VOICE_STYLE",
    "TTSClient",
    "confirm_task_completion",
    "get_embeddings_client",
    "get_gemini_client",
    "run_with_fallback",
]
