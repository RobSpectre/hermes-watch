# Hermes integration notes

Which Hermes APIs this project relies on, which ones it deliberately avoids, and
the one it needs and does not have. Written against **Hermes Agent v0.21.5+
(2026.9.24)**, documented at <https://hermes-agent.nousresearch.com/docs>.

## Surfaces used

### 1. Plugin hooks — `ctx.register_hook(name, callback)`

Loaded from `~/.hermes/plugins/hermes-watch/` by `PluginManager`; requires the
plugin's name in `plugins.enabled` (general plugins are opt-in). Registered
callbacks are called in-process on the agent's thread, so this project treats
every hook body as a place to *enqueue* and nothing else.

| Hook | Why the project uses it | Payload fields read |
|---|---|---|
| `pre_api_request` | Establishes "a turn started" (once per `turn_id`), and the **pre-call** context size | `model, provider, base_url, api_call_count, approx_input_tokens, turn_id, session_id` |
| `post_api_request` | The only source of **exact** generation latency and token usage | `api_duration, usage{prompt_tokens,completion_tokens,reasoning_tokens}, response_model, api_call_count` |
| `post_tool_call` | Drives the `tool` state and the tool timer | `tool_name, status, duration_ms, tool_call_id, turn_id` |
| `pre_tool_call` | Detects `clarify` — the agent asking a human a free-text question | `tool_name, args.question` |
| `on_session_start/end/finalize`, `agent_loop_stopped` | Session boundaries and interruption | `session_id, model, platform, turn_exit_reason` |
| `pre_approval_request`, `post_approval_response` | Fire even when another surface shows the prompt, so the watch can clear a stale prompt | `command, description, pattern_key, surface, tool_call_id, choice` |

Category note: all of these are **observer** hooks except `pre_tool_call`, which
is directive/control. This plugin returns `None` from it and never blocks or
rewrites a tool call — a notifier has no business in the control path, and
`pre_tool_call` can fail *closed* if it times out.

### 2. Approval transport — `ctx.register_approval_transport(name, present_fn)`

The feature that makes this project worth building. Hermes lets a plugin change
*where a human sees and answers* an approval; the host keeps ownership of
detection, policy, scope validation, and timeouts.

```python
def register(ctx):
    ctx.register_approval_transport("pixel-watch", present)
```

Verified contract (from `hermes_cli/approval_transport.py` in v0.21.5):

* `present(request)` may be sync or async. It runs on a bounded worker
  thread; async callbacks are awaited with `asyncio.run` **on that worker**, never
  on the gateway/TUI loop. This project uses a sync callback doing one blocking
  HTTP request that stays open until the watch answers.
* The request is an immutable `ApprovalRequest` with `request_id`, `digest`,
  `command`, `description`, `pattern_key`, `pattern_keys`, `surface`,
  `timeout_seconds`, `allowed_choices`.
* Return `request.respond(choice)` — a decision **bound to that request**. The
  host rejects unbound dicts and stale ids/digests.
* A plugin cannot grant a scope the host didn't offer.
* Registration alone does nothing: the user also selects it with
  `security.approval.transport: pixel-watch`.
* Exceptions, timeouts, invalid choices and stale responses **deny by default**.
  That is why the plugin raises `WatchUnavailable` when no watch is connected:
  it is the sanctioned way to say "not me, use something else", which shows the
  normal prompt when `security.approval.transport_fallback: builtin`.

### 3. Platform adapter — `ctx.register_platform(...)`

The watch socket is hosted by the gateway as a platform (`pixel_watch`), which is
what lets Hermes own connection lifecycle, pairing, delivery and authorization
instead of this project reimplementing them. Contracts verified against
`gateway/platforms/base.py` and `gateway/platform_registry.py` in v0.21.5:

* `register_platform(name, label, adapter_factory, check_fn, ...)` where `name` is
  a **raw lowercase string**. `Platform("pixel_watch")` resolves through the
  registry, so no enum change is needed; `platform_registry.register` must be
  called before any `Platform(...)` lookup for that name.
* **The adapter never signals "unauthorized".** Authorization is the runner's
  job: `run_inbound._is_user_authorized_for_source` checks the pairing store, and
  a DM from an unknown device goes down `generate_code` → the owner runs
  `hermes pairing approve <platform> <code>`. `PairingStore` is keyed on the same
  raw string and lives in `~/.hermes/platforms/pairing/`, so a plugin platform
  gets pairing for free — including `hermes pairing list|approve|revoke`.
* **Inbound goes through `self.handle_message(MessageEvent(...))`** with a source
  from `self.build_source(...)`. Bypassing it would skip session guards, profile
  routing and the delivery ledger.
* **`connect()` is where you bind.** Holding a socket in `__init__` leaks file
  descriptors for adapters the runner abandons; a bind failure should call
  `_set_fatal_error(..., retryable=False)` rather than spin the reconnect watcher.
  `disconnect()` must be idempotent and end with `_mark_disconnected()`.
* **A client disconnecting is not the platform disconnecting.** Report
  `send_path_degraded()` and keep returning `True`.
* **`enforces_own_access_policy` defaults to False**, so a socket-hosting adapter
  authenticates its own peers — this one rejects frame answers from unpaired
  devices and withholds snapshots from them.
* One adapter instance exists per connection attempt, and each profile binds its
  own port, so `extra.port` has to be per-profile if you run several.

The platform is registered through a **lazy factory**: the adapter module imports
gateway machinery, and `kind: platform` plugins are discovered in every process
including a plain CLI session, so the import happens only when the gateway
actually builds an adapter.

### 4. The session store — `$HERMES_HOME/state.db`

Opened **read-only** (`file:...?mode=ro`), because the bridge must never be able
to corrupt a live agent's history. Columns used, all from `sessions`:

`id, source, model, started_at, ended_at, message_count, tool_call_count,
api_call_count, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, reasoning_tokens, estimated_cost_usd, cost_status, title,
profile_name, last_activity_at, archived, hidden`.

Notes that shaped the code:

* Hermes targets WAL mode, so a second reader is free — the aggregate queries
  never block a running turn.
* `messages.token_count` is **NULL in practice**: Hermes does not backfill it.
  The transcript therefore cannot be summed to derive context usage. Context used
  falls back to `(input_tokens + cache_read_tokens) / api_call_count` and is
  labelled `db_estimate` rather than presented as fact.
* The path is `get_hermes_home() / "state.db"`, which is
  `~/.hermes/profiles/<name>/state.db` under a named profile. Resolve it from
  `$HERMES_HOME`; never hard-code `~/.hermes`.

### 5. Context window — `agent.model_metadata.get_model_context_length`

Imported lazily inside the plugin, which runs in the agent's process, so the
full resolution chain applies (config override → provider API → models.dev →
fallbacks). It is wrapped in `try/except`: a trimmed install or a future rename
degrades the context percentage to "unknown", never breaks the hook.

Both other readers of this number — the adapter (inside the gateway) and the
standalone CLI — read the cache Hermes writes at
`$HERMES_HOME/context_length_cache.yaml` (`context_lengths["<model>@<base_url>"]`).
Narrower, and documented as such. In practice the plugin's live observation wins
whenever a turn is running, so this only covers the cold-start case: the adapter
is up, no plugin has reported anything yet.

## Surfaces deliberately not used

* **Outbound webhooks** (`hooks.outbound:` in `config.yaml`) would push lifecycle
  events to the bridge without a plugin, and are a fine alternative for the
  stats-only half. They are not enough here: they are notify-only, cannot carry
  a blocking approval to a decision, and would need a second
  `pre_approval_request`-shaped event that isn't in their documented set. The
  plugin gives one code path for both halves.
* ~~**Gateway platform adapters** (`ctx.register_platform`)~~ — **adopted.** The
  watch *is* registered as a platform (`pixel_watch`), because that is what makes
  Hermes own the socket, the pairing, the delivery ledger and the authz decision
  instead of this project reimplementing them. What we do not use from the
  platform machinery: the message loop is used only for a watch's own messages
  (which need pairing anyway), and no chat surface is registered.
* **`ctx.inject_message`** — could push a "your watch said X" into a session, but
  that is a *new user message*, not an approval decision. Wrong tool.

## The gap: answering questions

Hermes exposes an approval transport but **no input transport**. When the agent
calls `clarify`, the question is presented by the CLI/TUI/gateway and the answer
is collected there; a plugin can observe that it happened (`pre_tool_call` with
`tool_name == "clarify"`) but cannot present or answer it.

So v1 of this project is **notify-only for questions**, and the roadmap entry is
a Hermes-side feature request rather than a watch-side one:

> `ctx.register_input_transport(name, present_fn)` — a sibling of
> `register_approval_transport` for free-text human input: immutable request
> (`prompt`, `surface`, `timeout_seconds`, `allowed_replies` maybe), `respond(text)`,
> `security.input.transport` + `transport_fallback`, fail-closed on timeout.

Until that exists, the watch can buzz and show the question so you know to go
back to the terminal. Claiming more than that would be a lie about what the
platform supports.

## Compatibility watch-list

Things that would break this project if they changed upstream, and how it fails:

| Upstream change | Effect | Mitigation in this repo |
|---|---|---|
| `APPROVAL_CHOICES` renamed | Approvals break | `test_approval_choices_match_the_hermes_approval_transport_contract` fails loudly |
| Hook payload field renamed | A stat silently becomes "unknown" | Each field is read defensively; the watch shows a dash rather than a zero |
| `messages.token_count` backfilled | Better context estimate available | `db_estimate` can then be replaced by a real sum; the `source` field already distinguishes methods |
| `state.db` schema change | Stats go blank | Queries select named columns only; a missing column raises where the stats were read (the adapter or the CLI), never inside a Hermes turn |
| `get_model_context_length` moved | Context % becomes unknown | Guarded import, falls back to the cache file |
