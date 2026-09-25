# Contributing

## Layout

| Path | What it is |
|---|---|
| `bridge/hermes_watch/` | The Python half: protocol, the gateway platform adapter, plugin hooks, the in-process bus, stats, CLI |
| `bridge/tests/` | pytest — the authoritative check on the wire contract |
| `bridge/tools/fake_watch.py` | A terminal stand-in for the watch; use it instead of a device |
| `watch/app/` | The Wear OS app (Kotlin, Compose for Wear) |
| `docs/protocol.md` | The normative wire contract |
| `docs/hermes-integration.md` | Which Hermes APIs are used, and which are missing |

## Building and testing

```bash
# bridge
python -m venv .venv && . .venv/bin/activate
uv pip install -e "./bridge[dev]"
cd bridge && pytest -q

# watch
cd watch && gradle wrapper --gradle-version 8.9 && ./gradlew :app:assembleDebug
```

`test_platform.py` and `test_watch_contract.py` need a Hermes installation on the
path (they skip without one, and CI has none); `conftest.py` finds
`$HERMES_HOME/hermes-agent` or `~/.hermes/hermes-agent` automatically. To run the
whole thing against a real gateway instead of the in-process harness:

```bash
# in another terminal, with the platform enabled and the gateway running:
python bridge/tools/fake_watch.py --pair          # triggers the pairing code
python bridge/tools/fake_watch.py --answer once   # answers the first approval
```

A watch prompt with nobody to answer it is best exercised through the tests: they
post to `/event`, watch the frame arrive, and assert the response. Doing it by
hand means racing a real human.

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
7. **Read `state.db` read-only.** Nothing in this project may damage a live
   agent's history.
8. **The adapter is a guest in the gateway process.** It must not raise out of a
   `BasePlatformAdapter` callback, must not hold a file descriptor before
   `connect()`, and must treat a watch disconnecting as `send_path_degraded`, not
   as the platform going down.

## Tests

* Protocol change → a test in `bridge/tests/test_protocol.py` and an update to
  `docs/protocol.md`, in the same PR.
* Stats change → a test in `test_stats.py` that pins the label (`live`,
  `live_approximate`, `db_estimate`) as well as the number.
* Anything the watch parses → `test_watch_contract.py`, which speaks the app's
  exact frames. It is the only thing standing between a refactor and a broken
  watch in the field.
* Approval-path change → a test in `test_platform.py` (real sockets, both the
  gateway-native and the out-of-process path) and `test_plugin.py` (fail-closed
  semantics).
* App change → it should at least still compile; CI runs `assembleDebug` on every
  PR touching `watch/`.

## Style

Python: type hints on public functions, dataclasses over dicts for structured
data, docstrings that explain *why* a decision was made rather than restating the
code. Comments earn their place by recording a non-obvious constraint.

Kotlin: Compose-first, no logic in composables that belongs in the service or the
bridge, and every user-visible number formatted through `ui/Format.kt` so the
em-dash rule holds in one place.
