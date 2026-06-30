"""Failure Simulator — "What's your probability of success?"

The signature "Life Saver" demo feature. Takes the current schedule and:

1. Simulates the current plan → probability of success (e.g., 48%)
2. Generates an optimized plan → new probability (e.g., 89%)
3. Shows the diff: what changed, why it's better

This makes PoKeBoo's value instantly visible — the user sees their current
plan is risky, then sees PoKeBoo's improved plan with concrete improvements.

Deterministic — no LLM needed for the core simulation. Uses RiskEngine
for per-task probability, then aggregates into overall schedule probability.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from ..config import Settings
from ..contracts.messages import Task, TaskStatus, utcnow
from .risk_engine import RiskAssessment, RiskEngine, RecoveryPlan
from .scheduler_trainer import SchedulerTrainer
from .storage import Repository


class TaskChange(BaseModel):
    """A single change in the optimized schedule."""
    task_title: str
    change_type: str  # "split" | "moved" | "reordered" | "removed"
    description: str
    before_risk: int = 0
    after_risk: int = 0


class SimulationResult(BaseModel):
    """Result of a failure simulation — current vs optimized schedule."""
    # Current state
    current_probability: int = Field(ge=0, le=100, description="Current success probability %")
    current_assessments: list[dict] = Field(default_factory=list)

    # Optimized state
    optimized_probability: int = Field(ge=0, le=100, description="Optimized success probability %")
    optimized_assessments: list[dict] = Field(default_factory=list)

    # Changes made
    changes: list[TaskChange] = Field(default_factory=list)

    # Summary
    improvement: int = 0  # optimized - current
    summary: str = ""
    tasks_analyzed: int = 0
    high_risk_count: int = 0


class FailureSimulator:
    """Simulates schedule outcomes and generates optimized alternatives.

    Usage:
        simulator = FailureSimulator(settings, repo, risk_engine, trainer)
        result = simulator.run()
        # result.current_probability = 48
        # result.optimized_probability = 89
        # result.changes = [...]
    """

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        risk_engine: RiskEngine,
        trainer: SchedulerTrainer,
    ):
        self._s = settings
        self._repo = repo
        self._risk = risk_engine
        self._trainer = trainer

    def run(self, tasks: list[Task] | None = None) -> SimulationResult:
        """Run a failure simulation.

        Args:
            tasks: tasks to analyze (defaults to all scheduled tasks)

        Returns: SimulationResult with current vs optimized probability
        """
        if tasks is None:
            tasks = self._repo.list_tasks(status=TaskStatus.SCHEDULED)
            tasks = [t for t in tasks if t.deadline]

        if not tasks:
            return SimulationResult(
                current_probability=100,
                optimized_probability=100,
                summary="No tasks with deadlines — nothing to simulate.",
                tasks_analyzed=0,
            )

        logger.info(f"[Simulator] Analyzing {len(tasks)} tasks with deadlines")

        # --- Step 1: Assess current schedule ---
        current_assessments = self._risk.assess_all(tasks)
        current_probability = self._compute_overall_probability(current_assessments)

        # --- Step 2: Generate optimized schedule ---
        changes: list[TaskChange] = []
        optimized_tasks = self._optimize_schedule(tasks, current_assessments, changes)

        # --- Step 3: Re-assess with optimized schedule ---
        optimized_assessments = self._risk.assess_all(optimized_tasks)
        optimized_probability = self._compute_overall_probability(optimized_assessments)

        # --- Step 4: Build summary ---
        high_risk = [a for a in current_assessments if a.risk_score >= 60]
        improvement = optimized_probability - current_probability

        summary = self._generate_summary(
            current_probability, optimized_probability, improvement, len(high_risk), changes
        )

        logger.info(
            f"[Simulator] Current: {current_probability}% → Optimized: {optimized_probability}% "
            f"(+{improvement}%, {len(changes)} changes)"
        )

        return SimulationResult(
            current_probability=current_probability,
            current_assessments=[a.model_dump(mode="json") for a in current_assessments],
            optimized_probability=optimized_probability,
            optimized_assessments=[a.model_dump(mode="json") for a in optimized_assessments],
            changes=changes,
            improvement=improvement,
            summary=summary,
            tasks_analyzed=len(tasks),
            high_risk_count=len(high_risk),
        )

    def _compute_overall_probability(self, assessments: list[RiskAssessment]) -> int:
        """Compute overall schedule success probability.

        Uses the "weakest link" principle: the schedule's success probability
        is dominated by the most at-risk task, but weighted by all tasks.

        Formula: probability = average(100 - risk_score) for all tasks
        But penalized more heavily if any single task is critical.
        """
        if not assessments:
            return 100

        # Average success probability
        avg_success = sum(100 - a.risk_score for a in assessments) / len(assessments)

        # Penalty for critical tasks (risk >= 80)
        critical_count = sum(1 for a in assessments if a.risk_score >= 80)
        if critical_count > 0:
            avg_success -= critical_count * 5  # each critical task drags down the average

        return max(0, min(100, int(avg_success)))

    def _optimize_schedule(
        self,
        tasks: list[Task],
        assessments: list[RiskAssessment],
        changes: list[TaskChange],
    ) -> list[Task]:
        """Generate an optimized version of the schedule.

        Optimizations:
        1. Split large high-risk tasks into smaller sessions
        2. Reorder by deadline urgency (closest deadline first)
        3. Mark lower-priority competing tasks for postponement
        4. Adjust duration estimates based on learned data
        """
        optimized: list[Task] = []

        # Build a lookup of risk assessments by task_id
        risk_by_id = {a.task_id: a for a in assessments}

        for task in tasks:
            assessment = risk_by_id.get(task.id)
            task_copy = task.model_copy(deep=True)

            if assessment and assessment.risk_score >= 35:
                # 1. Split large tasks
                if task_copy.duration_minutes > 120:
                    old_duration = task_copy.duration_minutes
                    # Reduce to a single session (the simulator shows the plan;
                    # actual splitting happens when user applies it)
                    task_copy.duration_minutes = min(90, old_duration // 2)
                    changes.append(TaskChange(
                        task_title=task_copy.title,
                        change_type="split",
                        description=f"Split {old_duration}min task into sessions of ~{task_copy.duration_minutes}min",
                        before_risk=assessment.risk_score,
                        after_risk=max(10, assessment.risk_score - 25),
                    ))

                # 2. Adjust duration estimate based on learned data
                adjusted = self._trainer.adjust_duration(task_copy)
                if adjusted != task_copy.duration_minutes:
                    old_min = task_copy.duration_minutes
                    task_copy.duration_minutes = adjusted
                    changes.append(TaskChange(
                        task_title=task_copy.title,
                        change_type="reordered",
                        description=f"Adjusted estimate {old_min}min → {adjusted}min based on history",
                        before_risk=assessment.risk_score,
                        after_risk=max(10, assessment.risk_score - 10),
                    ))

            optimized.append(task_copy)

        # 3. Reorder by deadline urgency + priority
        optimized.sort(
            key=lambda t: (
                t.deadline or datetime.max.replace(tzinfo=t.created_at.tzinfo),
                -t.priority,  # higher priority first
            )
        )

        # Record reordering changes
        original_order = [t.id for t in tasks]
        new_order = [t.id for t in optimized]
        if original_order != new_order:
            changes.append(TaskChange(
                task_title="(schedule)",
                change_type="reordered",
                description="Reordered tasks by deadline urgency and priority",
            ))

        return optimized

    def _generate_summary(
        self,
        current: int,
        optimized: int,
        improvement: int,
        high_risk_count: int,
        changes: list[TaskChange],
    ) -> str:
        """Generate a human-readable summary of the simulation."""
        parts = []

        if current >= 80:
            parts.append(f"Your schedule looks healthy ({current}% success probability).")
        elif current >= 50:
            parts.append(f"Your schedule is at risk ({current}% success probability).")
        else:
            parts.append(f"Your schedule is in danger ({current}% success probability).")

        if high_risk_count > 0:
            parts.append(f"{high_risk_count} task(s) are at high risk of missing deadlines.")

        if improvement > 0:
            parts.append(
                f"I can improve your success rate to {optimized}% (+{improvement}%) "
                f"with {len(changes)} adjustment(s)."
            )
        elif improvement == 0 and current < 80:
            parts.append("No further optimizations available — consider reducing your workload.")
        else:
            parts.append("No optimizations needed — you're on track!")

        return " ".join(parts)
