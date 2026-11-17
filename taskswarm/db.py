"""SQLite schema and access. One database per run, written only by the orchestrator.

Everything a run needs to continue after a crash or a reboot is here: the plan, each
task's state, every agent's conversation, and undelivered messages.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,                -- planner | lead | worker | reviewer
    task TEXT,                         -- the task it works on or reviews; NULL for the lead and final reviewers
    spent REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    context_est INTEGER NOT NULL DEFAULT 0,   -- estimated prompt tokens of its next call
    api_failures INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'normal',  -- normal | compaction
    content TEXT NOT NULL,             -- JSON, exactly as sent to / returned by the API
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_agent ON messages(agent, id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    input_tokens INTEGER, cache_write_tokens INTEGER, cache_read_tokens INTEGER,
    output_tokens INTEGER, prompt_tokens INTEGER,
    compaction INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL,
    latency REAL,
    stop_reason TEXT,
    raw TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    cmd TEXT NOT NULL,
    exit_code INTEGER,
    seconds REAL,
    output_bytes INTEGER,
    output_path TEXT,
    timed_out INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,               -- a sub-task's id is <parent>.<name>
    parent TEXT,                       -- the task this one is a piece of; NULL = planned by the lead
    seq INTEGER NOT NULL,              -- order of creation
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    verify TEXT NOT NULL,
    blocked_by TEXT NOT NULL,          -- JSON list of task ids
    status TEXT NOT NULL,              -- pending | running | reviewing | done | failed | cancelled
    attempts INTEGER NOT NULL DEFAULT 0,   -- reports handed to a reviewer so far
    summary TEXT,                      -- the worker's latest report
    how TEXT,
    findings TEXT,                     -- the latest reviewer's findings, or why it failed
    decisions TEXT,                    -- JSON list: what the task's owner ruled after the brief was written
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY,
    to_agent TEXT NOT NULL,
    from_agent TEXT NOT NULL,          -- an agent id, or 'system'
    body TEXT NOT NULL,
    quiet INTEGER NOT NULL DEFAULT 0,  -- 1 = wait for the recipient's next turn instead of waking it
    delivered INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    agent TEXT,
    kind TEXT NOT NULL,
    detail TEXT,
    created REAL NOT NULL
);
"""

OPEN = ("pending", "running", "reviewing")


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for column in ("parent", "decisions"):       # runs made by an older version
            if column not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")

    # --- generic -------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def transaction(self):
        return _Tx(self.conn)

    # --- meta ----------------------------------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- agents --------------------------------------------------------------------

    def add_agent(self, agent_id: str, role: str, task: str | None = None) -> None:
        self.execute(
            "INSERT INTO agents(id, role, task, created) VALUES(?, ?, ?, ?)", (agent_id, role, task, time.time())
        )
        self.event(agent_id, "created", {"role": role, "task": task})

    def agents(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM agents ORDER BY created, id")

    def agent(self, agent_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM agents WHERE id = ?", (agent_id,))

    # --- messages ------------------------------------------------------------------

    def add_message(self, agent: str, role: str, content: Any, kind: str = "normal") -> None:
        self.execute(
            "INSERT INTO messages(agent, role, kind, content, created) VALUES(?, ?, ?, ?, ?)",
            (agent, role, kind, json.dumps(content, ensure_ascii=False), time.time()),
        )

    def history(self, agent: str) -> list[dict]:
        """What the API gets: everything from the latest compaction block on."""
        last = self.one(
            "SELECT MAX(id) AS id FROM messages WHERE agent = ? AND kind = 'compaction'", (agent,)
        )
        start = last["id"] if last and last["id"] is not None else 0
        rows = self.all(
            "SELECT role, content FROM messages WHERE agent = ? AND id >= ? ORDER BY id", (agent, start)
        )
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    # --- tasks ---------------------------------------------------------------------

    def tasks(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM tasks ORDER BY seq")

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def update_task(self, task_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE tasks SET {sets}, updated = ? WHERE id = ?", (*fields.values(), time.time(), task_id))

    def children(self, parent: str | None) -> list[sqlite3.Row]:
        """The tasks one owner planned: the lead's (parent None) or a worker's sub-tasks."""
        return [t for t in self.tasks() if t["parent"] == parent]

    def descendants(self, task_id: str) -> list[sqlite3.Row]:
        return [t for t in self.tasks() if t["id"].startswith(task_id + ".")]

    def tree_spent(self, root: str) -> float:
        """Spent by a top-level task, its reviewers and everything under it."""
        row = self.one("SELECT COALESCE(SUM(spent), 0) AS s FROM agents WHERE task = ? OR task LIKE ?",
                       (root, root + ".%"))
        return float(row["s"])

    # --- inbox ---------------------------------------------------------------------

    def send(self, to_agent: str, from_agent: str, body: str, quiet: bool = False) -> None:
        self.execute(
            "INSERT INTO inbox(to_agent, from_agent, body, quiet, created) VALUES(?, ?, ?, ?, ?)",
            (to_agent, from_agent, body, int(quiet), time.time()),
        )

    def take_inbox(self, agent: str) -> list[sqlite3.Row]:
        rows = self.all("SELECT * FROM inbox WHERE to_agent = ? AND delivered = 0 ORDER BY id", (agent,))
        if rows:
            self.execute("UPDATE inbox SET delivered = 1 WHERE to_agent = ? AND id <= ?", (agent, rows[-1]["id"]))
        return rows

    def waiting_mail(self) -> list[str]:
        """Agents with an undelivered message that should wake them."""
        rows = self.all("SELECT DISTINCT to_agent FROM inbox WHERE delivered = 0 AND quiet = 0")
        return [r["to_agent"] for r in rows]

    # --- events and spending -------------------------------------------------------

    def event(self, agent: str | None, kind: str, detail: Any = None) -> None:
        self.execute(
            "INSERT INTO events(agent, kind, detail, created) VALUES(?, ?, ?, ?)",
            (agent, kind, json.dumps(detail) if detail is not None else None, time.time()),
        )

    def total_spent(self) -> float:
        row = self.one("SELECT COALESCE(SUM(cost), 0) AS s FROM usage")
        return float(row["s"])


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
