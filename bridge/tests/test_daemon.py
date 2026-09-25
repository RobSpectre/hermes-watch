"""End-to-end over real sockets: ingest -> hub -> watch socket -> decision back.

These tests bind ephemeral ports on loopback and speak the protocol the way the
Wear OS client does, so they fail if the wire contract drifts from the
documentation in ``docs/protocol.md``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from hermes_watch import protocol as p
from hermes_watch.daemon import build_app
from hermes_watch.hub import Hub, WatchConnection
from hermes_watch.stats import StatsEngine


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


TOKEN = "test-token"


@pytest.fixture
async def daemon(store: Path):
    """A real HTTP+WS server on a random loopback port.

    Started by hand rather than through aiohttp's pytest fixtures so the test
    exercises the same ``build_app`` the daemon serves in production, with no
    test-only routing.
    """
    hub = Hub(StatsEngine(db_path=store))
    hub.bridge_version = "0.1.0-test"
    port = free_port()
    runner = web.AppRunner(build_app(hub, ingest_token=TOKEN, watch_token=TOKEN), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    hub.loop = asyncio.get_running_loop()

    class Server:
        def __init__(self, port: int):
            self.port = port
            self.url = f"http://127.0.0.1:{port}"
            self.headers = {"Authorization": f"Bearer {TOKEN}"}

    try:
        yield hub, Server(port)
    finally:
        await runner.cleanup()


def watch_url(server) -> str:
    return f"http://127.0.0.1:{server.port}/v1/watch?token={TOKEN}&device=test-watch"


class WatchClient:
    """Minimal stand-in for the Kotlin client, used by every test below."""

    def __init__(self, session: aiohttp.ClientSession, ws):
        self.session = session
        self.ws = ws

    async def read(self, timeout: float = 5.0) -> dict:
        message = await asyncio.wait_for(self.ws.receive(), timeout=timeout)
        assert message.type is aiohttp.WSMsgType.TEXT, message
        return json.loads(message.data)

    async def read_until(self, kind: str, timeout: float = 5.0) -> dict:
        async def loop():
            while True:
                frame = await self.read()
                if frame.get("type") == kind:
                    return frame

        return await asyncio.wait_for(loop(), timeout=timeout)

    async def read_event(self, name: str, timeout: float = 5.0) -> dict:
        """Wait for one specific event, skipping other frames already in flight."""
        async def loop():
            while True:
                frame = await self.read()
                if frame.get("type") == p.S_EVENT and frame.get("event") == name:
                    return frame

        return await asyncio.wait_for(loop(), timeout=timeout)

    async def send(self, frame: dict) -> None:
        await self.ws.send_str(json.dumps(frame))


@pytest.fixture
async def watch(daemon):
    _, server = daemon
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(watch_url(server)) as ws:
            client = WatchClient(session, ws)
            hello = await client.read_until(p.S_HELLO)
            assert hello["bridge_version"] == "0.1.0-test"
            yield client


async def test_watch_receives_hello_then_a_stats_snapshot(daemon):
    hub, server = daemon
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(watch_url(server)) as ws:
            client = WatchClient(session, ws)
            hello = await client.read()
            snapshot = await client.read()
    assert hello["type"] == p.S_HELLO and hello["protocol"] == p.PROTOCOL_VERSION
    assert snapshot["type"] == p.S_SNAPSHOT
    assert snapshot["session"]["id"] == "sess_live"
    assert snapshot["usage"]["window_days"] == 30
    assert hub.agent_state == "idle"


async def test_bad_watch_token_is_refused(daemon):
    _, server = daemon
    async with aiohttp.ClientSession() as session:
        response = await session.get(f"http://127.0.0.1:{server.port}/v1/watch?token=wrong")
        assert response.status == 401


async def test_ingest_requires_the_token(daemon):
    _, server = daemon
    async with aiohttp.ClientSession() as session:
        unauthorised = await session.post(
            f"http://127.0.0.1:{server.port}/v1/event", json={"event": "turn.started"}
        )
        assert unauthorised.status == 401


async def test_turn_lifecycle_drives_agent_state_and_events(daemon, watch):
    hub, server = daemon
    async with aiohttp.ClientSession() as session:
        ok = await session.post(
            f"http://127.0.0.1:{server.port}/v1/event",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"event": p.E_TOOL_STARTED, "payload": {"tool_name": "terminal", "turn_id": "t1"}},
        )
        assert ok.status == 200
    stats = await watch.read_until(p.S_STATS)
    assert stats["live"]["agent_state"] == "tool"
    assert stats["live"]["last_tool"] == "terminal"

    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://127.0.0.1:{server.port}/v1/event",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"event": p.E_TURN_ENDED, "payload": {"turn_id": "t1", "reason": "completed"}},
        )
    end = await watch.read_event(p.E_TURN_ENDED)
    assert hub.agent_state == "idle"


async def test_exact_usage_from_the_plugin_reaches_the_watch(daemon, watch):
    hub, server = daemon
    async with aiohttp.ClientSession() as session:
        response = await session.post(
            f"http://127.0.0.1:{server.port}/v1/stats",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"api_duration": 2.5, "output_tokens": 250, "prompt_tokens": 20_000,
                  "context_window": 200_000, "model": "test/model-1"},
        )
        assert response.status == 200
    frame = await watch.read_until(p.S_STATS)
    assert frame["session"]["tok_per_s"]["live"] == 100.0
    assert frame["context"]["source"] == "live"
    assert frame["context"]["remaining_pct"] == 90.0
    assert hub.stats.live.output_tokens == 250


async def test_approval_round_trip_from_watch_to_long_poll(daemon, watch):
    """The path that matters: agent blocks, wrist answers, agent unblocks."""
    hub, server = daemon
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession() as session:
        opened = await session.post(
            f"http://127.0.0.1:{server.port}/v1/pending",
            headers=headers,
            json={
                "kind": "approval",
                "payload": {"command": "rm -rf /tmp/scratch", "description": "recursive delete"},
                "choices": ["once", "deny"],
                "timeout": 30,
                "id": "apv_test1",
            },
        )
        body = await opened.json()
        assert body["ok"] and body["delivered"] == 1  # one watch connected

        request_frame = await watch.read_event(p.E_APPROVAL_REQUESTED)
        assert request_frame["id"] == "apv_test1"
        assert request_frame["payload"]["choices"] == ["once", "deny"]
        assert hub.agent_state == "waiting_approval"

        # The plugin's transport is blocking on this long-poll.
        waiter = asyncio.create_task(
            session.get(f"http://127.0.0.1:{server.port}/v1/pending/apv_test1?timeout=20", headers=headers)
        )
        await asyncio.sleep(0.1)
        await watch.send({"v": 1, "type": "answer", "id": "apv_test1", "choice": "once"})
        response = await waiter
        answer = await response.json()

    assert answer["ok"] and answer["resolution"] == "once"
    assert answer["responder"] == "test-watch"
    assert hub.pending == {}


async def test_watch_cannot_return_a_choice_the_host_did_not_offer(daemon, watch):
    hub, server = daemon
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://127.0.0.1:{server.port}/v1/pending",
            headers=headers,
            json={"kind": "approval", "payload": {"command": "x"}, "choices": ["once", "deny"],
                  "timeout": 30, "id": "apv_test2"},
        )
        await watch.read_event(p.E_APPROVAL_REQUESTED)
        await watch.send({"v": 1, "type": "answer", "id": "apv_test2", "choice": "always"})
        error = await watch.read_until(p.S_ERROR)
        assert error["error"] == "stale_or_unknown_request"
    # Still pending and still unanswered: a scope escalation must not land.
    pending = hub.pending["apv_test2"]
    assert pending.resolution is None
    assert pending.future.done() is False


async def test_unanswered_approval_fails_closed_on_timeout(daemon, watch):
    hub, server = daemon
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://127.0.0.1:{server.port}/v1/pending",
            headers=headers,
            json={"kind": "approval", "payload": {"command": "x"}, "choices": ["once", "deny"],
                  "timeout": 0.1, "id": "apv_test3"},
        )
        await watch.read_event(p.E_APPROVAL_REQUESTED)
        await asyncio.sleep(0.25)
        await hub.sweep_expired()
        response = await session.get(
            f"http://127.0.0.1:{server.port}/v1/pending/apv_test3?timeout=1", headers=headers
        )
        answer = await response.json()
    assert answer["ok"] is True
    assert answer["resolution"] is None        # None == deny at the plugin
    assert answer["responder"] == "timeout"
    # The watch must stop showing a prompt that nobody can answer any more.
    assert hub.agent_state == "thinking"


async def test_long_poll_reports_not_answered_instead_of_hanging(daemon, watch):
    _, server = daemon
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://127.0.0.1:{server.port}/v1/pending",
            headers=headers,
            json={"kind": "approval", "payload": {"command": "x"}, "choices": ["once"],
                  "timeout": 30, "id": "apv_test4"},
        )
        response = await session.get(
            f"http://127.0.0.1:{server.port}/v1/pending/apv_test4?timeout=0.2", headers=headers
        )
        body = await response.json()
    assert response.status == 202
    assert body["error"] == "not_answered"


async def test_question_hooks_are_notify_only_and_never_block_a_decision(daemon, watch):
    hub, server = daemon
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"http://127.0.0.1:{server.port}/v1/event",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"event": p.E_QUESTION_PENDING, "payload": {"question": "Deploy to prod?", "surface": "cli"}},
        )
    frame = await watch.read_event(p.E_QUESTION_PENDING)
    assert hub.agent_state == "waiting_input"


async def test_stats_endpoint_reports_health_for_the_cli(daemon):
    hub, server = daemon
    async with aiohttp.ClientSession() as session:
        response = await session.get(
            f"http://127.0.0.1:{server.port}/healthz", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        body = await response.json()
    assert body["ok"] is True
    assert body["bridge_version"] == "0.1.0-test"


async def test_disconnect_is_cleaned_up(daemon):
    hub, server = daemon
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(watch_url(server)) as ws:
            await asyncio.sleep(0.1)
            assert len(hub.connections) == 1
    await asyncio.sleep(0.1)
    assert hub.connections == {}
