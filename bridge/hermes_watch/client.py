"""Thin synchronous client the Hermes plugin uses to reach the daemon.

Standard library only (``urllib.request``). That is a hard requirement, not a
preference: this code is imported into a *live Hermes process*, so it must not
add an import-time dependency, must not touch the event loop, and must never
raise into the agent. Every public method swallows transport failures and
returns a neutral value -- a bridge that is down degrades the watch, never the
agent.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger("hermes_watch.client")

#: Short on purpose. Events are fire-and-forget; a slow daemon must not turn
#: into slow tool calls. The approval path uses its own, longer budget.
EVENT_TIMEOUT = 2.0


class BridgeClient:
    """Blocking HTTP client for the bridge daemon's loopback ingest API."""

    def __init__(self, base_url: str, token: str = "", *, timeout: float = EVENT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

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
        except urllib.error.HTTPError as exc:
            # 202 on the long-poll is "not answered yet", not an error.
            if exc.code == 202:
                try:
                    return json.loads(exc.read().decode("utf-8"))
                except Exception:
                    return {"ok": False, "error": "not_answered"}
            log.debug("bridge %s %s -> HTTP %s", method, path, exc.code)
            return None
        except Exception as exc:
            log.debug("bridge %s %s failed: %s", method, path, exc)
            return None

    # -- fire and forget -----------------------------------------------------

    def post_event(self, event: str, payload: Optional[dict] = None) -> bool:
        body: dict[str, Any] = {"event": event, "payload": payload or {}}
        return self._request("POST", "/v1/event", body) is not None

    def post_stats(self, **fields: Any) -> bool:
        """Report exact per-call measurements (durations, usage, context window)."""
        clean = {k: v for k, v in fields.items() if v is not None}
        if not clean:
            return False
        return self._request("POST", "/v1/stats", clean) is not None

    def health(self) -> Optional[dict]:
        return self._request("GET", "/healthz")

    # -- blocking attention --------------------------------------------------

    def open_request(
        self,
        kind: str,
        payload: dict,
        *,
        choices: tuple[str, ...] = (),
        timeout: float = 300.0,
        pending_id: Optional[str] = None,
    ) -> Optional[dict]:
        body = {
            "kind": kind,
            "payload": payload,
            "choices": list(choices),
            "timeout": timeout,
            "id": pending_id,
        }
        return self._request("POST", "/v1/pending", body)

    def wait_for_answer(self, pending_id: str, *, timeout: float) -> Optional[dict]:
        return self._request("GET", f"/v1/pending/{pending_id}?timeout={timeout}", timeout=timeout + 5.0)

    def resolve(self, pending_id: str, resolution: Optional[str], *, responder: str = "hermes") -> bool:
        return (
            self._request(
                "POST",
                f"/v1/pending/{pending_id}/resolve",
                {"id": pending_id, "resolution": resolution, "responder": responder},
            )
            is not None
        )

    def answered_by_watch(self, delivered: int) -> bool:
        """Whether any watch was connected when the request was opened."""
        return delivered > 0
