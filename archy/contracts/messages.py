"""Inter-agent message schemas — the communication protocol.

CRITICAL INVARIANT: agents NEVER exchange natural language with each other.
They emit structured pydantic objects via `AgentProposal`. Only Archy
produces user-facing natural language, via `UserFacingMessage`.

Why this matters:
- Token-efficient (small JSON vs verbose prose)
- Persona lives in ONE place (Archy)
- Each agent prompt stays focused and small
- Proposals are cacheable, loggable, and vetoable
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Domain models
# --------------------------------------------------------------------------- #


class TaskStatus(StrEnum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    MISSED = "missed"
    CANCELLED = "cancelled"


class PriorityLabel(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Task(BaseModel):
    id: str
    raw_transcript: str
    title: str
    category: str = "general"
    priority: int = Field(ge=0, le=100)
    complexity: int = Field(ge=0, le=100)
    deadline: datetime | None = None
    duration_minutes: int = Field(default=30, ge=5, le=480)
    reasoning: str = ""

    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def priority_label(self) -> PriorityLabel:
        if self.priority >= 85:
            return PriorityLabel.CRITICAL
        if self.priority >= 65:
            return PriorityLabel.HIGH
        if self.priority >= 40:
            return PriorityLabel.MEDIUM
        return PriorityLabel.LOW

    @property
    def rank_score(self) -> float:
        urgency = 0.0
        if self.deadline:
            hours_left = max(0.0, (self.deadline - utcnow()).total_seconds() / 3600.0)
            urgency = max(0.0, 100.0 - (hours_left / 168.0) * 100.0)
        inv_complexity = 100.0 - float(self.complexity)
        return 0.5 * self.priority + 0.35 * urgency + 0.15 * inv_complexity


class ScheduledSlot(BaseModel):
    task_id: str
    start: datetime
    end: datetime
    at_risk: bool = False


# --------------------------------------------------------------------------- #
# Agent proposal payloads (each agent emits one of these)
# --------------------------------------------------------------------------- #


class STTPayload(BaseModel):
    """SpeechToTextAgent output."""

    transcript: str
    language: str = "en"
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)


class ClassificationPayload(BaseModel):
    """ClassifierAgent output — structured task metadata."""

    title: str = Field(min_length=1, max_length=120)
    category: str = "general"
    priority: int = Field(ge=0, le=100)
    complexity: int = Field(ge=0, le=100)
    deadline_iso: str | None = None
    duration_minutes: int = Field(default=30, ge=5, le=480)
    reasoning: str = ""


class MoodAnalysisPayload(BaseModel):
    """MoodAgent output — analysis + advisory."""

    score: int = Field(ge=0, le=100)
    label: str  # deep_focus | watchful | drift_alert | critical_panic
    causes: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    reasoning: str = ""


class PlannerPayload(BaseModel):
    """PlannerAgent output — advisory schedule suggestion."""

    slots: list[ScheduledSlot] = Field(default_factory=list)
    at_risk_task_ids: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    reasoning: str = ""


# --------------------------------------------------------------------------- #
# The envelope every agent emits
# --------------------------------------------------------------------------- #


class AgentProposal(BaseModel):
    """Universal wrapper around an agent's output.

    Every agent call produces one of these. Archy evaluates the
    `relevancy_score` (or trusts the agent's `self_confidence` as a hint)
    and decides whether to accept, reject, or ask for a revision.
    """

    proposal_id: str
    agent_name: str  # "stt" | "classifier" | "planner" | "mood"
    proposal_type: str  # "transcription" | "classification" | "schedule" | "mood_analysis"
    payload: dict[str, Any]  # one of the *Payload models above, serialized
    self_confidence: int = Field(ge=0, le=100, default=80)
    internal_reasoning: str = ""  # agent's private logic, never shown to user
    constraints_used: dict[str, Any] = Field(default_factory=dict)
    round: int = 1  # which negotiation round (1 = initial, 2 = revised, ...)
    timestamp: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Archy's evaluation of a proposal
# --------------------------------------------------------------------------- #


class ProposalDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    MODIFY = "modify"


class ArchyEvaluation(BaseModel):
    """Archy's verdict on an agent proposal.

    The `relevancy_score` is computed by Archy (via the LLM) based on:
    - mood alignment (does the proposal fit the user's current emotional state?)
    - cognitive load (does it over-burden a stressed user?)
    - time pressure (does it respect deadlines?)
    - historical pattern (does it match what's worked before?)
    """

    proposal_id: str
    decision: ProposalDecision
    relevancy_score: int = Field(ge=0, le=100)
    feedback: str = ""  # natural-language feedback to the agent (NOT to the user)
    constraints: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    timestamp: datetime = Field(default_factory=utcnow)


class ConflictResolution(StrEnum):
    """How a conflict between Archy and an agent ended up."""

    ACCEPTED = "accepted"           # agent's original proposal accepted
    REVISED = "revised"             # agent re-ran with Archy's constraints
    REJECTED = "rejected"           # Archy vetoed; fell back to rule-based core
    ESCALATED = "escalated"         # max rounds hit; user notified


class ConflictEvent(BaseModel):
    """Emitted when Archy disagrees with an agent. Persisted + published.

    The UI can later show these as "Archy disagreed with Planner because
    the plan was too aggressive for your current mood" — transparency into
    the multi-agent negotiation.
    """

    conflict_id: str
    agent_name: str
    proposal_type: str
    original_proposal: dict[str, Any]
    evaluation: dict[str, Any]
    rounds: int = 1
    resolution: ConflictResolution
    final_outcome: str = ""  # one-sentence summary
    timestamp: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Archy's user-facing output
# --------------------------------------------------------------------------- #


class UserFacingMessage(BaseModel):
    """Archy's interpreted output — the only natural language the user sees."""

    text: str
    mood_label: str  # the mood Archy is matching tone to
    suggested_actions: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Drift event (user-reported schedule disruption)
# --------------------------------------------------------------------------- #


class DriftEvent(BaseModel):
    kind: Literal["snooze", "portal_lock", "nap", "conflict", "manual"]
    description: str
    minutes_lost: int = Field(ge=1, le=480)
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)


# ========================================================================== #
# v2.1 EXPANSION — Energy, Context, Proposals, Expanded States, Events       #
# ========================================================================== #


# --------------------------------------------------------------------------- #
# Energy Engine — execution capacity (separate from mood)                     #
# --------------------------------------------------------------------------- #


class EnergyLabel(StrEnum):
    FULL = "full"           # >= 80 — peak execution capacity
    STEADY = "steady"       # >= 60 — normal work capacity
    LOW = "low"             # >= 40 — reduced capacity, prefer light tasks
    DEPLETED = "depleted"   # < 40 — rest needed, avoid complex tasks


class EnergySnapshot(BaseModel):
    """Execution capacity reading. Independent from mood.

    Energy = how much work you CAN do.
    Mood = how you FEEL about doing it.
    A user can be in deep_focus (high mood) but depleted (low energy) —
    the scheduler must respect both.
    """

    score: int = Field(ge=0, le=100)
    label: EnergyLabel
    causes: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utcnow)

    @classmethod
    def from_score(cls, score: int, causes: list[str] | None = None) -> "EnergySnapshot":
        if score >= 80:
            label = EnergyLabel.FULL
        elif score >= 60:
            label = EnergyLabel.STEADY
        elif score >= 40:
            label = EnergyLabel.LOW
        else:
            label = EnergyLabel.DEPLETED
        recs: list[str] = []
        if score < 40:
            recs.append("Schedule a break before complex tasks")
        elif score < 60:
            recs.append("Prefer light tasks; avoid deep-focus work")
        return cls(score=score, label=label, causes=causes or [], recommendations=recs)


# --------------------------------------------------------------------------- #
# Task State Machine — formal states + transitions                           #
# --------------------------------------------------------------------------- #


class TaskState(StrEnum):
    """v2.1 expanded task states with formal transitions."""

    CREATED = "created"           # just ingested, not yet classified
    CLASSIFIED = "classified"     # classifier agent has assigned metadata
    SCHEDULED = "scheduled"       # scheduler has assigned a slot
    ACTIVE = "active"             # user started working on it
    COMPLETED = "completed"
    MISSED = "missed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


# Allowed transitions (from → set of allowed target states)
TASK_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.CLASSIFIED, TaskState.CANCELLED, TaskState.ARCHIVED},
    TaskState.CLASSIFIED: {TaskState.SCHEDULED, TaskState.CANCELLED, TaskState.ARCHIVED},
    TaskState.SCHEDULED: {TaskState.ACTIVE, TaskState.MISSED, TaskState.CANCELLED, TaskState.ARCHIVED, TaskState.SCHEDULED},  # reschedule allowed
    TaskState.ACTIVE: {TaskState.COMPLETED, TaskState.MISSED, TaskState.CANCELLED, TaskState.ARCHIVED},
    TaskState.COMPLETED: {TaskState.ARCHIVED},  # completed tasks are immutable except archive
    TaskState.MISSED: {TaskState.SCHEDULED, TaskState.ARCHIVED},  # can reschedule missed
    TaskState.CANCELLED: {TaskState.ARCHIVED},
    TaskState.ARCHIVED: set(),  # terminal
}


class TaskStateMachine:
    """Validates and executes task state transitions.

    Hard rule: never modify completed tasks (except archiving).
    This is a safety invariant enforced at the state machine level.
    """

    @staticmethod
    def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
        return to_state in TASK_TRANSITIONS.get(from_state, set())

    @staticmethod
    def transition(current: TaskState, target: TaskState) -> TaskState:
        if not TaskStateMachine.can_transition(current, target):
            raise ValueError(
                f"Invalid task state transition: {current.value} → {target.value}. "
                f"Allowed: {[s.value for s in TASK_TRANSITIONS.get(current, set())]}"
            )
        return target

    @staticmethod
    def is_terminal(state: TaskState) -> bool:
        return state == TaskState.ARCHIVED

    @staticmethod
    def is_immutable(state: TaskState) -> bool:
        """Completed and archived tasks cannot be modified."""
        return state in (TaskState.COMPLETED, TaskState.ARCHIVED)


# --------------------------------------------------------------------------- #
# Expanded Event Bus — granular event types                                   #
# --------------------------------------------------------------------------- #


class EventType(StrEnum):
    """v2.1 expanded event taxonomy."""

    # Task lifecycle
    TASK_CREATED = "TaskCreated"
    TASK_CLASSIFIED = "TaskClassified"
    TASK_SCHEDULED = "TaskScheduled"
    TASK_STARTED = "TaskStarted"
    TASK_COMPLETED = "TaskCompleted"
    TASK_MISSED = "TaskMissed"
    TASK_CANCELLED = "TaskCancelled"

    # Mood + Energy
    MOOD_CHANGED = "MoodChanged"
    ENERGY_CHANGED = "EnergyChanged"

    # Proposal system
    PROPOSAL_CREATED = "ProposalCreated"
    PROPOSAL_APPROVED = "ProposalApproved"
    PROPOSAL_REJECTED = "ProposalRejected"

    # Permissions
    PERMISSION_REQUESTED = "PermissionRequested"
    PERMISSION_GRANTED = "PermissionGranted"
    PERMISSION_DENIED = "PermissionDenied"

    # Memory
    MEMORY_STORED = "MemoryStored"
    MEMORY_RETRIEVED = "MemoryRetrieved"

    # Router
    ROUTER_ESCALATED = "RouterEscalated"
    ROUTER_DECISION_LOGGED = "RouterDecisionLogged"

    # Voice
    VOICE_STARTED = "VoiceStarted"
    VOICE_STOPPED = "VoiceStopped"

    # Conflict
    CONFLICT_DETECTED = "ConflictDetected"

    # Notifications
    NOTIFICATION_SENT = "NotificationSent"

    # Pipeline
    INGEST_STARTED = "IngestStarted"
    PLAN_READY = "PlanReady"
    ARCHY_MESSAGE = "ArchyMessage"


# --------------------------------------------------------------------------- #
# Proposal System — formal Proposal + validation                              #
# --------------------------------------------------------------------------- #


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"


class RiskLevel(StrEnum):
    LOW = "low"        # auto-approve (reads, list operations)
    MEDIUM = "medium"  # requires confirmation
    HIGH = "high"      # requires explicit approval


class Proposal(BaseModel):
    """Formal proposal — every non-trivial action goes through this.

    Flow: Intent → Proposal → Validation (PolicyEngine) → Execution
    """

    proposal_id: str
    intent: str                        # what the agent wants to do
    action: str                        # specific tool action (e.g., "calendar.create_event")
    parameters: dict[str, Any]         # action parameters
    risk_level: RiskLevel = RiskLevel.LOW
    confidence: int = Field(ge=0, le=100, default=75)
    reasoning: str = ""
    expected_impact: str = ""
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    executed_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Agent Context — layered context for agent calls                             #
# --------------------------------------------------------------------------- #


class AgentContext(BaseModel):
    """Layered context built by ContextBuilder before each agent call.

    Dramatically improves agent quality by providing full situational awareness
    instead of just "recent tasks".
    """

    system_context: dict[str, Any] = Field(default_factory=dict)      # time, mode, config
    task_context: dict[str, Any] = Field(default_factory=dict)        # active tasks, counts
    calendar_context: dict[str, Any] = Field(default_factory=dict)    # upcoming events, conflicts
    memory_context: dict[str, Any] = Field(default_factory=dict)      # retrieved memories
    mood_context: dict[str, Any] = Field(default_factory=dict)        # current mood + history
    energy_context: dict[str, Any] = Field(default_factory=dict)      # current energy + history
    user_preferences: dict[str, Any] = Field(default_factory=dict)   # learned preferences
    conflict_context: dict[str, Any] = Field(default_factory=dict)   # recent conflicts


# --------------------------------------------------------------------------- #
# Permission System                                                            #
# --------------------------------------------------------------------------- #


class PermissionRequest(BaseModel):
    """Request for user approval on a medium/high risk action."""

    id: str
    proposal_id: str
    action: str
    reason: str
    impact: str
    risk_level: RiskLevel
    expires_at: datetime
    status: Literal["pending", "granted", "denied", "expired"] = "pending"
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Memory System — 5 memory types                                              #
# --------------------------------------------------------------------------- #


class MemoryType(StrEnum):
    EPISODIC = "episodic"          # specific events ("yesterday I filed taxes")
    SEMANTIC = "semantic"          # general knowledge ("I prefer morning meetings")
    PREFERENCE = "preference"      # user preferences ("I don't like calls before 9am")
    RELATIONSHIP = "relationship"  # people/entities ("Dr. Smith is my oncologist")
    SUMMARY = "summary"            # daily/weekly summaries


class Memory(BaseModel):
    """A single memory record with embedding vector for semantic search."""

    id: str
    memory_type: MemoryType
    content: str
    embedding: list[float] = Field(default_factory=list)  # stored for cosine similarity
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    last_accessed: datetime | None = None
    access_count: int = 0
    importance: int = Field(ge=0, le=100, default=50)


# --------------------------------------------------------------------------- #
# Safety Layer — hard rules + risk levels                                     #
# --------------------------------------------------------------------------- #


class SafetyRule(StrEnum):
    """Hard rules that can never be violated."""

    NEVER_MODIFY_COMPLETED_TASKS = "never_modify_completed_tasks"
    NEVER_DELETE_MEMORIES_WITHOUT_CONFIRMATION = "never_delete_memories_without_confirmation"
    NEVER_SEND_EXTERNAL_MESSAGES_WITHOUT_APPROVAL = "never_send_external_messages_without_approval"
    NEVER_RESCHEDULE_MEDICAL_EVENTS_AUTOMATICALLY = "never_reschedule_medical_events_automatically"
    NEVER_EXCEED_DAILY_QUOTA = "never_exceed_daily_quota"


# --------------------------------------------------------------------------- #
# Observability — metrics + audit                                             #
# --------------------------------------------------------------------------- #


class RouterDecision(BaseModel):
    """Logged every time the router picks a model. Feeds the nightly trainer."""

    timestamp: datetime = Field(default_factory=utcnow)
    complexity_score: int
    risk_score: int
    confidence: int
    model_selected: str
    latency_ms: int
    success: bool
    escalated: bool = False
    feedback_signal: str | None = None  # task_completed | user_correction | etc.


# --------------------------------------------------------------------------- #
# Work Sessions (Execution Mode)                                              #
# --------------------------------------------------------------------------- #


class SessionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class WorkSession(BaseModel):
    """A single work session — tracks execution of a task in real time.

    Created when user clicks "Start" on a task. Tracks:
    - Timer (with pause/resume)
    - Interruption count (when user pauses)
    - Progress (estimated vs actual time)
    - Feeds analytics back to SchedulerTrainer on completion
    """
    session_id: str
    task_id: str
    task_title: str
    estimated_minutes: int
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    paused_at: datetime | None = None
    total_paused_seconds: int = 0
    interruption_count: int = 0
    actual_minutes: float = 0.0  # computed on end
    completion_percentage: int = 0  # 0-100, user-reported or time-based


# --------------------------------------------------------------------------- #
# Goal → Project → Task hierarchy                                            #
# --------------------------------------------------------------------------- #


class Goal(BaseModel):
    """A top-level goal — decomposes into projects.

    Example: "Graduate Thesis" → [Research Project, Writing Project, Defense Project]
    """
    id: str
    title: str
    description: str = ""
    target_date: datetime | None = None
    status: str = "active"  # active, completed, paused
    created_at: datetime = Field(default_factory=utcnow)


class Project(BaseModel):
    """A project within a goal — decomposes into tasks.

    Example: "Research Project" → [Literature review, Data collection, Analysis]
    """
    id: str
    goal_id: str | None = None
    title: str
    description: str = ""
    status: str = "active"  # active, completed, paused
    created_at: datetime = Field(default_factory=utcnow)


class TaskWithProject(BaseModel):
    """Extended task with optional goal/project linkage."""
    task: Task
    project_id: str | None = None
    goal_id: str | None = None
    subtask_order: int | None = None  # if this is a decomposed subtask
