"""DecomposerAgent — auto-splits complex tasks into subtasks.

Takes a high-complexity task and breaks it into actionable subtasks with
estimated durations. This bridges planning and execution.

Only activates for tasks with complexity >= 70 (the ones that benefit
most from decomposition).
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from ..contracts.messages import Task, utcnow
from ..gemini.client import GeminiClient


class SubTask(BaseModel):
    """A decomposed sub-task with estimated duration."""
    title: str
    estimated_minutes: int = Field(ge=5, le=240)
    order: int = 0


class DecompositionResult(BaseModel):
    """Result of decomposing a task into subtasks."""
    task_id: str
    task_title: str
    subtasks: list[SubTask]
    total_estimated_minutes: int
    reasoning: str = ""
    timestamp: str = Field(default_factory=lambda: utcnow().isoformat())


SYSTEM_PROMPT = """You are the DecomposerAgent in a personal scheduling system.

Given a complex task, break it down into 2-5 actionable subtasks. Each subtask must have:
- A concrete title (imperative, <= 8 words)
- A realistic time estimate (15-120 minutes)
- Sequential order (1, 2, 3, ...)

Rules:
- Only decompose if the task genuinely needs breaking down.
- Be realistic about time — don't underestimate.
- Consider the task's category and complexity when estimating.
- If the task involves writing/research, allocate more time for those phases.
- If the task involves coding/implementation, allocate time for testing.

Example decomposition:
Task: "Write Report" (complexity 85, 4hr estimated)
Output:
  1. Research — 40 min
  2. Outline — 20 min
  3. Draft — 120 min
  4. Review — 35 min
  Total: 215 min

Return STRICT JSON. Schema:
{
  "subtasks": [
    {"title": "Research sources", "estimated_minutes": 40, "order": 1},
    {"title": "Write outline", "estimated_minutes": 20, "order": 2},
    ...
  ],
  "reasoning": "One sentence explaining the breakdown."
}
"""


class DecomposerAgent:
    """Breaks complex tasks into actionable subtasks."""

    name = "decomposer"
    proposal_type = "decomposition"

    def __init__(self, client: GeminiClient):
        self._client = client

    async def decompose(self, task: Task) -> DecompositionResult:
        """Decompose a task into subtasks.

        Returns an empty subtask list if the task is simple (complexity < 70).
        """
        # Only decompose complex tasks
        if task.complexity < 70:
            logger.info(f"DecomposerAgent: skipping '{task.title}' (complexity {task.complexity} < 70)")
            return DecompositionResult(
                task_id=task.id,
                task_title=task.title,
                subtasks=[],
                total_estimated_minutes=task.duration_minutes,
                reasoning="Task is simple enough — no decomposition needed.",
            )

        user_prompt = (
            f"Task: {task.title}\n"
            f"Category: {task.category}\n"
            f"Complexity: {task.complexity}/100\n"
            f"Duration estimate: {task.duration_minutes} minutes\n"
            f"Deadline: {task.deadline.isoformat() if task.deadline else 'None'}\n\n"
            f"Break this into 2-5 subtasks with realistic time estimates."
        )

        logger.info(f"DecomposerAgent decomposing: '{task.title}' (complexity {task.complexity})")
        data = await self._client.generate_json_with_fallback(
            agent_name="classifier",  # use classifier's fallback chain (light models)
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            max_output_tokens=400,
            operation_name=f"decomposing '{task.title}'",
        )

        subtasks: list[SubTask] = []
        for item in data.get("subtasks", []):
            try:
                subtasks.append(SubTask(
                    title=item.get("title", "Untitled step"),
                    estimated_minutes=max(5, min(240, item.get("estimated_minutes", 30))),
                    order=item.get("order", len(subtasks) + 1),
                ))
            except Exception as e:
                logger.warning(f"Failed to parse subtask: {item} — {e}")

        subtasks.sort(key=lambda s: s.order)
        total = sum(s.estimated_minutes for s in subtasks)

        logger.info(f"DecomposerAgent: '{task.title}' → {len(subtasks)} subtasks, {total}min total")

        return DecompositionResult(
            task_id=task.id,
            task_title=task.title,
            subtasks=subtasks,
            total_estimated_minutes=total,
            reasoning=data.get("reasoning", ""),
        )
