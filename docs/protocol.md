# Wire protocol v1

Normative contract between the **watch listener** and a Wear OS client. The
implementation is [`bridge/hermes_watch/protocol.py`](../bridge/hermes_watch/protocol.py);
the Kotlin side is [`watch/app/src/main/java/.../data/Protocol.kt`](../watch/app/src/main/java/com/nousresearch/hermeswatch/data/Protocol.kt).
If this document and the code disagree, the code's tests
(`bridge/tests/test_protocol.py` and `bridge/tests/test_watch_contract.py`)
decide — and this document is wrong.

The watch app is built and released separately, so treat everything below as
**frozen**: additions only, and `bridge/tests/test_watch_contract.py` speaks the
app's exact frames to prove it. If those tests fail, watches in the field break.

## Transport

| | |
|---|---|
| Watch → listener | `ws://<host>:<port>/v1/watch?device=<label>` (the app also sends `&token=…`, ignored) |
| Plugin → listener | `POST http://127.0.0.1:<port>/event` (loopback; optional `ingest_token` in `extra` for a shared secret) |
| Liveness | `GET http://<host>:<port>/healthz` — no auth, no data, just `{ok, state, watches, authorized}` |
| Encoding | One JSON object per WebSocket **text** frame. No batching, no newline framing. |
| Heartbeat | Standard WebSocket ping/pong, 30 s, driven by the listener; a `ping` JSON frame every 30 s, answered by the app with `pong`. |
| Max frame | 64 KiB client→server, server frames are small by construction. |

**There is no shared secret.** Access is granted by Hermes' own pairing store:
the socket is accepted, the device is told it is unpaired, one message from it
triggers Hermes' standard unauthorized-device reply with an 8-character code,
and the owner approves it on the host with
`hermes pairing approve pixel_watch <code>`. The `token` query parameter is a
relic of the pre-gateway design and is accepted-and-ignored so older app builds
keep working.

Pairing is keyed on the connection's **device identity**, which is the
`device` query parameter if present, else the hello's `label` (the app sends
both, with the same value).

## Versioning

* Every server frame carries `"v": 1`.
* Additions are allowed inside a version: new `type`s, new `event`s, new
  optional fields. A client **must** ignore a `type` or field it does not know.
* Removals or type changes require a new `v`. A client that receives a `v` it
  does not implement must show "bridge too new", not guess.
* The connect handshake reports both `protocol` (the integer) and
  `bridge_version` (the semver) so a bug report can name both.

## Server → client frames

### `hello` — first frame after connect

```json
{"v":1,"type":"hello","bridge_version":"0.1.0","protocol":1,
 "server_time":1790354460.61,"profile":"default"}
```

### `snapshot` — second frame after connect, and the reply to `stats.request`

The complete current state. Always self-contained: a client that renders only
this frame is fully caught up.

```json
{"v":1,"type":"snapshot","ts":1790354460.62,
 "session":{
   "id":"20260925_115008_1e880d","title":"Create Pixel Watch app for Hermes alerts",
   "source":"cli","model":"deepseek-ai/DeepSeek-V4.1-Flash","profile":"default",
   "started_at":1790351489.97,"elapsed_s":2973.0,"ended":false,
   "message_count":135,"tool_call_count":81,"api_call_count":54,
   "tokens":{"input":463830,"output":58872,"cached_read":5122560,
             "cached_write":0,"reasoning":647,"total":5645262},
   "cost_usd":0.0,"cost_status":"unknown",
   "tok_per_s":{"live":null,"live_source":null,
                "session_avg":19.81,"session_avg_source":"wall_clock_includes_tool_time"}},
 "context":{
   "used_tokens":103452,"window_tokens":null,"remaining_tokens":null,
   "remaining_pct":null,"used_pct":null,"source":"db_estimate"},
 "usage":{
   "window_days":30,"session_count":3,"api_calls":6,"cost_usd":0.0,
   "tokens":{"input":49294,"output":635,"cached_read":48896,"cached_write":0,
             "reasoning":125,"total":98825},
   "by_model":[{"model":"deepseek-ai/DeepSeek-V4.1-Flash","session_count":3,
                "cost_usd":0.0,"tokens":{"input":49294,"output":635,
                "cached_read":48896,"cached_write":0,"reasoning":125,"total":98825}}],
   "by_day":[{"date":"2026-09-25","tokens":98825}]},
 "live":{"agent_state":"idle","state_changed_at":1790354000.0,"last_tool":null},
 "pending":[],
 "bridge":{"version":"0.1.0","profile":"default","uptime_s":5.6,
           "watches":[{"id":"pixel-watch","device":"pixel-watch",
                       "remote":"192.168.1.31","connected_at":1790354460.6,
                       "last_seen":1790354460.6,"protocol":1,"age_s":0.0}]}}
```

`session` is `null` when the store has no row to show. Every field above is
optional from the client's point of view: **render a dash for anything absent,
never a zero.**

### `event` — something happened

```json
{"v":1,"type":"event","event":"approval.requested","id":"apv_9f2c...","payload":{...}}
```

| `event` | `payload` | Meaning |
|---|---|---|
| `session.started` | `session_id, model, platform` | New session |
| `turn.started` | `turn_id, session_id, model, api_call_count` | First API call of a turn; agent is thinking |
| `turn.ended` | `turn_id, session_id, reason, failed` | Turn finished |
| `tool.started` | `tool_name, turn_id, tool_call_id` | A tool is running |
| `tool.finished` | `tool_name, status, duration_ms, turn_id, tool_call_id` | …and stopped |
| `approval.requested` | `id, kind, choices[], deadline, remaining_s, created_at, payload{command, description, pattern_key, surface, request_id, timeout_s}` | **The agent is blocked on you.** Actionable |
| `approval.resolved` | `id, kind, resolution, responder` | Answered, timed out (`resolution: null`), or answered elsewhere (`responder: "hermes"`) |
| `question.pending` | `question, surface, tool_call_id, turn_id` | Agent asked a question. **Notify-only in v1** |
| `question.resolved` | `id, resolution, responder` | Question cleared |
| `session.ended` | `session_id, reason` | Session finalised |
| `loop.stopped` | `platform, reason` | The turn was interrupted |

`payload` for `approval.requested` is the pending request document:

```json
{"id":"apv_9f2c1a","kind":"approval","created_at":1790354468.67,
 "deadline":1790354498.67,"remaining_s":29.9,
 "choices":["once","session","deny"],"resolution":null,"resolved_at":null,
 "payload":{"command":"rm -rf ~/build/cache",
            "description":"recursive delete outside the workspace",
            "pattern_key":"rm_rf","surface":"cli",
            "request_id":"9f2c1a...","timeout_s":300.0}}
```

`choices` is exactly what Hermes offered this request. **Render only those
buttons.** A watch that offers `always` for a once-only approval gets the answer
rejected by the host (see `test_watch_cannot_return_a_choice_the_host_did_not_offer`).

### `stats` — periodic refresh

Same shape as `snapshot`, sent every **5 s** while the agent is idle and every
**1.5 s** while it is busy. Clients should not poll on top of this.

### `ping` / `error`

`{"v":1,"type":"ping","server_time":...}` is sent only if a client has been
silent for a long time; answer with `pong`. `error` carries `{"error":"...",
"id":"<optional>"}` for a rejected client frame — a client should surface it and
carry on.

## Client → server frames

| Frame | Effect |
|---|---|
| `{"v":1,"type":"hello","label":"Pixel Watch 3"}` | Optional; updates the device label and triggers a fresh `snapshot` |
| `{"v":1,"type":"answer","id":"apv_...","choice":"once"}` | Resolves a pending approval. `choice` ∈ `once`, `session`, `always`, `deny` |
| `{"v":1,"type":"answer","id":"qst_...","choice":"reply","text":"..."}` | Reserved for v2 (answering questions); the bridge accepts the frame shape and currently ignores `text` |
| `{"v":1,"type":"stats.request","session_id":"..."}` | Ask for a fresh `snapshot` (optionally for another session) |
| `{"v":1,"type":"pong"}` | Answer to `ping` |

Unknown types and unknown fields are ignored, not errors. That is what lets the
app and bridge be upgraded independently.

## Failure semantics

The interesting half of the contract:

* **Unanswered approval** → after `deadline`, the bridge resolves it with
  `resolution: null` and `responder: "timeout"`. The plugin turns `null` into
  `deny`. Nothing is ever approved by inaction.
* **Watch disconnects while an approval is open** → the request stays pending on
  the bridge with its deadline intact. The plugin's transport keeps waiting, and
  if the deadline passes, denies. Reconnecting within the window redelivers the
  prompt in the next `snapshot` (from `pending[]`).
* **No watch connected when the approval opens** → the bridge answers with
  `delivered: 0`, and the plugin raises `WatchUnavailable` *immediately* rather
  than stalling. With `transport_fallback: builtin`, Hermes then shows its normal
  terminal prompt.
* **Stale or unknown `answer`** → `error` with `error: "stale_or_unknown_request"`.
  The request is untouched.
* **Bridge unreachable** → the plugin drops the event silently and the watch
  shows a stale snapshot. A dead bridge degrades the watch, never the agent.
