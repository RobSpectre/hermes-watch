# Contributing

## Layout

| Path | What it is |
|---|---|
| `bridge/hermes_watch/` | The Python half: protocol, stats, hub, daemon, Hermes plugin, CLI |
| `bridge/tests/` | pytest — the authoritative check on the wire contract |
| `bridge/tools/fake_watch.py` | A terminal stand-in for the watch; use it instead of a device |
| `watch/app/` | The Wear OS app (Kotlin, Compose for Wear) |
| `docs/protocol.md` | The normative wire contract |
| `docs/hermes-integration.md` | Which Hermes APIs are used, and which are missing |

## Building and testing

```bash
# bridge
python -m venv .venv && . .venv/bin/activate
pip install -e ./bridge pytest pytest-asyncio
cd bridge && pytest -q

# watch
cd watch && gradle wrapper --gradle-version 8.9 && ./gradlew :app:assembleDebug
```

Run the end-to-end loop by hand before opening a PR that touches the protocol or
the approval path:

```bash
hermes-watch-bridge serve &
python bridge/tools/fake_watch.py --answer once &   # answers the first approval
curl -s -X POST http://127.0.0.1:8788/v1/pending \
  -H "Authorization: Bearer $(hermes-watch-bridge token --show)" \
  -d '{"kind":"approval","id":"apv_x","timeout":30,"choices":["once","deny"],"payload":{"command":"rm -rf /tmp/x"}}'
```

## Rules that are not negotiable

These exist because the failure mode is "the agent did something you did not
approve", which is worse than any bug that just breaks the display.

1. **Fail closed, always.** Timeouts, disconnects, unparseable frames and unknown
   requests all resolve to *deny*, never to allow. If you add a code path that can
   return an approval without a human answering it, it is wrong.
2. **Never block the agent.** Plugin hooks enqueue and return. If your change can
   make a tool call wait on the network, it is wrong. The single exception is the
   approval transport, which is *supposed* to block on a human — on a bounded
   worker thread, with the host's own timeout enforced.
3. **Never raise into the agent.** Every hook body and every transport call is
   wrapped. `register()` must survive a hostile `ctx`.
4. **No invented numbers.** If a value is unknown, it is `null` and the watch
   renders an em dash. If it is derived rather than measured, it carries a
   `source` string and the watch shows "(est.)". A plausible-looking zero is
   worse than a dash.
5. **Additive protocol changes only.** New frame types and new optional fields are
   fine inside v1. Renaming or retyping a field requires a version bump and a
   client that refuses to guess.
6. **No transcript content over the wire.** Tool names, counts, ids, and the
   already-redacted approval command. Not tool arguments, not message bodies.
7. **Read `state.db` read-only.** The bridge must never be able to damage a live
   agent's history.

## Tests

* Protocol change → a test in `bridge/tests/test_protocol.py` and an update to
  `docs/protocol.md`, in the same PR.
* Stats change → a test in `test_stats.py` that pins the label (`live`,
  `live_approximate`, `db_estimate`) as well as the number.
* Approval-path change → a test in `test_daemon.py` (real sockets) and
  `test_plugin.py` (fail-closed semantics).
* App change → it should at least still compile; CI runs `assembleDebug` on every
  PR touching `watch/`.

## Style

Python: type hints on public functions, dataclasses over dicts for structured
data, docstrings that explain *why* a decision was made rather than restating the
code. Comments earn their place by recording a non-obvious constraint.

Kotlin: Compose-first, no logic in composables that belongs in the service or the
bridge, and every user-visible number formatted through `ui/Format.kt` so the
em-dash rule holds in one place.
