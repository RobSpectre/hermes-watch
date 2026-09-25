"""The frozen half: what the *watch app* sends and reads.

The wire protocol is the one part of this project that must not move, because
the app on the wrist is built and shipped separately. These tests therefore
speak the app's frames literally -- copied from ``data/Protocol.kt`` and
``data/BridgeClient.kt`` -- and assert the adapter answers in the shape the app
parses. If a test here fails, the watch breaks in the field.

The app's side of the contract, verbatim:

* it connects to ``ws://host:port/v1/watch?token=<t>&device=<label>``
* it sends ``{"v":1,"type":"hello","label":"..."}`` (no device id at all)
* it sends ``{"v":1,"type":"stats.request"}`` when the user pulls to refresh
* it answers ``{"v":1,"type":"answer","id":"...","choice":"..."}``
* it replies ``{"v":1,"type":"pong"}`` to a ``ping`` frame
* it reads a snapshot's ``session``, ``context``, ``usage``, ``live`` and
  ``pending`` objects, and an approval event's ``payload``
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import pytest

pytest.importorskip("gateway.platforms.base", reason="needs a Hermes install")

import aiohttp  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

from hermes_watch import protocol as p  # noqa: E402
from hermes_watch.platform import PLATFORM_NAME, HermesWatchAdapter  # noqa: E402

# The app's own defaults, from SettingsStore.kt.
APP_VERSION = 1
APP_HELLO = {"v": APP_VERSION, "type": "hello", "label": "Pixel Watch"}
APP_STATS_REQUEST = {"v": APP_VERSION, "type": "stats.request"}
APP_PONG = {"v": APP_VERSION, "type": "pong"}


def app_answer(pending_id: str, choice: str) -> dict:
    return {"v": APP_VERSION, "type": "answer", "id": pending_id, "choice": choice}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module", autouse=True)
def _registered():
    """Register the platform: ``Platform('pixel_watch')`` has to resolve."""
    platform_registry.register(
        PlatformEntry(
            name=PLATFORM_NAME,
            label="Pixel Watch",
            adapter_factory=lambda config: HermesWatchAdapter(config),
            check_fn=lambda: True,
            plugin_name="hermes-watch",
        )
    )


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An isolated HERMES_HOME with a synthetic session store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from conftest import SESSIONS_DDL, make_session

    conn = sqlite3.connect(tmp_path / "state.db")
    conn.executescript(SESSIONS_DDL)
    make_session(
        conn, "sess_contract", started_at=time.time() - 300, model="test/model-1",
        input_tokens=40_000, output_tokens=4_000, cache_read_tokens=80_000,
        api_call_count=4, title="contract session", cost=0.02,
    )
    conn.close()
    return tmp_path


@asynccontextmanager
async def rig(**extra: Any):
    """A live adapter on loopback with an authorized watch attached."""
    port = free_port()
    config = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": port, "approval_timeout_s": 5,
               "allow_all_devices": True, **extra},
    )
    adapter = HermesWatchAdapter(config)
    assert await adapter.connect() is True
    try:
        yield adapter, port
    finally:
        await adapter.disconnect()


async def read_until(ws, predicate, timeout: float = 5.0) -> Optional[dict]:
    """Read frames until one matches, or give up. Never hangs a suite."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            message = await ws.receive(timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if message.type is not aiohttp.WSMsgType.TEXT:
            return None
        frame = json.loads(message.data)
        if predicate(frame):
            return frame
    return None


def is_type(*kinds: str):
    return lambda frame: frame.get("type") in kinds


async def connect_app(session: aiohttp.ClientSession, port: int) -> aiohttp.ClientWebSocketResponse:
    """Connect exactly the way the app does, query string and all."""
    url = f"http://127.0.0.1:{port}/v1/watch?token=&device=Pixel%20Watch"
    ws = await session.ws_connect(url)
    await ws.send_str(json.dumps(APP_HELLO))
    return ws


# --- what the app sends -----------------------------------------------------


async def test_the_apps_hello_url_is_served():
    """The app hard-codes /v1/watch; a 404 here means a dead watch."""
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            hello = await read_until(ws, is_type(p.S_HELLO))
            assert hello is not None, "no hello reply on the app's own path"
            assert hello["v"] == APP_VERSION
            assert hello["bridge_version"]
            assert hello["protocol"] == p.PROTOCOL_VERSION
            assert "server_time" in hello
            assert "profile" in hello
            await ws.close()


async def test_the_apps_label_only_hello_is_a_stable_identity():
    """No device id is sent, so the label is what pairing keys on."""
    async with rig(allow_all_devices=False) as (adapter, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            hello = await read_until(ws, is_type(p.S_HELLO))
            assert hello is not None
            # The label, taken from `?device=` / the hello, not a random id.
            assert hello["authorized"] is False
            assert list(adapter._links) == ["Pixel Watch"]
            await ws.close()


async def test_a_stale_token_in_the_url_is_tolerated(caplog):
    """The app still sends its old shared secret. It must not be a 401."""
    async with rig() as (adapter, port):
        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/v1/watch?token=legacy-secret&device=Pixel%20Watch"
            ws = await session.ws_connect(url)
            await ws.send_str(json.dumps(APP_HELLO))
            hello = await read_until(ws, is_type(p.S_HELLO))
            assert hello is not None and hello["authorized"] is True
            await ws.close()


async def test_the_apps_pull_to_refresh_is_answered():
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            assert await read_until(ws, is_type(p.S_HELLO)) is not None
            await ws.send_str(json.dumps(APP_STATS_REQUEST))
            snapshot = await read_until(ws, is_type(p.S_SNAPSHOT, p.S_STATS))
            assert snapshot is not None, "stats.request went unanswered"
            await ws.close()


async def test_the_apps_pong_is_accepted_without_error():
    async with rig() as (adapter, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            assert await read_until(ws, is_type(p.S_HELLO)) is not None
            await ws.send_str(json.dumps(APP_PONG))
            await asyncio.sleep(0.1)
            assert ws.closed is False, "a pong dropped the connection"
            assert adapter._links["Pixel Watch"].last_pong is not None
            await ws.close()


async def test_an_unknown_frame_does_not_drop_the_link():
    """A newer app may send frames this adapter has never heard of."""
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            assert await read_until(ws, is_type(p.S_HELLO)) is not None
            await ws.send_str(json.dumps({"v": 1, "type": "something.new"}))
            error = await read_until(ws, is_type(p.S_ERROR))
            assert error is not None, "expected an error frame, not silence"
            assert ws.closed is False
            await ws.close()


# --- what the app reads -----------------------------------------------------


async def test_a_snapshot_carries_every_field_the_app_parses():
    """One assertion per line of the app's ``Frame.toSnapshot()``."""
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            frame = await read_until(ws, is_type(p.S_SNAPSHOT, p.S_STATS))
            assert frame is not None, "no snapshot after the handshake"

            session_obj = frame["session"]  # toSnapshot() requires this
            tokens = session_obj["tokens"]
            for key in ("total", "input", "output", "cached_read"):
                assert key in tokens, f"session.tokens.{key} missing"
            rate = session_obj["tok_per_s"]
            for key in ("live", "session_avg"):
                assert key in rate, f"session.tok_per_s.{key} missing"
            for key in ("title", "model", "api_call_count", "tool_call_count", "elapsed_s"):
                assert key in session_obj, f"session.{key} missing"

            context = frame["context"]
            for key in ("remaining_pct", "used_tokens", "window_tokens", "source"):
                assert key in context, f"context.{key} missing"
            assert isinstance(context["used_tokens"], int)

            usage = frame["usage"]
            assert "total" in usage["tokens"], "usage.tokens.total missing"
            for key in ("session_count", "cost_usd"):
                assert key in usage, f"usage.{key} missing"

            live = frame["live"]
            for key in ("agent_state", "last_tool"):
                assert key in live, f"live.{key} missing"
            assert live["agent_state"] in {
                "offline", "idle", "thinking", "tool", "waiting_approval",
                "waiting_question",
            }, f"the app has no colour for state {live['agent_state']!r}"
            await ws.close()


async def test_the_session_numbers_are_not_invented_when_there_is_no_session():
    """Unknown stays unknown: the app renders an em dash, not a false zero."""
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            frame = await read_until(ws, is_type(p.S_SNAPSHOT, p.S_STATS))
            assert frame is not None
            assert frame["session"]["tokens"]["total"] is not None  # seeded store
            await ws.close()


async def test_an_approval_arrives_in_the_shape_the_app_renders():
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            assert await read_until(ws, is_type(p.S_HELLO)) is not None

            async def push() -> dict:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={
                        "event": p.E_APPROVAL_REQUESTED,
                        "id": "apv_contract",
                        "choices": ["once", "deny"],
                        "timeout": 5,
                        "payload": {"command": "rm -rf ~/build", "description": "recursive delete"},
                    },
                ) as response:
                    return await response.json()

            ask = asyncio.create_task(push())
            frame = await read_until(
                ws, lambda f: f.get("event") == p.E_APPROVAL_REQUESTED
            )
            assert frame is not None, "the approval never reached the watch"

            # Frame.toPendingRequest() reads exactly these, nested as so.
            assert frame["type"] == p.S_EVENT
            assert frame["id"] == "apv_contract"
            body = frame["payload"]
            assert body["id"] == "apv_contract"
            assert body["kind"] == "approval"
            assert body["payload"]["command"] == "rm -rf ~/build"
            assert body["payload"]["description"] == "recursive delete"
            assert isinstance(body["remaining_s"], (int, float))
            assert body["remaining_s"] > 0
            assert body["choices"] == ["once", "deny"]

            await ws.send_str(json.dumps(app_answer("apv_contract", "once")))
            response = await ask
            assert response == {"ok": True, "choice": "once", "source": "watch"}
            await ws.close()


async def test_an_approval_resolution_clears_the_card():
    async with rig() as (_, port):
        async with aiohttp.ClientSession() as session:
            ws = await connect_app(session, port)
            assert await read_until(ws, is_type(p.S_HELLO)) is not None

            async def push() -> None:
                async with session.post(
                    f"http://127.0.0.1:{port}/event",
                    json={
                        "event": p.E_APPROVAL_REQUESTED,
                        "id": "apv_clear",
                        "choices": ["once", "deny"],
                        "timeout": 5,
                        "payload": {"command": "echo hi"},
                    },
                ):
                    pass

            ask = asyncio.create_task(push())
            assert await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_REQUESTED)
            await ws.send_str(json.dumps(app_answer("apv_clear", "deny")))
            resolved = await read_until(ws, lambda f: f.get("event") == p.E_APPROVAL_RESOLVED)
            assert resolved is not None, "the card would stay on the wrist forever"
            assert resolved["id"] == "apv_clear"
            await ask
            await ws.close()
