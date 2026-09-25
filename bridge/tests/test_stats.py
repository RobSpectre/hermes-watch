"""Statistics: real numbers, correct labels, no invented values."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_watch.stats import LiveObservation, StatsEngine, TokenCounts


def engine(store: Path, live: LiveObservation | None = None) -> StatsEngine:
    return StatsEngine(db_path=store, live=live or LiveObservation())


def test_token_counts_treat_cached_and_reasoning_separately():
    tokens = TokenCounts(input=1000, output=200, cache_read=5000, cache_write=50, reasoning=42)
    payload = tokens.to_dict()
    # Cached reads are additive: providers do not fold them into `input`, so
    # dropping them would understate a long session by an order of magnitude.
    assert payload == {
        "input": 1000, "output": 200, "cached_read": 5000, "cached_write": 50,
        "reasoning": 42, "total": 6250,
    }


def test_current_session_is_the_live_one(store: Path):
    snapshot = engine(store).session_snapshot()
    assert snapshot["id"] == "sess_live"
    assert snapshot["ended"] is False
    assert snapshot["tokens"]["total"] == 30_000 + 3_000 + 60_000


def test_throughput_is_labelled_by_how_it_was_measured(store: Path):
    # No live observation: only the wall-clock average exists, and it says so.
    cold = engine(store).session_snapshot("sess_live")
    assert cold["tok_per_s"]["live"] is None
    assert cold["tok_per_s"]["session_avg_source"] == "wall_clock_includes_tool_time"

    measured = engine(store, LiveObservation(api_duration=2.0, output_tokens=180))
    hot = measured.session_snapshot("sess_live")
    assert hot["tok_per_s"]["live"] == 90.0
    assert hot["tok_per_s"]["live_source"] == "api_call"


def test_context_usage_prefers_the_exact_prompt_size(store: Path):
    live = LiveObservation(prompt_tokens=12_345, context_window=100_000)
    context = engine(store, live).context_usage(engine(store, live).session_snapshot("sess_live"))
    assert context["source"] == "live"
    assert context["used_tokens"] == 12_345
    assert context["remaining_tokens"] == 87_655
    assert context["remaining_pct"] == 87.7


def test_context_usage_falls_back_to_a_labelled_estimate(store: Path):
    live = LiveObservation(context_window=100_000)
    stats = engine(store, live)
    context = stats.context_usage(stats.session_snapshot("sess_live"))
    # (30_000 input + 60_000 cached) / 4 api calls
    assert context["source"] == "db_estimate"
    assert context["used_tokens"] == 22_500


def test_unknown_context_window_reports_null_not_a_guess(store: Path):
    stats = engine(store, LiveObservation(prompt_tokens=5_000))
    context = stats.context_usage(stats.session_snapshot("sess_live"))
    assert context["window_tokens"] is None
    assert context["remaining_pct"] is None
    assert context["remaining_tokens"] is None


def test_approximate_pre_call_estimate_is_distinguished_from_exact_usage(store: Path):
    stats = engine(store, LiveObservation(approx_input_tokens=4_321, context_window=100_000))
    context = stats.context_usage(stats.session_snapshot("sess_live"))
    assert context["source"] == "live_approximate"


def test_rolling_window_excludes_the_current_session_and_old_rows(store: Path):
    stats = engine(store)
    session = stats.session_snapshot("sess_live")
    usage = stats.totals_last_days(30, session_id=session["id"])
    # sess_old_a is inside the window, sess_old_b is 40 days out, sess_live is
    # the session the user is looking at.
    assert usage["session_count"] == 1
    assert usage["tokens"]["total"] == 11_000
    assert [model["model"] for model in usage["by_model"]] == ["test/model-1"]
    assert len(usage["by_day"]) == 1


def test_rolling_window_includes_everything_without_a_session_exclusion(store: Path):
    usage = engine(store).totals_last_days(30)
    assert usage["session_count"] == 2
    assert usage["tokens"]["total"] == 11_000 + 93_000


def test_snapshot_is_json_serializable(store: Path):
    import json

    payload = engine(store, LiveObservation(prompt_tokens=100, context_window=1000)).snapshot()
    assert json.loads(json.dumps(payload))["session"]["id"] == "sess_live"


def test_session_snapshot_returns_none_for_an_empty_store(tmp_path: Path):
    import sqlite3

    from conftest import SESSIONS_DDL

    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SESSIONS_DDL)
    conn.commit()
    conn.close()
    assert StatsEngine(db_path=db_path).session_snapshot() is None


def test_elapsed_time_is_reported_for_a_live_session(store: Path):
    snapshot = StatsEngine(db_path=store).session_snapshot("sess_live")
    assert 590 <= snapshot["elapsed_s"] <= 620
