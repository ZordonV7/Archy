"""Embeddings client — for semantic task search and similarity.

Uses `gemini-embedding-001` (1500 RPD, 100 RPM).
Generates vector embeddings of task titles/descriptions so we can:
- Find similar past tasks ("have I done this before?")
- Cluster tasks by topic
- Build a semantic search index over the task history

When `ARCHY_MODE=offline`, `get_embeddings_client()` returns an
`OfflineEmbeddingsClient` that produces deterministic hash-based pseudo-vectors.
These are NOT real semantic embeddings — see the docstring on
`OfflineEmbeddingsClient` for caveats.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from ..config import Settings

if TYPE_CHECKING:
    from .offline_client import OfflineEmbeddingsClient


class EmbeddingsClient:
    """Generates and stores embeddings for semantic search."""

    URL_TMPL = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
    )

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY required for EmbeddingsClient")
        self._api_key = settings.gemini_api_key
        self._model = settings.embedding_model
        self._client = httpx.AsyncClient(timeout=30.0)

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        url = self.URL_TMPL.format(model=self._model)
        params = {"key": self._api_key}
        payload = {
            "model": f"models/{self._model}",
            "content": {"parts": [{"text": text}]},
        }
        resp = await self._client.post(url, params=params, json=payload)
        if resp.status_code >= 400:
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            raise RuntimeError(f"Embeddings API error {resp.status_code}: {err_msg}")
        data = resp.json()
        return data.get("embedding", {}).get("values", [])

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts (sequential — API doesn't batch)."""
        results = []
        for text in texts:
            try:
                vec = await self.embed(text)
                results.append(vec)
            except Exception as e:
                logger.error(f"Failed to embed '{text[:40]}...': {e}")
                results.append([])
        return results

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two embedding vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def close(self) -> None:
        await self._client.aclose()


# --- Factory ---

_embeddings_client: EmbeddingsClient | OfflineEmbeddingsClient | None = None


def get_embeddings_client(
    settings: Settings | None = None,
) -> EmbeddingsClient | OfflineEmbeddingsClient:
    """Get the shared embeddings client instance.

    Dispatches on `settings.mode`:
    - `"online"`: real `EmbeddingsClient` hitting the Gemini embeddings API.
    - `"offline"`: `OfflineEmbeddingsClient` returning hash-based pseudo-vectors.

    Note: `OfflineEmbeddingsClient` vectors are NOT semantically meaningful.
    Two texts with similar meaning will NOT have similar vectors. Good enough
    to keep `/memories` endpoints from crashing; not for real semantic search.
    """
    global _embeddings_client
    if _embeddings_client is not None:
        return _embeddings_client
    from ..config import get_settings

    s = settings or get_settings()

    if s.mode == "offline":
        from .offline_client import OfflineEmbeddingsClient
        _embeddings_client = OfflineEmbeddingsClient(s)
        return _embeddings_client

    _embeddings_client = EmbeddingsClient(s)
    return _embeddings_client
