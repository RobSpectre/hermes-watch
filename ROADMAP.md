# Roadmap

Ordered by what unblocks the most, with the reason each item is where it is.
Work in progress lives in the issue tracker; this file explains *why* the order
is what it is.

## v0.1 — the bridge (done)

The Python half is finished and verified:

* Wire protocol v1 with additive-versioning rules (`docs/protocol.md`).
* Stats from `state.db` plus exact per-call measurements from Hermes hooks.
* Approval transport over the watch socket, fail-closed on every failure path.
* 46 tests, including an approval round-trip over real sockets and a
  long-poll/token/scope-escalation matrix.

Deliberately not in scope: any UI. Nothing on the watch side was needed to prove
the contract works, and the contract is the risky part — the ordering of
`pre_api_request` → tool loop → `post_llm_call`, the fail-closed semantics of a
plugin-owned approval transport, and whether Hermes' session store really carries
what a stats display needs. It does, with one exception documented below.

## v0.2 — the app on real hardware

The app code is written but has never been compiled or run. Nothing else should
start until it has, because several decisions in it are guesses that only a
device can confirm:

1. **Notification action rendering.** Wear OS renders notification actions as
   buttons, but how many and how prominently varies by device and version. If
   four choices do not fit, the fallback is two buttons (Allow / Deny) plus an
   expanded view for the rest.
2. **Foreground-service survival.** `dataSync` should keep the socket alive with
   the screen off. Verify across a charge cycle, especially through Doze.
3. **Battery.** Unmeasured. See `docs/setup.md` for the current behaviour and the
   planned lever (slower ticker when idle, snapshot on screen-wake).
4. **Wake latency.** How long between "you tap Allow" and the agent continuing?
   Should be under a second on a good network; the long-poll design means it is
   one round trip.

## v0.3 — answering questions

Blocked upstream, not here. Hermes exposes an approval transport but no **input
transport**, so a plugin cannot present or answer a `clarify` prompt. v1
notifies; anything more would be a lie about the platform.

There is a concrete proposal in `docs/hermes-integration.md`
(`ctx.register_input_transport`), which is the shape Hermes' own approval
transport already uses — same host-owned timeout, same fail-closed defaults, same
fallback to the built-in surface. Worth filing upstream. Until then the watch can
buzz and show the question so you know to walk back to the terminal.

## v0.4 — reachability

The current design assumes the watch can open a TCP connection to the bridge
machine: same Wi-Fi, or watch LTE with a route home. Two gaps:

* **Bluetooth-only watches.** A Pixel Watch on LTE with Wi-Fi off cannot reach a
  LAN address. The fix is a companion phone app that holds the socket and relays
  over the Wearable Data Layer (`MessageClient`) — the watch then only needs its
  phone. This also makes the watch far cheaper on battery, since the phone does
  the radio work.
* **Away from home.** Requires either a TLS + auth story for a
  publicly-reachable bridge, or an FCM relay: bridge → relay → FCM data message →
  watch. FCM is the only mechanism that reliably wakes an app that is not running,
  which is also why it is worth doing properly rather than shipping a
  half-reachable push path.

Both change the trust story, so neither should land before the local path is
proven on a device.

## v0.5 — polish

* **Complication.** A watch-face complication for context remaining, so the number
  is visible without raising the app. Straightforward; it needs a
  `ComplicationDataSourceService` and a cached snapshot, which `TileCache` already
  provides.
* **Ambient / always-on.** A dimmed variant of the stats screen.
* **Per-day sparkline** on the 30-day view. The data is already in the snapshot
  (`usage.by_day`).
* **`/watch` slash command** in Hermes for "what does the watch see right now",
  and a bridge-side `--once` dry-run that renders a fake approval for testing
  without a device.

## Considered and rejected

* **Polling from the watch instead of a socket.** Worse on battery and worse on
  latency; a push socket with a heartbeat costs less than a 10-second poll.
* **TLS with a self-signed cert.** Self-signed certs on a watch are either
  pinned-to-one-cert (breaks on IP change) or clicked through (trains the user to
  ignore warnings). If TLS lands it should be a real certificate via a tunnel,
  not a self-signed LAN cert.
* **Making the bridge a Hermes gateway platform.** The watch is not a chat
  platform. Registering it as one would drag session routing, message history and
  delivery obligations into a component whose whole job is to be a socket and a
  notification.
* **Computing stats on the watch.** The watch cannot read `state.db`, cannot
  resolve `get_model_context_length`, and has no history. Duplicating the logic
  would guarantee the two halves disagree.
