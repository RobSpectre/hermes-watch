# Hermes Watch

[![bridge](https://github.com/RobSpectre/hermes-watch/actions/workflows/bridge-ci.yml/badge.svg)](https://github.com/RobSpectre/hermes-watch/actions/workflows/bridge-ci.yml)
[![watch](https://github.com/RobSpectre/hermes-watch/actions/workflows/watch-ci.yml/badge.svg)](https://github.com/RobSpectre/hermes-watch/actions/workflows/watch-ci.yml)

Your agent, on your wrist. A Wear OS app for the Pixel Watch that tells you when
Hermes Agent is **waiting for you** — and lets you answer from the watch — plus a
live readout of how the run is actually going: tokens per second, context
remaining, tokens this session, tokens over the last 30 days.

Approvals are two-way. Questions are notify-only in v1 (see
[Roadmap](#roadmap) for why).

```
   ┌──────────────────────────┐          ┌──────────────────────────┐
   │  Hermes needs approval   │          │  HERMES                  │
   │  ─────────────────────   │          │  deepseek-v4.1-flash     │
   │  rm -rf ~/build/cache    │          │  ─────────────────────   │
   │  recursive delete        │          │  88.4 tok/s   1,204 tok  │
   │                          │          │  context ██████░░ 71%    │
   │   [ ONCE ]    [ DENY ]   │          │  30d  5.6M tok  $1.84    │
   └──────────────────────────┘          └──────────────────────────┘
        tap ONCE                    the live stats screen
```

## What it does

| Feature | Status |
|---|---|
| Push notification when Hermes blocks on a tool approval | **working** (adapter + plugin + 87 tests; app code in CI) |
| Approve / deny / allow-for-session from the watch notification | **working** |
| Same approvals from a **CLI** session, not just the gateway | **working** (in-process approval transport) |
| Live notification when the agent is waiting on a question | **working**, notify-only |
| Tokens/sec, context remaining, session tokens, 30-day totals | **working** |
| Watch tile + stats screen | app code written, not yet built on a device |
| Complication | not started |

Honest status: the **Python half is finished and verified** — 87 tests, including
real-socket round trips for both approval paths and a contract suite that pins
the app's own frames. The **Wear OS app is written but has never been compiled or
run by hand** (this repo was created on a machine with no Android SDK; CI
compiles it). See [ROADMAP.md](ROADMAP.md).

## How it works

The watch is a **Hermes gateway platform**, not a sidecar. That means Hermes'
own gateway owns the socket, the pairing, the delivery ledger and the approvals,
and there is no second daemon to run, supervise or secure.

```
   Hermes gateway process                                   Pixel Watch
 ┌──────────────────────────────────────────────┐        ┌──────────────┐
 │  plugin hooks    ─► in-process bus (live.py)  │        │              │
 │   pre_api_request, post_api_request, ...      │        │  foreground  │
 │                                               │        │  service     │
 │  gateway runner ─► runs the platform adapter: │  WS    │      │       │
 │   · WS listener  :8787  /v1/watch   ◄─────────┼────────┤      ▼       │
 │   · pending approvals + questions             │        │  notifications
 │   · pairing via PairingStore                  │        │  + tile + UI │
 │   · stats snapshots ──────────────────────────┼────────┤              │
 │                                               │        └──────────────┘
 │  approval transport ◄── HTTP 127.0.0.1:8787 ──┼── a CLI session's
 │                       (only when the gateway │   approval, routed
 │                        isn't the one asking) │   through the plugin
 └───────────────────────┬───────────────────────┘
                         │ read-only
                         ▼
                   ~/.hermes/state.db
                   (sessions: tokens, cost, api_calls)
```

Three pieces, each with one job:

* **`platform.py` — the adapter.** A `BasePlatformAdapter` subclass the gateway
  builds and runs: it hosts the WebSocket listener, validates frames, holds
  pending approvals/questions, asks `PairingStore` whether a device is approved,
  and routes watch messages back through `handle_message` so they land in normal
  Hermes sessions. It never decides authorization itself — Hermes' runner does
  that centrally, which is why no shared secret exists anywhere in this repo.
* **`plugin.py` — the hooks and the CLI approval transport.** It runs *inside* a
  live Hermes process and streams lifecycle events into the adapter's in-process
  bus when the gateway is right there, or over HTTP when it isn't. It also
  registers the approval transport, which is Hermes' supported way to change
  *where* a human answers. That transport is what makes a **CLI** approval
  reachable from your wrist; the gateway can't do that by itself.
* **The watch app** is a thin client: one WebSocket, notifications, a tile, a
  stats screen. No logic the adapter could do instead — and its wire protocol is
  frozen (see [docs/protocol.md](docs/protocol.md)), because it ships separately.

## Quickstart

**1. Install the plugin** (no Python install step needed — the repo root is the
plugin and adds `bridge/` to `sys.path` itself):

```bash
hermes plugins install RobSpectre/hermes-watch --enable
```

**2. Enable the platform** in `~/.hermes/config.yaml`, and pick the approval
transport for CLI sessions:

```yaml
gateway:
  platforms:
    pixel_watch:
      enabled: true
      extra:
        host: 0.0.0.0        # 127.0.0.1 keeps it on this machine only
        port: 8787

security:
  approval:
    transport: pixel-watch
    transport_fallback: builtin   # the normal prompt when no watch is connected
```

`transport_fallback: builtin` is the important line: without it, a failed
transport denies by default, and an approval you never saw would be silently
refused. With it, Hermes falls back to the terminal prompt whenever your watch
isn't connected.

**3. Start the gateway** — the listener starts with it:

```bash
hermes gateway run          # or the installed service
```

**4. Pair the watch.** Connect the app to `host:port` (the token field is a
leftover from the pre-gateway design: leave it blank), send any message, and
Hermes answers with an 8-character pairing code. Approve it on the host:

```bash
hermes pairing list
hermes pairing approve pixel_watch <code>
```

That is the whole authorization story: no token file, nothing to rotate, and
`hermes pairing revoke pixel_watch <device>` to undo it. (Wear OS app build
instructions: [docs/setup.md](docs/setup.md).)

**5. Check it without a watch** — this is also how you develop the app:

```bash
pip install ./bridge            # optional: only for the standalone CLI
hermes-watch stats              # the same numbers the watch shows
hermes-watch doctor             # listener up? plugin installed? devices paired?
python bridge/tools/fake_watch.py --pair --answer once
```

## Sending notifications to the watch

Approvals and questions notify on their own — that is the point of the app. For
everything else, the watch is a registered platform, so Hermes' normal delivery
paths reach it:

```bash
hermes send -t pixel_watch "build finished"                 # home channel
hermes send -t "pixel_watch:Pixel Watch 4" "build finished"  # a named device
```

`hermes send` is the script/cron path: no LLM, no agent loop. The agent itself
can do the same with its `send_message` tool, and a cron job with
`deliver=pixel_watch`. Set the home channel once so the bare form resolves:

```bash
hermes config set platforms.pixel_watch.home_channel.chat_id "Pixel Watch 4"
hermes config set platforms.pixel_watch.home_channel.platform pixel_watch
```

Anything that is not Hermes at all can use the listener's loopback endpoint
directly:

```bash
curl -X POST http://127.0.0.1:8787/notify \
  -H 'Content-Type: application/json' \
  -d '{"text": "backup finished", "device": "Pixel Watch 4"}'
```

Notes that matter:

* The **device id is the label** the watch sends — the app's protocol carries no
  other identifier. `hermes send -l` lists the platform; an unknown device is an
  error, not a silent drop.
* Delivery **fails honestly**: with no watch connected, `hermes send` reports the
  failure and `/notify` returns HTTP 503 with `{"ok": false}`. Nothing is ever
  reported as sent that did not reach a socket.
* A message arrives on the **Notifications** channel, one notification at a time:
  the newest replaces the previous one rather than stacking up on a 2-inch
  screen.
* `/notify` and `/event` are **loopback only** — the listener is bound to the LAN
  so the watch can reach it, and these endpoints would otherwise let anyone on
  the network speak as Hermes. Set `extra.ingest_token` and pass it as `token` to
  require a shared secret on both.

## What the numbers mean

Precision here is the point of the project, so each number says how it was
obtained and the watch renders a difference between measured and estimated:

| Number | Source | Notes |
|---|---|---|
| **tokens/sec** (live) | `post_api_request.api_duration` ÷ `usage.completion_tokens` | Real provider-call latency. This is what people mean by tok/s. |
| **tokens/sec** (session avg) | session `output_tokens` ÷ wall-clock elapsed | Labelled, and much lower: wall clock includes tool execution and your think time. |
| **context remaining** | last exact `usage.prompt_tokens` (or the pre-call `approx_input_tokens`) ÷ the model's real context window | Window from `agent.model_metadata.get_model_context_length`. If the window is unknown the watch shows `—`, never a guessed percentage. |
| **tokens this session** | `sessions.input_tokens/output_tokens/cache_read_tokens/...` | Cached reads are reported separately, not folded into `input` — providers don't include them, and hiding them understates a long session by an order of magnitude. |
| **tokens last 30 days** | rolling window over `sessions`, excluding the session you're looking at | Per-model and per-day breakdowns included. |

Cold start honesty: with no plugin connected, context used falls back to
`sessions.(input+cache_read) ÷ api_call_count` and is labelled `db_estimate`.
Hermes does not backfill `messages.token_count`, so the transcript itself cannot
be summed — the per-call average is the best available proxy and says so.

## Repository layout

```
__init__.py                   the Hermes plugin entry point (shim → bridge/)
plugin.yaml                   plugin manifest (kind: platform)
bridge/                       the Python half — complete, tested
  hermes_watch/
    protocol.py               the wire contract, in code
    platform.py               the gateway platform adapter + WS listener
    plugin.py                 hooks, in-process bus, approval transport
    live.py                   the bus shared by the plugin and the adapter
    stats.py                  state.db queries + live measurements
    settings.py               address/timeout config, no secrets
    client.py                 stdlib HTTP client the plugin uses
    cli.py                    hermes-watch stats|doctor|sessions
  tests/                      87 tests, incl. two real-socket approval paths
                                and a frozen contract suite for the app
  tools/fake_watch.py         terminal stand-in for the watch
watch/                        the Wear OS half — written, built by CI
  app/src/main/java/...       Compose for Wear UI, foreground service, tile
docs/
  protocol.md                 normative WebSocket contract (frozen)
  hermes-integration.md       which Hermes APIs this uses, and which it can't
  setup.md                    build, install, pair, troubleshoot
```

## Security

Design choices, not promises:

* **No shared secret.** Authorization is Hermes' `PairingStore`: a device the
  owner approved on the host, and nothing else. There is no token file in this
  repository's code path to leak, rotate or forget.
* **Pairing is keyed on the watch's self-declared label.** The app sends no
  device id (its protocol is frozen and it has nowhere to keep one), so a rogue
  LAN client claiming an approved label would be treated as that device. Treat
  the watch port as a LAN trust boundary: bind it to a private interface, or pin
  `HERMES_WATCH_ALLOWED_USERS` to the exact labels you own.
* **Unauthenticated sockets get no data.** A watch that isn't paired receives a
  hello and a pairing code, never a snapshot, and its answers are refused.
* **Every unanswered request fails closed**: a timeout, a disconnect, or an
  unparseable frame becomes a *denial* or falls back to the terminal prompt,
  never an allow.
* **A watch cannot return a scope Hermes did not offer** (`always` on a
  once-only request is rejected, both by the adapter and by Hermes).
* **Tool arguments and transcript content are never forwarded** — tool *names*,
  counts, ids, and the already-redacted approval command only.
* **The plugin never raises into the agent.** Hooks queue and drop on overflow;
  a watch fault is never an agent fault.

## Roadmap

Short version; details and reasoning in [ROADMAP.md](ROADMAP.md).

1. Build the app (CI compiles it; needs a device run).
2. Answering questions from the watch — blocked on a Hermes input-transport API
   that doesn't exist yet; see [docs/hermes-integration.md](docs/hermes-integration.md).
3. Phone relay for when the watch is Bluetooth-only and off Wi-Fi.
4. Tile + complication polish, always-on ambient mode.
5. FCM relay for notifications away from home.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Google, Fitbit, or Nous Research. "Pixel Watch" and
"Wear OS" are trademarks of Google LLC; "Hermes Agent" is a project of Nous
Research.
