"""Token / context / throughput statistics for the watch.

Two data sources, and the difference matters:

* **Hermes' session store** (``$HERMES_HOME/state.db``) is the durable record.
  It is written by every Hermes surface (CLI, TUI, desktop, gateway) and is the
  only source for "last 30 days" and for the session's cumulative counters.
  It has no per-call timing, so anything derived from it is an *average over
  wall clock* unless noted.
* **Live hook payloads** forwarded by the Hermes plugin (see ``plugin.py``) are
  per-API-call and exact: real generation latency, real prompt size. The bridge
  keeps the most recent values and prefers them when present.

Every number this module emits carries a ``source`` string when the value is
derived rather than measured, so the watch can render a "~" prefix instead of
presenting an estimate as a fact. Read :func:`totals_last_days` and
:func:`context_usage` for the two places that matters most.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

#: Hermes' canonical session store, resolved per profile (never hard-code
#: ``~/.hermes`` -- a named profile has its own database).
DEFAULT_DB_NAME = "state.db"

#: Learned context windows, written by ``agent.model_metadata.save_context_length``
#: as ``{"context_lengths": {"<model>@<base_url>": <int>}}``.
CONTEXT_CACHE_NAME = "context_length_cache.yaml"

#: Session columns summed into "tokens used this session".
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def hermes_home() -> Path:
    """Resolve the Hermes home directory for this process.

    ``HERMES_HOME`` wins (Hermes sets it for a named profile); otherwise the
    platform default. Duplicated rather than imported so this package runs
    outside a Hermes installation.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def state_db_path(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / DEFAULT_DB_NAME


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the session store read-only.

    Read-only on purpose: the bridge must never be able to corrupt a live
    Hermes database, and WAL mode means a second reader is free.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class TokenCounts:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        """Everything the provider accounted for, cached reads included.

        ``cache_read``/``cache_write`` are *not* subsets of ``input`` in the
        providers Hermes targets, so adding them is the honest total; the
        watch shows the cached share separately rather than folding it in.
        """
        return self.input + self.output + self.cache_read + self.cache_write

    def to_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cached_read": self.cache_read,
            "cached_write": self.cache_write,
            "reasoning": self.reasoning,
            "total": self.total,
        }


@dataclass
class LiveObservation:
    """Last exact measurements forwarded by the Hermes plugin.

    All fields optional: a bridge running without the plugin (or before the
    first API call of a turn) has nothing to report and must say so rather
    than fabricate a rate.
    """

    api_call_count: Optional[int] = None
    api_duration: Optional[float] = None
    output_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    approx_input_tokens: Optional[int] = None
    context_window: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class StatsEngine:
    """Assembles the stats payload the watch renders."""

    db_path: Optional[Path] = None
    live: LiveObservation = field(default_factory=LiveObservation)
    _context_window_cache: dict[str, Optional[int]] = field(default_factory=dict, repr=False)

    # -- session store -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path or state_db_path())

    def current_session_id(self, conn: sqlite3.Connection) -> Optional[str]:
        """Most recently active session that has not ended.

        ``hidden``/``archived`` rows are excluded so a compacted-away or
        rewind-archived session never wins the race.
        """
        row = conn.execute(
            """
            SELECT id FROM sessions
            WHERE ended_at IS NULL AND COALESCE(hidden, 0) = 0 AND COALESCE(archived, 0) = 0
            ORDER BY COALESCE(last_activity_at, started_at) DESC
            LIMIT 1
            """
        ).fetchone()
        return row["id"] if row else None

    def session_snapshot(self, session_id: Optional[str] = None) -> Optional[dict]:
        """Cumulative counters for one session, or None when there is none."""
        with self._connect() as conn:
            if session_id is None:
                session_id = self.current_session_id(conn)
            if session_id is None:
                return None
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            return self._session_row_to_dict(row)

    def _session_row_to_dict(self, row: sqlite3.Row) -> dict:
        now = time.time()
        tokens = TokenCounts(
            input=row["input_tokens"] or 0,
            output=row["output_tokens"] or 0,
            cache_read=row["cache_read_tokens"] or 0,
            cache_write=row["cache_write_tokens"] or 0,
            reasoning=row["reasoning_tokens"] or 0,
        )
        started = row["started_at"]
        ended = row["ended_at"] or now
        elapsed = max(ended - started, 0.0)
        api_calls = int(row["api_call_count"] or 0)
        snapshot: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "source": row["source"],
            "model": row["model"],
            "profile": row["profile_name"],
            "started_at": started,
            "elapsed_s": round(elapsed, 1),
            "ended": row["ended_at"] is not None,
            "message_count": int(row["message_count"] or 0),
            "tool_call_count": int(row["tool_call_count"] or 0),
            "api_call_count": api_calls,
            "tokens": tokens.to_dict(),
            "cost_usd": row["estimated_cost_usd"] or 0.0,
            "cost_status": row["cost_status"],
        }
        snapshot["tok_per_s"] = self._throughput(snapshot, tokens, elapsed)
        return snapshot

    def _throughput(self, session: dict, tokens: TokenCounts, elapsed: float) -> dict:
        """Output-token throughput, exact when we have it and labelled when we don't.

        ``live`` is output tokens over the provider-call latency -- the number
        people mean by tok/s. ``session_avg`` is output tokens over wall-clock
        session time, which includes tool execution and human think time and is
        therefore much lower; it is reported *and labelled* because it is the
        only figure available on a cold start.
        """
        out: dict[str, Any] = {"live": None, "live_source": None, "session_avg": None, "session_avg_source": None}
        obs = self.live
        if obs.api_duration and obs.output_tokens is not None and obs.api_duration > 0:
            out["live"] = round(obs.output_tokens / obs.api_duration, 1)
            out["live_source"] = "api_call"
        if elapsed > 0 and tokens.output > 0:
            out["session_avg"] = round(tokens.output / elapsed, 2)
            out["session_avg_source"] = "wall_clock_includes_tool_time"
        return out

    def context_usage(self, session: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        """How much of the model's context window the current session occupies.

        Preference order, and the reason for it:

        1. ``live`` -- the plugin's last exact ``usage.prompt_tokens`` (or the
           pre-call ``approx_input_tokens``). This is what the model actually
           received.
        2. ``db_estimate`` -- the session's cumulative prompt tokens divided by
           its API call count. Hermes does *not* backfill
           ``messages.token_count``, so the transcript cannot be summed
           directly; per-call average is the best cold-start proxy and is
           reported as approximate.

        ``remaining_pct`` is null when the window is unknown rather than
        guessed -- the watch renders a dash, not a made-up percentage.
        """
        obs = self.live
        used: Optional[int] = None
        source: Optional[str] = None
        if obs.prompt_tokens:
            used, source = int(obs.prompt_tokens), "live"
        elif obs.approx_input_tokens:
            used, source = int(obs.approx_input_tokens), "live_approximate"
        elif session is not None:
            calls = int(session.get("api_call_count") or 0)
            if calls > 0:
                tokens = session.get("tokens") or {}
                cumulative = int(tokens.get("input", 0)) + int(tokens.get("cached_read", 0))
                if cumulative:
                    used, source = int(round(cumulative / calls)), "db_estimate"

        window = obs.context_window or self.context_window(session.get("model") if session else None)
        result: dict[str, Any] = {
            "used_tokens": used,
            "window_tokens": window,
            "remaining_tokens": None,
            "remaining_pct": None,
            "used_pct": None,
            "source": source,
        }
        if used is not None and window:
            remaining = max(window - used, 0)
            result["remaining_tokens"] = remaining
            result["remaining_pct"] = round(100.0 * remaining / window, 1)
            result["used_pct"] = round(100.0 * used / window, 1)
        return result

    def context_window(self, model: Optional[str], base_url: str = "") -> Optional[int]:
        """Look up a model's context window without importing Hermes.

        Reads the same cache Hermes writes (``context_length_cache.yaml``).
        Returns None when nothing is known; the plugin path inside a live
        Hermes process uses ``agent.model_metadata.get_model_context_length``
        instead, which resolves far more cases.
        """
        if not model:
            return None
        key = f"{model}@{base_url}"
        if key in self._context_window_cache:
            return self._context_window_cache[key]
        window = None
        path = hermes_home() / CONTEXT_CACHE_NAME
        try:
            if path.exists():
                import yaml  # type: ignore

                document = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
                table = document.get("context_lengths") or {}
                if isinstance(table, dict):
                    for candidate in (key, model, f"{key}/"):
                        value = table.get(candidate)
                        if isinstance(value, int) and value > 0:
                            window = value
                            break
        except Exception:
            window = None
        self._context_window_cache[key] = window
        return window

    # -- history -------------------------------------------------------------

    def totals_last_days(self, days: int = 30, session_id: Optional[str] = None) -> dict:
        """Token and cost totals over a rolling window, with per-model and per-day splits.

        ``session_id`` is excluded from the windowed totals so "this session"
        and "last 30 days" can be read side by side without double counting in
        the user's head.
        """
        cutoff = time.time() - days * 86400.0
        sums = ", ".join(f"COALESCE(SUM({c}), 0) AS {c}" for c in TOKEN_COLUMNS)
        with self._connect() as conn:
            where = "started_at >= ?"
            params: list[Any] = [cutoff]
            if session_id:
                where += " AND id != ?"
                params.append(session_id)
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS session_count,
                       COALESCE(SUM(api_call_count), 0) AS api_calls,
                       COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd,
                       {sums}
                FROM sessions WHERE {where}
                """,
                params,
            ).fetchone()
            tokens = TokenCounts(
                input=row["input_tokens"],
                output=row["output_tokens"],
                cache_read=row["cache_read_tokens"],
                cache_write=row["cache_write_tokens"],
                reasoning=row["reasoning_tokens"],
            )
            by_model = []
            for model_row in conn.execute(
                f"""
                SELECT COALESCE(model, 'unknown') AS model,
                       COUNT(*) AS session_count,
                       COALESCE(SUM(estimated_cost_usd), 0) AS cost_usd,
                       {sums}
                FROM sessions WHERE {where}
                GROUP BY model ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
                LIMIT 8
                """,
                params,
            ):
                by_model.append(
                    {
                        "model": model_row["model"],
                        "session_count": model_row["session_count"],
                        "cost_usd": round(model_row["cost_usd"] or 0.0, 6),
                        "tokens": TokenCounts(
                            input=model_row["input_tokens"],
                            output=model_row["output_tokens"],
                            cache_read=model_row["cache_read_tokens"],
                            cache_write=model_row["cache_write_tokens"],
                            reasoning=model_row["reasoning_tokens"],
                        ).to_dict(),
                    }
                )
            by_day = [
                {"date": day_row["day"], "tokens": int(day_row["total"] or 0)}
                for day_row in conn.execute(
                    f"""
                    SELECT date(started_at, 'unixepoch', 'localtime') AS day,
                           SUM(input_tokens + output_tokens + cache_read_tokens
                               + cache_write_tokens) AS total
                    FROM sessions WHERE {where}
                    GROUP BY day ORDER BY day
                    """,
                    params,
                )
            ]
        return {
            "window_days": days,
            "session_count": int(row["session_count"]),
            "api_calls": int(row["api_calls"]),
            "tokens": tokens.to_dict(),
            "cost_usd": round(row["cost_usd"] or 0.0, 6),
            "by_model": by_model,
            "by_day": by_day,
        }

    # -- assembly ------------------------------------------------------------

    def snapshot(self, session_id: Optional[str] = None, window_days: int = 30) -> dict:
        """The full stats document, exactly as the watch receives it."""
        session = self.session_snapshot(session_id)
        return {
            "ts": time.time(),
            "session": session,
            "context": self.context_usage(session),
            "usage": self.totals_last_days(window_days, session_id=session.get("id") if session else session_id),
            "live": self.live.to_dict(),
            "hermes_home": str(hermes_home()),
        }


def find_recent_sessions(limit: int = 10, db_path: Optional[Path] = None) -> Iterable[dict]:
    """Recent sessions, newest first -- used by ``hermes-watch-bridge stats``."""
    with connect(db_path or state_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT id, title, source, model, started_at, ended_at, message_count,
                   input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd
            FROM sessions ORDER BY started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            yield dict(row)
