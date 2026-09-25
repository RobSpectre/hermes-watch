"""In-process state shared between the plugin half and the adapter half.

When Hermes runs as a gateway, the plugin's hooks and the watch adapter live in
the *same* process, so a lifecycle event can be handed straight to the adapter
instead of going over HTTP. When Hermes runs as a plain CLI session there is no
adapter in this process at all, and the plugin posts to the gateway over
loopback instead (see :mod:`hermes_watch.client`).

This module is the meeting point, and it is deliberately stdlib-only: it is
imported into a live agent process on every session, gateway or not, so it must
stay cheap and must never import anything from ``gateway``.

The bus is a plain object rather than a global so tests can construct one
without touching module state; :data:`bus` is the process-wide instance both
halves use.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: How long a "busy" observation stays fresh. Longer than the slowest stats
#: tick, shorter than a human noticing a stale spinner.
BUSY_TTL_SECONDS = 20.0


@dataclass
class LiveState:
    """What the current process knows about the agent right now.

    Everything here is an *observation*, never a prediction: a field is None
    until something actually reported it, and the watch renders None as an em
    dash rather than a plausible-looking zero.
    """

    agent_state: str = "idle"
    last_tool: Optional[str] = None
    session_id: Optional[str] = None
    session_title: Optional[str] = None
    #: Unix time of the last turn-level observation.
    updated_at: float = 0.0
    #: (wall seconds, tokens) of the last provider call, for live tok/s.
    last_api_call: Optional[tuple[float, int]] = None
    #: Rolling observations, newest last.
    api_observations: list[tuple[float, int]] = field(default_factory=list)
    #: Last exact measurement reported by the plugin, merged field-wise. Fed
    #: straight into ``stats.LiveObservation``, which is why the keys are the
    #: measurement names rather than anything watch-shaped.
    measurement: dict[str, Any] = field(default_factory=dict)

    def busy(self, now: Optional[float] = None) -> bool:
        """True when something happened recently enough to call the agent busy."""
        now = time.time() if now is None else now
        return (now - self.updated_at) < BUSY_TTL_SECONDS and self.agent_state in (
            "thinking", "tool", "waiting_approval", "waiting_input",
        )

    def tokens_per_second(self) -> Optional[float]:
        """Tokens/second over the provider calls seen in the last minute.

        Measured, not modelled: this is the sum of output tokens actually
        returned by calls this process observed, over the wall time those calls
        took. Returns None when nothing has been observed, which the UI shows as
        an em dash.
        """
        cutoff = time.time() - 60.0
        recent = [(ts, tokens) for ts, tokens in self.api_observations if ts >= cutoff]
        if not recent:
            return None
        tokens = sum(tokens for _, tokens in recent)
        if tokens <= 0:
            return None
        span = max(recent[-1][0] - recent[0][0], 0.001)
        if len(recent) == 1:
            # A single call still gives a rate: its own duration is unknown here,
            # so report None rather than inventing a duration.
            return None
        return round(tokens / span, 1)


class LiveBus:
    """Fan-out for observations, with a synchronous path to the adapter.

    Handlers run on the caller's thread. They must be cheap and must not block:
    a hook firing inside an agent turn cannot be allowed to stall the turn, which
    is the whole reason the plugin half is defensive everywhere.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: list[Callable[[str, dict], None]] = []
        self.state = LiveState()

    # -- subscription --------------------------------------------------------

    def subscribe(self, handler: Callable[[str, dict], None]) -> Callable[[], None]:
        """Register ``handler(name, payload)``. Returns an unsubscribe callable."""
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)

        return unsubscribe

    def has_subscriber(self) -> bool:
        """True when an adapter is listening in this process."""
        with self._lock:
            return bool(self._handlers)

    # -- observation ---------------------------------------------------------

    def observe(self, name: str, payload: Optional[dict] = None) -> None:
        """Record and publish one lifecycle observation.

        Never raises: a malformed payload from a hook must not break the agent
        turn that produced it.
        """
        payload = dict(payload or {})
        try:
            self._apply(name, payload)
        except Exception:
            pass
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(name, payload)
            except Exception:
                # A subscriber that throws (a closed socket, a full queue) is the
                # subscriber's problem; the agent turn keeps going.
                continue

    def _apply(self, name: str, payload: dict) -> None:
        """Fold an observation into :attr:`state`."""
        now = time.time()
        state = self.state
        if name == "api_request":
            tokens = int(payload.get("output_tokens") or 0)
            if tokens > 0:
                state.api_observations.append((now, tokens))
                del state.api_observations[:-40]
                state.last_api_call = (now, tokens)
            # Merge only measured values: a None would otherwise erase a good
            # earlier reading with "unknown", and the UI would flicker to a dash.
            state.measurement.update({k: v for k, v in payload.items() if v is not None})
        elif name == "turn.started":
            state.agent_state = "thinking"
            state.updated_at = now
            session_id = payload.get("session_id")
            if session_id:
                state.session_id = str(session_id)
        elif name == "turn.ended":
            state.agent_state = "idle"
            state.last_tool = None
            state.updated_at = now
        elif name == "tool.started":
            state.agent_state = "tool"
            state.last_tool = payload.get("tool")
            state.updated_at = now
        elif name == "tool.finished":
            state.agent_state = "thinking"
            state.updated_at = now
        elif name == "approval.requested":
            state.agent_state = "waiting_approval"
            state.updated_at = now
        elif name == "approval.resolved":
            state.agent_state = "thinking"
            state.updated_at = now
        elif name == "question.pending":
            state.agent_state = "waiting_input"
            state.updated_at = now
        elif name == "question.resolved":
            state.agent_state = "thinking"
            state.updated_at = now
        elif name == "session.started":
            state.session_id = payload.get("session_id") or state.session_id
            state.session_title = payload.get("title") or state.session_title
            state.updated_at = now
        elif name == "session.ended":
            state.agent_state = "idle"
            state.updated_at = now
        elif name == "loop.stopped":
            state.agent_state = "idle"
            state.last_tool = None
            state.updated_at = now
        else:
            # Unknown observation: keep the timestamp fresh so activity is still
            # visible, but do not invent a state for it.
            state.updated_at = now


#: Process-wide instance. The plugin writes, the adapter reads.
bus = LiveBus()
