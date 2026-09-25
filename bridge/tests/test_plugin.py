"""The Hermes-side plugin: fail closed, never block, never raise into the agent.

These tests exercise the plugin against a fake client, because the failure modes
that matter are the ones where the bridge is *absent* or *hostile* rather than
the happy path (covered end to end in ``test_daemon.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_watch import protocol as p
from hermes_watch.plugin import WatchBridgePlugin, WatchUnavailable, register


class FakeRequest:
    """Stand-in for ``hermes_cli.approval_transport.ApprovalRequest``."""

    def __init__(self, choices=("once", "session", "deny"), timeout=300.0):
        self.request_id = "abcdef0123456789"
        self.digest = "digest"
        self.command = "rm -rf /tmp/scratch"
        self.description = "recursive delete"
        self.pattern_key = "rm_rf"
        self.pattern_keys = ("rm_rf",)
        self.surface = "cli"
        self.timeout_seconds = timeout
        self.allowed_choices = choices
        self.tool_call_id = "call_1"

    def respond(self, choice: str):
        return ("decision", self.request_id, self.digest, choice)


class FakeClient:
    def __init__(self, *, reachable=True, delivered=1, answer=None):
        self.base_url = "http://127.0.0.1:8788"
        self.reachable = reachable
        self.delivered = delivered
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []

    def post_event(self, event, payload=None):
        self.calls.append(("event", event, payload))
        return self.reachable

    def post_stats(self, **fields):
        self.calls.append(("stats", fields))
        return self.reachable

    def open_request(self, kind, payload, *, choices=(), timeout=300.0, pending_id=None):
        self.calls.append(("open", {"kind": kind, "choices": choices, "id": pending_id}))
        if not self.reachable:
            return None
        return {"ok": True, "id": pending_id, "delivered": self.delivered}

    def wait_for_answer(self, pending_id, *, timeout):
        self.calls.append(("wait", pending_id))
        return self.answer

    def resolve(self, pending_id, resolution, *, responder="hermes"):
        self.calls.append(("resolve", {"id": pending_id, "resolution": resolution, "responder": responder}))
        return self.reachable

    def answered_by_watch(self, delivered):
        return delivered > 0


def plugin(client: FakeClient) -> WatchBridgePlugin:
    instance = WatchBridgePlugin.__new__(WatchBridgePlugin)  # bypass config/threading setup
    from hermes_watch.plugin import _Dispatcher

    instance.ctx = None
    instance.client = client
    instance.dispatcher = _Dispatcher()
    instance._context_windows = {}
    instance._turn_seen = {}

    class Config:
        approval_timeout_s = 300.0
        notify_questions = True

    instance.config = Config()
    return instance


def test_watch_answer_becomes_a_bound_decision():
    client = FakeClient(answer={"ok": True, "resolution": "once", "responder": "watch"})
    decision = plugin(client).present_approval(FakeRequest())
    assert decision == ("decision", "abcdef0123456789", "digest", "once")
    opened = next(call for call in client.calls if call[0] == "open")[1]
    # The watch is offered exactly the scopes Hermes offered, no more.
    assert opened["choices"] == ("once", "session", "deny")
    assert opened["id"] == "apv_abcdef0123456789"


def test_denial_on_the_watch_is_honoured():
    client = FakeClient(answer={"ok": True, "resolution": "deny", "responder": "watch"})
    assert plugin(client).present_approval(FakeRequest())[-1] == "deny"


def test_unreachable_bridge_fails_closed_and_defers_to_the_builtin_prompt():
    client = FakeClient(reachable=False)
    with pytest.raises(WatchUnavailable):
        plugin(client).present_approval(FakeRequest())


def test_no_connected_watch_fails_closed_without_waiting():
    client = FakeClient(delivered=0)
    with pytest.raises(WatchUnavailable, match="no watch"):
        plugin(client).present_approval(FakeRequest())
    # The request must be withdrawn so a later watch connection cannot answer a
    # question the human already saw somewhere else.
    assert any(call[0] == "resolve" and call[1]["resolution"] is None for call in client.calls)
    assert not any(call[0] == "wait" for call in client.calls)


def test_unanswered_request_becomes_a_denial_not_a_hang():
    client = FakeClient(answer={"ok": False, "error": "not_answered"})
    assert plugin(client).present_approval(FakeRequest())[-1] == "deny"


def test_a_resolution_outside_the_offered_scopes_is_downgraded_to_deny():
    client = FakeClient(answer={"ok": True, "resolution": "always", "responder": "watch"})
    # `always` was not offered (once/session/deny), so it must not be granted.
    assert plugin(client).present_approval(FakeRequest(choices=("once", "session", "deny")))[-1] == "deny"


def test_api_hooks_report_exact_measurements():
    client = FakeClient()
    instance = plugin(client)
    instance.on_pre_api_request(
        model="test/model-1", base_url="http://localhost", provider="test",
        api_call_count=2, approx_input_tokens=12_000, turn_id="t1", session_id="s1",
    )
    instance.on_post_api_request(
        model="test/model-1", response_model="test/model-1", api_call_count=2, api_duration=2.0,
        usage={"prompt_tokens": 12_100, "completion_tokens": 400, "reasoning_tokens": 90},
    )
    instance.dispatcher._queue.join()
    stats = [call[1] for call in client.calls if call[0] == "stats"]
    assert stats[0]["approx_input_tokens"] == 12_000
    assert stats[1]["api_duration"] == 2.0
    assert stats[1]["output_tokens"] == 400
    assert stats[1]["prompt_tokens"] == 12_100
    assert stats[1]["reasoning_tokens"] == 90
    assert any(call[0] == "event" and call[1] == p.E_TURN_STARTED for call in client.calls)


def test_usage_objects_are_read_from_dicts_or_dataclasses():
    class Usage:
        prompt_tokens = 500
        completion_tokens = 60

    client = FakeClient()
    instance = plugin(client)
    instance.on_post_api_request(model="m", api_duration=1.0, usage=Usage())
    instance.dispatcher._queue.join()
    stats = [call[1] for call in client.calls if call[0] == "stats"][-1]
    assert (stats["prompt_tokens"], stats["output_tokens"]) == (500, 60)


def test_clarify_notifies_the_watch_but_never_answers_for_the_human():
    client = FakeClient()
    instance = plugin(client)
    instance.on_pre_tool_call(tool_name="clarify", args={"question": "Deploy to prod?"}, turn_id="t1")
    instance.dispatcher._queue.join()
    events = [call[1] for call in client.calls if call[0] == "event"]
    assert p.E_QUESTION_PENDING in events
    assert not any(call[0] in ("open", "wait") for call in client.calls)


def test_question_text_is_clipped_before_it_leaves_the_process():
    client = FakeClient()
    instance = plugin(client)
    instance.on_pre_tool_call(tool_name="clarify", args={"question": "x" * 5000})
    instance.dispatcher._queue.join()
    payload = next(call[2] for call in client.calls if call[0] == "event" and call[1] == p.E_QUESTION_PENDING)
    assert len(payload["question"]) == 240


def test_tool_arguments_are_never_forwarded():
    client = FakeClient()
    instance = plugin(client)
    instance.on_pre_tool_call(
        tool_name="terminal", args={"command": "curl -H 'Authorization: Bearer hunter2'"},
        turn_id="t1", tool_call_id="c1",
    )
    instance.dispatcher._queue.join()
    for call in client.calls:
        assert "hunter2" not in repr(call)
    payload = next(call[2] for call in client.calls if call[0] == "event" and call[1] == p.E_TOOL_STARTED)
    # Names, ids and counts only -- never a tool's arguments.
    assert payload == {"tool_name": "terminal", "turn_id": "t1", "tool_call_id": "c1"}


def test_approval_resolution_on_another_surface_clears_the_watch_prompt():
    client = FakeClient()
    instance = plugin(client)
    from hermes_watch import plugin as plugin_module

    plugin_module._open_approvals["apv_x"] = "call_1"
    instance.on_post_approval_response(tool_call_id="call_1", choice="deny", surface="cli")
    instance.dispatcher._queue.join()
    assert any(
        call[0] == "resolve" and call[1]["id"] == "apv_x" and call[1]["resolution"] == "deny"
        for call in client.calls
    )


def test_register_never_raises_even_when_the_plugin_manager_is_hostile():
    class HostileContext:
        def register_hook(self, *args, **kwargs):
            raise RuntimeError("nope")

        def register_approval_transport(self, *args, **kwargs):
            raise RuntimeError("nope")

    register(HostileContext())  # must not propagate


def test_register_wires_the_expected_hooks():
    class RecordingContext:
        def __init__(self):
            self.hooks = []
            self.transports = []

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

        def register_approval_transport(self, name, callback):
            self.transports.append((name, callback))

    ctx = RecordingContext()
    register(ctx)
    names = {name for name, _ in ctx.hooks}
    assert {
        "pre_api_request", "post_api_request", "on_session_start", "on_session_end",
        "agent_loop_stopped", "post_tool_call", "pre_approval_request", "post_approval_response",
    } <= names
    assert ctx.transports[0][0] == "pixel-watch"
