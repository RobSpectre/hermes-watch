"""The Hermes-side plugin: hooks in, watch frames out.

Loaded by Hermes' ``PluginManager`` from ``~/.hermes/plugins/hermes-watch/``
(see ``docs/hermes-integration.md`` for the install paths). It does four
things, in ascending order of how much they matter:

1. **Notifies** the watch that a turn started, a tool is running, or the loop
   stopped (``pre_api_request``/``post_tool_call``/``agent_loop_stopped``).
2. **Reports exact statistics** the session store cannot hold: real API-call
   latency and real prompt size, which is what makes a truthful tok/s and
   context-remaining readout possible.
3. **Notifies on questions** the agent is blocked on (``clarify``) -- one-way
   in v1, because Hermes exposes no input transport to answer them from a
   plugin (tracked in ``docs/hermes-integration.md``).
4. **Routes approvals to the watch** through ``ctx.register_approval_transport``,
   so an approval can be answered from the wrist instead of the terminal.

Hard rules honoured here:

* Nothing blocks the agent. Every hook hands work to a bounded background
  queue; overflow is dropped with a warning, never back-pressured into a turn.
* Nothing raises into the agent. Hook callbacks are wrapped whole.
* No transcript content leaves the process by default -- names, counts, and the
  already-redacted approval command only.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from . import protocol as p
from .client import BridgeClient
from .settings import load_config, load_token

log = logging.getLogger("hermes_watch.plugin")

TRANSPORT_NAME = "pixel-watch"
QUEUE_DEPTH = 256
MAX_TEXT = 240

#: Maps an in-flight approval to the pending id the watch knows it by, so the
#: ``post_approval_response`` back-channel can clear a prompt answered
#: elsewhere (terminal, /approve on another surface, or a timeout).
_open_approvals: dict[str, str] = {}
_approvals_lock = threading.Lock()


class WatchUnavailable(RuntimeError):
    """Raised when the selected transport cannot present the prompt.

    Hermes fails closed on a transport exception and, with
    ``security.approval.transport_fallback: builtin``, re-materialises the
    prompt on the ordinary CLI/TUI surface. That is the intended behaviour when
    no watch is connected: never swallow an approval silently, always let the
    human see it somewhere.
    """


class _Dispatcher:
    """One background thread, bounded queue, drop-on-overflow."""

    def __init__(self, depth: int = QUEUE_DEPTH):
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=depth)
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._dropped = 0

    def submit(self, task: Callable[[], None]) -> None:
        self._ensure_started()
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 50 == 1:
                log.warning("watch bridge queue full; dropped %d events", self._dropped)

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="hermes-watch-bridge", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                task()
            except Exception as exc:  # a bridge fault is never an agent fault
                log.debug("watch bridge task failed: %s", exc)
            finally:
                # Paired with Queue.join(): callers that need "everything I
                # handed over has been attempted" can wait on the queue.
                self._queue.task_done()


def _usage_field(usage: Any, *names: str) -> Optional[int]:
    """Pull an integer out of a usage object that may be a dict or a dataclass."""
    if usage is None:
        return None
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _clip(text: Any, limit: int = MAX_TEXT) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _resolve_context_window(model: str, base_url: str, provider: str) -> Optional[int]:
    """Ask Hermes for the model's real context window, from inside Hermes.

    Imported lazily and guarded: this resolves far more cases than the bridge's
    cache-only lookup (config override, provider APIs, models.dev, fallbacks),
    but it is optional -- an older or trimmed install must not break the hook.
    """
    try:
        from agent.model_metadata import get_model_context_length  # type: ignore

        value = get_model_context_length(model, base_url=base_url or "", provider=provider or "")
        return int(value) if value else None
    except Exception as exc:
        log.debug("context window lookup failed for %s: %s", model, exc)
        return None


class WatchBridgePlugin:
    """Holds the wiring so ``register()`` stays readable."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.config = load_config()
        self.client = BridgeClient(_ingest_url(), token=load_token() or "")
        self.dispatcher = _Dispatcher()
        self._context_windows: dict[tuple[str, str], Optional[int]] = {}
        self._turn_seen: dict[str, float] = {}

    # -- dispatch ------------------------------------------------------------

    def emit(self, event: str, **payload: Any) -> None:
        clean = {k: v for k, v in payload.items() if v is not None}
        self.dispatcher.submit(lambda: self.client.post_event(event, clean))

    def report(self, **fields: Any) -> None:
        clean = {k: v for k, v in fields.items() if v is not None}
        if clean:
            self.dispatcher.submit(lambda: self.client.post_stats(**clean))

    # -- stats hooks ---------------------------------------------------------

    def on_pre_api_request(self, **kw: Any) -> None:
        model = str(kw.get("model") or "")
        base_url = str(kw.get("base_url") or "")
        provider = str(kw.get("provider") or "")
        key = (model, base_url)
        if model and key not in self._context_windows:
            self._context_windows[key] = _resolve_context_window(model, base_url, provider)
        turn_id = kw.get("turn_id")
        if turn_id and turn_id not in self._turn_seen:
            self._turn_seen[turn_id] = time.time()
            self.emit(p.E_TURN_STARTED, turn_id=turn_id, session_id=kw.get("session_id"),
                      model=model, api_call_count=kw.get("api_call_count"))
        self.report(
            model=model,
            provider=provider,
            base_url=base_url,
            api_call_count=kw.get("api_call_count"),
            approx_input_tokens=kw.get("approx_input_tokens"),
            context_window=self._context_windows.get(key),
        )

    def on_post_api_request(self, **kw: Any) -> None:
        usage = kw.get("usage")
        self.report(
            model=str(kw.get("response_model") or kw.get("model") or ""),
            api_call_count=kw.get("api_call_count"),
            api_duration=kw.get("api_duration"),
            output_tokens=_usage_field(usage, "completion_tokens", "output_tokens"),
            prompt_tokens=_usage_field(usage, "prompt_tokens", "input_tokens"),
            reasoning_tokens=_usage_field(usage, "reasoning_tokens", "completion_tokens_details"),
        )

    # -- lifecycle hooks -----------------------------------------------------

    def on_session_start(self, **kw: Any) -> None:
        self.emit(p.E_SESSION_STARTED, session_id=kw.get("session_id"), model=kw.get("model"),
                  platform=kw.get("platform"))

    def on_session_end(self, **kw: Any) -> None:
        turn_id = kw.get("turn_id")
        if turn_id:
            self._turn_seen.pop(turn_id, None)
        self.emit(p.E_TURN_ENDED, turn_id=turn_id, session_id=kw.get("session_id"),
                  reason=kw.get("turn_exit_reason") or ("interrupted" if kw.get("interrupted") else "completed"),
                  failed=bool(kw.get("failed")))

    def on_session_finalize(self, **kw: Any) -> None:
        self.emit(p.E_SESSION_ENDED, session_id=kw.get("session_id"), reason=kw.get("reason"))

    def on_loop_stopped(self, **kw: Any) -> None:
        self.emit(p.E_LOOP_STOPPED, platform=kw.get("platform"),
                  reason=kw.get("reason") or kw.get("invalidation_reason"))

    # -- tools ---------------------------------------------------------------

    def on_pre_tool_call(self, **kw: Any) -> None:
        tool = str(kw.get("tool_name") or "")
        self.emit(p.E_TOOL_STARTED, tool_name=tool, turn_id=kw.get("turn_id"),
                  tool_call_id=kw.get("tool_call_id"))
        # The clarify tool is the one place the agent stops and asks a human a
        # free-text question. v1 notifies; answering needs an upstream input
        # transport (docs/hermes-integration.md).
        if tool == "clarify" and self.config.notify_questions:
            question = (kw.get("args") or {}).get("question") if isinstance(kw.get("args"), dict) else None
            self.emit(p.E_QUESTION_PENDING, question=_clip(question), surface="cli",
                      tool_call_id=kw.get("tool_call_id"), turn_id=kw.get("turn_id"))
        return None

    def on_post_tool_call(self, **kw: Any) -> None:
        self.emit(p.E_TOOL_FINISHED, tool_name=kw.get("tool_name"), status=kw.get("status"),
                  duration_ms=kw.get("duration_ms"), turn_id=kw.get("turn_id"),
                  tool_call_id=kw.get("tool_call_id"))

    # -- approvals -----------------------------------------------------------

    def present_approval(self, request: Any):
        """The approval transport. Blocking, on a Hermes-owned worker thread."""
        choices = tuple(getattr(request, "allowed_choices", ()) or ())
        pending_id = f"apv_{getattr(request, 'request_id', '')[:16]}"
        payload = {
            "command": _clip(getattr(request, "command", ""), 400),
            "description": _clip(getattr(request, "description", ""), 200),
            "surface": getattr(request, "surface", ""),
            "pattern_key": getattr(request, "pattern_key", ""),
            "timeout_s": getattr(request, "timeout_seconds", None),
            "request_id": getattr(request, "request_id", ""),
            "session_key": None,  # intentionally omitted: session keys are routing data
        }
        timeout = float(getattr(request, "timeout_seconds", 0) or self.config.approval_timeout_s)
        opened = self.client.open_request(
            "approval", payload, choices=choices, timeout=timeout, pending_id=pending_id
        )
        if not opened or not opened.get("ok"):
            raise WatchUnavailable("bridge daemon unreachable")
        if not self.client.answered_by_watch(int(opened.get("delivered") or 0)):
            # Nobody is wearing the watch. Fail closed so Hermes can fall back
            # to the built-in prompt instead of stalling on a dead transport.
            self.client.resolve(pending_id, None, responder="no_watch")
            raise WatchUnavailable("no watch connected")

        with _approvals_lock:
            _open_approvals[pending_id] = str(getattr(request, "tool_call_id", "") or "")
        try:
            answer = self.client.wait_for_answer(pending_id, timeout=timeout)
        finally:
            with _approvals_lock:
                _open_approvals.pop(pending_id, None)

        choice = (answer or {}).get("resolution") if (answer or {}).get("ok") else None
        if choice not in choices:
            choice = "deny"
        log.info("watch answered approval %s: %s", pending_id, choice)
        return request.respond(choice)

    def on_pre_approval_request(self, **kw: Any) -> None:
        # The transport already delivered a full prompt when it is selected;
        # this hook keeps the watch informed when the prompt is shown on another
        # surface (or when the transport is not selected at all).
        self.emit(
            p.E_APPROVAL_REQUESTED,
            id=f"apv_{str(kw.get('turn_id') or '')[:12]}",
            command=_clip(kw.get("command"), 400),
            description=_clip(kw.get("description")),
            pattern_key=kw.get("pattern_key"),
            surface=kw.get("surface"),
            transport=TRANSPORT_NAME,
        )

    def on_post_approval_response(self, **kw: Any) -> None:
        tool_call_id = str(kw.get("tool_call_id") or "")
        with _approvals_lock:
            match = next(
                (pid for pid, tcid in _open_approvals.items() if tcid and tcid == tool_call_id), None
            )
        if match:
            self.dispatcher.submit(
                lambda: self.client.resolve(match, str(kw.get("choice") or "deny"), responder="hermes")
            )
        self.emit(p.E_APPROVAL_RESOLVED, id=match, choice=kw.get("choice"), surface=kw.get("surface"))


def _ingest_url() -> str:
    from .settings import ingest_url as resolve

    return resolve()


def register(ctx: Any) -> None:  # noqa: D401 - Hermes plugin entry point
    """Hermes plugin entry point. Must not raise."""
    try:
        plugin = WatchBridgePlugin(ctx)
    except Exception as exc:
        log.warning("hermes-watch disabled: %s", exc)
        return

    hooks: list[tuple[str, Callable[..., Any]]] = [
        ("pre_api_request", plugin.on_pre_api_request),
        ("post_api_request", plugin.on_post_api_request),
        ("on_session_start", plugin.on_session_start),
        ("on_session_end", plugin.on_session_end),
        ("on_session_finalize", plugin.on_session_finalize),
        ("agent_loop_stopped", plugin.on_loop_stopped),
        ("post_tool_call", plugin.on_post_tool_call),
        ("pre_approval_request", plugin.on_pre_approval_request),
        ("post_approval_response", plugin.on_post_approval_response),
    ]
    if plugin.config.notify_questions:
        hooks.append(("pre_tool_call", plugin.on_pre_tool_call))
    for name, callback in hooks:
        try:
            ctx.register_hook(name, callback)
        except Exception as exc:
            log.warning("could not register %s hook: %s", name, exc)

    try:
        ctx.register_approval_transport(TRANSPORT_NAME, plugin.present_approval)
    except Exception as exc:
        log.warning("could not register %s approval transport: %s", TRANSPORT_NAME, exc)

    log.info("hermes-watch plugin registered against %s", plugin.client.base_url)
