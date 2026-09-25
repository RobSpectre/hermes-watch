"""Thin synchronous client the plugin half uses to reach the watch adapter.

Standard library only (``urllib.request``). That is a hard requirement, not a
preference: this module is imported into a *live Hermes process* -- often a CLI
session, where the module is loaded on every start -- so it must add no
import-time dependency, must not touch the event loop, and must never raise into
the agent. Every method swallows transport failures and returns a neutral
value. A watch that is unreachable degrades the watch, never the agent.

There are exactly two things the plugin can say to the adapter:

* ``post_event`` -- a lifecycle observation. Fire and forget.
* ``ask`` -- "a human must decide this, tell me what the watch said". The HTTP
  request *is* the pending state: it stays open until the watch answers or the
  budget expires, so neither side needs a registry, a sweeper, or a callback.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger("hermes_watch.client")

#: Short on purpose: observations are fire-and-forget, and a wedged listener
#: must not turn into a slow tool call.
EVENT_TIMEOUT = 2.0
#: Grace on top of the caller's own budget, so the adapter's timeout (which
#: fails closed) is the one that decides, not a socket timeout.
ASK_GRACE = 5.0


class AdapterClient:
    """Blocking HTTP client for the watch adapter's loopback API."""

    def __init__(self, base_url: str, *, timeout: float = EVENT_TIMEOUT, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token

    # -- plumbing ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except Exception as exc:
            # Includes HTTPError, URLError, socket timeouts and a gateway that is
            # simply not running. Every one of them means "no watch path", which
            # the caller handles by falling back to Hermes' own prompt.
            log.debug("watch %s %s failed: %s", method, path, exc)
            return None

    # -- fire and forget -----------------------------------------------------

    def post_event(self, event: str, payload: Optional[dict] = None) -> bool:
        """Report one lifecycle observation. False when nothing is listening."""
        return self._request("POST", "/event", {"event": event, "payload": payload or {}}) is not None

    def health(self) -> Optional[dict]:
        return self._request("GET", "/healthz")

    def reachable(self) -> bool:
        return self.health() is not None

    # -- blocking human input -------------------------------------------------

    def ask(
        self,
        kind: str,
        payload: dict,
        *,
        choices: tuple[str, ...] = (),
        timeout: float = 300.0,
        pending_id: Optional[str] = None,
    ) -> Optional[str]:
        """Ask the watch and block until it answers.

        Returns the chosen string, or None when the watch is unreachable, the
        device is not paired, nobody answered in time, or the frame was refused.
        None always means *no human answered* -- callers treat it as failure and
        let Hermes fall back to its own prompt, so a watch that is off can never
        turn into a silent denial of a command the user would have approved.
        """
        event = "approval.requested" if kind == "approval" else "question.pending"
        body: dict[str, Any] = {
            "event": event,
            "payload": payload,
            "choices": list(choices),
            "timeout": timeout,
            "id": pending_id,
        }
        response = self._request("POST", "/event", body, timeout=timeout + ASK_GRACE)
        if not response or not response.get("ok"):
            return None
        choice = response.get("choice")
        return choice if isinstance(choice, str) and choice else None
