"""The watch as a first-class Hermes gateway platform.

This is the whole watch-facing half of the project. Instead of a bespoke daemon
that reimplemented approval prompts, timeouts and pairing, the watch is a
platform adapter: Hermes' own machinery renders the approval buttons, enforces
the timeout, authorizes the user, and routes sessions.

What the adapter owns, and why nothing smaller works:

* **The socket.** A watch is a device with a live readout, not a chat that
  happens to be short. Hermes' chat model has no place to put "tokens per
  second", so the adapter hosts the WebSocket and pushes snapshots itself.
* **Authorization of that socket.** The gateway's own allow-list runs *after*
  the adapter (``enforces_own_access_policy`` is False by default), so the
  socket must authenticate its peer. It does that with Hermes' pairing store:
  a device connects with a stable id, sends one message, and the gateway's
  standard unauthorized-DM path replies with a pairing code that the owner
  approves on the host with ``hermes pairing approve``. No shared secret to
  copy onto a watch keyboard.
* **The out-of-process bridge.** A CLI session has no adapter in its process,
  so its plugin half posts approval prompts here over loopback and *blocks on
  the HTTP response* until the watch answers. The request is the pending state;
  there is no registry to sweep.

Everything else -- prompt wording, the choice set, timeouts, session routing,
delivery retries -- comes from the base class.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _datetime
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

from aiohttp import WSMsgType, web

from gateway.config import Platform
from gateway.platforms._shared import extra_or_secret, get_scoped_secret
from gateway.platforms.base import (
    BasePlatformAdapter,
    ExecApprovalPrompt,
    SendResult,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.tcp_site import start_tcp_site

from . import protocol as p
from .live import bus
from .settings import (
    DEFAULT_WATCH_PORT,
    PLATFORM_LABEL,
    PLATFORM_NAME,
    load_config,
)
from .stats import LiveObservation, StatsEngine

log = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = DEFAULT_WATCH_PORT
DEFAULT_APPROVAL_TIMEOUT_S = 300.0

#: Stats cadence: cheap while idle, brisk while there is something to watch.
IDLE_TICK_S = 5.0
BUSY_TICK_S = 1.5
#: Keepalive; the watch shows "reconnecting" after ~2 missed pings.
PING_INTERVAL_S = 30.0
#: How long an approved-unpaired decision is cached before re-reading the store.
PAIRING_CACHE_S = 5.0

#: States that mean "the agent is blocked on a human". They are entered from a
#: working state and left back to it, which is why the pair is named once here.
WAITING_STATES = ("waiting_approval", "waiting_input")

MAX_BODY_BYTES = 256 * 1024
MAX_FRAME_BYTES = 64 * 1024


def _version() -> str:
    with contextlib.suppress(Exception):
        from . import __version__

        return str(__version__)
    return "0.0.0"


@dataclass
class _Pending:
    """One request the agent is blocked on, keyed by the id the watch sees."""

    id: str
    kind: str  # "approval" | "question"
    created_at: float
    deadline: float
    choices: tuple[str, ...]
    payload: dict
    #: Set for approvals that arrive over HTTP from an out-of-process plugin:
    #: the waiting HTTP request's future, resolved by the watch's answer.
    future: Optional["asyncio.Future[Optional[str]]"] = None
    #: Set for approvals Hermes raised inside the gateway process.
    session_key: Optional[str] = None
    request_id: Optional[str] = None
    #: Set for clarify prompts Hermes raised inside the gateway process.
    clarify_id: Optional[str] = None

    @property
    def remaining_s(self) -> float:
        return max(self.deadline - time.time(), 0.0)

    def to_dict(self) -> dict[str, Any]:
        """The snapshot shape the watch's pending list expects."""
        return {
            "id": self.id,
            "kind": self.kind,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "remaining_s": round(self.remaining_s, 1),
            "choices": list(self.choices),
            "payload": self.payload,
        }


class _WatchLink:
    """One WebSocket to one watch."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.device_id: Optional[str] = None
        self.label: Optional[str] = None
        self.authorized = False
        self.connected_at = time.time()
        self.last_pong = time.time()

    @property
    def chat_id(self) -> str:
        return self.device_id or "unknown"

    async def send(self, frame: dict) -> bool:
        try:
            await self.ws.send_str(json.dumps(frame, separators=(",", ":")))
            return True
        except Exception:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "authorized": self.authorized,
            "connected_s": round(time.time() - self.connected_at, 1),
        }


class HermesWatchAdapter(BasePlatformAdapter):
    """Hosts the watch WebSocket and pushes stats to it."""

    def __init__(self, config) -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", None) or {}
        fallback = load_config()
        self._host = str(extra_or_secret(extra, "host", "HERMES_WATCH_HOST", fallback.watch_host))
        self._port = int(extra_or_secret(extra, "port", "HERMES_WATCH_WATCH_PORT", fallback.watch_port) or 0)
        self._approval_timeout = float(
            extra.get("approval_timeout_s", fallback.approval_timeout_s) or DEFAULT_APPROVAL_TIMEOUT_S
        )
        #: Escape hatches for setups that do not want Hermes' pairing flow (a
        #: dedicated watch VLAN, a CI rig). Pairing is the default, not the rule.
        self._allowed_devices = {str(d).strip() for d in (extra.get("allowed_devices") or []) if str(d).strip()}
        self._allow_all = bool(extra.get("allow_all_devices", False))
        self._ingest_token = str(extra.get("ingest_token", "") or "")

        self._links: dict[str, _WatchLink] = {}
        self._pending: dict[str, _Pending] = {}
        self._approval_cache: dict[str, tuple[float, bool]] = {}
        #: Devices that have been authorized at least once. A revoke is meant to
        #: stop delivery, and "never paired" has to stay distinguishable from
        #: "paired then revoked" for that to work -- see _delivery_targets.
        self._ever_authorized: set[str] = set()

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._unsubscribe = None

        self._state = "idle"
        self._state_changed_at = time.time()
        #: What the agent was doing before a prompt parked it. Restoring this is
        #: why an idle gateway stops claiming to be thinking.
        self._state_before_wait: Optional[str] = None
        self._last_tool: Optional[str] = None
        self._started_at = time.time()
        # DB access is blocking (sqlite3); it runs off-loop, and the engine is
        # built per call so a reconnected adapter never holds a stale handle.
        self._db_path = None
        self._stats = StatsEngine()

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Pixel Watch"

    @property
    def send_path_degraded(self) -> bool:
        """True while no authorized watch can receive anything.

        The base class reads this to publish ``retrying`` instead of
        ``connected``: with the watch is asleep or not yet paired, a delivery
        will not land, and saying so is more useful than claiming success.
        """
        return not any(link.authorized for link in self._links.values())

    # -- lifecycle -----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._acquire_platform_lock(
            PLATFORM_NAME, f"{self._host}:{self._port}", f"watch listener {self._host}:{self._port}"
        ):
            return False

        app = web.Application(client_max_size=MAX_BODY_BYTES)
        # The app's path is frozen at /v1/watch (it also carries a `token` and a
        # `device` query parameter, both of which this adapter handles). /watch
        # is an alias so scripts and tools/fake_watch.py can stay terse.
        app.router.add_get("/v1/watch", self._handle_watch)
        app.router.add_get("/watch", self._handle_watch)
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_post("/event", self._handle_event)

        self._runner = web.AppRunner(app)
        try:
            await self._runner.setup()
            self._site = await start_tcp_site(self._runner, self._host, self._port, log_tag=PLATFORM_NAME)
        except OSError as exc:
            # Someone else owns the port. Retrying forever leaks file
            # descriptors and spins the reconnect watcher, so this is fatal.
            await self._teardown_listener()
            self._set_fatal_error(
                "watch_port_in_use",
                f"Could not bind {self._host}:{self._port} for the watch listener ({exc}). "
                f"Change gateway.platforms.{PLATFORM_NAME}.extra.port.",
                retryable=False,
            )
            logger = log
            logger.error("[%s] bind failed: %s", PLATFORM_NAME, exc)
            return False

        bound = self._bound_port()
        self._mark_connected()
        self._unsubscribe = bus.subscribe(self._on_observation)
        self._stats_task = asyncio.create_task(self._stats_loop(), name="hermes-watch-stats")
        self._ping_task = asyncio.create_task(self._ping_loop(), name="hermes-watch-ping")
        self._wire_plugin_handlers(None)
        log.info("[%s] watch listener on %s:%s", PLATFORM_NAME, self._host, bound)
        return True

    async def disconnect(self) -> None:
        self._release_platform_lock()
        if self._unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe()
            self._unsubscribe = None
        for task in (self._stats_task, self._ping_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._stats_task = self._ping_task = None
        for link in list(self._links.values()):
            with contextlib.suppress(Exception):
                await link.ws.close()
        self._links.clear()
        # Any approval still waiting has no one left to answer it: settle it as
        # "no answer", which fails closed at the caller.
        for pending in list(self._pending.values()):
            if pending.future is not None and not pending.future.done():
                pending.future.set_result(None)
        self._pending.clear()
        await self._teardown_listener()
        self._mark_disconnected()
        log.info("[%s] disconnected", PLATFORM_NAME)

    async def _teardown_listener(self) -> None:
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        self._site = None

    def _bound_port(self) -> Optional[int]:
        with contextlib.suppress(Exception):
            if self._site is not None and self._site._server and self._site._server.sockets:
                return self._site._server.sockets[0].getsockname()[1]
        return self._port

    # -- outbound: messages, approvals, questions ----------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata=None) -> SendResult:
        """Push a message to the watch (or to every connected watch)."""
        frame = p.envelope(p.S_EVENT, event="message", payload={"text": str(content)})
        targets = await self._delivery_targets(chat_id)
        if not targets:
            return SendResult(success=False, error="no watch connected", retryable=True, error_kind="transient")
        delivered = 0
        for link in targets:
            if await link.send(frame):
                delivered += 1
        if not delivered:
            return SendResult(success=False, error="watch socket refused the frame", retryable=True, error_kind="transient")
        return SendResult(success=True, message_id=f"watch-{int(time.time() * 1000)}")

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Render Hermes' approval prompt with the watch's own buttons.

        The command text, the deadline wording and the choice set all come from
        the base class, so an approval looks and reads identically whether it
        was answered on the watch, in the terminal, or on Telegram.
        """
        pending = self._register(
            kind="approval",
            payload={"command": prompt.command, "description": prompt.description, "text": prompt.text},
            choices=tuple(prompt.choices),
            timeout=self._approval_timeout,
            session_key=prompt.session_key,
            request_id=self._request_id_from(prompt.metadata),
        )
        sent = await self._push_pending(pending)
        if not sent:
            self._pending.pop(pending.id, None)
            return SendResult(success=False, error="no watch connected to approve on", retryable=True,
                              error_kind="transient")
        return SendResult(success=True, message_id=pending.id)

    async def send_clarify(
        self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
        session_key: str, metadata: Optional[dict] = None,
    ) -> SendResult:
        """Ask a question with the watch's answer chips."""
        pending = self._register(
            kind="question",
            payload={"question": str(question), "text": str(question)},
            choices=tuple(str(c) for c in (choices or ())),
            timeout=self._approval_timeout,
            session_key=session_key,
            clarify_id=clarify_id,
        )
        sent = await self._push_pending(pending)
        if not sent:
            self._pending.pop(pending.id, None)
            return SendResult(success=False, error="no watch connected to answer on", retryable=True,
                              error_kind="transient")
        return SendResult(success=True, message_id=pending.id)

    async def retire_clarify_card(self, clarify_id: str, notice: Optional[str] = None) -> None:
        """Clear a question card the agent has moved past.

        Without this the watch keeps offering answers to a question that has
        already been settled some other way (a timeout, or free text typed into
        the terminal), which invites the user to answer into the void.
        """
        for pending_id, pending in list(self._pending.items()):
            if pending.kind == "question" and pending.clarify_id == clarify_id:
                self._pending.pop(pending_id, None)
                await self._broadcast(
                    p.event(p.E_QUESTION_RESOLVED, event_id=pending_id, resolution=None,
                            notice=notice or "no longer waiting")
                )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        link = self._links.get(chat_id)
        return {
            "name": (link.label if link else None) or chat_id,
            "type": "dm",
            "chat_id": chat_id,
        }

    # -- inbound: the watch socket -------------------------------------------

    async def _handle_watch(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=PING_INTERVAL_S * 2, max_msg_size=MAX_FRAME_BYTES)
        await ws.prepare(request)
        link = _WatchLink(ws)
        # The app's URL carries `device` (its label) and `token`. The label is
        # the useful part: it names the device before the hello arrives. The
        # token is a leftover from the pre-gateway design -- pairing is Hermes'
        # job now -- so it is accepted and ignored, with a nudge once per
        # connection if someone actually set one.
        link.device_id = str(request.query.get("device") or "").strip() or None
        if str(request.query.get("token") or "").strip():
            log.info(
                "[%s] the watch token is no longer used: pairing is handled by the gateway "
                "(run `hermes pairing list` to see devices, `hermes pairing approve %s <code>`)",
                PLATFORM_NAME,
                PLATFORM_NAME,
            )
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._on_client_frame(link, message.data)
                elif message.type == WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("[%s] watch socket ended: %s", PLATFORM_NAME, exc)
        finally:
            if link.device_id and self._links.get(link.device_id) is link:
                self._links.pop(link.device_id, None)
                log.info("[%s] watch %s disconnected", PLATFORM_NAME, link.device_id)
        return ws

    async def _on_client_frame(self, link: _WatchLink, raw: Any) -> None:
        try:
            decoded = json.loads(raw)
        except Exception:
            await link.send(p.envelope(p.S_ERROR, error="frame is not JSON"))
            return
        frame, error = p.parse_client_frame(decoded)
        if error is not None:
            await link.send(p.envelope(p.S_ERROR, error=error))
            return
        kind = frame.get("type")

        if kind == p.C_HELLO:
            await self._on_hello(link, frame)
        elif kind == p.C_TEXT:
            await self._on_text(link, frame)
        elif kind == p.C_ANSWER:
            await self._on_answer(link, frame)
        elif kind == p.C_STATS_REQUEST:
            if link.authorized:
                await self._send_snapshot(link)
        elif kind == p.C_PONG:
            link.last_pong = time.time()
        else:
            # Well-formed but unknown: answer with an error frame rather than
            # silence, so a newer app is diagnosable instead of mysteriously
            # hanging. The link stays up: the app may simply be ahead of us.
            log.debug("[%s] ignoring unsupported frame type %r", PLATFORM_NAME, kind)
            await link.send(p.envelope(p.S_ERROR, error=f"unsupported frame type: {kind}"))

    async def _on_hello(self, link: _WatchLink, frame: dict) -> None:
        device_id = (
            str(frame.get("device_id") or "").strip()
            or (link.device_id or "")
            # The app sends only a label (and the same value as `?device=`).
            # Making it a stable identity beats rejecting a client that cannot
            # be updated in step with the gateway.
            or str(frame.get("label") or "").strip()
            or f"watch-{secrets.token_hex(4)}"
        )
        link.device_id = device_id
        link.label = str(frame.get("label") or "").strip() or None
        link.authorized = await self._is_authorized(device_id)
        self._links[device_id] = link
        await link.send(
            p.envelope(
                p.S_HELLO,
                bridge_version=_version(),
                protocol=p.PROTOCOL_VERSION,
                server_time=time.time(),
                profile=str(getattr(self.config, "profile", "") or "default"),
                authorized=link.authorized,
            )
        )
        if link.authorized:
            await self._send_snapshot(link)
        log.info("[%s] watch %s connected (authorized=%s)", PLATFORM_NAME, device_id, link.authorized)

    async def _on_text(self, link: _WatchLink, frame: dict) -> None:
        """A watch message goes through the normal platform ingress.

        That is what makes pairing work: Hermes sees a DM from an unknown user
        and answers with a pairing code, which arrives here as a plain ``send``
        and shows up on the watch.
        """
        text = str(frame.get("text") or "").strip()
        if not text:
            return
        if link.device_id is None:
            device_id = str(frame.get("device_id") or "unknown-watch").strip()
            link.device_id = device_id
            link.authorized = await self._is_authorized(device_id)
        source = self.build_source(
            chat_id=link.chat_id,
            chat_name=link.label or link.chat_id,
            chat_type="dm",
            user_id=link.chat_id,
            user_name=link.label or link.chat_id,
        )
        await self.handle_message(
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=f"watch-{int(time.time() * 1000)}",
                timestamp=_datetime.datetime.now(),
            )
        )

    async def _on_answer(self, link: _WatchLink, frame: dict) -> None:
        if not link.authorized:
            # Fail closed: an unpaired socket does not get to approve anything.
            log.warning("[%s] ignoring answer from unpaired device %s", PLATFORM_NAME, link.device_id)
            return
        pending_id = str(frame.get("id") or "")
        choice = frame.get("choice")
        if not isinstance(choice, str) or not choice:
            return
        await self.resolve(pending_id, choice)

    # -- pending requests ----------------------------------------------------

    def _register(
        self,
        *,
        kind: str,
        payload: dict,
        choices: tuple[str, ...],
        timeout: float,
        session_key: Optional[str] = None,
        request_id: Optional[str] = None,
        clarify_id: Optional[str] = None,
        pending_id: Optional[str] = None,
        future: Optional["asyncio.Future[Optional[str]]"] = None,
    ) -> _Pending:
        pending_id = pending_id or f"{kind[:3]}_{secrets.token_hex(8)}"
        pending = _Pending(
            id=pending_id,
            kind=kind,
            created_at=time.time(),
            deadline=time.time() + max(timeout, 0.0),
            choices=tuple(choices),
            payload=payload,
            future=future,
            session_key=session_key,
            request_id=request_id,
            clarify_id=clarify_id,
        )
        self._pending[pending_id] = pending
        return pending

    async def _push_pending(self, pending: _Pending) -> bool:
        """Send a prompt to every authorized watch and move the agent state."""
        self._set_state(pending.kind)
        await self._broadcast_frame(self._pending_event(pending))
        await self._broadcast_stats()
        return any(link.authorized for link in self._links.values())

    def _pending_event(self, pending: _Pending) -> dict:
        name = p.E_APPROVAL_REQUESTED if pending.kind == "approval" else p.E_QUESTION_PENDING
        return p.event(name, event_id=pending.id, **pending.to_dict())

    async def resolve(self, pending_id: str, resolution: Optional[str]) -> bool:
        """Answer a pending request. False when unknown, already settled, or off-menu."""
        pending = self._pending.get(pending_id)
        if pending is None:
            return False
        if resolution is not None and pending.choices and resolution not in pending.choices:
            log.warning("[%s] rejecting choice %r for %s; offered %s",
                        PLATFORM_NAME, resolution, pending_id, pending.choices)
            return False
        self._pending.pop(pending_id, None)

        if pending.future is not None and not pending.future.done():
            # Out-of-process plugin (a CLI session): the waiting HTTP request
            # carries the answer back to the process that owns the approval.
            pending.future.set_result(resolution)
        elif pending.kind == "approval" and pending.session_key:
            await asyncio.to_thread(
                _resolve_gateway_approval, pending.session_key, resolution, pending.request_id
            )
        elif pending.kind == "question" and pending.clarify_id:
            await asyncio.to_thread(_resolve_gateway_clarify, pending.clarify_id, resolution or "")

        resolved_event = p.E_APPROVAL_RESOLVED if pending.kind == "approval" else p.E_QUESTION_RESOLVED
        await self._broadcast(
            p.event(resolved_event, event_id=pending.id, resolution=resolution,
                    responder="watch" if resolution is not None else "timeout")
        )
        if not self._pending:
            self._resume_after_wait()
        await self._broadcast_stats()
        return True

    async def _sweep_expired(self) -> None:
        """Fail closed on prompts nobody answered.

        Hermes enforces its own approval timeout for in-gateway approvals; this
        covers the prompts it does not own (an out-of-process plugin's) so a
        watch card never outlives its request.
        """
        now = time.time()
        for pending_id, pending in list(self._pending.items()):
            if pending.deadline > now:
                continue
            self._pending.pop(pending_id, None)
            if pending.future is not None and not pending.future.done():
                pending.future.set_result(None)
            resolved_event = p.E_APPROVAL_RESOLVED if pending.kind == "approval" else p.E_QUESTION_RESOLVED
            await self._broadcast(p.event(resolved_event, event_id=pending_id, resolution=None, responder="timeout"))

    # -- HTTP: health and the out-of-process plugin --------------------------

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "platform": PLATFORM_NAME,
                "version": _version(),
                "uptime_s": round(time.time() - self._started_at, 1),
                "state": self._state,
                "watches": [link.to_dict() for link in self._links.values()],
                "pending": [pending.to_dict() for pending in self._pending.values()],
            }
        )

    async def _handle_event(self, request: web.Request) -> web.Response:
        """Lifecycle events and approval prompts from a plugin in another process.

        Loopback only: these payloads include command text and questions from a
        live agent session, and this listener is bound to the LAN so the watch
        can reach it. A remote caller gets nothing.
        """
        if not self._caller_is_local(request):
            return web.json_response({"ok": False, "error": "loopback only"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if isinstance(body, dict) and self._ingest_token:
            supplied = str(body.get("token") or "")
            if not secrets.compare_digest(supplied, self._ingest_token):
                return web.json_response({"ok": False, "error": "bad token"}, status=403)

        name = str(body.get("event") or "")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}

        if name in (p.E_APPROVAL_RESOLVED, p.E_QUESTION_RESOLVED):
            # Answered somewhere else (the terminal, or another surface). Hand
            # that answer to the parked plugin request so it responds with what
            # the human actually chose, instead of timing out and making Hermes
            # raise a second prompt for a request that is already settled.
            resolved_id = str(payload.get("id") or "")
            pending = self._pending.pop(resolved_id, None)
            if pending is not None and pending.future is not None and not pending.future.done():
                pending.future.set_result(payload.get("choice") or payload.get("resolution"))
            bus.observe(name, payload)
            await self._broadcast(p.event(name, event_id=resolved_id, resolution=payload.get("choice")))
            return web.json_response({"ok": True})

        if name != p.E_APPROVAL_REQUESTED and name != p.E_QUESTION_PENDING:
            # A lifecycle observation: fold it into state and we are done.
            bus.observe(name, payload)
            return web.json_response({"ok": True})

        kind = "approval" if name == p.E_APPROVAL_REQUESTED else "question"
        choices = tuple(str(c) for c in (body.get("choices") or ()))
        timeout = float(body.get("timeout") or self._approval_timeout)
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Optional[str]]" = loop.create_future()
        pending = self._register(
            kind=kind,
            payload=payload,
            choices=choices,
            timeout=timeout,
            pending_id=str(body.get("id") or "") or None,
            future=future,
        )
        if not await self._push_pending(pending):
            self._pending.pop(pending.id, None)
            return web.json_response({"ok": False, "error": "no watch connected"}, status=503)
        try:
            choice = await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._pending.pop(pending.id, None)
            return web.json_response({"ok": True, "choice": None, "source": "timeout"}, status=200)
        finally:
            self._pending.pop(pending.id, None)
        return web.json_response(
            {"ok": True, "choice": choice, "source": "watch" if choice is not None else "timeout"}
        )

    @staticmethod
    def _caller_is_local(request: web.Request) -> bool:
        peer = request.remote
        if peer is None:
            # A unix-socket peer (or aiohttp without a peername): treat as local
            # only when the transport reports no address at all.
            return request.transport is not None and request.transport.get_extra_info("peername") is None
        return peer in ("127.0.0.1", "::1", "localhost")

    # -- state + stats -------------------------------------------------------

    def _on_observation(self, name: str, payload: dict) -> None:
        """Consume a lifecycle observation from a co-located plugin.

        Runs on the plugin's thread, so it only touches plain state and hands
        the frame to the loop.
        """
        state = bus.state
        mapping = {
            p.E_TURN_STARTED: "thinking",
            p.E_TOOL_STARTED: "tool",
            p.E_TOOL_FINISHED: "thinking",
            p.E_TURN_ENDED: "idle",
            p.E_APPROVAL_REQUESTED: "waiting_approval",
            p.E_QUESTION_PENDING: "waiting_input",
            # Resolutions are handled by _resume_after_wait, not by a mapping:
            # they restore the state the prompt interrupted.
            p.E_SESSION_ENDED: "idle",
            p.E_LOOP_STOPPED: "idle",
        }
        if name == p.E_TOOL_STARTED:
            self._last_tool = payload.get("tool_name") or state.last_tool
        elif name in (p.E_TURN_ENDED, p.E_LOOP_STOPPED, p.E_SESSION_ENDED):
            self._last_tool = None
        if name in (p.E_APPROVAL_RESOLVED, p.E_QUESTION_RESOLVED):
            # Not "thinking" (see _resume_after_wait): the agent goes back to
            # what it was doing, which may be nothing at all.
            self._resume_after_wait()
        else:
            new_state = mapping.get(name)
            if new_state is None:
                return
            self._set_state(new_state)
        pending = self._pending_for_event(name, payload)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._broadcast_frame(self._state_stats(name, pending)))
            )

    def _pending_for_event(self, name: str, payload: dict) -> list[_Pending]:
        if name in (p.E_APPROVAL_RESOLVED, p.E_QUESTION_RESOLVED):
            pending_id = str(payload.get("id") or "")
            self._pending.pop(pending_id, None)
        return []

    def _state_stats(self, name: str, _pending: list[_Pending]) -> dict:
        return p.envelope(p.S_STATS, live=self._live_frame(), pending=self._pending_frame())

    def _set_state(self, kind_or_state: str) -> None:
        state = {
            "approval": "waiting_approval",
            "question": "waiting_input",
        }.get(kind_or_state, kind_or_state)
        if state == self._state:
            return
        if state in WAITING_STATES and self._state_before_wait is None:
            self._state_before_wait = self._state
        self._state = state
        self._state_changed_at = time.time()

    def _resume_after_wait(self) -> None:
        """Leave a waiting state by returning to what the agent was doing.

        A wait is a parenthesis: while the agent blocks on a human it is doing
        nothing, so the state has to say ``waiting_*``. When the answer lands
        the agent resumes whatever it was doing before -- and if we have no
        evidence it was doing anything, the honest state is ``idle``, not
        ``thinking``. Assuming thinking left an idle gateway claiming to work.
        """
        previous = self._state_before_wait or "idle"
        self._state_before_wait = None
        self._set_state(previous)

    def _live_frame(self) -> dict[str, Any]:
        return {
            "agent_state": self._state,
            "state_changed_at": self._state_changed_at,
            "last_tool": self._last_tool,
        }

    def _pending_frame(self) -> list[dict[str, Any]]:
        return [pending.to_dict() for pending in self._pending.values()]

    def _observation(self) -> LiveObservation:
        """Live measurements for the throughput and context maths.

        Comes from whichever half saw the provider call: co-located, the plugin
        wrote them straight into the bus; out of process, they arrived as an
        ``api_request`` observation over loopback. Either way this is a report
        of something measured, never an estimate.
        """
        allowed = set(LiveObservation().__dataclass_fields__)
        measured = {k: v for k, v in bus.state.measurement.items() if k in allowed}
        return LiveObservation(**measured)

    def _build_stats(self) -> dict[str, Any]:
        """Blocking: reads the session store. Always called off-loop."""
        engine = StatsEngine(db_path=self._db_path, live=self._observation())
        payload = engine.snapshot()
        payload["live"] = {**payload.get("live", {}), **self._live_frame()}
        payload["pending"] = self._pending_frame()
        payload["bridge"] = {
            "version": _version(),
            "profile": str(getattr(self.config, "profile", "") or "default"),
            "uptime_s": round(time.time() - self._started_at, 1),
            "host": self._host,
            "port": self._bound_port(),
            "watches": [link.to_dict() for link in self._links.values()],
        }
        return payload

    async def _stats_loop(self) -> None:
        """Push snapshots on a cadence that depends on whether anything is happening."""
        while True:
            try:
                busy = self._state != "idle"
                await asyncio.sleep(BUSY_TICK_S if busy else IDLE_TICK_S)
                if not any(link.authorized for link in self._links.values()):
                    continue
                await self._sweep_expired()
                await self._broadcast_stats()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Stats must never take the gateway down with them.
                log.warning("[%s] stats tick failed: %s", PLATFORM_NAME, exc)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if not self._links:
                continue
            frame = p.envelope(p.S_PING, ts=time.time())
            for link in list(self._links.values()):
                if not await link.send(frame):
                    self._links.pop(link.device_id or "", None)

    async def _send_snapshot(self, link: _WatchLink) -> None:
        try:
            payload = await asyncio.to_thread(self._build_stats)
        except Exception as exc:
            log.warning("[%s] stats build failed: %s", PLATFORM_NAME, exc)
            return
        await link.send(p.envelope(p.S_SNAPSHOT, **payload))

    async def _broadcast_stats(self) -> None:
        if not any(link.authorized for link in self._links.values()):
            return
        try:
            payload = await asyncio.to_thread(self._build_stats)
        except Exception as exc:
            log.warning("[%s] stats build failed: %s", PLATFORM_NAME, exc)
            return
        await self._broadcast(p.envelope(p.S_STATS, **payload))

    async def _broadcast(self, frame: dict) -> int:
        return await self._broadcast_frame(frame)

    async def _broadcast_frame(self, frame: dict) -> int:
        sent = 0
        for link in list(self._links.values()):
            if not link.authorized:
                continue
            if await link.send(frame):
                sent += 1
        return sent

    # -- helpers -------------------------------------------------------------

    async def _delivery_targets(self, chat_id: str) -> list[_WatchLink]:
        """Who may receive this message.

        The split here is deliberate, and both halves were learned the hard way:

        * An **exact** chat id reaches its device even when that device is not
          paired. The pairing code is addressed that way, and refusing it made
          onboarding impossible: the one message an unpaired watch must receive
          was the one message the adapter dropped. It is safe because an
          unpaired sender never gets a session, so no agent output can be aimed
          at it -- only the runner's own pairing code or refusal notice is.
        * A device that was paired and has since been **revoked** gets nothing.
          That is what a revoke is for, which is why "never paired" and "paired
          then revoked" are told apart rather than both being "not authorized".
        * A **broadcast** (no chat id, or ``all``) goes to paired devices only,
          so a notice meant for the household never lands on a stranger's watch.

        Authorization is re-read here rather than trusted from hello time, so
        revoking a device takes effect as messages flow instead of at its next
        reconnect.
        """
        if chat_id and chat_id not in ("", "all"):
            link = self._links.get(chat_id)
            if link is None:
                return []
            device_id = link.device_id or chat_id
            link.authorized = await self._is_authorized(device_id)
            if link.authorized:
                return [link]
            if device_id in self._ever_authorized:
                log.info("[%s] withholding a message for revoked device %s", PLATFORM_NAME, device_id)
                return []
            return [link]

        targets: list[_WatchLink] = []
        for link in self._links.values():
            if not link.device_id:
                continue
            link.authorized = await self._is_authorized(link.device_id)
            if link.authorized:
                targets.append(link)
        return targets

    def _request_id_from(self, metadata: Optional[dict]) -> Optional[str]:
        for key in ("request_id", "approval_request_id", "id"):
            value = (metadata or {}).get(key)
            if isinstance(value, str) and value:
                return value
        return None

    async def _is_authorized(self, device_id: str) -> bool:
        """May this device receive stats and resolve approvals?

        Hermes' pairing store is the default authority; explicit config beats it
        in both directions so a locked-down or a zero-config rig both work.
        """
        if self._allow_all:
            self._ever_authorized.add(device_id)
            return True
        if device_id in self._allowed_devices:
            self._ever_authorized.add(device_id)
            return True
        now = time.time()
        cached = self._approval_cache.get(device_id)
        if cached is not None and (now - cached[0]) < PAIRING_CACHE_S:
            approved = cached[1]
        else:
            approved = await asyncio.to_thread(self._read_pairing_store, device_id)
            self._approval_cache[device_id] = (now, approved)
        if approved:
            self._ever_authorized.add(device_id)
        return approved

    def _read_pairing_store(self, device_id: str) -> bool:
        """Blocking file read; fail closed if the store is unreadable."""
        try:
            store = getattr(self.gateway_runner, "pairing_store", None)
            if store is None:
                from gateway.pairing import PairingStore

                store = PairingStore()
            return bool(store.is_approved(PLATFORM_NAME, device_id))
        except Exception as exc:
            log.debug("[%s] pairing lookup failed for %s: %s", PLATFORM_NAME, device_id, exc)
            return False


def _resolve_gateway_approval(session_key: str, choice: Optional[str], request_id: Optional[str]) -> int:
    """Unblock the agent thread waiting on an approval. Blocking; call off-loop."""
    if not choice:
        return 0
    from tools.approval import resolve_gateway_approval

    return resolve_gateway_approval(session_key, choice, request_id=request_id)


def _resolve_gateway_clarify(clarify_id: str, response: str) -> bool:
    from tools.clarify_gateway import resolve_gateway_clarify

    return resolve_gateway_clarify(clarify_id, response)
