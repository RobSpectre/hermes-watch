"""Shared fixtures: a synthetic Hermes session store with the real column names.

The schema below is the subset of ``state.db`` the bridge reads. It is created
with the *same* column names and semantics Hermes uses so a query that works
here works against the real store; see ``docs/hermes-integration.md`` for the
authoritative schema reference.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    model TEXT,
    model_config TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    cost_status TEXT,
    api_call_count INTEGER DEFAULT 0,
    title TEXT,
    profile_name TEXT,
    last_activity_at REAL,
    archived INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    tool_names TEXT
);
"""


def make_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    started_at: float,
    ended_at: float | None = None,
    model: str = "test/model-1",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
    api_call_count: int = 0,
    source: str = "cli",
    title: str = "a session",
    cost: float = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (id, source, model, started_at, ended_at, message_count, tool_call_count,
                              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                              reasoning_tokens, estimated_cost_usd, api_call_count, title, profile_name,
                              last_activity_at, archived, hidden)
        VALUES (?, ?, ?, ?, ?, 4, 2, ?, ?, ?, 0, ?, ?, ?, ?, NULL, ?, 0, 0)
        """,
        (
            session_id, source, model, started_at, ended_at, input_tokens, output_tokens,
            cache_read_tokens, reasoning_tokens, cost, api_call_count, title,
            ended_at or started_at,
        ),
    )
    conn.commit()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A fresh state.db with one live session and two finished ones."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SESSIONS_DDL)
    now = time.time()
    make_session(
        conn, "sess_live", started_at=now - 600, model="test/model-1",
        input_tokens=30_000, output_tokens=3_000, cache_read_tokens=60_000,
        reasoning_tokens=500, api_call_count=4, title="live session", cost=0.012,
    )
    make_session(
        conn, "sess_old_a", started_at=now - 3 * 86400, ended_at=now - 3 * 86400 + 300,
        model="test/model-1", input_tokens=10_000, output_tokens=1_000, api_call_count=2,
        title="three days ago",
    )
    make_session(
        conn, "sess_old_b", started_at=now - 40 * 86400, ended_at=now - 40 * 86400 + 300,
        model="test/model-2", input_tokens=999_999, output_tokens=999_999, api_call_count=2,
        title="outside the window",
    )
    conn.close()
    return db_path
