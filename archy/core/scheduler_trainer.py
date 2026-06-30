"""Scheduler Trainer — learns from task completion patterns.

The scheduler becomes more accurate over time by tracking:
1. **Duration prediction error** — predicted vs actual completion time
2. **Preferred focus windows** — when the user is most productive (by hour of day)
3. **Meeting fatigue** — how much energy drains per meeting type/category
4. **Context switching penalties** — empirical cost of switching between categories
5. **Postponement patterns** — which task categories get postponed most often
6. **Completion likelihood** — P(task completed | category, time_of_day, complexity)

Training data is stored in SQLite (`scheduler_learning` table) and
parameters are recomputed nightly (or on-demand via `retrain()`).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from ..contracts.messages import Task, TaskStatus, utcnow
from .storage import Repository


class SchedulerTrainer:
    """Learns scheduling parameters from historical task data.

    Usage:
        trainer = SchedulerTrainer(repo)
        trainer.record_completion(task, predicted_minutes, actual_minutes)
        trainer.record_postponement(task)
        params = trainer.get_learned_params()
        # params.duration_adjustments["finance"] = 1.15  # finance tasks take 15% longer
        # params.focus_windows[14] = 0.85  # 2pm is a high-productivity slot
    """

    def __init__(self, repo: Repository):
        self._repo = repo
        self._cache: dict[str, Any] | None = None

    # --- Recording events ---

    def record_completion(
        self,
        task: Task,
        predicted_minutes: int,
        actual_minutes: int,
    ) -> None:
        """Record that a task was completed — feeds duration learning."""
        entry = {
            "task_id": task.id,
            "category": task.category,
            "complexity": task.complexity,
            "priority": task.priority,
            "predicted_minutes": predicted_minutes,
            "actual_minutes": actual_minutes,
            "duration_error": actual_minutes - predicted_minutes,
            "duration_ratio": actual_minutes / max(predicted_minutes, 1),
            "completed_at_hour": task.completed_at.hour if task.completed_at else None,
            "completed_at_weekday": task.completed_at.weekday() if task.completed_at else None,
            "timestamp": utcnow().isoformat(),
        }
        self._repo.append_scheduler_learning("completion", entry)
        logger.info(
            f"[Trainer] Recorded completion: {task.title} predicted={predicted_minutes}min "
            f"actual={actual_minutes}min (ratio={entry['duration_ratio']:.2f})"
        )
        # Invalidate cache so next read pulls fresh data
        self._cache = None

    def record_postponement(self, task: Task, reason: str = "") -> None:
        """Record that a task was postponed/cancelled."""
        entry = {
            "task_id": task.id,
            "category": task.category,
            "complexity": task.complexity,
            "priority": task.priority,
            "reason": reason,
            "timestamp": utcnow().isoformat(),
        }
        self._repo.append_scheduler_learning("postponement", entry)
        logger.info(f"[Trainer] Recorded postponement: {task.title} ({reason})")
        self._cache = None

    # --- Computing learned parameters ---

    def retrain(self) -> dict[str, Any]:
        """Recompute all learned parameters from historical data.

        Returns the full parameter dict.
        """
        records = self._repo.list_scheduler_learning(limit=10000)

        params = {
            "duration_adjustments": self._compute_duration_adjustments(records),
            "focus_windows": self._compute_focus_windows(records),
            "category_fatigue": self._compute_category_fatigue(records),
            "context_switch_costs": self._compute_context_switch_costs(records),
            "postponement_rates": self._compute_postponement_rates(records),
            "completion_likelihood": self._compute_completion_likelihood(records),
            "sample_counts": {
                "completions": sum(1 for r in records if r["record_type"] == "completion"),
                "postponements": sum(1 for r in records if r["record_type"] == "postponement"),
                "context_switches": sum(1 for r in records if r["record_type"] == "context_switch"),
            },
            "trained_at": utcnow().isoformat(),
        }

        self._repo.set_preference(
            "scheduler_learned_params",
            json.dumps(params),
            category="scheduler",
        )
        self._cache = params
        logger.info(
            f"[Trainer] Retrained with {params['sample_counts']['completions']} completions, "
            f"{params['sample_counts']['postponements']} postponements"
        )
        return params

    def get_learned_params(self) -> dict[str, Any]:
        """Get current learned parameters (cached)."""
        if self._cache is not None:
            return self._cache
        raw = self._repo.get_preference("scheduler_learned_params")
        if raw:
            try:
                self._cache = json.loads(raw)
                return self._cache
            except json.JSONDecodeError:
                pass
        # Return defaults if no training data yet
        return self._default_params()

    def get_bias_summary(self) -> list[dict]:
        """Return human-readable bias summary for each category.

        Example:
        [
          {"category": "coding", "bias": "underestimate", "percentage": 40, "multiplier": 1.4},
          {"category": "email", "bias": "overestimate", "percentage": 50, "multiplier": 0.5},
          {"category": "finance", "bias": "accurate", "percentage": 5, "multiplier": 1.05}
        ]
        """
        params = self.get_learned_params()
        adjustments = params.get("duration_adjustments", {})
        results = []
        for cat, mult in adjustments.items():
            if mult > 1.15:
                pct = int((mult - 1) * 100)
                results.append({
                    "category": cat,
                    "bias": "underestimate",
                    "percentage": pct,
                    "multiplier": round(mult, 2),
                    "description": f"You underestimate {cat} tasks by {pct}%."
                })
            elif mult < 0.85:
                pct = int((1 - mult) * 100)
                results.append({
                    "category": cat,
                    "bias": "overestimate",
                    "percentage": pct,
                    "multiplier": round(mult, 2),
                    "description": f"You overestimate {cat} tasks by {pct}%."
                })
            else:
                pct = int(abs(1 - mult) * 100)
                results.append({
                    "category": cat,
                    "bias": "accurate",
                    "percentage": pct,
                    "multiplier": round(mult, 2),
                    "description": f"Your {cat} estimates are fairly accurate ({pct}% off)."
                })
        # Sort by bias severity (underestimate first)
        results.sort(key=lambda x: x["multiplier"], reverse=True)
        return results

    def _default_params(self) -> dict[str, Any]:
        return {
            "duration_adjustments": {},  # category → multiplier (1.0 = no adjustment)
            "focus_windows": {},         # hour_of_day → productivity score (0-1)
            "category_fatigue": {},      # category → energy cost per task
            "context_switch_costs": {},  # "from->to" → cost (0-1)
            "postponement_rates": {},    # category → postponement rate (0-1)
            "completion_likelihood": {}, # "category:complexity_bucket" → completion rate
            "sample_counts": {"completions": 0, "postponements": 0, "context_switches": 0},
            "trained_at": None,
        }

    # --- Parameter computation methods ---

    def _compute_duration_adjustments(self, records: list[dict]) -> dict[str, float]:
        """For each category, compute average (actual / predicted) duration ratio."""
        by_category: dict[str, list[float]] = defaultdict(list)
        for r in records:
            if r["record_type"] == "completion":
                data = r["data"]
                if "duration_ratio" in data:
                    by_category[data["category"]].append(data["duration_ratio"])
        return {
            cat: sum(ratios) / len(ratios)
            for cat, ratios in by_category.items()
            if ratios
        }

    def _compute_focus_windows(self, records: list[dict]) -> dict[int, float]:
        """For each hour of day, compute completion rate as productivity score."""
        by_hour: dict[int, list[int]] = defaultdict(list)  # hour → [1 if completed, 0 if postponed]
        for r in records:
            data = r["data"]
            if r["record_type"] == "completion" and data.get("completed_at_hour") is not None:
                by_hour[data["completed_at_hour"]].append(1)
            elif r["record_type"] == "postponement":
                # Approximate: postponements happen at creation hour
                hour = datetime.fromisoformat(data["timestamp"]).hour
                by_hour[hour].append(0)
        return {
            hour: sum(outcomes) / len(outcomes)
            for hour, outcomes in by_hour.items()
            if outcomes
        }

    def _compute_category_fatigue(self, records: list[dict]) -> dict[str, float]:
        """Average energy drop per task in each category."""
        # This would need energy snapshots before/after — for now, use complexity as proxy
        by_category: dict[str, list[float]] = defaultdict(list)
        for r in records:
            if r["record_type"] == "completion":
                data = r["data"]
                # Energy cost ≈ complexity / 100 * 30 (rough estimate)
                cost = (data.get("complexity", 50) / 100.0) * 30.0
                by_category[data["category"]].append(cost)
        return {
            cat: sum(costs) / len(costs)
            for cat, costs in by_category.items()
            if costs
        }

    def _compute_context_switch_costs(self, records: list[dict]) -> dict[str, float]:
        """Average energy drop when switching from one category to another."""
        by_pair: dict[str, list[float]] = defaultdict(list)
        for r in records:
            if r["record_type"] == "context_switch":
                data = r["data"]
                key = f"{data['from_category']}->{data['to_category']}"
                by_pair[key].append(data["energy_drop"])
        return {
            pair: sum(drops) / len(drops)
            for pair, drops in by_pair.items()
            if drops
        }

    def _compute_postponement_rates(self, records: list[dict]) -> dict[str, float]:
        """For each category, P(postponed | task in category)."""
        completions = defaultdict(int)
        postponements = defaultdict(int)
        for r in records:
            data = r["data"]
            if r["record_type"] == "completion":
                completions[data["category"]] += 1
            elif r["record_type"] == "postponement":
                postponements[data["category"]] += 1
        rates = {}
        all_cats = set(completions.keys()) | set(postponements.keys())
        for cat in all_cats:
            total = completions[cat] + postponements[cat]
            if total > 0:
                rates[cat] = postponements[cat] / total
        return rates

    def _compute_completion_likelihood(self, records: list[dict]) -> dict[str, float]:
        """For each (category, complexity_bucket), P(completed)."""
        buckets = defaultdict(lambda: {"completed": 0, "total": 0})
        for r in records:
            data = r["data"]
            complexity = data.get("complexity", 50)
            bucket = f"{complexity // 25 * 25}-{complexity // 25 * 25 + 24}"
            key = f"{data['category']}:{bucket}"
            buckets[key]["total"] += 1
            if r["record_type"] == "completion":
                buckets[key]["completed"] += 1
        return {
            key: b["completed"] / b["total"]
            for key, b in buckets.items()
            if b["total"] > 0
        }

    # --- Applying learned parameters ---

    def adjust_duration(self, task: Task) -> int:
        """Return adjusted duration estimate for a task based on learned data."""
        params = self.get_learned_params()
        multiplier = params["duration_adjustments"].get(task.category, 1.0)
        adjusted = int(task.duration_minutes * multiplier)
        logger.debug(
            f"[Trainer] Duration for '{task.title}': {task.duration_minutes}min × {multiplier:.2f} = {adjusted}min"
        )
        return max(5, adjusted)

