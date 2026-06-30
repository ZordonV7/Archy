"""Archy veto tests — relevancy scoring + conflict resolution.

These tests use a fake Gemini client (no network) to exercise Archy's
supervision loop deterministically.
"""
from __future__ import annotations

from typing import Any

import pytest

from archy.config import Settings
from archy.contracts.messages import (
    AgentProposal,
    ClassificationPayload,
    ArchyEvaluation,
    ProposalDecision,
    Task,
    TaskStatus,
    utcnow,
)
from archy.core.storage import Repository
from archy.assistant.manager import Archy


class FakeGeminiClient:
    """Returns canned responses so we can test Archy's logic without network."""

    def __init__(self):
        self.classifier_responses = [
            {
                "title": "File Q2 Taxes",
                "category": "finance",
                "priority": 95,
                "complexity": 30,
                "deadline_iso": None,
                "duration_minutes": 30,
                "reasoning": "Critical deadline.",
            }
        ]
        self.mood_responses = [
            {
                "score": 40,
                "label": "critical_panic",
                "causes": ["missed task"],
                "recommendations": ["reduce load"],
                "reasoning": "User stressed.",
            }
        ]
        self.planner_responses = [
            {
                "ordered_task_ids": [],
                "at_risk_task_ids": [],
                "suggestions": [],
                "reasoning": "Plan.",
            }
        ]
        self.interpreter_responses = ["Hey, I adjusted things for you."]
        self.call_count = {"classifier": 0, "mood": 0, "planner": 0, "interpreter": 0}

    async def generate_json(self, system_prompt, user_prompt, **kwargs):
        if "ClassifierAgent" in system_prompt or "Classifier" in system_prompt or "classifier" in system_prompt.lower() or "priority" in system_prompt.lower() and "complexity" in system_prompt.lower():
            idx = self.call_count["classifier"]
            self.call_count["classifier"] += 1
            return self.classifier_responses[min(idx, len(self.classifier_responses) - 1)]
        if "MoodAgent" in system_prompt or "mood" in system_prompt.lower() and "stress" in system_prompt.lower():
            idx = self.call_count["mood"]
            self.call_count["mood"] += 1
            return self.mood_responses[min(idx, len(self.mood_responses) - 1)]
        if "PlannerAgent" in system_prompt or "Planner" in system_prompt or "ordered_task_ids" in user_prompt:
            idx = self.call_count["planner"]
            self.call_count["planner"] += 1
            return self.planner_responses[min(idx, len(self.planner_responses) - 1)]
        # Default: interpreter response
        return {"text": "Hey there!"}

    async def generate_json_pro(self, system_prompt, user_prompt, **kwargs):
        idx = self.call_count["interpreter"]
        self.call_count["interpreter"] += 1
        return {"text": self.interpreter_responses[min(idx, len(self.interpreter_responses) - 1)]}

    async def transcribe_audio(self, audio_bytes, mime, **kwargs):
        return "fake transcript"

    async def close(self):
        pass


pytestmark = pytest.mark.asyncio


async def _seed_panic_mood(repo: Repository):
    """Pre-populate mood table with critical_panic so Archy's veto triggers."""
    repo.append_mood(40, "critical_panic", ["test setup"])


async def test_veto_triggers_during_critical_panic(tmp_settings: Settings, repo: Repository):
    """When user is in critical_panic, planner proposals with >2 slots get vetoed."""
    await _seed_panic_mood(repo)
    fake = FakeGeminiClient()
    assistant = Archy(tmp_settings, repo, fake)

    # Manually invoke the evaluation method on a mock proposal
    proposal = AgentProposal(
        proposal_id="test-1",
        agent_name="planner",
        proposal_type="schedule",
        payload={
            "slots": [
                {"task_id": "a", "start": "2026-01-01T09:00:00+00:00", "end": "2026-01-01T10:00:00+00:00"},
                {"task_id": "b", "start": "2026-01-01T10:05:00+00:00", "end": "2026-01-01T11:00:00+00:00"},
                {"task_id": "c", "start": "2026-01-01T11:05:00+00:00", "end": "2026-01-01T12:00:00+00:00"},
            ],
            "at_risk_task_ids": [],
            "suggestions": [],
            "reasoning": "",
        },
        self_confidence=80,
    )

    evaluation = await assistant._evaluate_proposal(proposal)

    assert evaluation.decision == ProposalDecision.REJECT
    assert evaluation.relevancy_score <= tmp_settings.relevancy_threshold
    assert "max_slots" in evaluation.constraints
    assert evaluation.constraints["max_slots"] == 2


async def test_accept_when_mood_is_stable(tmp_settings: Settings, repo: Repository):
    """When mood is good, normal planner proposals are accepted."""
    repo.append_mood(85, "deep_focus", ["baseline"])
    fake = FakeGeminiClient()
    assistant = Archy(tmp_settings, repo, fake)

    proposal = AgentProposal(
        proposal_id="test-2",
        agent_name="planner",
        proposal_type="schedule",
        payload={"slots": [], "at_risk_task_ids": [], "suggestions": [], "reasoning": ""},
        self_confidence=85,
    )
    evaluation = await assistant._evaluate_proposal(proposal)
    assert evaluation.decision == ProposalDecision.ACCEPT
    assert evaluation.relevancy_score > tmp_settings.relevancy_threshold


async def test_conflict_event_persisted_on_rejection(tmp_settings: Settings, repo: Repository):
    """When Archy vetoes, a ConflictEvent should be persisted."""
    await _seed_panic_mood(repo)
    fake = FakeGeminiClient()
    # Make planner keep proposing too many slots even after revision
    fake.planner_responses = [
        {
            "ordered_task_ids": [],
            "at_risk_task_ids": [],
            "suggestions": [],
            "reasoning": "v1",
        }
    ] * 5  # enough for multiple rounds
    assistant = Archy(tmp_settings, repo, fake)

    # Directly call _run_with_supervision with a planner-like agent
    # We'll use the real PlannerAgent — but its actual output doesn't matter
    # because we test the *evaluation* path.

    # Inject a fake agent that returns a proposal with many slots
    class FakePlanner:
        name = "planner"
        proposal_type = "schedule"

        async def run(self, **kwargs):
            return AgentProposal(
                proposal_id="fake-1",
                agent_name="planner",
                proposal_type="schedule",
                payload={
                    "slots": [{"task_id": f"t{i}", "start": "2026-01-01T09:00:00+00:00", "end": "2026-01-01T10:00:00+00:00"} for i in range(5)],
                    "at_risk_task_ids": [],
                    "suggestions": [],
                    "reasoning": "v1",
                },
                self_confidence=80,
            )

        async def revise(self, evaluation, **kwargs):
            return await self.run(**kwargs)

    result = await assistant._run_with_supervision(FakePlanner())
    # After max rounds, should have been vetoed at least once and a conflict logged
    conflicts = repo.list_conflicts(limit=10)
    assert len(conflicts) > 0
    assert any(c["agent_name"] == "planner" for c in conflicts)
