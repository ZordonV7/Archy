"""PostgreSQL storage adapter — mirrors the SQLite Repository class.

This is the PostgreSQL adapter for web deployment (Neon, Render, Supabase,
or any managed Postgres). It mirrors ``pokeboo/core/storage.py`` (SQLite)
method-for-method so that callers can swap backends without touching any
other module.

Selection rules:
    - The SQLite ``Repository`` is the default for local/desktop use.
    - ``PostgresRepository`` is selected via the ``POKEBOO_DB_URL`` env var
      (a libpq-style connection string, e.g.
      ``postgresql://user:pass@host:5432/db?sslmode=require``) inside the
      module-level ``get_repository()`` factory.

Key differences from the SQLite adapter:
    - Connection pooling via ``psycopg2.pool.ThreadedConnectionPool``
      (minconn=1, maxconn=5). The SQLite adapter uses a single shared
      connection guarded by a ``threading.Lock``; with a pool we no longer
      need an explicit lock — each thread gets its own connection.
    - ``?`` placeholders become ``%s`` (psycopg2 pyformat style). Named
      parameters use ``%(name)s``.
    - ``INSERT OR REPLACE INTO t (...) VALUES (...)`` becomes
      ``INSERT INTO t (...) VALUES (...) ON CONFLICT (pk) DO UPDATE SET ...``
      using the ``EXCLUDED.`` reference for the would-be-inserted values.
    - ``cursor.lastrowid`` (SQLite autoincrement) becomes
      ``INSERT ... RETURNING id`` followed by ``fetchone()[0]``.
    - ``sqlite3.Row`` access via ``row["col"]`` is preserved by using
      ``psycopg2.extras.RealDictCursor`` for every query.
    - JSON columns stay ``TEXT`` (not ``JSONB``) so the existing
      ``json.dumps()`` / ``json.loads()`` calls in both this module and the
      SQLite module produce identical on-disk representations.
    - ``INTEGER PRIMARY KEY AUTOINCREMENT`` becomes ``SERIAL PRIMARY KEY``.
      Booleans stay ``INTEGER`` (0/1) to match the SQLite code's
      ``int(decision.success)`` style writes.

The public surface — every method name, signature, and return type — is
identical to ``Repository``. The rest of the codebase does not need to
know which backend is in use.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import datetime
from typing import Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool
from loguru import logger

from ..contracts.messages import (
    AgentProposal,
    ConflictEvent,
    ConflictResolution,
    PokeBoEvaluation,
    ProposalDecision,
    Task,
    TaskStatus,
)


# --------------------------------------------------------------------------- #
# Schema — mirrors SCHEMA_V2 in storage.py, translated to PostgreSQL dialect.  #
# --------------------------------------------------------------------------- #


SCHEMA_POSTGRES = """
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
    id          SERIAL PRIMARY KEY,
    score       INTEGER NOT NULL,
    label       TEXT NOT NULL,
    reasons     TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_proposals (
    proposal_id          TEXT PRIMARY KEY,
    agent_name           TEXT NOT NULL,
    proposal_type        TEXT NOT NULL,
    payload              TEXT NOT NULL,           -- JSON (TEXT for compat)
    self_confidence      INTEGER NOT NULL,
    internal_reasoning   TEXT NOT NULL DEFAULT '',
    constraints_used     TEXT NOT NULL DEFAULT '{}',
    round                INTEGER NOT NULL DEFAULT 1,
    timestamp            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pokebo_evaluations (
    proposal_id      TEXT PRIMARY KEY,
    decision         TEXT NOT NULL,                -- accept | reject | modify
    relevancy_score  INTEGER NOT NULL,
    feedback         TEXT NOT NULL DEFAULT '',
    constraints      TEXT NOT NULL DEFAULT '{}',   -- JSON (TEXT for compat)
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
    id          SERIAL PRIMARY KEY,
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
    id              SERIAL PRIMARY KEY,
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
    id                  SERIAL PRIMARY KEY,
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
    id              SERIAL PRIMARY KEY,
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
    id              SERIAL PRIMARY KEY,
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

-- v2.1: scheduler_learning (created lazily in SQLite, declared up-front here)
CREATE TABLE IF NOT EXISTS scheduler_learning (
    id          SERIAL PRIMARY KEY,
    record_type TEXT NOT NULL,
    data        TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduler_learning_timestamp ON scheduler_learning(timestamp);
"""


class PostgresRepository:
    """PostgreSQL-backed drop-in replacement for the SQLite ``Repository``.

    Every public method has the same signature and return type as the
    corresponding method on ``pokeboo.core.storage.Repository``. Callers
    (the rest of the codebase) should be agnostic to which backend is
    selected at runtime.
    """

    def __init__(self, db_url: str):
        self._db_url = db_url
        # ThreadedConnectionPool hands out one connection per thread,
        # so we do not need a separate threading.Lock like the SQLite adapter.
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5, dsn=db_url
        )
        self._init_schema()
        logger.info("PostgresRepository initialized (pool minconn=1 maxconn=5)")

    # --- internals ------------------------------------------------------- #

    def _init_schema(self) -> None:
        """Apply SCHEMA_POSTGRES (idempotent — every statement uses IF NOT EXISTS)."""
        with self._conn() as (_conn, cur):
            cur.execute(SCHEMA_POSTGRES)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[tuple[psycopg2.extensions.connection, psycopg2.extras.RealDictCursor]]:
        """Acquire a connection + RealDictCursor from the pool.

        Commits on clean exit, rolls back on exception, always returns the
        connection to the pool. Mirrors the SQLite adapter's
        ``with self._lock: ... self._conn.execute(...)`` usage pattern while
        delegating concurrency control to the pool itself.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield conn, cur
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover — best-effort cleanup
                logger.warning("rollback failed during error handling")
            raise
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        """Close all connections in the pool and release resources."""
        if self._pool is not None and not self._pool.closed:
            self._pool.closeall()
            logger.info("PostgresRepository connection pool closed")

    # --- Tasks ----------------------------------------------------------- #

    def upsert_task(self, task: Task) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """
                INSERT INTO tasks (
                    id, raw_transcript, title, category, priority, complexity,
                    deadline, duration_minutes, reasoning, status, created_at,
                    scheduled_start, scheduled_end, started_at, completed_at
                ) VALUES (
                    %(id)s, %(raw_transcript)s, %(title)s, %(category)s,
                    %(priority)s, %(complexity)s, %(deadline)s,
                    %(duration_minutes)s, %(reasoning)s, %(status)s,
                    %(created_at)s, %(scheduled_start)s, %(scheduled_end)s,
                    %(started_at)s, %(completed_at)s
                )
                ON CONFLICT(id) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    priority = EXCLUDED.priority,
                    complexity = EXCLUDED.complexity,
                    deadline = EXCLUDED.deadline,
                    duration_minutes = EXCLUDED.duration_minutes,
                    reasoning = EXCLUDED.reasoning,
                    status = EXCLUDED.status,
                    scheduled_start = EXCLUDED.scheduled_start,
                    scheduled_end = EXCLUDED.scheduled_end,
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at
                """,
                _task_to_row(task),
            )

    def get_task(self, task_id: str) -> Task | None:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM tasks WHERE id = %s", (task_id,)
            )
            row = cur.fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self, status: TaskStatus | None = None, limit: int = 200
    ) -> list[Task]:
        with self._conn() as (_conn, cur):
            if status is None:
                cur.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            else:
                cur.execute(
                    "SELECT * FROM tasks WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                    (status.value, limit),
                )
            rows = cur.fetchall()
        return [_row_to_task(r) for r in rows]

    def list_schedulable_tasks(self) -> list[Task]:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM tasks WHERE status IN (%s, %s) ORDER BY created_at ASC",
                (TaskStatus.PENDING.value, TaskStatus.SCHEDULED.value),
            )
            rows = cur.fetchall()
        return [_row_to_task(r) for r in rows]

    # --- Mood ------------------------------------------------------------ #

    def append_mood(self, score: int, label: str, reasons: list[str]) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                "INSERT INTO mood_snapshots (score, label, reasons, timestamp) VALUES (%s, %s, %s, %s)",
                (score, label, json.dumps(reasons), _now_iso()),
            )

    def latest_mood(self) -> tuple[int, str, list[str]] | None:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM mood_snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        return (row["score"], row["label"], json.loads(row["reasons"]))

    def mood_history(self, limit: int = 100) -> list[dict]:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM mood_snapshots ORDER BY timestamp DESC LIMIT %s", (limit,)
            )
            rows = cur.fetchall()
        return [
            {
                "score": r["score"],
                "label": r["label"],
                "reasons": json.loads(r["reasons"]),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    # --- Agent proposals ------------------------------------------------- #

    def append_proposal(self, p: AgentProposal) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """
                INSERT INTO agent_proposals (
                    proposal_id, agent_name, proposal_type, payload,
                    self_confidence, internal_reasoning, constraints_used,
                    round, timestamp
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (proposal_id) DO UPDATE SET
                    agent_name = EXCLUDED.agent_name,
                    proposal_type = EXCLUDED.proposal_type,
                    payload = EXCLUDED.payload,
                    self_confidence = EXCLUDED.self_confidence,
                    internal_reasoning = EXCLUDED.internal_reasoning,
                    constraints_used = EXCLUDED.constraints_used,
                    round = EXCLUDED.round,
                    timestamp = EXCLUDED.timestamp
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
        with self._conn() as (_conn, cur):
            if agent_name:
                cur.execute(
                    "SELECT * FROM agent_proposals WHERE agent_name = %s ORDER BY timestamp DESC LIMIT %s",
                    (agent_name, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM agent_proposals ORDER BY timestamp DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        return [_row_to_proposal_dict(r) for r in rows]

    # --- PokeBo evaluations --------------------------------------------- #

    def append_evaluation(self, e: PokeBoEvaluation) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """
                INSERT INTO pokebo_evaluations (
                    proposal_id, decision, relevancy_score, feedback,
                    constraints, reasoning, timestamp
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (proposal_id) DO UPDATE SET
                    decision = EXCLUDED.decision,
                    relevancy_score = EXCLUDED.relevancy_score,
                    feedback = EXCLUDED.feedback,
                    constraints = EXCLUDED.constraints,
                    reasoning = EXCLUDED.reasoning,
                    timestamp = EXCLUDED.timestamp
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

    # --- Conflict events ------------------------------------------------- #

    def append_conflict(self, c: ConflictEvent) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """
                INSERT INTO conflict_events (
                    conflict_id, agent_name, proposal_type, original_proposal,
                    evaluation, rounds, resolution, final_outcome, timestamp
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (conflict_id) DO UPDATE SET
                    agent_name = EXCLUDED.agent_name,
                    proposal_type = EXCLUDED.proposal_type,
                    original_proposal = EXCLUDED.original_proposal,
                    evaluation = EXCLUDED.evaluation,
                    rounds = EXCLUDED.rounds,
                    resolution = EXCLUDED.resolution,
                    final_outcome = EXCLUDED.final_outcome,
                    timestamp = EXCLUDED.timestamp
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
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM conflict_events ORDER BY timestamp DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        return [_row_to_conflict_dict(r) for r in rows]

    # --- v2.1: Energy --------------------------------------------------- #

    def append_energy(self, score: int, label: str, causes: list[str]) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                "INSERT INTO energy_snapshots (score, label, causes, timestamp) VALUES (%s, %s, %s, %s)",
                (score, label, json.dumps(causes), _now_iso()),
            )

    def latest_energy(self) -> EnergySnapshot | None:
        """Returns EnergySnapshot or None. Uses contracts.EnergySnapshot.

        Note: ``EnergySnapshot`` is intentionally not imported at module top
        (mirrors the SQLite adapter's pattern); the annotation works thanks
        to ``from __future__ import annotations``.
        """
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM energy_snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
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
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM energy_snapshots ORDER BY timestamp DESC LIMIT %s", (limit,)
            )
            rows = cur.fetchall()
        return [
            {"score": r["score"], "label": r["label"],
             "causes": json.loads(r["causes"]), "timestamp": r["timestamp"]}
            for r in rows
        ]

    # --- v2.1: Proposals (formal) --------------------------------------- #

    def append_proposal_v2(self, p) -> None:
        """Store a v2.1 Proposal object."""
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO proposals
                (proposal_id, intent, action, parameters, risk_level, confidence,
                 reasoning, expected_impact, status, created_at, expires_at,
                 approved_by, approved_at, executed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (proposal_id) DO UPDATE SET
                    intent = EXCLUDED.intent,
                    action = EXCLUDED.action,
                    parameters = EXCLUDED.parameters,
                    risk_level = EXCLUDED.risk_level,
                    confidence = EXCLUDED.confidence,
                    reasoning = EXCLUDED.reasoning,
                    expected_impact = EXCLUDED.expected_impact,
                    status = EXCLUDED.status,
                    expires_at = EXCLUDED.expires_at,
                    approved_by = EXCLUDED.approved_by,
                    approved_at = EXCLUDED.approved_at,
                    executed_at = EXCLUDED.executed_at""",
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
        with self._conn() as (_conn, cur):
            cur.execute(
                "UPDATE proposals SET status = %s WHERE proposal_id = %s",
                (status, proposal_id),
            )

    def list_proposals_v2(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self._conn() as (_conn, cur):
            if status:
                cur.execute(
                    "SELECT * FROM proposals WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Permissions ---------------------------------------------- #

    def append_permission(self, perm) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO permissions
                (id, proposal_id, action, reason, impact, risk_level, status,
                 approved_by, approved_at, expires_at, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    proposal_id = EXCLUDED.proposal_id,
                    action = EXCLUDED.action,
                    reason = EXCLUDED.reason,
                    impact = EXCLUDED.impact,
                    risk_level = EXCLUDED.risk_level,
                    status = EXCLUDED.status,
                    approved_by = EXCLUDED.approved_by,
                    approved_at = EXCLUDED.approved_at,
                    expires_at = EXCLUDED.expires_at""",
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
        with self._conn() as (_conn, cur):
            cur.execute(
                "UPDATE permissions SET status = %s, approved_by = %s, approved_at = %s WHERE id = %s",
                (status, approved_by, _now_iso(), perm_id),
            )

    def list_permissions(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self._conn() as (_conn, cur):
            if status:
                cur.execute(
                    "SELECT * FROM permissions WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM permissions ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Audit logs ----------------------------------------------- #

    def append_audit_log(self, event_type: str, actor: str, action: str,
                         target: str | None = None, details: dict | None = None) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO audit_logs (event_type, actor, action, target, details, timestamp)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (event_type, actor, action, target,
                 json.dumps(details or {}), _now_iso()),
            )

    def list_audit_logs(self, limit: int = 100) -> list[dict]:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT %s", (limit,)
            )
            rows = cur.fetchall()
        return [
            {"event_type": r["event_type"], "actor": r["actor"], "action": r["action"],
             "target": r["target"], "details": json.loads(r["details"]),
             "timestamp": r["timestamp"]}
            for r in rows
        ]

    # --- v2.1: Router logs ---------------------------------------------- #

    def append_router_log(self, decision) -> None:
        """Store a RouterDecision for the nightly trainer."""
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO router_logs
                (timestamp, complexity_score, risk_score, confidence, model_selected,
                 latency_ms, success, escalated, feedback_signal)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    decision.timestamp.isoformat(), decision.complexity_score,
                    decision.risk_score, decision.confidence, decision.model_selected,
                    decision.latency_ms, int(decision.success), int(decision.escalated),
                    decision.feedback_signal,
                ),
            )

    def list_router_logs(self, limit: int = 500) -> list[dict]:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM router_logs ORDER BY timestamp DESC LIMIT %s", (limit,)
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Preferences ---------------------------------------------- #

    def set_preference(self, key: str, value: str, category: str = "general") -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO preferences (key, value, category, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    category = EXCLUDED.category,
                    updated_at = EXCLUDED.updated_at""",
                (key, value, category, _now_iso()),
            )

    def get_preference(self, key: str) -> str | None:
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT value FROM preferences WHERE key = %s", (key,)
            )
            row = cur.fetchone()
        return row["value"] if row else None

    def list_preferences(self, category: str | None = None) -> list[dict]:
        with self._conn() as (_conn, cur):
            if category:
                cur.execute(
                    "SELECT * FROM preferences WHERE category = %s", (category,)
                )
            else:
                cur.execute("SELECT * FROM preferences")
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Memories ------------------------------------------------- #

    def append_memory(self, memory) -> None:
        """Store a Memory record with embedding."""
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO memories
                (id, memory_type, content, embedding, metadata, importance,
                 access_count, created_at, last_accessed)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    memory_type = EXCLUDED.memory_type,
                    content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding,
                    metadata = EXCLUDED.metadata,
                    importance = EXCLUDED.importance,
                    access_count = EXCLUDED.access_count,
                    last_accessed = EXCLUDED.last_accessed""",
                (
                    memory.id, memory.memory_type.value, memory.content,
                    json.dumps(memory.embedding), json.dumps(memory.metadata),
                    memory.importance, memory.access_count,
                    memory.created_at.isoformat(),
                    memory.last_accessed.isoformat() if memory.last_accessed else None,
                ),
            )

    def list_memories(self, memory_type: str | None = None, limit: int = 100) -> list[dict]:
        with self._conn() as (_conn, cur):
            if memory_type:
                cur.execute(
                    "SELECT * FROM memories WHERE memory_type = %s ORDER BY created_at DESC LIMIT %s",
                    (memory_type, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM memories ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
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
        with self._conn() as (_conn, cur):
            cur.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = %s WHERE id = %s",
                (_now_iso(), memory_id),
            )

    # --- v2.1: Summaries ------------------------------------------------ #

    def append_summary(self, summary_type: str, period_start: str, period_end: str,
                       content: str, mood_avg: int | None = None,
                       energy_avg: int | None = None,
                       tasks_completed: int = 0, tasks_missed: int = 0) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO summaries
                (summary_type, period_start, period_end, content, mood_avg, energy_avg,
                 tasks_completed, tasks_missed, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (summary_type, period_start, period_end, content, mood_avg, energy_avg,
                 tasks_completed, tasks_missed, _now_iso()),
            )

    def list_summaries(self, summary_type: str | None = None, limit: int = 30) -> list[dict]:
        with self._conn() as (_conn, cur):
            if summary_type:
                cur.execute(
                    "SELECT * FROM summaries WHERE summary_type = %s ORDER BY created_at DESC LIMIT %s",
                    (summary_type, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM summaries ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Notifications -------------------------------------------- #

    def append_notification(self, notification_type: str, title: str, body: str,
                            priority: str = "normal") -> int:
        # SQLite used cursor.lastrowid; Postgres uses RETURNING id.
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO notifications (notification_type, title, body, priority, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id""",
                (notification_type, title, body, priority, _now_iso()),
            )
            row = cur.fetchone()
        if row is None:
            logger.warning("append_notification: RETURNING id yielded no row")
            return 0
        return int(row["id"]) or 0

    def list_notifications(self, undelivered_only: bool = False, limit: int = 50) -> list[dict]:
        with self._conn() as (_conn, cur):
            if undelivered_only:
                cur.execute(
                    "SELECT * FROM notifications WHERE delivered = 0 ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            else:
                cur.execute(
                    "SELECT * FROM notifications ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # --- v2.1: Permanent delete ----------------------------------------- #

    def delete_task(self, task_id: str) -> bool:
        """Permanently delete a task from the database."""
        with self._conn() as (_conn, cur):
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            return cur.rowcount > 0

    def delete_tasks_by_status(self, statuses: list[str]) -> int:
        """Delete all tasks matching the given statuses. Returns count deleted."""
        if not statuses:
            return 0
        placeholders = ",".join("%s" * len(statuses))
        with self._conn() as (_conn, cur):
            cur.execute(
                f"DELETE FROM tasks WHERE status IN ({placeholders})",
                statuses,
            )
            return cur.rowcount

    # --- Goals & Projects ----------------------------------------------- #

    def upsert_goal(self, goal_id: str, title: str, description: str = "", target_date: str | None = None, status: str = "active") -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO goals (id, title, description, target_date, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    target_date = EXCLUDED.target_date,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at""",
                (goal_id, title, description, target_date, status, _now_iso()),
            )

    def list_goals(self, status: str | None = None) -> list[dict]:
        with self._conn() as (_conn, cur):
            if status:
                cur.execute("SELECT * FROM goals WHERE status = %s ORDER BY created_at DESC", (status,))
            else:
                cur.execute("SELECT * FROM goals ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def upsert_project(self, project_id: str, goal_id: str | None, title: str, description: str = "", status: str = "active") -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO projects (id, goal_id, title, description, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    goal_id = EXCLUDED.goal_id,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at""",
                (project_id, goal_id, title, description, status, _now_iso()),
            )

    def list_projects(self, goal_id: str | None = None) -> list[dict]:
        with self._conn() as (_conn, cur):
            if goal_id:
                cur.execute("SELECT * FROM projects WHERE goal_id = %s ORDER BY created_at DESC", (goal_id,))
            else:
                cur.execute("SELECT * FROM projects ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def link_task_to_project(self, task_id: str, project_id: str | None, goal_id: str | None = None, subtask_order: int | None = None) -> None:
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO task_projects (task_id, project_id, goal_id, subtask_order)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (task_id) DO UPDATE SET
                    project_id = EXCLUDED.project_id,
                    goal_id = EXCLUDED.goal_id,
                    subtask_order = EXCLUDED.subtask_order""",
                (task_id, project_id, goal_id, subtask_order),
            )

    def get_task_project(self, task_id: str) -> dict | None:
        with self._conn() as (_conn, cur):
            cur.execute("SELECT * FROM task_projects WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    # --- v2.1: Scheduler Learning --------------------------------------- #

    def append_scheduler_learning(self, record_type: str, data: dict) -> None:
        """Store a scheduler learning record (completion, postponement, context_switch).

        The table is declared up-front in SCHEMA_POSTGRES; the SQLite adapter
        creates it lazily on each call. We keep the CREATE TABLE IF NOT EXISTS
        here as well for parity / safety in case the schema init was skipped.
        """
        with self._conn() as (_conn, cur):
            cur.execute(
                """CREATE TABLE IF NOT EXISTS scheduler_learning (
                    id SERIAL PRIMARY KEY,
                    record_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )"""
            )
            cur.execute(
                "INSERT INTO scheduler_learning (record_type, data, timestamp) VALUES (%s, %s, %s)",
                (record_type, json.dumps(data), _now_iso()),
            )

    def list_scheduler_learning(self, limit: int = 10000) -> list[dict]:
        """Fetch scheduler learning records for training."""
        with self._conn() as (_conn, cur):
            cur.execute(
                """CREATE TABLE IF NOT EXISTS scheduler_learning (
                    id SERIAL PRIMARY KEY,
                    record_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )"""
            )
            cur.execute(
                "SELECT * FROM scheduler_learning ORDER BY timestamp DESC LIMIT %s", (limit,)
            )
            rows = cur.fetchall()
        return [
            {"record_type": r["record_type"], "data": json.loads(r["data"]), "timestamp": r["timestamp"]}
            for r in rows
        ]

    # --- v2.1: Work Sessions (Execution Mode) --------------------------- #

    def upsert_session(self, session) -> None:
        """Insert or update a work session."""
        with self._conn() as (_conn, cur):
            cur.execute(
                """INSERT INTO work_sessions
                (id, task_id, task_title, estimated_minutes, started_at, ended_at,
                 status, paused_at, total_paused_seconds, interruption_count,
                 actual_minutes, completion_percentage)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    task_id = EXCLUDED.task_id,
                    task_title = EXCLUDED.task_title,
                    estimated_minutes = EXCLUDED.estimated_minutes,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    status = EXCLUDED.status,
                    paused_at = EXCLUDED.paused_at,
                    total_paused_seconds = EXCLUDED.total_paused_seconds,
                    interruption_count = EXCLUDED.interruption_count,
                    actual_minutes = EXCLUDED.actual_minutes,
                    completion_percentage = EXCLUDED.completion_percentage""",
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
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM work_sessions WHERE id = %s", (session_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_active_session(self) -> dict | None:
        """Get the currently active session (if any)."""
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM work_sessions WHERE status IN ('active', 'paused') ORDER BY started_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_sessions(self, task_id: str | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as (_conn, cur):
            if task_id:
                cur.execute(
                    "SELECT * FROM work_sessions WHERE task_id = %s ORDER BY started_at DESC LIMIT %s",
                    (task_id, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM work_sessions ORDER BY started_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
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
        with self._conn() as (_conn, cur):
            # Check if user exists by google_id
            cur.execute(
                "SELECT * FROM users WHERE google_id = %s", (google_id,)
            )
            row = cur.fetchone()
            if row:
                # Update existing user
                cur.execute(
                    """UPDATE users SET email = %s, name = %s, picture = %s, last_login_at = %s
                       WHERE google_id = %s RETURNING *""",
                    (email, name, picture, now, google_id),
                )
                row = cur.fetchone()
                return dict(row)
            # Insert new user
            user_id = str(uuid.uuid4())
            cur.execute(
                """INSERT INTO users (id, google_id, email, name, picture, created_at, last_login_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (user_id, google_id, email, name, picture, now, now),
            )
            row = cur.fetchone()
            return dict(row)

    def get_user_by_google_id(self, google_id: str) -> dict | None:
        """Look up a user by their Google ID."""
        with self._conn() as (_conn, cur):
            cur.execute(
                "SELECT * FROM users WHERE google_id = %s", (google_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None


# --- Helpers --------------------------------------------------------------- #
# These are identical to the SQLite adapter's helpers — they operate on
# dict-like rows (sqlite3.Row or psycopg2 RealDictRow), so no changes
# were needed beyond the type annotation on _row_to_task.


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


def _row_to_task(row) -> Task:
    # Works for both sqlite3.Row and psycopg2 RealDictRow — both support
    # row["col"] access.
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


def _row_to_proposal_dict(row) -> dict:
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


def _row_to_conflict_dict(row) -> dict:
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


# --- Singleton ------------------------------------------------------------- #


_repo: PostgresRepository | None = None
_repo_lock = threading.Lock()


def get_repository(settings=None) -> PostgresRepository:
    """Return a process-wide :class:`PostgresRepository` singleton.

    The ``settings`` argument is accepted for signature parity with the
    SQLite adapter's ``get_repository(settings)`` but is not consumed —
    the connection URL is read exclusively from the ``POKEBOO_DB_URL``
    environment variable (a libpq connection string).

    Raises:
        RuntimeError: if ``POKEBOO_DB_URL`` is not set.
    """
    global _repo
    if _repo is not None:
        return _repo
    with _repo_lock:
        if _repo is None:
            db_url = os.environ.get("POKEBOO_DB_URL")
            if not db_url:
                raise RuntimeError(
                    "POKEBOO_DB_URL env var not set — required to construct "
                    "PostgresRepository. Set it to a libpq connection string "
                    "(e.g. postgresql://user:pass@host:5432/db?sslmode=require)."
                )
            _repo = PostgresRepository(db_url)
    return _repo
