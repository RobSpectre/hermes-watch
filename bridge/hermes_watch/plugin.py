"""The Hermes-side plugin: lifecycle hooks in, watch observations out.

Loaded by Hermes' ``PluginManager`` from ``~/.hermes/plugins/hermes-watch/``.
It does two jobs, and the second is why it still exists at all:

1. **Reports what happened.** Turn starts, tool calls, session ends, and the
   exact provider-call measurements that make a truthful tokens/second and
   context-remaining readout possible.
2. **Routes approvals to the watch** through
   ``ctx.register_approval_transport`` -- but only when the agent is running in
   a process with no watch adapter of its own, which in practice means a plain
   CLI session. A gateway session is dispatched to the adapter directly by
   Hermes's own approval plumbing and never touches this file.

Where an observation goes depends on what is in the process:

* If an adapter is co-located (Hermes as a gateway), it is written straight to
  the in-process bus. No socket, no HTTP, no serialisation.
* Otherwise (a CLI session) it is posted to the gateway's loopback ingest
  endpoint, and a *failure to post is not an error*: it means nobody is running
  a watch listener, so there is nothing to tell.

Hard rules honoured here, because this code runs inside a live agent turn:

* Nothing blocks the agent. Every notification is handed to a bounded background
  queue; overflow is dropped with a warning, never back-pressured into a turn.
* Nothing raises into the agent. Hook callbacks are wrapped whole.
* No transcript content leaves the process -- names, counts, timings and the
  already-redacted approval command only.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from . import protocol as p
from .client import AdapterClient
from .live import bus
from .settings import (
    PLATFORM_LABEL,
    PLATFORM_NAME,
    load_config,
    watch_url,
)

log = logging.getLogger("hermes_watch.plugin")

#: Approval transport name, as named in ``security.approval.transport``.
TRANSPORT_NAME = "pixel-watch"
QUEUE_DEPTH = 256
MAX_TEXT = 240


class WatchUnavailable(RuntimeError):
    """Raised when the watch cannot present the prompt.

    Hermes fails closed on a transport exception and, with
    ``security.approval.transport_fallback: builtin``, re-materialises the
    prompt on the ordinary CLI/TUI surface. That is the intended behaviour when
    no watch is connected: never swallow an approval silently, always let the
    human see it somewhere.
    """


class _Dispatcher:
    """One background thread, bounded queue, drop-on-overflow."""

    def __init__(self, depth: int = QUEUE_DEPTH) -> None:
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
                log.warning("watch queue full; dropped %d observations", self._dropped)

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="hermes-watch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                task()
            except Exception as exc:  # a watch fault is never an agent fault
                log.debug("watch task failed: %s", exc)
            finally:
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

    Imported lazily and guarded. Called from whichever process sees the provider
    call, which also populates Hermes' own context-length cache under the shared
    Hermes home -- so a CLI session doing this work is what lets the gateway's
    readout show a percentage instead of a dash.
    """
    try:
        from agent.model_metadata import get_model_context_length  # type: ignore

        value = get_model_context_length(model, base_url=base_url or "", provider=provider or "")
        return int(value) if value else None
    except Exception as exc:
        log.debug("context window lookup failed for %s: %s", model, exc)
        return None


class WatchPlugin:
    """Holds the wiring so ``register()`` stays readable."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.config = load_config()
        self.client = AdapterClient(watch_url())
        self.dispatcher = _Dispatcher()
        self._context_windows: dict[tuple[str, str], Optional[int]] = {}
        self._turn_seen: dict[str, float] = {}
        #: Approval id -> the pending id the watch knows it by, so the
        #: response hook can clear a prompt answered on another surface.
        self._open_approvals: dict[str, str] = {}
        self._approvals_lock = threading.Lock()

    # -- dispatch ------------------------------------------------------------

    def observe(self, name: str, **payload: Any) -> None:
        """Publish one observation, in-process when we can, over HTTP when we must."""
        clean = {k: v for k, v in payload.items() if v is not None}
        if bus.has_subscriber():
            bus.observe(name, clean)
            return
        self.dispatcher.submit(lambda: self.client.post_event(name, clean))

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
            self.observe(p.E_TURN_STARTED, turn_id=turn_id, session_id=kw.get("session_id"),
                         model=model, api_call_count=kw.get("api_call_count"))
        self.observe(
            "api_request",
            model=model,
            provider=provider,
            base_url=base_url,
            api_call_count=kw.get("api_call_count"),
            approx_input_tokens=kw.get("approx_input_tokens"),
            context_window=self._context_windows.get(key),
        )

    def on_post_api_request(self, **kw: Any) -> None:
        usage = kw.get("usage")
        self.observe(
            "api_request",
            model=str(kw.get("response_model") or kw.get("model") or ""),
            api_call_count=kw.get("api_call_count"),
            api_duration=kw.get("api_duration"),
            output_tokens=_usage_field(usage, "completion_tokens", "output_tokens"),
            prompt_tokens=_usage_field(usage, "prompt_tokens", "input_tokens"),
            reasoning_tokens=_usage_field(usage, "reasoning_tokens", "completion_tokens_details"),
        )

    # -- lifecycle hooks -----------------------------------------------------

    def on_session_start(self, **kw: Any) -> None:
        self.observe(p.E_SESSION_STARTED, session_id=kw.get("session_id"), model=kw.get("model"),
                     platform=kw.get("platform"))

    def on_session_end(self, **kw: Any) -> None:
        turn_id = kw.get("turn_id")
        if turn_id:
            self._turn_seen.pop(turn_id, None)
        self.observe(p.E_TURN_ENDED, turn_id=turn_id, session_id=kw.get("session_id"),
                     reason=kw.get("turn_exit_reason") or ("interrupted" if kw.get("interrupted") else "completed"),
                     failed=bool(kw.get("failed")))

    def on_session_finalize(self, **kw: Any) -> None:
        self.observe(p.E_SESSION_ENDED, session_id=kw.get("session_id"), reason=kw.get("reason"))

    def on_loop_stopped(self, **kw: Any) -> None:
        self.observe(p.E_LOOP_STOPPED, platform=kw.get("platform"),
                     reason=kw.get("reason") or kw.get("invalidation_reason"))

    # -- tools ---------------------------------------------------------------

    def on_pre_tool_call(self, **kw: Any) -> None:
        tool = str(kw.get("tool_name") or "")
        self.observe(p.E_TOOL_STARTED, tool_name=tool, turn_id=kw.get("turn_id"),
                     tool_call_id=kw.get("tool_call_id"))
        # The clarify tool is where a CLI session stops and asks. On a gateway
        # surface Hermes renders and resolves the prompt itself (the adapter
        # implements send_clarify); in a CLI session there is no input transport
        # to answer through, so this stays a notification -- the watch shows
        # "waiting on a question" and the answer is typed in the terminal.
        if tool == "clarify" and self.config.approval_timeout_s > 0:
            args = kw.get("args") if isinstance(kw.get("args"), dict) else {}
            self.observe(p.E_QUESTION_PENDING, question=_clip(args.get("question")), surface="cli",
                         tool_call_id=kw.get("tool_call_id"), turn_id=kw.get("turn_id"))
        return None

    def on_post_tool_call(self, **kw: Any) -> None:
        self.observe(p.E_TOOL_FINISHED, tool_name=kw.get("tool_name"), status=kw.get("status"),
                     duration_ms=kw.get("duration_ms"), turn_id=kw.get("turn_id"),
                     tool_call_id=kw.get("tool_call_id"))

    # -- approvals -----------------------------------------------------------

    def present_approval(self, request: Any):
        """The approval transport. Blocking, on a Hermes-owned worker thread.

        One HTTP request, held open until the watch answers: the request *is*
        the pending state, so there is no registry to keep in step and nothing
        to sweep if this process dies mid-approval.
        """
        choices = tuple(getattr(request, "allowed_choices", ()) or ())
        request_id = str(getattr(request, "request_id", "") or "")
        pending_id = f"apv_{request_id[:16]}" if request_id else None
        timeout = float(getattr(request, "timeout_seconds", 0) or self.config.approval_timeout_s)
        payload = {
            "command": _clip(getattr(request, "command", ""), 400),
            "description": _clip(getattr(request, "description", ""), 200),
            "surface": getattr(request, "surface", ""),
            "pattern_key": getattr(request, "pattern_key", ""),
            "timeout_s": getattr(request, "timeout_seconds", None),
            "request_id": request_id,
        }
        if pending_id:
            with self._approvals_lock:
                self._open_approvals[pending_id] = str(getattr(request, "tool_call_id", "") or "")

        try:
            choice = self.client.ask(
                "approval", payload, choices=choices, timeout=timeout, pending_id=pending_id
            )
        except WatchUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            # Any transport fault has to look like "no watch": Hermes catching a
            # WatchUnavailable is a path it already handles, and an arbitrary
            # exception escaping a transport callback is not.
            raise WatchUnavailable(f"watch transport failed: {exc}") from exc
        finally:
            if pending_id:
                with self._approvals_lock:
                    self._open_approvals.pop(pending_id, None)

        if choice is None or (choices and choice not in choices):
            # Nobody answered: no listener, no paired watch, or a timeout. Fail
            # closed so Hermes falls back to its built-in prompt rather than
            # leaving a dangerous command silently unanswered.
            raise WatchUnavailable("no watch answered the approval")
        log.info("watch answered approval %s: %s", pending_id, choice)
        return request.respond(choice)

    def on_pre_approval_request(self, **kw: Any) -> None:
        """Keep the watch informed of approvals shown on other surfaces.

        The transport above only runs when it is the selected transport; this
        hook fires either way, so a gateway approval (or a terminal one) still
        shows up on the wrist.
        """
        self.observe(
            p.E_APPROVAL_REQUESTED,
            id=f"apv_{str(kw.get('turn_id') or '')[:12]}",
            command=_clip(kw.get("command"), 400),
            description=_clip(kw.get("description")),
            pattern_key=kw.get("pattern_key"),
            surface=kw.get("surface"),
            transport=TRANSPORT_NAME,
        )

    def on_post_approval_response(self, **kw: Any) -> None:
        """Clear the card once the approval is settled anywhere.

        Passing the winning choice back matters: the transport may still be
        parked on a request the terminal just answered, and telling it what was
        chosen lets it respond correctly instead of failing over to a prompt for
        something already decided.
        """
        tool_call_id = str(kw.get("tool_call_id") or "")
        with self._approvals_lock:
            match = next(
                (pid for pid, tcid in self._open_approvals.items() if tcid and tcid == tool_call_id),
                None,
            )
            if match:
                # Settled: drop it now, so a second response cannot resolve a
                # card that is already gone from the wrist.
                self._open_approvals.pop(match, None)
        self.observe(
            p.E_APPROVAL_RESOLVED,
            id=match,
            choice=kw.get("choice"),
            surface=kw.get("surface"),
        )


def _parse_target_ref(ref: str):
    """Resolve a send target on this platform to a chat id.

    A watch's identity *is* its label: the app's hello carries no device id (its
    protocol is frozen and has nowhere to keep one), so the label is what the
    listener keys on and what a caller names here. Any non-empty label parses;
    whether a watch is actually listening is the adapter's business, and it
    reports that honestly rather than accepting a message into the void.
    """
    label = str(ref or "").strip()
    return (label, None) if label else None


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process delivery (``standalone_sender_fn`` contract).

    Hermes calls this when the sending process is not the gateway: ``hermes
    send`` from a script, a cron job's delivery, a tool call in another process.
    The watch listener is part of the gateway, so the message is posted to its
    loopback API and the gateway's adapter puts it on the socket. ``thread_id``
    and ``media_files`` are signature parity only: a watch has no threads and
    this listener takes text.
    """
    extra = getattr(pconfig, "extra", None) or {}
    url = str(extra.get("url") or "") or watch_url()
    token = str(extra.get("ingest_token") or "")
    client = AdapterClient(url, token=token)
    return await asyncio.to_thread(client.notify, message, device=chat_id or "")


def _load_adapter(config: Any) -> Any:
    """Import the adapter only when the gateway actually asks for it.

    ``kind: platform`` plugins are discovered in every process, CLI included,
    and the adapter module imports the gateway's platform machinery. Registering
    the platform is deferred to here so a plain ``hermes`` session pays nothing
    for a listener it is not running.
    """
    from .platform import HermesWatchAdapter

    return HermesWatchAdapter(config)


def _check_requirements() -> bool:
    """Passive probe. This adapter needs no third-party SDK: the listener uses
    aiohttp, which the gateway already depends on."""
    return True


def _validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", None) or {}
    port = extra.get("port")
    if port in (None, ""):
        return True
    try:
        return 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        return False


def _env_enablement() -> Optional[dict]:
    """Env-driven auto-enable, so an env-only setup shows up in gateway status.

    Guarded and lazily imported: this is called from the gateway's config path,
    never from a plain CLI session.
    """
    try:
        from gateway.platforms._shared import get_scoped_secret

        enabled = str(get_scoped_secret("HERMES_WATCH_ENABLED", "") or "").strip().lower()
        if enabled not in ("1", "true", "yes", "on"):
            return None
        config = load_config()
        return {"host": config.watch_host, "port": config.watch_port}
    except Exception:
        return None


def register(ctx: Any) -> None:  # noqa: D401 - Hermes plugin entry point
    """Hermes plugin entry point. Must not raise."""
    try:
        plugin = WatchPlugin(ctx)
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
        ("pre_tool_call", plugin.on_pre_tool_call),
        ("post_tool_call", plugin.on_post_tool_call),
        ("pre_approval_request", plugin.on_pre_approval_request),
        ("post_approval_response", plugin.on_post_approval_response),
    ]
    for name, callback in hooks:
        try:
            ctx.register_hook(name, callback)
        except Exception as exc:
            log.warning("could not register %s hook: %s", name, exc)

    try:
        ctx.register_approval_transport(TRANSPORT_NAME, plugin.present_approval)
    except Exception as exc:
        log.warning("could not register %s approval transport: %s", TRANSPORT_NAME, exc)

    try:
        ctx.register_platform(
            name=PLATFORM_NAME,
            label=PLATFORM_LABEL,
            adapter_factory=_load_adapter,
            check_fn=_check_requirements,
            validate_config=_validate_config,
            required_env=[],
            install_hint="No extra packages: the watch listener uses aiohttp, which Hermes already ships.",
            env_enablement_fn=_env_enablement,
            allowed_users_env="HERMES_WATCH_ALLOWED_USERS",
            allow_all_env="HERMES_WATCH_ALLOW_ALL_USERS",
            # Addressing and out-of-process delivery. Without a target parser,
            # `hermes send -t pixel_watch:"Pixel Watch 4"` fails to resolve at
            # all ("the plugin parser did not recognize it"); without the
            # standalone sender it needs a gateway adapter in the same process,
            # which a cron job or a plain script does not have.
            parse_target_ref_fn=_parse_target_ref,
            standalone_sender_fn=_standalone_send,
            cron_deliver_env_var="HERMES_WATCH_HOME_CHANNEL",
            max_message_length=0,
            emoji="⌚",
            platform_hint=(
                "You are replying to a Wear OS watch. Answers are read on a 2-inch screen, often "
                "glancing: keep them to a couple of short lines, lead with the answer, and skip "
                "preamble. Long output belongs in a file or a chat surface, not here."
            ),
        )
    except Exception as exc:
        # Registration is a gateway capability. A CLI session has no adapter to
        # offer and must not lose the hooks above because of it.
        log.debug("watch platform not registered in this process: %s", exc)

    log.info("hermes-watch plugin registered (watch at %s)", plugin.client.base_url)
