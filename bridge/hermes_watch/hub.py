"""Connection and attention state shared by the bridge daemon.

The hub owns exactly three things:

* **who is listening** -- connected watch sockets, their device labels, and
  when each was last heard from;
* **what needs a human** -- pending approvals and pending questions, each with
  a blocker the daemon can await and the watch can resolve;
* **what the agent is doing right now** -- a derived :data:`AGENT_STATE` used
  for the notification and the tile/complication.

It is transport-agnostic on purpose: the aiohttp layer in ``daemon.py`` only
translates HTTP/WebSocket frames into hub calls, so the hub can be unit-tested
without a socket in sight.

Everything here runs on the daemon's single asyncio loop except
:meth:`Hub.publish_threadsafe`, which exists for callers on other threads.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import protocol as p
from .stats import StatsEngine

log = logging.getLogger("hermes_watch.hub")


@dataclass
class WatchConnection:
    """One connected Wear OS client."""

    id: str
    device: str
    send: Callable[[dict], Awaitable[None]]
    remote: str = ""
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    protocol: int = p.PROTOCOL_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "device": self.device,
            "remote": self.remote,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "protocol": self.protocol,
            "age_s": round(time.time() - self.connected_at, 1),
        }


@dataclass
class PendingHumanInput:
    """A request that is blocking the agent until a human answers.

    ``kind`` is ``"approval"`` or ``"question"``. The future resolves to a
    choice string (approval) or a free-text reply (question), or to ``None``
    on timeout/withdrawal -- which fail closed at the caller.
    """

    id: str
    kind: str
    created_at: float
    deadline: float
    choices: tuple[str, ...]
    payload: dict
    future: "asyncio.Future[Optional[str]]"
    resolution: Optional[str] = None
    resolved_at: Optional[float] = None
    source: str = "hermes"

    @property
    def remaining_s(self) -> float:
        return max(self.deadline - time.time(), 0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "remaining_s": round(self.remaining_s, 1),
            "choices": list(self.choices),
            "payload": self.payload,
            "resolution": self.resolution,
            "resolved_at": self.resolved_at,
        }


class Hub:
    """Fan-out, attention tracking, and stats assembly."""

    def __init__(self, stats: Optional[StatsEngine] = None, *, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.stats = stats or StatsEngine()
        self.loop = loop
        self.connections: dict[str, WatchConnection] = {}
        self.pending: dict[str, PendingHumanInput] = {}
        self.agent_state: str = "idle"
        self.agent_detail: dict[str, Any] = {}
        self.bridge_version = "0.0.0"
        self.profile = "default"
        self.started_at = time.time()
        self._state_changed_at = time.time()
        self._idle_task: Optional[asyncio.Task] = None

    # -- connections ---------------------------------------------------------

    def _loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is not None:
            return self.loop
        return asyncio.get_running_loop()

    async def add_watch(self, connection: WatchConnection) -> None:
        """Send the connect handshake: hello, then one full snapshot.

        Deliberately just those two frames. The snapshot already carries live
        agent state, pending requests and stats, so a client that has consumed
        them is fully caught up -- no separate state frame to race against.
        """
        self.connections[connection.id] = connection
        log.info("watch connected: %s (%s)", connection.id, connection.remote)
        await self.send_to(connection.id, p.hello(self.bridge_version, time.time(), self.profile))
        await self.send_to(connection.id, p.envelope(p.S_SNAPSHOT, **self.stats_payload()))

    async def remove_watch(self, watch_id: str) -> None:
        if self.connections.pop(watch_id, None) is not None:
            log.info("watch disconnected: %s", watch_id)
        if not self.connections:
            self._set_state("idle", detail={"reason": "no_watch_connected"})

    async def send_to(self, watch_id: str, frame: dict) -> bool:
        connection = self.connections.get(watch_id)
        if connection is None:
            return False
        try:
            await connection.send(frame)
        except Exception as exc:  # a dead socket must not kill fan-out
            log.debug("send to %s failed: %s", watch_id, exc)
            return False
        return True

    async def broadcast(self, frame: dict) -> int:
        """Send to every connected watch. Returns the number that accepted it."""
        if not self.connections:
            return 0
        results = await asyncio.gather(
            *(self.send_to(watch_id, frame) for watch_id in list(self.connections)), return_exceptions=True
        )
        return sum(1 for accepted in results if accepted is True)

    def publish_threadsafe(self, frame: dict) -> None:
        """Queue a broadcast from a non-loop thread (the Hermes plugin path).

        The plugin's hook callbacks can run on worker threads; handing the frame
        to the loop keeps socket writes single-threaded. A closed loop is
        ignored -- the daemon is going away anyway.
        """
        try:
            loop = self._loop()
        except RuntimeError:
            return
        try:
            loop.call_soon_threadsafe(lambda: loop.create_task(self.broadcast(frame)))
        except RuntimeError:
            pass

    # -- agent state ---------------------------------------------------------

    def _set_state(self, state: str, detail: Optional[dict] = None) -> None:
        if state not in p.AGENT_STATES:
            log.warning("ignoring unknown agent state %r", state)
            return
        changed = state != self.agent_state
        self.agent_state = state
        if detail:
            self.agent_detail = {**self.agent_detail, **detail}
        if changed:
            self._state_changed_at = time.time()
            log.info("agent state -> %s %s", state, detail or {})

    async def handle_hermes_event(self, name: str, payload: dict) -> None:
        """Translate a Hermes lifecycle event into watch state and frames."""
        if name == p.E_TURN_STARTED:
            self._set_state("thinking", detail={"turn_id": payload.get("turn_id")})
        elif name == p.E_TOOL_STARTED:
            self._set_state("tool", detail={"last_tool": payload.get("tool_name"),
                                            "turn_id": payload.get("turn_id")})
        elif name == p.E_TOOL_FINISHED:
            self._set_state("thinking", detail={"last_tool": payload.get("tool_name")})
        elif name == p.E_APPROVAL_REQUESTED:
            self._set_state("waiting_approval", detail={"pending_id": payload.get("id")})
        elif name == p.E_APPROVAL_RESOLVED:
            self._set_state("thinking", detail={"pending_id": None})
        elif name == p.E_QUESTION_PENDING:
            self._set_state("waiting_input", detail={"pending_id": payload.get("id")})
        elif name == p.E_QUESTION_RESOLVED:
            self._set_state("thinking", detail={"pending_id": None})
        elif name in (p.E_TURN_ENDED, p.E_LOOP_STOPPED, p.E_SESSION_ENDED):
            self._set_state("idle", detail={"last_tool": None, "pending_id": None})
        await self.broadcast(p.envelope(p.S_STATS, live=self._live_frame(), pending=self._pending_frame()))
        await self.broadcast(p.event(name, event_id=payload.get("id"), **payload))

    def _live_frame(self) -> dict:
        return {
            "agent_state": self.agent_state,
            "state_changed_at": self._state_changed_at,
            **self.agent_detail,
        }

    def _pending_frame(self) -> list[dict]:
        return [item.to_dict() for item in self.pending.values()]

    # -- human attention -----------------------------------------------------

    async def request_human_input(
        self,
        kind: str,
        payload: dict,
        *,
        choices: tuple[str, ...] = (),
        timeout: float = 300.0,
        pending_id: Optional[str] = None,
    ) -> PendingHumanInput:
        """Register a blocking request and notify watchers.

        Does not wait -- the caller awaits :meth:`resolve_future`. Kept separate
        so the HTTP ingest handler can return an id immediately and the caller
        can then block on its own time budget.
        """
        loop = self._loop()
        pending_id = pending_id or f"{kind[:3]}_{uuid.uuid4().hex[:12]}"
        item = PendingHumanInput(
            id=pending_id,
            kind=kind,
            created_at=time.time(),
            deadline=time.time() + max(timeout, 0.0),
            choices=tuple(choices),
            payload=payload,
            future=loop.create_future(),
        )
        self.pending[pending_id] = item
        if kind == "approval":
            self._set_state("waiting_approval", detail={"pending_id": pending_id})
        elif kind == "question":
            self._set_state("waiting_input", detail={"pending_id": pending_id})
        await self.broadcast(
            p.event(
                p.E_APPROVAL_REQUESTED if kind == "approval" else p.E_QUESTION_PENDING,
                event_id=pending_id,
                **item.to_dict(),
            )
        )
        log.info("%s pending: %s (timeout %.0fs)", kind, pending_id, timeout)
        return item

    async def resolve(self, pending_id: str, resolution: Optional[str], *, responder: str = "watch") -> bool:
        """Answer a pending request. Returns False when it is unknown or already settled."""
        item = self.pending.get(pending_id)
        if item is None or item.future.done():
            return False
        if resolution is not None and item.choices and resolution not in item.choices:
            log.warning("rejecting choice %r for %s; offered %s", resolution, pending_id, item.choices)
            return False
        item.resolution = resolution
        item.resolved_at = time.time()
        item.future.set_result(resolution)
        if resolution is None:
            item.source = "timeout"
        else:
            item.source = responder
        await self.broadcast(
            p.event(
                p.E_APPROVAL_RESOLVED if item.kind == "approval" else p.E_QUESTION_RESOLVED,
                event_id=pending_id,
                id=pending_id,
                kind=item.kind,
                resolution=resolution,
                responder=item.source,
            )
        )
        # The agent resumes as soon as this returns, so drop out of the
        # waiting_* state rather than leaving the watch showing a stale prompt.
        # The next lifecycle event corrects this if the turn actually ended.
        if not any(other.future is not item.future and not other.future.done() for other in self.pending.values()):
            self._set_state("thinking", detail={"pending_id": None})
        return True

    def drop_request(self, pending_id: str) -> None:
        """Remove a settled request from the pending set (after the caller has it)."""
        self.pending.pop(pending_id, None)

    async def sweep_expired(self) -> None:
        """Time out requests whose deadline passed without an answer.

        Fail-closed by design: an unanswered approval becomes ``None``, which
        the plugin translates into a denial -- never an allow.
        """
        now = time.time()
        for item in list(self.pending.values()):
            if item.deadline <= now and not item.future.done():
                log.info("%s %s expired unanswered", item.kind, item.id)
                await self.resolve(item.id, None, responder="timeout")

    # -- stats ---------------------------------------------------------------

    def stats_payload(self, session_id: Optional[str] = None) -> dict:
        payload = self.stats.snapshot(session_id)
        payload["live"] = {**payload.get("live", {}), **self._live_frame()}
        payload["pending"] = self._pending_frame()
        payload["bridge"] = {
            "version": self.bridge_version,
            "profile": self.profile,
            "uptime_s": round(time.time() - self.started_at, 1),
            "watches": [c.to_dict() for c in self.connections.values()],
        }
        return payload

    async def stats_tick(self, every: float = 5.0, *, busy_every: float = 1.5) -> None:
        """Push stats to watchers; faster while the agent is doing something.

        A watch wants a live tok/s readout during a turn and does not want its
        radio woken every second while the agent is idle. Cadence is the
        compromise, and the watch's own screen-off state is the final brake.
        """
        while True:
            await asyncio.sleep(busy_every if self.agent_state not in ("idle", "offline") else every)
            if not self.connections:
                continue
            try:
                await self.sweep_expired()
                await self.broadcast(p.envelope(p.S_STATS, **self.stats_payload()))
            except Exception as exc:  # stats must never take the daemon down
                log.warning("stats tick failed: %s", exc)
