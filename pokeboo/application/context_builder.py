"""ContextBuilder — assembles 7-layer AgentContext with TTL caching.

Layers:
1. system_context    — time, mode, config
2. task_context      — active tasks, counts, status breakdown
3. calendar_context  — upcoming events, conflicts
4. memory_context    — retrieved memories (semantic search)
5. mood_context      — current mood + recent history
6. energy_context    — current energy + recent history
7. user_preferences  — learned preferences from DB
+ conflict_context   — recent agent/PokeBo conflicts

Caching strategy:
- Each layer cached independently with its own TTL
- Invalidated on relevant events (e.g., task_context invalidated on TaskCreated)
- Memory layer is expensive (embedding search) — longer TTL
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from ..config import Settings
from ..contracts.messages import AgentContext, utcnow
from ..core.storage import Repository


# TTL per layer (seconds)
LAYER_TTL = {
    "system_context": 300,      # 5 min (rarely changes)
    "task_context": 30,         # 30 sec (invalidated on task events)
    "calendar_context": 60,     # 1 min
    "memory_context": 120,      # 2 min (expensive to rebuild)
    "mood_context": 15,         # 15 sec (changes with events)
    "energy_context": 15,       # 15 sec
    "user_preferences": 600,    # 10 min (very stable)
    "conflict_context": 30,     # 30 sec
}


class ContextBuilder:
    """Builds layered AgentContext with per-layer TTL caching.

    Usage:
        builder = ContextBuilder(settings, repo)
        context = await builder.build()  # returns AgentContext
        # ... pass context to agents

    Invalidate on events:
        builder.invalidate("task_context")  # after TaskCreated
    """

    def __init__(self, settings: Settings, repo: Repository, embeddings_client=None):
        self._s = settings
        self._repo = repo
        self._embeddings = embeddings_client
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._invalidated: set[str] = set()

    def invalidate(self, layer: str | None = None) -> None:
        """Invalidate a specific layer, or all layers if None."""
        if layer is None:
            self._cache.clear()
            logger.debug("ContextBuilder: all layers invalidated")
        elif layer in self._cache:
            del self._cache[layer]
            logger.debug(f"ContextBuilder: layer '{layer}' invalidated")

    async def build(
        self,
        query: str | None = None,
        memory_top_k: int = 10,
    ) -> AgentContext:
        """Build the full AgentContext.

        Args:
            query: optional query for semantic memory retrieval
            memory_top_k: number of memories to retrieve
        """
        layers = await self._gather_layers(query, memory_top_k)
        return AgentContext(**layers)

    async def _gather_layers(
        self, query: str | None, memory_top_k: int
    ) -> dict[str, dict[str, Any]]:
        """Gather all 8 context layers, using cache where valid."""
        return {
            "system_context": self._get_cached("system_context", self._build_system),
            "task_context": self._get_cached("task_context", self._build_task),
            "calendar_context": self._get_cached("calendar_context", self._build_calendar),
            "memory_context": await self._get_cached_async(
                "memory_context",
                lambda: self._build_memory(query, memory_top_k),
            ),
            "mood_context": self._get_cached("mood_context", self._build_mood),
            "energy_context": self._get_cached("energy_context", self._build_energy),
            "user_preferences": self._get_cached("user_preferences", self._build_preferences),
            "conflict_context": self._get_cached("conflict_context", self._build_conflicts),
        }

    def _get_cached(self, layer: str, builder_fn) -> dict[str, Any]:
        """Get a layer from cache or rebuild it."""
        now = time.time()
        if layer in self._cache:
            cached_at, data = self._cache[layer]
            if now - cached_at < LAYER_TTL.get(layer, 60):
                return data
        # Rebuild
        data = builder_fn()
        self._cache[layer] = (now, data)
        return data

    async def _get_cached_async(self, layer: str, builder_fn) -> dict[str, Any]:
        """Async version of _get_cached for layers that need async builders."""
        now = time.time()
        if layer in self._cache:
            cached_at, data = self._cache[layer]
            if now - cached_at < LAYER_TTL.get(layer, 60):
                return data
        data = await builder_fn()
        self._cache[layer] = (now, data)
        return data

    # --- Layer builders ---

    def _build_system(self) -> dict[str, Any]:
        """System context: time, mode, config."""
        now = utcnow()
        return {
            "current_time": now.isoformat(),
            "day_of_week": now.strftime("%A"),
            "mode": self._s.mode,
            "workday_start": self._s.workday_start,
            "workday_end": self._s.workday_end,
            "buffer_minutes": self._s.buffer_minutes,
        }

    def _build_task(self) -> dict[str, Any]:
        """Task context: active tasks, counts by status."""
        tasks = self._repo.list_tasks(limit=50)
        by_status: dict[str, int] = {}
        for t in tasks:
            s = t.status.value
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "total": len(tasks),
            "by_status": by_status,
            "active_count": by_status.get("scheduled", 0) + by_status.get("in_progress", 0),
            "pending_count": by_status.get("pending", 0),
            "recent_titles": [t.title for t in tasks[:5]],
        }

    def _build_calendar(self) -> dict[str, Any]:
        """Calendar context: upcoming events (from Google Calendar if available)."""
        # For now, derive from scheduled tasks
        tasks = self._repo.list_schedulable_tasks()
        upcoming = []
        for t in tasks[:10]:
            if t.scheduled_start:
                upcoming.append({
                    "title": t.title,
                    "start": t.scheduled_start.isoformat(),
                    "end": t.scheduled_end.isoformat() if t.scheduled_end else None,
                    "category": t.category,
                })
        return {
            "upcoming_events": upcoming,
            "next_event": upcoming[0] if upcoming else None,
            "conflict_count": 0,  # would be computed from actual Calendar API
        }

    async def _build_memory(self, query: str | None, top_k: int) -> dict[str, Any]:
        """Memory context: semantic search over stored memories."""
        memories = self._repo.list_memories(limit=top_k * 2)
        if not memories:
            return {"memories": [], "query": query, "hit_count": 0}

        # If we have a query and embeddings client, do semantic search
        if query and self._embeddings:
            try:
                query_vec = await self._embeddings.embed(query)
                scored = []
                for m in memories:
                    if m.get("embedding"):
                        sim = self._embeddings.cosine_similarity(query_vec, m["embedding"])
                        scored.append((sim, m))
                scored.sort(key=lambda x: x[0], reverse=True)
                top = [m for _, m in scored[:top_k]]
                # Update access counts
                for m in top:
                    self._repo.increment_memory_access(m["id"])
                return {
                    "memories": [{"content": m["content"], "type": m["memory_type"],
                                  "importance": m["importance"]} for m in top],
                    "query": query,
                    "hit_count": len(top),
                }
            except Exception as e:
                logger.warning(f"Memory semantic search failed: {e}")

        # Fallback: return recent memories sorted by importance
        sorted_memories = sorted(memories, key=lambda m: m.get("importance", 50), reverse=True)
        top = sorted_memories[:top_k]
        return {
            "memories": [{"content": m["content"], "type": m["memory_type"],
                          "importance": m["importance"]} for m in top],
            "query": query,
            "hit_count": len(top),
        }

    def _build_mood(self) -> dict[str, Any]:
        """Mood context: current mood + recent history."""
        latest = self._repo.latest_mood()
        history = self._repo.mood_history(limit=5)
        if latest:
            score, label, reasons = latest
        else:
            score, label, reasons = 75, "watchful", ["no data"]
        return {
            "current_score": score,
            "current_label": label,
            "reasons": reasons,
            "trend": [h["score"] for h in history[:5]],
        }

    def _build_energy(self) -> dict[str, Any]:
        """Energy context: current energy + recent history."""
        latest = self._repo.latest_energy()
        history = self._repo.energy_history(limit=5)
        if latest:
            return {
                "current_score": latest.score,
                "current_label": latest.label.value,
                "recommendations": latest.recommendations,
                "trend": [h["score"] for h in history[:5]],
            }
        return {
            "current_score": 80,
            "current_label": "full",
            "recommendations": [],
            "trend": [],
        }

    def _build_preferences(self) -> dict[str, Any]:
        """User preferences from DB."""
        prefs = self._repo.list_preferences()
        return {
            "preferences": {p["key"]: p["value"] for p in prefs},
            "category_count": len(set(p["category"] for p in prefs)),
        }

    def _build_conflicts(self) -> dict[str, Any]:
        """Recent conflicts (PokeBo vs agents)."""
        conflicts = self._repo.list_conflicts(limit=5)
        return {
            "recent_count": len(conflicts),
            "recent": [
                {
                    "agent": c["agent_name"],
                    "resolution": c["resolution"],
                    "outcome": c["final_outcome"],
                }
                for c in conflicts
            ],
        }
