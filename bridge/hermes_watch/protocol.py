"""Wire protocol between the Hermes Watch bridge daemon and a Wear OS client.

One JSON object per WebSocket text frame, defined by :data:`PROTOCOL_VERSION`.
The normative description lives in ``docs/protocol.md``; this module is the
single implementation of that description, and both the bridge and the tests
import it rather than hand-rolling dicts.

Design rules
------------
* Additive only. New ``type`` / ``event`` names and new optional fields are
  allowed inside a protocol version; removing or retyping a field is not.
* Every server frame carries ``v``. A client that sees a major version it does
  not understand must disconnect and surface "bridge too new" rather than
  guess.
* Timestamps are POSIX seconds (float, UTC). Durations are seconds. Token
  counts are integers. Percentages are 0-100 floats.
"""

from __future__ import annotations

from typing import Any, Optional

PROTOCOL_VERSION = 1

# --- client -> server -------------------------------------------------------

C_HELLO = "hello"
C_ANSWER = "answer"
C_STATS_REQUEST = "stats.request"
C_PONG = "pong"
#: A message from the watch. This is what makes the watch a platform rather than
#: a readout: it goes through the normal ingress, so Hermes' pairing flow can
#: answer an unknown device with a code, and a paired one can start a session.
C_TEXT = "text"

# --- server -> client -------------------------------------------------------

S_HELLO = "hello"
S_SNAPSHOT = "snapshot"
S_EVENT = "event"
S_STATS = "stats"
S_PING = "ping"
S_ERROR = "error"

# --- event names (server -> client, ``S_EVENT``) ----------------------------

E_TURN_STARTED = "turn.started"
E_TURN_ENDED = "turn.ended"
E_TOOL_STARTED = "tool.started"
E_TOOL_FINISHED = "tool.finished"
E_APPROVAL_REQUESTED = "approval.requested"
E_APPROVAL_RESOLVED = "approval.resolved"
E_QUESTION_PENDING = "question.pending"
E_QUESTION_RESOLVED = "question.resolved"
E_SESSION_STARTED = "session.started"
E_SESSION_ENDED = "session.ended"
E_LOOP_STOPPED = "loop.stopped"

#: Agent-level states the watch can render on a tile / complication.
AGENT_STATES = (
    "offline",          # no bridge connection
    "idle",             # connected, no turn running
    "thinking",         # model call in flight
    "tool",             # a tool is executing
    "waiting_approval", # blocked on a human approval
    "waiting_input",    # blocked on a human answer
)

#: Approval scopes Hermes may offer. The watch must render exactly the subset
#: present in ``choices`` -- never invent ``always`` for a once-only request.
APPROVAL_CHOICES = ("once", "session", "always", "deny")


def envelope(kind: str, **fields: Any) -> dict:
    """Build a server frame: version tag, then payload fields."""
    return {"v": PROTOCOL_VERSION, "type": kind, **fields}


def event(name: str, event_id: Optional[str] = None, **payload: Any) -> dict:
    """Build an event frame. ``event_id`` correlates request/resolution pairs."""
    frame = envelope(S_EVENT, event=name, payload=payload)
    if event_id is not None:
        frame["id"] = event_id
    return frame


def hello(bridge_version: str, now: float, profile: str = "default") -> dict:
    return envelope(
        S_HELLO,
        bridge_version=bridge_version,
        protocol=PROTOCOL_VERSION,
        server_time=now,
        profile=profile,
    )


def validation_error(frame: dict) -> Optional[str]:
    """Return a human-readable reason a client frame is unacceptable, else None.

    Kept deliberately permissive: unknown frame types and unknown fields are
    ignored so an older bridge never hard-fails a newer app.
    """
    if not isinstance(frame, dict):
        return "frame is not a JSON object"
    kind = frame.get("type")
    if not isinstance(kind, str) or not kind:
        return "frame has no type"
    version = frame.get("v", PROTOCOL_VERSION)
    if not isinstance(version, int) or version < 1:
        return f"bad protocol version: {version!r}"
    if kind == C_ANSWER:
        if not isinstance(frame.get("id"), str) or not frame["id"]:
            return "answer frame has no id"
        choice = frame.get("choice")
        if not isinstance(choice, str) or choice not in APPROVAL_CHOICES + ("reply", "cancel"):
            return f"unsupported choice: {choice!r}"
    elif kind == C_TEXT:
        text = frame.get("text")
        if not isinstance(text, str) or not text.strip():
            return "text frame has no text"
    return None


def parse_client_frame(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate a decoded client frame. Returns ``(frame, error)``."""
    error = validation_error(raw)
    if error is not None:
        return None, error
    return raw, None
