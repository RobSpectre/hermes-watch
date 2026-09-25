"""End-to-end tests for the gateway platform adapter.

These drive a real adapter over real loopback sockets with a real aiohttp
WebSocket client: the point is to prove the wire behaviour, not to mock it.

They need a Hermes installation to import ``BasePlatformAdapter``, so they skip
where Hermes is not present (the repo's own CI) and run for real everywhere it
is. That is a deliberate trade: the adapter is meaningless without Hermes, so a
green run of these on a machine that has neither tests nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

import pytest

pytest.importorskip("gateway.platforms.base", reason="needs a Hermes installation")
aiohttp = pytest.importorskip("aiohttp")

from aiohttp import WSMsgType  # noqa: E402
from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

from hermes_watch import protocol as p  # noqa: E402
from hermes_watch.live import bus  # noqa: E402
from hermes_watch.platform import PLATFORM_NAME, HermesWatchAdapter  # noqa: E402


# --- harness ----------------------------------------------------------------


def free_port() -> int:
    """A port nobody is using right now.

    Distinct per test so the adapter's machine-global listener lock is per-test
    too, and so a leaked listener from a failed test cannot make the next one
    silently pass.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_config(port: int, **extra: Any) -> PlatformConfig:
    settings = {"host": "127.0.0.1", "port": port, "approval_timeout_s": 5.0}
    settings.update(extra)
    return PlatformConfig(enabled=True, extra=settings)


@pytest.fixture(autouse=True)
def registered_platform():
    """``Platform('pixel_watch')`` only resolves for a registered plugin."""
    entry = PlatformEntry(
        name=PLATFORM_NAME,
        label="Pixel Watch",
        adapter_factory=HermesWatchAdapter,
        check_fn=lambda: True,
        plugin_name="hermes-watch",
    )
    platform_registry.register(entry)
    yield


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep the pairing store, runtime status and session store inside the test.

    The store is synthetic (the same DDL the unit tests use), so nothing here
    reads a real transcript.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import sqlite3

    from conftest import SESSIONS_DDL, make_session

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(SESSIONS_DDL)
    make_session(
        conn, "sess_watch", started_at=time.time() - 120, model="test/model-1",
        input_tokens=30_000, output_tokens=3_000, cache_read_tokens=60_000,
        api_call_count=3, title="watch test session", cost=0.01,
    )
    conn.close()
    return tmp_path


@asynccontextmanager
async def rig(home, **extra: Any) -> AsyncIterator[tuple[HermesWatchAdapter, int]]:
    port = free_port()
    adapter = HermesWatchAdapter(make_config(port, **extra))
    assert await adapter.connect() is True, "adapter failed to bind its listener"
    try:
        yield adapter, port
    finally:
        await adapter.disconnect()


@asynccontextmanager
async def watch(port: int) -> AsyncIterator["aiohttp.ClientWebSocketResponse"]:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"http://127.0.0.1:{port}/watch") as ws:
            yield ws


async def read_until(
    ws: "aiohttp.ClientWebSocketResponse",
    predicate: Callable[[dict], bool],
    *,
    timeout: float = 5.0,
) -> Optional[dict]:
    """Next frame matching ``predicate``, or None on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if message.type is not WSMsgType.TEXT:
            continue
        frame = json.loads(message.data)
        if predicate(frame):
            return frame


async def say_hello(ws, device_id: str = "dev-1", label: str = "Pixel Watch") -> dict:
    await ws.send_str(json.dumps({"v": 1, "type": p.C_HELLO, "device_id": device_id, "label": label}))
    frame = await read_until(ws, lambda f: f.get("type") == p.S_HELLO)
    assert frame is not None, "no server hello"
    return frame


def approve_device(home, device_id: str = "dev-1") -> None:
    """Write the pairing store entry ``hermes pairing approve`` would."""
    directory = home / "platforms" / "pairing"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{PLATFORM_NAME}-approved.json").write_text(
        json.dumps({device_id: {"user_name": device_id, "approved_at": time.time()}}),
        encoding="utf-8",
    )


class Collector:
    """Stands in for the gateway runner's message handler."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def handle(self, event) -> None:
        self.events.append(event)


# --- lifecycle --------------------------------------------------------------


async def test_connect_binds_and_healthz_answers():
    async with rig(None) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/healthz") as response:
                assert response.status == 200
                body = await response.json()
        assert body["ok"] is True
        assert body["platform"] == PLATFORM_NAME
        assert body["watches"] == []
        assert adapter.send_path_degraded is True, "nothing connected yet"


async def test_disconnect_releases_the_port():
    port = free_port()
    adapter = HermesWatchAdapter(make_config(port))
    assert await adapter.connect() is True
    await adapter.disconnect()
    # Rebinding the same port proves the listener and its socket are really gone:
    # a leaked socket here is what makes a gateway restart fail with EADDRINUSE.
    second = HermesWatchAdapter(make_config(port))
    assert await second.connect() is True
    await second.disconnect()


async def test_bind_conflict_is_fatal_and_not_retried(monkeypatch):
    held_port = free_port()
    holder = HermesWatchAdapter(make_config(held_port))
    assert await holder.connect() is True
    try:
        clash = HermesWatchAdapter(make_config(held_port))
        assert await clash.connect() is False
        # Retrying forever on a taken port leaks fds and spins the reconnect
        # watcher, so this must be a non-retryable fatal error.
        assert clash.has_fatal_error is True
    finally:
        await holder.disconnect()


# --- authorization ----------------------------------------------------------


async def test_unpaired_watch_gets_no_snapshot(isolated_home):
    async with rig(isolated_home) as (adapter, port):
        async with watch(port) as ws:
            hello = await say_hello(ws)
            assert hello["authorized"] is False
            assert hello["bridge_version"]
            # An unpaired device must not see session data at all.
            assert await read_until(ws, lambda f: f.get("type") == p.S_SNAPSHOT, timeout=1.5) is None


async def test_paired_watch_is_authorized_and_gets_a_snapshot(isolated_home):
    approve_device(isolated_home, "dev-1")
    async with rig(isolated_home) as (adapter, port):
        async with watch(port) as ws:
            hello = await say_hello(ws, device_id="dev-1")
            assert hello["authorized"] is True
            snapshot = await read_until(ws, lambda f: f.get("type") == p.S_SNAPSHOT)
            assert snapshot is not None
            assert snapshot["live"]["agent_state"] == "idle"
            assert snapshot["pending"] == []
            assert adapter.send_path_degraded is False


async def test_allow_all_devices_bypasses_pairing(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with watch(port) as ws:
            hello = await say_hello(ws, device_id="unpaired-device")
            assert hello["authorized"] is True


async def test_unpaired_watch_cannot_resolve_an_approval(isolated_home):
    """The socket is unauthenticated, so it must not be able to approve anything."""
    async with rig(isolated_home) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws, device_id="dev-1")
            pending = adapter._register(
                kind="approval", payload={"command": "rm -rf /"}, choices=("once", "deny"),
                timeout=5.0, session_key="sess", future=asyncio.get_running_loop().create_future(),
            )
            await ws.send_str(json.dumps({"v": 1, "type": p.C_ANSWER, "id": pending.id, "choice": "once"}))
            await asyncio.sleep(0.2)
            assert pending.future is not None and pending.future.done() is False, (
                "an unpaired socket resolved an approval"
            )
            assert await adapter.resolve(pending.id, "once") is True


# --- inbound watch messages -------------------------------------------------


async def test_watch_text_reaches_handle_message(isolated_home):
    """A watch message is a normal platform inbound, which is what makes
    Hermes' own pairing flow able to answer it."""
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        collector = Collector()
        adapter.set_message_handler(collector.handle)
        async with watch(port) as ws:
            await say_hello(ws, device_id="dev-7", label="Rob's Watch")
            await ws.send_str(json.dumps({"v": 1, "type": p.C_TEXT, "text": "hello"}))
            for _ in range(50):
                if collector.events:
                    break
                await asyncio.sleep(0.05)
        assert collector.events, "watch message never reached the ingress"
        event = collector.events[0]
        assert event.text == "hello"
        assert event.source.chat_id == "dev-7"
        assert event.source.user_id == "dev-7"
        assert event.source.chat_type == "dm"
        assert event.source.platform.value == PLATFORM_NAME


async def test_empty_text_frame_is_rejected(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        collector = Collector()
        adapter.set_message_handler(collector.handle)
        async with watch(port) as ws:
            await say_hello(ws)
            await ws.send_str(json.dumps({"v": 1, "type": p.C_TEXT, "text": "   "}))
            error = await read_until(ws, lambda f: f.get("type") == p.S_ERROR)
            assert error is not None
        assert collector.events == []


# --- approvals from another process (a CLI session) -------------------------


async def test_out_of_process_approval_round_trip(isolated_home):
    """The CLI path: a plugin in another process posts a prompt and blocks on
    the response until the watch answers."""
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/watch") as ws:
                await say_hello(ws)
                ask = asyncio.create_task(
                    session.post(
                        f"http://127.0.0.1:{port}/event",
                        json={
                            "event": p.E_APPROVAL_REQUESTED,
                            "payload": {"command": "rm -rf ~/build/cache", "description": "recursive delete"},
                            "choices": ["once", "deny"],
                            "timeout": 5.0,
                            "id": "apv_test",
                        },
                    )
                )
                frame = await read_until(ws, lambda f: f.get("type") == p.S_EVENT
                                         and f.get("event") == p.E_APPROVAL_REQUESTED)
                assert frame is not None, "the watch was never asked"
                assert frame["id"] == "apv_test"
                assert frame["payload"]["choices"] == ["once", "deny"]
                assert frame["payload"]["payload"]["command"] == "rm -rf ~/build/cache"
                assert adapter._live_frame()["agent_state"] == "waiting_approval"

                await ws.send_str(json.dumps({"v": 1, "type": p.C_ANSWER, "id": "apv_test", "choice": "deny"}))
                response = await ask
                assert response.status == 200
                body = await response.json()
                assert body == {"ok": True, "choice": "deny", "source": "watch"}
                resolved = await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_RESOLVED)
                assert resolved is not None


async def test_approval_off_menu_choice_is_refused(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/watch") as ws:
                await say_hello(ws)
                ask = asyncio.create_task(
                    session.post(
                        f"http://127.0.0.1:{port}/event",
                        json={
                            "event": p.E_APPROVAL_REQUESTED,
                            "payload": {"command": "x"},
                            "choices": ["once", "deny"],
                            "timeout": 0.8,
                            "id": "apv_off",
                        },
                    )
                )
                await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_REQUESTED)
                # `always` was never offered, so honouring it would widen a
                # one-shot approval into a permanent one.
                await ws.send_str(json.dumps({"v": 1, "type": p.C_ANSWER, "id": "apv_off", "choice": "always"}))
                response = await ask
                body = await response.json()
        assert body["choice"] is None, "an off-menu choice was accepted"


async def test_unanswered_approval_times_out_failing_closed(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/watch") as ws:
                await say_hello(ws)
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={
                        "event": p.E_APPROVAL_REQUESTED,
                        "payload": {"command": "x"},
                        "choices": ["once", "deny"],
                        "timeout": 0.5,
                        "id": "apv_slow",
                    },
                ) as response:
                    assert response.status == 200
                    body = await response.json()
        assert body == {"ok": True, "choice": None, "source": "timeout"}
        assert adapter._pending == {}, "a timed-out prompt was left pending"


async def test_approval_with_no_watch_connected_is_refused(isolated_home):
    """No watch means fail closed: the caller must fall back to its own prompt
    rather than block on a wrist that is not there."""
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/event",
                json={
                    "event": p.E_APPROVAL_REQUESTED,
                    "payload": {"command": "x"},
                    "choices": ["once", "deny"],
                    "timeout": 2.0,
                },
            ) as response:
                assert response.status == 503
        assert adapter._pending == {}


async def test_answer_elsewhere_is_carried_back_to_the_blocked_caller(isolated_home):
    """An approval answered in the terminal must reach the parked caller with
    the choice the human actually made, not as a timeout."""
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/watch") as ws:
                await say_hello(ws)
                ask = asyncio.create_task(
                    session.post(
                        f"http://127.0.0.1:{port}/event",
                        json={
                            "event": p.E_APPROVAL_REQUESTED,
                            "payload": {"command": "x"},
                            "choices": ["once", "deny"],
                            "timeout": 5.0,
                            "id": "apv_else",
                        },
                    )
                )
                await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_REQUESTED)
                await session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={"event": p.E_APPROVAL_RESOLVED, "payload": {"id": "apv_else", "choice": "deny"}},
                )
                response = await ask
                body = await response.json()
        assert body == {"ok": True, "choice": "deny", "source": "watch"}


# --- approvals raised inside the gateway ------------------------------------


async def test_native_approval_prompt_uses_hermes_choice_set(isolated_home, monkeypatch):
    """An approval Hermes raised itself renders with the base class' labels and
    resolves through ``resolve_gateway_approval``."""
    resolved: list[tuple] = []

    def fake_resolve(session_key: str, choice: str, request_id: Optional[str] = None) -> int:
        resolved.append((session_key, choice, request_id))
        return 1

    monkeypatch.setattr("hermes_watch.platform._resolve_gateway_approval", fake_resolve)

    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        from gateway.platforms.base import ExecApprovalPrompt

        prompt = ExecApprovalPrompt(
            chat_id="dev-1",
            session_key="agent:main:cli",
            text="Approval needed: run `rm -rf ~/cache`?",
            actions=adapter._exec_approval_actions(allow_permanent=True, allow_session=True, smart_denied=False),
            command="rm -rf ~/cache",
            description="recursive delete",
            smart_denied=False,
            metadata={"request_id": "req-42"},
        )
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/watch") as ws:
                await say_hello(ws)
                # Through the base class' own entry point, metadata included:
                # that is how Hermes passes the request id a late answer must
                # be matched against.
                result = await adapter.send_exec_approval(
                    "dev-1", prompt.command, prompt.session_key,
                    description=prompt.description, metadata=prompt.metadata,
                )
                assert result.success is True
                frame = await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_REQUESTED)
                assert frame is not None
                choices = frame["payload"]["choices"]
                assert choices == ["once", "session", "always", "deny"]
                await ws.send_str(json.dumps({"v": 1, "type": p.C_ANSWER, "id": frame["id"], "choice": "session"}))
                for _ in range(50):
                    if resolved:
                        break
                    await asyncio.sleep(0.05)
        assert resolved == [("agent:main:cli", "session", "req-42")]


async def test_question_prompt_carries_choices_and_can_be_retired(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            result = await adapter.send_clarify(
                chat_id="dev-1", question="Deploy to which region?", choices=["us-east", "eu-west"],
                clarify_id="clr_1", session_key="agent:main:cli",
            )
            assert result.success is True
            frame = await read_until(ws, lambda f: f.get("event") == p.E_QUESTION_PENDING)
            assert frame is not None
            assert frame["payload"]["choices"] == ["us-east", "eu-west"]
            assert adapter._live_frame()["agent_state"] == "waiting_input"
            await adapter.retire_clarify_card("clr_1", notice="answered in terminal")
            retired = await read_until(ws, lambda f: f.get("event") == p.E_QUESTION_RESOLVED)
            assert retired is not None
            assert adapter._pending == {}


# --- stats and state --------------------------------------------------------


async def test_observations_drive_agent_state_and_are_pushed(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            bus.observe(p.E_TOOL_STARTED, {"tool_name": "terminal"})
            assert adapter._live_frame()["agent_state"] == "tool"
            assert adapter._live_frame()["last_tool"] == "terminal"
            stats = await read_until(ws, lambda f: f.get("type") == p.S_STATS)
            assert stats is not None
            assert stats["live"]["agent_state"] == "tool"
            bus.observe(p.E_TURN_ENDED, {})
            assert adapter._live_frame()["agent_state"] == "idle"


async def test_api_measurements_reach_the_stats_payload(isolated_home):
    """Live tok/s must come from a measured call, not from a guess."""
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            bus.observe("api_request", {"output_tokens": 400, "api_duration": 2.0, "prompt_tokens": 12_000})
            await asyncio.sleep(0.05)
            snapshot = await asyncio.to_thread(adapter._build_stats)
        session = snapshot["session"]
        observation = adapter._observation()
        assert observation.output_tokens == 400
        assert observation.api_duration == 2.0
        # 400 tokens in a 2.0 s provider call is 200 tok/s. The number comes
        # from the measurement, not from wall-clock session time.
        assert session is not None
        assert session["tok_per_s"]["live"] == 200.0
        assert session["tok_per_s"]["live_source"] == "api_call"
        # Context usage prefers the exact prompt size over any estimate.
        assert snapshot["context"]["used_tokens"] == 12_000
        assert snapshot["context"]["source"] == "live"


async def test_send_reports_failure_with_no_watch(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        result = await adapter.send("dev-1", "hello")
        assert result.success is False
        assert result.retryable is True


async def test_send_delivers_to_a_connected_watch(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            result = await adapter.send("dev-1", "pairing code: ABCD1234")
            assert result.success is True
            frame = await read_until(ws, lambda f: f.get("event") == "message")
            assert frame is not None
            assert frame["payload"]["text"] == "pairing code: ABCD1234"


async def test_the_pairing_code_reaches_an_unpaired_watch(isolated_home):
    """The one message an unpaired watch must get. Refusing it broke onboarding.

    Hermes' unauthorized-DM path answers a stranger with a pairing code by
    calling ``send(source.chat_id, reply)`` -- to a device that is, by
    definition, not authorized yet.
    """
    async with rig(isolated_home, allow_all_devices=False) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)  # hello itself resolves to authorized=False
            assert adapter._links["dev-1"].authorized is False
            result = await adapter.send("dev-1", "Pairing code: ABCD1234")
            assert result.success is True, "an unpaired watch was never told its code"
            frame = await read_until(ws, lambda f: f.get("event") == "message")
            assert frame is not None
            assert frame["payload"]["text"] == "Pairing code: ABCD1234"


async def test_a_revoked_watch_stops_receiving_messages(isolated_home, monkeypatch):
    """`revoke` has to mean something: paired once, then un-approved."""
    from hermes_watch import platform as platform_module

    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            assert (await adapter.send("dev-1", "before")).success is True

            # The owner revokes the device: the store now says no, and the
            # explicit allow-all escape hatch is switched off too.
            monkeypatch.setattr(platform_module.HermesWatchAdapter, "_allow_all", False, raising=False)
            adapter._allow_all = False
            adapter._allowed_devices = set()
            adapter._approval_cache.clear()
            monkeypatch.setattr(adapter, "_read_pairing_store", lambda device_id: False)

            # Authorization is re-read per delivery, so this bites immediately
            # rather than at the next reconnect.
            result = await adapter.send("dev-1", "after")
            assert result.success is False, "a revoked watch still received a message"


async def test_a_broadcast_never_reaches_an_unpaired_watch(isolated_home):
    """`send` with no chat id is for the household, not for strangers."""
    async with rig(isolated_home, allow_all_devices=False) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            result = await adapter.send("", "gateway notice")
            assert result.success is False
            assert await read_until(ws, lambda f: f.get("event") == "message", timeout=0.5) is None


async def test_a_resolved_approval_returns_to_the_state_it_interrupted(isolated_home):
    """A wait is a parenthesis. Restoring "thinking" made an idle gateway lie.

    Seen live: an approval posted to /event with no agent behind it left the
    listener reporting ``state: thinking`` forever.
    """
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        assert adapter._state == "idle"

        async def push() -> dict:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={"event": p.E_APPROVAL_REQUESTED, "id": "apv_state",
                          "choices": ["once", "deny"], "timeout": 10,
                          "payload": {"command": "echo hi"}},
                ) as response:
                    return await response.json()

        async with watch(port) as ws:
            await say_hello(ws)
            # The watch has to be connected before the prompt is posted: with
            # nowhere to deliver it, the adapter refuses the request outright.
            ask = asyncio.create_task(push())
            await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_REQUESTED)
            assert adapter._state == "waiting_approval"
            await ws.send_str(json.dumps({"v": 1, "type": "answer",
                                          "id": "apv_state", "choice": "once"}))
            body = await ask
        assert body["choice"] == "once"
        assert adapter._state == "idle", "an idle gateway claimed to be working"


async def test_a_resolved_approval_returns_to_thinking_during_a_turn(isolated_home):
    """The same rule in the case it was written for: mid-turn, resume working."""
    async with rig(isolated_home, allow_all_devices=True) as (adapter, port):
        bus.observe(p.E_TURN_STARTED, {"turn_id": "t1"})
        assert adapter._state == "thinking"
        bus.observe(p.E_APPROVAL_REQUESTED, {"id": "apv_mid", "command": "echo hi"})
        assert adapter._state == "waiting_approval"
        bus.observe(p.E_APPROVAL_RESOLVED, {"id": "apv_mid", "choice": "once"})
        assert adapter._state == "thinking"
        bus.observe(p.E_TURN_ENDED, {"turn_id": "t1"})
        assert adapter._state == "idle"


async def test_the_watch_is_told_when_the_state_changes(isolated_home):
    """The state rides the stats frame, so a change must be pushed."""
    async with rig(isolated_home, allow_all_devices=True) as (_, port):
        async with watch(port) as ws:
            await say_hello(ws)
            bus.observe(p.E_TURN_STARTED, {"turn_id": "t1"})
            frame = await read_until(ws, lambda f: (f.get("live") or {}).get("agent_state") == "thinking")
            assert frame is not None, "the watch never learned the agent was working"


async def test_notify_pushes_text_to_the_watch(isolated_home):
    """The host-side push path: anything on this machine can reach the wrist."""
    async with rig(isolated_home, allow_all_devices=True) as (_, port):
        async with watch(port) as ws:
            await say_hello(ws)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/notify",
                    json={"text": "Hermes: build finished", "device": "dev-1"},
                ) as response:
                    assert response.status == 200
                    body = await response.json()
                    assert body["ok"] is True
                    assert body["message_id"]
            frame = await read_until(ws, lambda f: f.get("event") == "message")
            assert frame is not None, "the notification never reached the watch"
            assert frame["payload"]["text"] == "Hermes: build finished"


async def test_a_prompt_that_times_out_still_returns_to_idle(isolated_home):
    """The wait ends when the deadline does, even with no answer.

    Seen live: after the host's approval timeout fired, healthz reported
    waiting_approval with an empty pending list -- the listener claiming the
    agent was blocked when nothing was pending at all.
    """
    async with rig(isolated_home, allow_all_devices=True, approval_timeout_s=1) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)                            # registered, so it can be asked
            bus.observe(p.E_TURN_STARTED, {"turn_id": "t1"})   # a working state to restore
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={"event": p.E_APPROVAL_REQUESTED, "id": "apv_slow",
                          "choices": ["once", "deny"], "timeout": 0.4,
                          "payload": {"command": "echo hi"}},
                ) as response:
                    body = await response.json()
        assert body.get("choice") is None
        assert body.get("source") == "timeout"
        assert adapter._pending == {}
        assert adapter._state == "thinking", "an unanswered prompt left the wait state behind"


async def test_turn_finished_stays_silent_by_default(isolated_home):
    """Off unless asked: one buzz per turn is noise in a busy gateway."""
    async with rig(isolated_home, allow_all_devices=True) as (_, port):
        async with watch(port) as ws:
            await say_hello(ws)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={"event": p.E_LOOP_STOPPED, "payload": {"reason": "completed"}},
                ) as response:
                    assert response.status == 200
            assert await read_until(ws, lambda f: f.get("event") == p.E_LOOP_STOPPED, timeout=0.6) is None


async def test_turn_finished_notifies_when_opted_in(isolated_home):
    async with rig(isolated_home, allow_all_devices=True, notify_turn_finished=True) as (_, port):
        async with watch(port) as ws:
            await say_hello(ws)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={"event": p.E_LOOP_STOPPED, "payload": {"reason": "completed"}},
                ) as response:
                    assert response.status == 200
            frame = await read_until(ws, lambda f: f.get("event") == p.E_LOOP_STOPPED)
            assert frame is not None, "an opted-in host never told the watch the turn ended"
            assert frame["payload"]["reason"] == "completed"


async def test_an_opted_in_host_still_reports_state(isolated_home):
    """The notification is additional: the state line keeps working either way."""
    async with rig(isolated_home, allow_all_devices=True, notify_turn_finished=True) as (adapter, port):
        async with watch(port) as ws:
            await say_hello(ws)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={"event": p.E_LOOP_STOPPED, "payload": {}},
                ) as response:
                    assert response.status == 200
            await read_until(ws, lambda f: f.get("event") == p.E_LOOP_STOPPED)
            assert adapter._state == "idle"


async def test_notify_says_so_when_no_watch_is_listening(isolated_home):
    """503, not a cheerful 200: a notification must never be claimed undelivered."""
    async with rig(isolated_home, allow_all_devices=True) as (_, port):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/notify", json={"text": "anyone there?"}
            ) as response:
                assert response.status == 503
                body = await response.json()
                assert body["ok"] is False
                assert "no watch" in body["error"]


async def test_notify_refuses_empty_text(isolated_home):
    async with rig(isolated_home, allow_all_devices=True) as (_, port):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/notify", json={"text": "   "}
            ) as response:
                assert response.status == 400


async def test_notify_needs_the_shared_secret_when_one_is_configured(isolated_home):
    async with rig(isolated_home, allow_all_devices=True, ingest_token="s3cret") as (_, port):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/notify", json={"text": "hi"}
            ) as response:
                assert response.status == 403
            async with session.post(
                f"http://127.0.0.1:{port}/notify", json={"text": "hi", "token": "s3cret"}
            ) as response:
                assert response.status in (200, 503)  # authorized, watch may be absent


async def test_ingest_token_is_enforced_when_configured(isolated_home):
    async with rig(isolated_home, allow_all_devices=True, ingest_token="s3cret") as (adapter, port):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/event",
                json={"event": p.E_TURN_STARTED, "payload": {}, "token": "wrong"},
            ) as response:
                assert response.status == 403
            async with session.post(
                f"http://127.0.0.1:{port}/event",
                json={"event": p.E_TURN_STARTED, "payload": {}, "token": "s3cret"},
            ) as response:
                assert response.status == 200
