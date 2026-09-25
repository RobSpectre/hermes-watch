"""HTTP + WebSocket daemon.

Two listeners, deliberately separate:

* ``ingest_port`` bound to loopback -- the Hermes plugin posts lifecycle events
  here. Never exposed off-host: those payloads contain commands and tool
  arguments.
* ``watch_port`` bound to the LAN -- Wear OS clients connect here. Requires the
  pairing token, but treat it as a trust boundary and run it on a network you
  control (see ``docs/setup.md``).

The daemon is a dumb translator: it validates and rates-limits transport
frames, then calls :class:`~hermes_watch.hub.Hub`. All attention state lives in
the hub so it survives an HTTP hiccup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import WSMsgType, web

from . import protocol as p
from .hub import Hub, WatchConnection

log = logging.getLogger("hermes_watch.daemon")

MAX_BODY_BYTES = 256 * 1024

#: Typed app key for the hub (aiohttp warns on bare string keys).
HUB_KEY: web.AppKey["Hub"] = web.AppKey("hub", object)


def _bearer(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.query.get("token", "").strip()


def _token_ok(request: web.Request, expected: str) -> bool:
    supplied = _bearer(request)
    if not expected:
        return True
    # Constant-time compare: the token gates command execution on the watch.
    import hmac

    return hmac.compare_digest(supplied, expected)


def build_app(
    hub: Hub, *, ingest_token: str = "", watch_token: str = "", enable_watch: bool = True
) -> web.Application:
    """Build the daemon's routes.

    ``enable_watch`` is False for the loopback ingest listener: the WebSocket
    endpoint is the one route that must not be reachable from a local process,
    because answering a pending approval is equivalent to typing "yes" at the
    approval prompt.
    """
    app: web.Application = web.Application(client_max_size=MAX_BODY_BYTES)
    app[HUB_KEY] = hub

    # ---- plugin ingest (loopback) -----------------------------------------

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "bridge_version": hub.bridge_version,
                "profile": hub.profile,
                "agent_state": hub.agent_state,
                "watches": len(hub.connections),
                "pending": len(hub.pending),
                "uptime_s": round(time.time() - hub.started_at, 1),
            }
        )

    async def post_event(request: web.Request) -> web.Response:
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        body = await request.json()
        name = str(body.get("event", "")).strip()
        if not name:
            raise web.HTTPBadRequest(text="missing event")
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="payload must be an object")
        if body.get("context_window"):
            hub.stats.live.context_window = int(body["context_window"])
        await hub.handle_hermes_event(name, payload)
        return web.json_response({"ok": True, "delivered": len(hub.connections)})

    async def post_stats(request: web.Request) -> web.Response:
        """Live measurements pushed by the plugin (exact, per-API-call)."""
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        body = await request.json()
        live = hub.stats.live
        for field in (
            "api_call_count", "api_duration", "output_tokens", "prompt_tokens",
            "reasoning_tokens", "approx_input_tokens", "context_window", "model",
            "provider", "base_url",
        ):
            if body.get(field) is not None:
                setattr(live, field, body[field])
        live.at = time.time()
        await hub.broadcast(p.envelope(p.S_STATS, **hub.stats_payload()))
        return web.json_response({"ok": True})

    async def get_stats(request: web.Request) -> web.Response:
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        session_id = request.query.get("session_id") or None
        return web.json_response(hub.stats_payload(session_id))

    # ---- approvals / questions --------------------------------------------

    async def post_pending(request: web.Request) -> web.Response:
        """Called by the plugin to open a blocking request. Returns its id."""
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        body = await request.json()
        kind = str(body.get("kind", "approval"))
        if kind not in ("approval", "question"):
            raise web.HTTPBadRequest(text="kind must be approval or question")
        choices = tuple(body.get("choices") or ())
        timeout = float(body.get("timeout") or 300.0)
        item = await hub.request_human_input(
            kind,
            body.get("payload") or {},
            choices=choices,
            timeout=timeout,
            pending_id=body.get("id"),
        )
        return web.json_response({"ok": True, "id": item.id, "deadline": item.deadline, "delivered": len(hub.connections)})

    async def wait_pending(request: web.Request) -> web.Response:
        """Long-poll for the answer. This is what the plugin's transport blocks on."""
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        pending_id = request.match_info["pending_id"]
        item = hub.pending.get(pending_id)
        if item is None:
            return web.json_response({"ok": False, "error": "unknown or already collected"}, status=404)
        wait = min(float(request.query.get("timeout") or item.remaining_s or 1.0), item.remaining_s)
        try:
            if wait > 0 and not item.future.done():
                await asyncio.wait_for(asyncio.shield(item.future), timeout=wait)
        except asyncio.TimeoutError:
            return web.json_response(
                {"ok": False, "error": "not_answered", "remaining_s": round(item.remaining_s, 1)},
                status=202,
            )
        result = {"ok": True, "id": item.id, "kind": item.kind, "resolution": item.resolution,
                  "responder": item.source}
        if request.query.get("collect", "1") != "0":
            hub.drop_request(pending_id)
        return web.json_response(result)

    async def resolve_pending(request: web.Request) -> web.Response:
        """Back-channel: the plugin reports a decision made elsewhere."""
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        body = await request.json()
        pending_id = body.get("id") or request.match_info.get("pending_id")
        ok = await hub.resolve(pending_id, body.get("resolution"), responder=str(body.get("responder") or "hermes"))
        return web.json_response({"ok": ok})

    # ---- watch socket ------------------------------------------------------

    async def watch_ws(request: web.Request) -> web.WebSocketResponse:
        if not _token_ok(request, watch_token):
            log.warning("rejected watch connection from %s: bad token", request.remote)
            raise web.HTTPUnauthorized(text="bad pairing token")
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=64 * 1024)
        await ws.prepare(request)
        watch_id = request.query.get("device") or f"watch-{request.remote}-{int(time.time())}"
        device = request.query.get("label") or watch_id

        async def send(frame: dict) -> None:
            await ws.send_str(json.dumps(frame, separators=(",", ":")))

        connection = WatchConnection(id=watch_id, device=device, send=send, remote=request.remote or "")
        await hub.add_watch(connection)
        try:
            async for message in ws:
                if message.type is WSMsgType.TEXT:
                    await _handle_client_frame(hub, connection, message.data, send)
                elif message.type is WSMsgType.ERROR:
                    log.info("watch %s socket error: %s", watch_id, ws.exception())
                    break
        finally:
            await hub.remove_watch(watch_id)
        return ws

    async def index(request: web.Request) -> web.Response:
        if not _token_ok(request, ingest_token):
            raise web.HTTPUnauthorized(text="bad ingest token")
        return web.json_response(hub.stats_payload())

    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/", index),
            web.post("/v1/event", post_event),
            web.post("/v1/stats", post_stats),
            web.get("/v1/stats", get_stats),
            web.post("/v1/pending", post_pending),
            web.get("/v1/pending/{pending_id}", wait_pending),
            web.post("/v1/pending/{pending_id}/resolve", resolve_pending),
        ]
    )
    if enable_watch:
        app.add_routes([web.get("/v1/watch", watch_ws)])
    return app


async def _handle_client_frame(hub: Hub, connection: WatchConnection, raw: str, send) -> None:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        await send(p.envelope(p.S_ERROR, error="malformed json"))
        return
    frame, error = p.parse_client_frame(decoded)
    if error is not None:
        await send(p.envelope(p.S_ERROR, error=error))
        return

    connection.last_seen = time.time()
    kind = frame["type"]
    if kind == p.C_HELLO:
        connection.device = str(frame.get("label") or connection.device)
        version = frame.get("v")
        if isinstance(version, int):
            connection.protocol = version
        await send(p.envelope(p.S_SNAPSHOT, **hub.stats_payload()))
    elif kind == p.C_ANSWER:
        accepted = await hub.resolve(frame["id"], frame.get("choice"), responder=connection.device or connection.id)
        if not accepted:
            await send(p.envelope(p.S_ERROR, error="stale_or_unknown_request", id=frame["id"]))
    elif kind == p.C_STATS_REQUEST:
        await send(p.envelope(p.S_SNAPSHOT, **hub.stats_payload(frame.get("session_id"))))
    elif kind == p.C_PONG:
        pass
    else:
        log.debug("ignoring unknown client frame type %r", kind)


async def serve(
    hub: Hub,
    *,
    watch_host: str = "0.0.0.0",
    watch_port: int = 8787,
    ingest_host: str = "127.0.0.1",
    ingest_port: int = 8788,
    ingest_token: str = "",
    watch_token: str = "",
) -> None:
    """Run both listeners plus the stats ticker until cancelled."""
    app_watch = build_app(hub, ingest_token=ingest_token, watch_token=watch_token, enable_watch=True)
    app_ingest = build_app(hub, ingest_token=ingest_token, watch_token=watch_token, enable_watch=False)

    runner_watch = web.AppRunner(app_watch, access_log=None)
    runner_ingest = web.AppRunner(app_ingest, access_log=None)
    await runner_watch.setup()
    await runner_ingest.setup()
    site_watch = web.TCPSite(runner_watch, watch_host, watch_port)
    site_ingest = web.TCPSite(runner_ingest, ingest_host, ingest_port)
    await site_watch.start()
    await site_ingest.start()
    hub.loop = asyncio.get_running_loop()
    log.info("watch websocket on ws://%s:%d/v1/watch", watch_host, watch_port)
    log.info("plugin ingest on http://%s:%d", ingest_host, ingest_port)
    ticker = asyncio.create_task(hub.stats_tick())
    try:
        await asyncio.Event().wait()
    finally:
        ticker.cancel()
        await runner_watch.cleanup()
        await runner_ingest.cleanup()
