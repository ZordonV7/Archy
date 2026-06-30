"""SQLite storage — extended with agent proposal/evaluation/conflict audit trail.

Tables:
- tasks              (existing)
- mood_snapshots     (existing)
- agent_proposals    (NEW — every proposal from every agent)
- assistant_evaluations (NEW — Archy's verdict on each proposal)
- conflict_events    (NEW — when Archy and an agent disagreed)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterator

from loguru import logger

from ..config import Settings
from ..contracts.messages import (
    AgentProposal,
    ConflictEvent,
    ConflictResolution,
    ArchyEvaluation,
    ProposalDecision,
    Task,
    TaskStatus,
)


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    raw_transcript  TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    priority        INTEGER NOT NULL,
    complexity      INTEGER NOT NULL,
    deadline        TEXT,
    duration_minutes INTEGER NOT NULL,
    reasoning       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    scheduled_start TEXT,
    scheduled_end   TEXT,
    started_at      TEXT,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS mood_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    score       INTEGER NOT NULL,
    label       TEXT NOT NULL,
    reasons     TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_proposals (
    proposal_id          TEXT PRIMARY KEY,
    agent_name           TEXT NOT NULL,
    proposal_type        TEXT NOT NULL,
    payload              TEXT NOT NULL,           -- JSON
    self_confidence      INTEGER NOT NULL,
    internal_reasoning   TEXT NOT NULL DEFAULT '',
    constraints_used     TEXT NOT NULL DEFAULT '{}',
    round                INTEGER NOT NULL DEFAULT 1,
    timestamp            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_evaluations (
    proposal_id      TEXT PRIMARY KEY,
    decision         TEXT NOT NULL,                -- accept | reject | modify
    relevancy_score  INTEGER NOT NULL,
    feedback         TEXT NOT NULL DEFAULT '',
    constraints      TEXT NOT NULL DEFAULT '{}',   -- JSON
    reasoning        TEXT NOT NULL DEFAULT '',
    timestamp        TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES agent_proposals(proposal_id)
);

CREATE TABLE IF NOT EXISTS conflict_events (
    conflict_id        TEXT PRIMARY KEY,
    agent_name         TEXT NOT NULL,
    proposal_type      TEXT NOT NULL,
    original_proposal  TEXT NOT NULL,             -- JSON
    evaluation         TEXT NOT NULL,             -- JSON
    rounds             INTEGER NOT NULL,
    resolution         TEXT NOT NULL,             -- accepted | revised | rejected | escalated
    final_outcome      TEXT NOT NULL DEFAULT '',
    timestamp          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled_start ON tasks(scheduled_start);
CREATE INDEX IF NOT EXISTS idx_mood_timestamp ON mood_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_proposals_agent ON agent_proposals(agent_name);
CREATE INDEX IF NOT EXISTS idx_proposals_timestamp ON agent_proposals(timestamp);
CREATE INDEX IF NOT EXISTS idx_conflicts_agent ON conflict_events(agent_name);
CREATE INDEX IF NOT EXISTS idx_conflicts_timestamp ON conflict_events(timestamp);

-- v2.1 tables

CREATE TABLE IF NOT EXISTS energy_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    score       INTEGER NOT NULL,
    label       TEXT NOT NULL,
    causes      TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_energy_timestamp ON energy_snapshots(timestamp);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id     TEXT PRIMARY KEY,
    intent          TEXT NOT NULL,
    action          TEXT NOT NULL,
    parameters      TEXT NOT NULL,
    risk_level      TEXT NOT NULL,
    confidence      INTEGER NOT NULL,
    reasoning       TEXT NOT NULL DEFAULT '',
    expected_impact TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    approved_by     TEXT,
    approved_at     TEXT,
    executed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_v2_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_v2_action ON proposals(action);

CREATE TABLE IF NOT EXISTS permissions (
    id              TEXT PRIMARY KEY,
    proposal_id     TEXT NOT NULL,
    action          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    impact          TEXT NOT NULL,
    risk_level      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    approved_by     TEXT,
    approved_at     TEXT,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_permissions_status ON permissions(status);

CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT,
    details         TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_logs(event_type);

CREATE TABLE IF NOT EXISTS router_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    complexity_score    INTEGER NOT NULL,
    risk_score          INTEGER NOT NULL,
    confidence          INTEGER NOT NULL,
    model_selected      TEXT NOT NULL,
    latency_ms          INTEGER NOT NULL,
    success             INTEGER NOT NULL,
    escalated           INTEGER NOT NULL DEFAULT 0,
    feedback_signal     TEXT
);

CREATE INDEX IF NOT EXISTS idx_router_logs_timestamp ON router_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_router_logs_model ON router_logs(model_selected);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_type TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'normal',
    delivered       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    delivered_at    TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general',
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
    id              TEXT PRIMARY KEY,
    entity_name     TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    relationship    TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relationships_name ON relationships(entity_name);

CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_type    TEXT NOT NULL,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    content         TEXT NOT NULL,
    mood_avg        INTEGER,
    energy_avg      INTEGER,
    tasks_completed INTEGER,
    tasks_missed    INTEGER,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summaries_type ON summaries(summary_type);

CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    memory_type     TEXT NOT NULL,
    content         TEXT NOT NULL,
    embedding       TEXT NOT NULL DEFAULT '[]',
    metadata        TEXT NOT NULL DEFAULT '{}',
    importance      INTEGER NOT NULL DEFAULT 50,
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_accessed   TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);

-- Work sessions (Execution Mode)
CREATE TABLE IF NOT EXISTS work_sessions (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    task_title          TEXT NOT NULL,
    estimated_minutes   INTEGER NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    paused_at           TEXT,
    total_paused_seconds INTEGER NOT NULL DEFAULT 0,
    interruption_count  INTEGER NOT NULL DEFAULT 0,
    actual_minutes      REAL NOT NULL DEFAULT 0,
    completion_percentage INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_task ON work_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON work_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON work_sessions(started_at);

-- Goals and Projects (hierarchical planning)
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    target_date TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    goal_id     TEXT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES goals(id)
);

CREATE TABLE IF NOT EXISTS task_projects (
    task_id     TEXT PRIMARY KEY,
    project_id  TEXT,
    goal_id     TEXT,
    subtask_order INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS idx_projects_goal ON projects(goal_id);

-- Users (web deployment auth)
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,             -- UUID
    google_id       TEXT UNIQUE NOT NULL,         -- Google's "sub" claim
    email           TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    picture         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""


class Repository:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_V2)
        logger.info(f"Repository initialized at {db_path}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- Tasks ---

    def upsert_task(self, task: Task) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    id, raw_transcript, title, category, priority, complexity,
                    deadline, duration_minutes, reasoning, status, created_at,
                    scheduled_start, scheduled_end, started_at, completed_at
                ) VALUES (
                    :id, :raw_transcript, :title, :category, :priority, :complexity,
                    :deadline, :duration_minutes, :reasoning, :status, :created_at,
                    :scheduled_start, :scheduled_end, :started_at, :completed_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    priority = excluded.priority,
                    complexity = excluded.complexity,
                    deadline = excluded.deadline,
                    duration_minutes = excluded.duration_minutes,
                    reasoning = excluded.reasoning,
                    status = excluded.status,
                    scheduled_start = excluded.scheduled_start,
                    scheduled_end = excluded.scheduled_end,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at
                """,
                _task_to_row(task),
            )

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self, status: TaskStatus | None = None, limit: int = 200
    ) -> list[Task]:
        with self._lock:
            if status is None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_schedulable_tasks(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?) ORDER BY created_at ASC",
                (TaskStatus.PENDING.value, TaskStatus.SCHEDULED.value),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    # --- Mood ---

    def append_mood(self, score: int, label: str, reasons: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO mood_snapshots (score, label, reasons, timestamp) VALUES (?, ?, ?, ?)",
                (score, label, json.dumps(reasons), _now_iso()),
            )

    def latest_mood(self) -> tuple[int, str, list[str]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mood_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return (row["score"], row["label"], json.loads(row["reasons"]))

    def mood_history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mood_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "score": r["score"],
                "label": r["label"],
                "reasons": json.loads(r["reasons"]),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    # --- Agent proposals ---

    def append_proposal(self, p: AgentProposal) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO agent_proposals (
                    proposal_id, agent_name, proposal_type, payload,
                    self_confidence, internal_reasoning, constraints_used,
                    round, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p.proposal_id,
                    p.agent_name,
                    p.proposal_type,
                    json.dumps(p.payload),
                    p.self_confidence,
                    p.internal_reasoning,
                    json.dumps(p.constraints_used),
                    p.round,
                    p.timestamp.isoformat(),
                ),
            )

    def list_proposals(
        self, agent_name: str | None = None, limit: int = 100
    ) -> list[dict]:
        with self._lock:
            if agent_name:
                rows = self._conn.execute(
                    "SELECT * FROM agent_proposals WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ?",
                    (agent_name, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM agent_proposals ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_proposal_dict(r) for r in rows]

    # --- Archy evaluations ---

    def append_evaluation(self, e: ArchyEvaluation) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO assistant_evaluations (
                    proposal_id, decision, relevancy_score, feedback,
                    constraints, reasoning, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    e.proposal_id,
                    e.decision.value,
                    e.relevancy_score,
                    e.feedback,
                    json.dumps(e.constraints),
                    e.reasoning,
                    e.timestamp.isoformat(),
                ),
            )

    # --- Conflict events ---

    def append_conflict(self, c: ConflictEvent) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO conflict_events (
                    conflict_id, agent_name, proposal_type, original_proposal,
                    evaluation, rounds, resolution, final_outcome, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c.conflict_id,
                    c.agent_name,
                    c.proposal_type,
                    json.dumps(c.original_proposal, default=str),
                    json.dumps(c.evaluation, default=str),
                    c.rounds,
                    c.resolution.value,
                    c.final_outcome,
                    c.timestamp.isoformat(),
                ),
            )

    def list_conflicts(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conflict_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_conflict_dict(r) for r in rows]

    # --- v2.1: Energy ---

    def append_energy(self, score: int, label: str, causes: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO energy_snapshots (score, label, causes, timestamp) VALUES (?, ?, ?, ?)",
                (score, label, json.dumps(causes), _now_iso()),
            )

    def latest_energy(self) -> EnergySnapshot | None:
        """Returns EnergySnapshot or None. Uses contracts.EnergySnapshot."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM energy_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        from ..contracts.messages import EnergyLabel, EnergySnapshot
        try:
            label = EnergyLabel(row["label"])
        except ValueError:
            label = EnergyLabel.STEADY
        return EnergySnapshot(
            score=row["score"], label=label,
            causes=json.loads(row["causes"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
        )

    def energy_history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM energy_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"score": r["score"], "label": r["label"],
             "causes": json.loads(r["causes"]), "timestamp": r["timestamp"]}
            for r in rows
        ]

    # --- v2.1: Proposals (formal) ---

    def append_proposal_v2(self, p) -> None:
        """Store a v2.1 Proposal object."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO proposals
                (proposal_id, intent, action, parameters, risk_level, confidence,
                 reasoning, expected_impact, status, created_at, expires_at,
                 approved_by, approved_at, executed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.proposal_id, p.intent, p.action, json.dumps(p.parameters),
                    p.risk_level.value, p.confidence, p.reasoning, p.expected_impact,
                    p.status.value, p.created_at.isoformat(),
                    p.expires_at.isoformat() if p.expires_at else None,
                    p.approved_by,
                    p.approved_at.isoformat() if p.approved_at else None,
                    p.executed_at.isoformat() if p.executed_at else None,
                ),
            )

    def update_proposal_status(self, proposal_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proposals SET status = ? WHERE proposal_id = ?",
                (status, proposal_id),
            )

    def list_proposals_v2(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Permissions ---

    def append_permission(self, perm) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO permissions
                (id, proposal_id, action, reason, impact, risk_level, status,
                 approved_by, approved_at, expires_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    perm.id, perm.proposal_id, perm.action, perm.reason, perm.impact,
                    perm.risk_level.value, perm.status,
                    perm.approved_by,
                    perm.approved_at.isoformat() if perm.approved_at else None,
                    perm.expires_at.isoformat(), perm.created_at.isoformat(),
                ),
            )

    def update_permission_status(self, perm_id: str, status: str,
                                  approved_by: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE permissions SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
                (status, approved_by, _now_iso(), perm_id),
            )

    def list_permissions(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM permissions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM permissions ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Audit logs ---

    def append_audit_log(self, event_type: str, actor: str, action: str,
                         target: str | None = None, details: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO audit_logs (event_type, actor, action, target, details, timestamp)
                VALUES (?,?,?,?,?,?)""",
                (event_type, actor, action, target,
                 json.dumps(details or {}), _now_iso()),
            )

    def list_audit_logs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"event_type": r["event_type"], "actor": r["actor"], "action": r["action"],
             "target": r["target"], "details": json.loads(r["details"]),
             "timestamp": r["timestamp"]}
            for r in rows
        ]

    # --- v2.1: Router logs ---

    def append_router_log(self, decision) -> None:
        """Store a RouterDecision for the nightly trainer."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO router_logs
                (timestamp, complexity_score, risk_score, confidence, model_selected,
                 latency_ms, success, escalated, feedback_signal)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    decision.timestamp.isoformat(), decision.complexity_score,
                    decision.risk_score, decision.confidence, decision.model_selected,
                    decision.latency_ms, int(decision.success), int(decision.escalated),
                    decision.feedback_signal,
                ),
            )

    def list_router_logs(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM router_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Preferences ---

    def set_preference(self, key: str, value: str, category: str = "general") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO preferences (key, value, category, updated_at) VALUES (?,?,?,?)",
                (key, value, category, _now_iso()),
            )

    def get_preference(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM preferences WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def list_preferences(self, category: str | None = None) -> list[dict]:
        with self._lock:
            if category:
                rows = self._conn.execute(
                    "SELECT * FROM preferences WHERE category = ?", (category,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM preferences").fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Memories ---

    def append_memory(self, memory) -> None:
        """Store a Memory record with embedding."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO memories
                (id, memory_type, content, embedding, metadata, importance,
                 access_count, created_at, last_accessed)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    memory.id, memory.memory_type.value, memory.content,
                    json.dumps(memory.embedding), json.dumps(memory.metadata),
                    memory.importance, memory.access_count,
                    memory.created_at.isoformat(),
                    memory.last_accessed.isoformat() if memory.last_accessed else None,
                ),
            )

    def list_memories(self, memory_type: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            if memory_type:
                rows = self._conn.execute(
                    "SELECT * FROM memories WHERE memory_type = ? ORDER BY created_at DESC LIMIT ?",
                    (memory_type, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [
            {"id": r["id"], "memory_type": r["memory_type"], "content": r["content"],
             "embedding": json.loads(r["embedding"]), "metadata": json.loads(r["metadata"]),
             "importance": r["importance"], "access_count": r["access_count"],
             "created_at": r["created_at"], "last_accessed": r["last_accessed"]}
            for r in rows
        ]

    def search_memories_by_type(self, memory_type: str, limit: int = 50) -> list[dict]:
        """Fetch memories of a type for in-memory cosine similarity."""
        return self.list_memories(memory_type=memory_type, limit=limit)

    def increment_memory_access(self, memory_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (_now_iso(), memory_id),
            )

    # --- v2.1: Summaries ---

    def append_summary(self, summary_type: str, period_start: str, period_end: str,
                       content: str, mood_avg: int | None = None,
                       energy_avg: int | None = None,
                       tasks_completed: int = 0, tasks_missed: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO summaries
                (summary_type, period_start, period_end, content, mood_avg, energy_avg,
                 tasks_completed, tasks_missed, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (summary_type, period_start, period_end, content, mood_avg, energy_avg,
                 tasks_completed, tasks_missed, _now_iso()),
            )

    def list_summaries(self, summary_type: str | None = None, limit: int = 30) -> list[dict]:
        with self._lock:
            if summary_type:
                rows = self._conn.execute(
                    "SELECT * FROM summaries WHERE summary_type = ? ORDER BY created_at DESC LIMIT ?",
                    (summary_type, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM summaries ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Notifications ---

    def append_notification(self, notification_type: str, title: str, body: str,
                            priority: str = "normal") -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO notifications (notification_type, title, body, priority, created_at)
                VALUES (?,?,?,?,?)""",
                (notification_type, title, body, priority, _now_iso()),
            )
            return cur.lastrowid or 0

    def list_notifications(self, undelivered_only: bool = False, limit: int = 50) -> list[dict]:
        with self._lock:
            if undelivered_only:
                rows = self._conn.execute(
                    "SELECT * FROM notifications WHERE delivered = 0 ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Permanent delete ---

    def delete_task(self, task_id: str) -> bool:
        """Permanently delete a task from the database."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    def delete_tasks_by_status(self, statuses: list[str]) -> int:
        """Delete all tasks matching the given statuses. Returns count deleted."""
        if not statuses:
            return 0
        placeholders = ",".join("?" * len(statuses))
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM tasks WHERE status IN ({placeholders})",
                statuses,
            )
            return cur.rowcount

    # --- Goals & Projects ---

    def upsert_goal(self, goal_id: str, title: str, description: str = "", target_date: str | None = None, status: str = "active") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO goals (id, title, description, target_date, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (goal_id, title, description, target_date, status, _now_iso()),
            )

    def list_goals(self, status: str | None = None) -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM goals WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def upsert_project(self, project_id: str, goal_id: str | None, title: str, description: str = "", status: str = "active") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO projects (id, goal_id, title, description, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, goal_id, title, description, status, _now_iso()),
            )

    def list_projects(self, goal_id: str | None = None) -> list[dict]:
        with self._lock:
            if goal_id:
                rows = self._conn.execute("SELECT * FROM projects WHERE goal_id = ? ORDER BY created_at DESC", (goal_id,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def link_task_to_project(self, task_id: str, project_id: str | None, goal_id: str | None = None, subtask_order: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO task_projects (task_id, project_id, goal_id, subtask_order)
                VALUES (?, ?, ?, ?)""",
                (task_id, project_id, goal_id, subtask_order),
            )

    def get_task_project(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_projects WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    # --- v2.1: Scheduler Learning ---

    def append_scheduler_learning(self, record_type: str, data: dict) -> None:
        """Store a scheduler learning record (completion, postponement, context_switch)."""
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS scheduler_learning (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )"""
            )
            self._conn.execute(
                "INSERT INTO scheduler_learning (record_type, data, timestamp) VALUES (?, ?, ?)",
                (record_type, json.dumps(data), _now_iso()),
            )

    def list_scheduler_learning(self, limit: int = 10000) -> list[dict]:
        """Fetch scheduler learning records for training."""
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS scheduler_learning (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )"""
            )
            rows = self._conn.execute(
                "SELECT * FROM scheduler_learning ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"record_type": r["record_type"], "data": json.loads(r["data"]), "timestamp": r["timestamp"]}
            for r in rows
        ]

    # --- v2.1: Work Sessions (Execution Mode) ---

    def upsert_session(self, session) -> None:
        """Insert or update a work session."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO work_sessions
                (id, task_id, task_title, estimated_minutes, started_at, ended_at,
                 status, paused_at, total_paused_seconds, interruption_count,
                 actual_minutes, completion_percentage)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session.session_id, session.task_id, session.task_title,
                    session.estimated_minutes,
                    session.started_at.isoformat(),
                    session.ended_at.isoformat() if session.ended_at else None,
                    session.status.value,
                    session.paused_at.isoformat() if session.paused_at else None,
                    session.total_paused_seconds,
                    session.interruption_count,
                    session.actual_minutes,
                    session.completion_percentage,
                ),
            )

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM work_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_active_session(self) -> dict | None:
        """Get the currently active session (if any)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM work_sessions WHERE status IN ('active', 'paused') ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, task_id: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            if task_id:
                rows = self._conn.execute(
                    "SELECT * FROM work_sessions WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM work_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # --- Users (auth) ---

    def upsert_user(
        self,
        google_id: str,
        email: str,
        name: str = "",
        picture: str = "",
    ) -> dict:
        """Insert or update a user (called on Google OAuth login).

        Returns the user dict (with `id`, `google_id`, `email`, `name`,
        `picture`, `created_at`, `last_login_at`).
        """
        import uuid
        now = _now_iso()
        with self._lock:
            # Check if user exists by google_id
            row = self._conn.execute(
                "SELECT * FROM users WHERE google_id = ?", (google_id,)
            ).fetchone()
            if row:
                # Update existing user
                self._conn.execute(
                    """UPDATE users SET email = ?, name = ?, picture = ?, last_login_at = ?
                       WHERE google_id = ?""",
                    (email, name, picture, now, google_id),
                )
                row = self._conn.execute(
                    "SELECT * FROM users WHERE google_id = ?", (google_id,)
                ).fetchone()
                return dict(row)
            # Insert new user
            user_id = str(uuid.uuid4())
            self._conn.execute(
                """INSERT INTO users (id, google_id, email, name, picture, created_at, last_login_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, google_id, email, name, picture, now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return dict(row)

    def get_user_by_google_id(self, google_id: str) -> dict | None:
        """Look up a user by their Google ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE google_id = ?", (google_id,)
            ).fetchone()
        return dict(row) if row else None


# --- Helpers ---


def _now_iso() -> str:
    return datetime.now().isoformat()


def _task_to_row(t: Task) -> dict:
    return {
        "id": t.id,
        "raw_transcript": t.raw_transcript,
        "title": t.title,
        "category": t.category,
        "priority": t.priority,
        "complexity": t.complexity,
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "duration_minutes": t.duration_minutes,
        "reasoning": t.reasoning,
        "status": t.status.value,
        "created_at": t.created_at.isoformat(),
        "scheduled_start": t.scheduled_start.isoformat() if t.scheduled_start else None,
        "scheduled_end": t.scheduled_end.isoformat() if t.scheduled_end else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        raw_transcript=row["raw_transcript"],
        title=row["title"],
        category=row["category"],
        priority=row["priority"],
        complexity=row["complexity"],
        deadline=datetime.fromisoformat(row["deadline"]) if row["deadline"] else None,
        duration_minutes=row["duration_minutes"],
        reasoning=row["reasoning"],
        status=TaskStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        scheduled_start=datetime.fromisoformat(row["scheduled_start"]) if row["scheduled_start"] else None,
        scheduled_end=datetime.fromisoformat(row["scheduled_end"]) if row["scheduled_end"] else None,
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_proposal_dict(row: sqlite3.Row) -> dict:
    return {
        "proposal_id": row["proposal_id"],
        "agent_name": row["agent_name"],
        "proposal_type": row["proposal_type"],
        "payload": json.loads(row["payload"]),
        "self_confidence": row["self_confidence"],
        "internal_reasoning": row["internal_reasoning"],
        "constraints_used": json.loads(row["constraints_used"]),
        "round": row["round"],
        "timestamp": row["timestamp"],
    }


def _row_to_conflict_dict(row: sqlite3.Row) -> dict:
    return {
        "conflict_id": row["conflict_id"],
        "agent_name": row["agent_name"],
        "proposal_type": row["proposal_type"],
        "original_proposal": json.loads(row["original_proposal"]),
        "evaluation": json.loads(row["evaluation"]),
        "rounds": row["rounds"],
        "resolution": row["resolution"],
        "final_outcome": row["final_outcome"],
        "timestamp": row["timestamp"],
    }


# --- Singleton ---

_repo: Repository | None = None
_repo_lock = threading.Lock()


def get_repository(settings: Settings | None = None) -> Repository:
    """Get the shared Repository instance.

    Dispatches on `settings.db_url`:
    - If `ARCHY_DB_URL` is set (web deployment): returns a `PostgresRepository`
      connected to the PostgreSQL database at that URL.
    - Otherwise (desktop default): returns a SQLite `Repository` at `settings.db_path`.
    """
    global _repo
    if _repo is not None:
        return _repo
    with _repo_lock:
        if _repo is None:
            from ..config import get_settings

            s = settings or get_settings()
            if s.db_url:
                # Web deployment — PostgreSQL (Neon, Render Postgres, etc.)
                from .storage_postgres import PostgresRepository
                _repo = PostgresRepository(s.db_url)
                logger.info(f"Repository: PostgreSQL backend (db_url set)")
            else:
                # Desktop default — SQLite (zero-config)
                _repo = Repository(s.db_path)
                logger.info(f"Repository: SQLite backend at {s.db_path}")
    return _repo
