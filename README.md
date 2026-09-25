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
| Push notification when Hermes blocks on a tool approval | **working** (bridge + protocol + tests; app code ready) |
| Approve / deny / allow-for-session from the watch notification | **working** (same) |
| Live notification when the agent is waiting on a question | **working**, notify-only |
| Tokens/sec, context remaining, session tokens, 30-day totals | **working** |
| Watch tile + stats screen | app code written, not yet built on a device |
| Complication | not started |

Honest status: the **bridge is finished and verified** — 46 tests including a
live approval round-trip over real sockets, plus a run against a real Hermes
session store. The **Wear OS app is written but has never been compiled or run**
(this repo was created on a machine with no Android SDK; CI compiles it). See
[ROADMAP.md](ROADMAP.md).

## How it works

```
   Hermes process                     hermes-watch-bridge                Pixel Watch
 ┌───────────────────┐               ┌──────────────────────┐         ┌──────────────┐
 │ plugin hooks      │  POST events  │  ingest 127.0.0.1:8788│         │              │
 │  pre_api_request  ├──────────────►│                      │         │              │
 │  post_tool_call   │               │        Hub           │  WS     │  foreground  │
 │  agent_loop_stop  │               │  · agent state       ├────────►│  service     │
 │                   │               │  · pending approvals │  :8787  │      │       │
 │ approval          │               │  · stats snapshots   │         │      ▼       │
 │  transport  ◄─────┼───long-poll───┤                      │◄────────┤  notifications│
 │  present(request) │   the decision│                      │ answer  │  + tile + UI │
 └─────────┬─────────┘               └──────────┬───────────┘         └──────────────┘
           │                                    │ read-only
           │ reads live usage/latency           │
           │                                    ▼
           │                          ~/.hermes/state.db
           │                          (sessions: tokens, cost, api_calls)
           ▼
     agent.model_metadata
     get_model_context_length()   ← the real context window
```

Three components, and the split matters:

* **The plugin** runs *inside* a live Hermes process. It subscribes to lifecycle
  hooks (`pre_api_request`, `post_api_request`, `post_tool_call`,
  `agent_loop_stopped`, …) and registers an **approval transport**, which is
  Hermes' supported way to change *where* a human sees and answers an approval.
  It never blocks the agent: every hook hands work to a bounded background queue.
* **The bridge daemon** is a separate process because the state must outlive any
  one Hermes process, and because a watch socket has no business living in the
  agent's event loop. It owns watch connections, pending requests, and stats.
* **The watch app** is a thin client: one WebSocket, notifications, a tile, and
  a stats screen. No logic the bridge could do instead.

## Quickstart

**1. Install the bridge** (Python 3.10+, needs `aiohttp` and `PyYAML`):

```bash
pip install ./bridge          # provides the `hermes-watch-bridge` command
hermes-watch-bridge serve
```

The first run prints the LAN address to pair with and creates a pairing token at
`$HERMES_HOME/hermes-watch/token` (mode 0600). Leave it running.

**2. Install the Hermes plugin** so Hermes starts feeding the bridge:

```bash
hermes plugins install RobSpectre/hermes-watch --enable
```

Then set the approval transport and the fallback, in `~/.hermes/config.yaml`:

```yaml
security:
  approval:
    transport: pixel-watch
    transport_fallback: builtin   # show the normal prompt when no watch is connected
```

`transport_fallback: builtin` is the important line: without it, a failed
transport denies by default, and an approval you never saw would be silently
refused. With it, Hermes falls back to the terminal prompt whenever your watch
isn't connected.

**3. Check it works without a watch** — this is also how you develop the app:

```bash
hermes-watch-bridge stats                 # the same numbers the watch shows
python bridge/tools/fake_watch.py --answer once
```

**4. Pair the watch.** Open the app, enter `host:port` and the pairing token.
(Wear OS app build instructions: [docs/setup.md](docs/setup.md).)

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
bridge/                       the Python half — complete, tested
  hermes_watch/
    protocol.py               the wire contract, in code
    stats.py                  state.db queries + live measurements
    hub.py                    connections, pending approvals, agent state
    daemon.py                 aiohttp HTTP + WebSocket listeners
    plugin.py                 the Hermes plugin (hooks + approval transport)
    client.py                 stdlib client the plugin uses
    cli.py                    hermes-watch-bridge serve|stats|token|doctor
  tests/                      46 tests, incl. approval round-trip over sockets
  tools/fake_watch.py         terminal stand-in for the watch
watch/                        the Wear OS half — written, not yet built
  app/src/main/java/...       Compose for Wear UI, foreground service, tile
docs/
  protocol.md                 normative WebSocket contract
  hermes-integration.md       which Hermes APIs this uses, and which it can't
  setup.md                    build, install, pair, troubleshoot
```

## Security

The pairing token is the only thing between your LAN and the ability to approve
tool calls on your machine, so treat the watch port as a trust boundary:

* The plugin ingest port binds to `127.0.0.1` and is never exposed. The WebSocket
  endpoint is deliberately *not* served on the loopback listener.
* Token comparisons are constant-time; the token file is created `0600` before
  anything is written to it.
* Every unanswered request **fails closed**: a timeout, a disconnect, or an
  unparseable frame becomes a *denial*, never an allow.
* A watch cannot return a scope Hermes did not offer (`always` on a once-only
  request is rejected by the host).
* Tool arguments and transcript content are never forwarded — tool *names*,
  counts, ids, and the already-redacted approval command only.
* `pre_approval_request` payloads can contain secrets in the command text; the
  bridge clips and forwards the command because that is exactly what the human
  needs to see to decide. Point it at networks and hosts you control.

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
