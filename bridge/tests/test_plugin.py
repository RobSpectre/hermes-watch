"""The plugin half: hooks in, watch observations out, approvals routed.

Two failure modes matter here, and both are absences rather than errors:

* **Nothing blocks the agent.** Hook callbacks run inside a live turn, so every
  notification is queued, and a transport that throws is somebody else's turn
  to handle.
* **Nothing answers for the human.** When no watch answers, the transport raises
  so Hermes falls back to its own prompt, instead of leaving a dangerous command
  silently unanswered.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Optional

import pytest

from hermes_watch import protocol as p
from hermes_watch.live import bus
from hermes_watch.plugin import (
    TRANSPORT_NAME,
    WatchPlugin,
    WatchUnavailable,
    _clip,
    _usage_field,
    register,
)


class _ImmediateDispatcher:
    """Runs tasks inline so tests do not race a background thread.

    Like the real worker it contains a failing task rather than letting it
    escape, but it remembers the failure so a test can assert the task really
    did blow up instead of quietly doing nothing.
    """

    def __init__(self) -> None:
        self.errors: list[BaseException] = []

    def submit(self, task) -> None:
        try:
            task()
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)


class FakeRequest:
    """Stands in for Hermes' ``ApprovalRequest``."""

    def __init__(self, choices=("once", "session", "deny"), timeout=300.0, request_id="req-1"):
        self.command = "rm -rf ~/build/cache"
        self.description = "recursive delete"
        self.pattern_key = "rm:-rf"
        self.surface = "cli"
        self.tool_call_id = "call_1"
        self.request_id = request_id
        self.timeout_seconds = timeout
        self.allowed_choices = choices
        self.responses: list[str] = []

    def respond(self, choice: str):
        self.responses.append(choice)
        return {"choice": choice}


class FakeClient:
    """Stands in for :class:`hermes_watch.client.AdapterClient`."""

    def __init__(self, *, reachable=True, answer: Optional[str] = None, post_ok=True):
        self.reachable = reachable
        self.answer = answer
        self.post_ok = post_ok
        self.posts: list[tuple[str, dict]] = []
        self.asks: list[dict] = []

    def post_event(self, event: str, payload: Optional[dict] = None) -> bool:
        self.posts.append((event, payload or {}))
        return self.post_ok

    def health(self):
        return {"ok": True} if self.reachable else None

    def ask(self, kind, payload, *, choices=(), timeout=300.0, pending_id=None) -> Optional[str]:
        self.asks.append({"kind": kind, "payload": payload, "choices": choices,
                          "timeout": timeout, "pending_id": pending_id})
        return self.answer


class FakeContext:
    """Stands in for ``PluginContext``, recording what got registered."""

    def __init__(self):
        self.hooks: list[tuple[str, Any]] = []
        self.transports: list[tuple[str, Any]] = []
        self.platforms: list[dict] = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_approval_transport(self, name, callback):
        self.transports.append((name, callback))

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


@pytest.fixture
def plugin(monkeypatch) -> WatchPlugin:
    """A plugin wired to a fake client, with nothing subscribed to the bus."""
    monkeypatch.setattr(bus, "_handlers", [])
    instance = WatchPlugin(ctx=FakeContext())
    instance.client = FakeClient()
    instance.dispatcher = _ImmediateDispatcher()
    return instance


@pytest.fixture
def offline(monkeypatch):
    """Builds a plugin whose watch path is down."""

    def make(**client_kwargs) -> WatchPlugin:
        monkeypatch.setattr(bus, "_handlers", [])
        instance = WatchPlugin(ctx=FakeContext())
        instance.client = FakeClient(**client_kwargs)
        instance.dispatcher = _ImmediateDispatcher()
        return instance

    return make


# --- approvals --------------------------------------------------------------


def test_watch_answer_becomes_the_transport_decision(plugin):
    plugin.client = FakeClient(answer="once")
    assert plugin.present_approval(FakeRequest()) == {"choice": "once"}


def test_the_choice_the_watch_made_is_the_one_sent_back(plugin):
    plugin.client = FakeClient(answer="session")
    request = FakeRequest(choices=("once", "session", "always", "deny"))
    plugin.present_approval(request)
    assert request.responses == ["session"]


def test_the_watch_is_offered_exactly_the_scopes_hermes_offered(plugin):
    plugin.client = FakeClient(answer="once")
    plugin.present_approval(FakeRequest(choices=("once", "deny")))
    assert plugin.client.asks[0]["choices"] == ("once", "deny")
    assert plugin.client.asks[0]["pending_id"] == "apv_req-1"


def test_unreachable_watch_defers_to_hermes_own_prompt(offline):
    """A watch that is off must not become a silent denial."""
    with pytest.raises(WatchUnavailable):
        offline(reachable=False, post_ok=False).present_approval(FakeRequest())


def test_no_answer_defers_to_hermes_own_prompt(plugin):
    plugin.client = FakeClient(answer=None)
    with pytest.raises(WatchUnavailable):
        plugin.present_approval(FakeRequest())


def test_an_off_menu_answer_defers_rather_than_widening_scope(plugin):
    """``always`` was never offered; honouring it would make a one-shot permanent."""
    plugin.client = FakeClient(answer="always")
    with pytest.raises(WatchUnavailable):
        plugin.present_approval(FakeRequest(choices=("once", "deny")))


def test_the_approval_never_leaks_session_keys_to_the_watch(plugin):
    plugin.client = FakeClient(answer="once")
    plugin.present_approval(FakeRequest())
    payload = plugin.client.asks[0]["payload"]
    assert "session_key" not in payload
    assert payload["command"] == "rm -rf ~/build/cache"


def test_a_client_that_throws_still_yields_the_documented_signal(plugin, monkeypatch):
    """Any transport fault has to look like ``no watch``, not like a crash."""

    def explode(*args, **kwargs):
        raise RuntimeError("socket exploded")

    plugin.client = FakeClient()
    monkeypatch.setattr(plugin.client, "ask", explode)
    with pytest.raises(WatchUnavailable):
        plugin.present_approval(FakeRequest())


# --- observation routing ----------------------------------------------------


def test_observations_go_to_the_bus_when_an_adapter_is_colocated(plugin):
    seen: list[tuple[str, dict]] = []
    bus.subscribe(lambda name, payload: seen.append((name, payload)))
    try:
        plugin.observe(p.E_TURN_STARTED, turn_id="t1")
    finally:
        bus._handlers.clear()
    assert seen == [(p.E_TURN_STARTED, {"turn_id": "t1"})]
    # Co-located: nothing went over the wire, because it did not have to.
    assert plugin.client.posts == []


def test_observations_are_posted_when_no_adapter_is_here(plugin):
    plugin.observe(p.E_TOOL_STARTED, tool_name="terminal")
    assert plugin.client.posts == [(p.E_TOOL_STARTED, {"tool_name": "terminal"})]


def test_none_fields_are_dropped_before_they_leave_the_process(plugin):
    """A None would erase a good earlier reading at the far end."""
    plugin.observe("api_request", output_tokens=10, api_duration=None)
    assert plugin.client.posts == [("api_request", {"output_tokens": 10})]


def test_a_failing_post_is_not_raised(offline):
    offline(post_ok=False).observe(p.E_TURN_STARTED, turn_id="t1")  # must not raise


# --- hooks ------------------------------------------------------------------


def test_api_hooks_report_exact_measurements(plugin):
    plugin.on_pre_api_request(
        model="test/model-1", base_url="http://localhost", provider="test",
        turn_id="t1", session_id="s1", approx_input_tokens=12_000,
    )
    plugin.on_post_api_request(
        model="test/model-1", api_call_count=1, api_duration=2.0,
        usage={"prompt_tokens": 12_100, "completion_tokens": 400, "reasoning_tokens": 90},
    )
    names = [name for name, _ in plugin.client.posts]
    assert p.E_TURN_STARTED in names
    measured = [payload for name, payload in plugin.client.posts if name == "api_request"][-1]
    assert (measured["output_tokens"], measured["api_duration"]) == (400, 2.0)
    assert measured["prompt_tokens"] == 12_100
    assert measured["reasoning_tokens"] == 90


def test_a_turn_start_is_reported_once_per_turn(plugin):
    for _ in range(3):
        plugin.on_pre_api_request(model="m", base_url="", provider="", turn_id="t1")
    assert [name for name, _ in plugin.client.posts].count(p.E_TURN_STARTED) == 1


def test_usage_objects_are_read_from_dicts_or_dataclasses():
    class Usage:
        completion_tokens = 42

    assert _usage_field({"completion_tokens": 7}, "completion_tokens") == 7
    assert _usage_field(Usage(), "completion_tokens") == 42
    assert _usage_field(None, "completion_tokens") is None


def test_clarify_notifies_the_watch_without_answering_for_the_human(plugin):
    plugin.on_pre_tool_call(tool_name="clarify", args={"question": "Deploy where?"}, turn_id="t1")
    pending = [payload for name, payload in plugin.client.posts if name == p.E_QUESTION_PENDING]
    assert pending and pending[0]["question"] == "Deploy where?"
    # The CLI has no inbound transport for a question, so nothing was asked back.
    assert plugin.client.asks == []


def test_question_text_is_clipped_before_it_leaves_the_process(plugin):
    plugin.on_pre_tool_call(tool_name="clarify", args={"question": "x" * 5_000})
    payload = [payload for name, payload in plugin.client.posts if name == p.E_QUESTION_PENDING][0]
    assert len(payload["question"]) == 240


def test_tool_arguments_are_never_forwarded(plugin):
    plugin.on_pre_tool_call(
        tool_name="terminal",
        args={"command": "curl -H 'Authorization: Bearer hunter2'"},
        turn_id="t1", tool_call_id="c1",
    )
    for _, payload in plugin.client.posts:
        assert "hunter2" not in repr(payload)
    started = [payload for name, payload in plugin.client.posts if name == p.E_TOOL_STARTED][0]
    # Names, ids and counts only -- never a tool's arguments.
    assert started == {"tool_name": "terminal", "turn_id": "t1", "tool_call_id": "c1"}


def test_a_hook_never_raises_when_the_client_is_broken(plugin, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin.client, "post_event", explode)
    # The hook returns and the agent's turn is untouched; the failure stayed in
    # the dispatcher where it started.
    plugin.on_session_start(session_id="s1")
    assert len(plugin.dispatcher.errors) == 1
    assert isinstance(plugin.dispatcher.errors[0], RuntimeError)


def test_an_approval_settled_elsewhere_clears_the_watch_card(plugin):
    plugin._open_approvals["apv_x"] = "call_1"
    plugin.on_post_approval_response(tool_call_id="call_1", choice="deny", surface="cli")
    resolved = [payload for name, payload in plugin.client.posts if name == p.E_APPROVAL_RESOLVED]
    assert resolved and resolved[0]["id"] == "apv_x" and resolved[0]["choice"] == "deny"
    assert plugin._open_approvals == {}


# --- registration -----------------------------------------------------------


def test_register_wires_hooks_transport_and_platform():
    ctx = FakeContext()
    register(ctx)
    assert {
        "pre_api_request", "post_api_request", "on_session_start", "on_session_end",
        "on_session_finalize", "agent_loop_stopped", "pre_tool_call", "post_tool_call",
        "pre_approval_request", "post_approval_response",
    } <= {name for name, _ in ctx.hooks}
    assert [name for name, _ in ctx.transports] == [TRANSPORT_NAME]

    assert len(ctx.platforms) == 1
    platform = ctx.platforms[0]
    assert platform["name"] == "pixel_watch"
    assert platform["allowed_users_env"] == "HERMES_WATCH_ALLOWED_USERS"
    assert "wear os watch" in platform["platform_hint"].lower()
    assert callable(platform["adapter_factory"])
    assert callable(platform["check_fn"])


@pytest.mark.skipif(
    importlib.util.find_spec("gateway.platforms.base") is None,
    reason="needs a Hermes installation",
)
def test_the_registered_factory_builds_a_real_adapter():
    from gateway.config import PlatformConfig

    ctx = FakeContext()
    register(ctx)
    adapter = ctx.platforms[0]["adapter_factory"](
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0})
    )
    assert adapter.platform.value == "pixel_watch"
    assert adapter.name == "Pixel Watch"


def test_register_never_raises_on_a_hostile_context():
    class Hostile:
        def register_hook(self, *args, **kwargs):
            raise RuntimeError("no hooks for you")

        def register_approval_transport(self, *args, **kwargs):
            raise RuntimeError("no transport for you")

        def register_platform(self, **kwargs):
            raise RuntimeError("no platform for you")

    register(Hostile())  # must return quietly, not take the CLI down


def test_register_survives_a_context_with_no_platform_support():
    """A CLI process may expose a context that cannot register platforms."""

    class ContextWithoutPlatforms(FakeContext):
        def register_platform(self, **kwargs):
            raise AttributeError("register_platform")

    ctx = ContextWithoutPlatforms()
    register(ctx)
    assert ctx.hooks, "hooks must still be registered"


def test_clip_leaves_short_text_alone():
    assert _clip("short") == "short"
    assert len(_clip("x" * 500, 100)) == 100
