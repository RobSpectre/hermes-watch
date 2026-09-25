# Setup

Two things to install: the Hermes plugin (which brings the gateway platform
that runs the watch listener) and the watch app. There is no separate daemon,
and nothing to run in the background beyond Hermes' own gateway.

## 1. Hermes plugin

```bash
hermes plugins install RobSpectre/hermes-watch --enable
```

That clones the repo into `~/.hermes/plugins/hermes-watch/`. The repository root
is the plugin (`plugin.yaml` + an `__init__.py` that delegates into `bridge/`), so
there is no build step and no `pip install` — the shim puts `bridge/` on
`sys.path` itself. If you would rather not use `hermes plugins install`, a
`git clone` into `~/.hermes/plugins/hermes-watch/` plus adding the name to
`plugins.enabled` in `~/.hermes/config.yaml` does the same thing.

Then enable the platform and select the approval transport:

```yaml
# ~/.hermes/config.yaml
gateway:
  platforms:
    pixel_watch:
      enabled: true
      extra:
        host: 0.0.0.0      # 127.0.0.1 to keep the listener on this machine only
        port: 8787
        approval_timeout_s: 300

security:
  approval:
    transport: pixel-watch
    transport_fallback: builtin
```

* `gateway.platforms.pixel_watch.enabled` — the gateway runs the WebSocket
  listener as a platform adapter. It starts and stops with the gateway.
* `security.approval.transport: pixel-watch` — approvals are presented on the
  watch. This transport also covers approvals raised by a **CLI** session, which
  the gateway cannot see on its own.
* `transport_fallback: builtin` — **do not skip this.** Without it a failed
  transport denies by default, so an approval you never saw would be refused
  silently. With it, Hermes shows its normal terminal prompt whenever the watch
  is not connected.

Restart the gateway (`hermes gateway restart`, or however you run it), and start
a new CLI session for the transport.

Verify:

```bash
hermes gateway status                   # pixel_watch should be listed as enabled
hermes-watch doctor                     # listener up? plugin installed? devices paired?
hermes-watch stats                      # the numbers the watch will show
python bridge/tools/fake_watch.py --pair   # a terminal stand-in for the watch
```

`hermes-watch` comes from the optional standalone CLI:

```bash
pip install ./bridge        # only needed for the CLI; the plugin does not need it
```

Prefer not to install anything? Every command above is a thin wrapper over what
the plugin already does — `hermes pairing list` for devices, the gateway log for
the listener, and `curl http://127.0.0.1:8787/healthz` for liveness.

### Ports

| Port | Default | Bound to | Who talks to it |
|---|---|---|---|
| watch listener | 8787 | `0.0.0.0` (your LAN), from `extra.host` | the watch app, and the plugin's CLI approval transport over loopback |

One port serves both: the watch connects to `/v1/watch`, and the plugin posts to
`/event` on the same listener. Nothing else is exposed.

## 2. Pair the watch

The app is not published to any store. Build it first (§3), then:

1. On the watch, open **Hermes Watch** and enter the host (the gateway machine's
   LAN IP) and port (default 8787). The **token field is a leftover from the
   pre-gateway design — leave it blank**; it is ignored, and the host logs a note
   if it is set.
2. Tap Connect. The watch sends a hello and is told it is not paired yet.
3. Send any message from the watch, or run
   `python bridge/tools/fake_watch.py --pair`. Hermes' own unauthorized-device
   path answers with an **8-character pairing code**, which arrives as a message
   notification on the watch.
4. Approve it on the host:

```bash
hermes pairing list                              # pending requests + approved devices
hermes pairing approve pixel_watch <code>
hermes pairing revoke pixel_watch "Pixel Watch"  # undo
```

Pairing lasts until you revoke it. There is no token file, nothing to rotate, and
nothing in this repository that holds a secret.

> Pairing is keyed on the **label** the watch sends (the app has no device id to
> send, and its protocol is frozen). Two watches with the same label share one
> pairing entry, and a rogue LAN client claiming an approved label looks like
> that device. Bind the listener to a private interface, or pin
> `HERMES_WATCH_ALLOWED_USERS="Pixel Watch"` to the labels you actually own.

## 3. Watch app

```bash
cd watch
gradle wrapper --gradle-version 8.9     # once, to generate ./gradlew (see note)
./gradlew :app:assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

Note on the wrapper: `gradle/wrapper/gradle-wrapper.properties` is committed but
the wrapper JAR is not (it is a binary). Either run `gradle wrapper` once, or use
any local Gradle 8.9+ directly — CI uses `gradle/actions/setup-gradle` with the
same pinned version.

Building requires JDK 17 and an Android SDK with API 35. Android Studio can open
`watch/` directly. If you have no Android toolchain at all, download the APK CI
already built:

```bash
gh run list --workflow watch-ci.yml --limit 1
gh run download <run-id> -n hermes-watch-debug-apk
```

### Notification permission

Wear OS 3+ asks for `POST_NOTIFICATIONS` on first launch. Approvals arrive as
notifications, so denying it means the app can only show approvals you open
yourself — which defeats the purpose.

## Troubleshooting

| Symptom | Check |
|---|---|
| Watch says "reconnecting…" forever | `hermes-watch doctor`; confirm the watch is on the same network and `extra.host` is reachable — `0.0.0.0` binds every interface, including the LAN one. |
| "bridge too new" on the watch | Protocol mismatch: update the app or the plugin. The error names both versions. |
| No notifications at all | Notification permission; also `adb shell dumpsys notification --noredact \| grep hermes`. |
| Listener is DOWN | The gateway must be running with the platform enabled: check `gateway.platforms.pixel_watch.enabled` and the gateway log for `bind failed`. A port already in use is fatal by design rather than retried forever. |
| Watch connects but gets no stats | It isn't paired: `hermes pairing list`. An unpaired socket is sent a hello and a code, never a snapshot. |
| Approvals never appear on the watch | `hermes plugins list` shows the plugin enabled; `hermes-watch doctor` shows `authorized=1`. If it shows 0, the transport deliberately raises `WatchUnavailable`, so Hermes uses its built-in prompt instead of stalling. |
| Approvals work in the gateway but not from a CLI session | `security.approval.transport: pixel-watch` plus `transport_fallback: builtin`, and the CLI session must have started after the config change. |
| tok/s shows `—` | No live measurement yet. The plugin reports it from `post_api_request`; check the plugin is enabled. |
| context shows `—` | The model's context window is unknown. Set `model.context_length` in `config.yaml` to pin it, and the percentage will appear. |

## Battery

A watch holding a socket is a watch spending battery. Current behaviour:

* One WebSocket, kept alive by the adapter's own pings (30 s). No polling.
* Stats are pushed every 5 s while the agent is idle and every 1.5 s while it is
  busy, and the tile renders from a cached snapshot with no socket at all.
* Frames are tiny — a snapshot is well under 2 KB — so the radio wakes briefly
  rather than transferring a payload.

Expected cost is in the low single-digit percent per day on a device that is
otherwise idle, but this has **not** been measured on real hardware yet; treat
any number as unverified until someone reports one. The obvious next lever, not
yet implemented, is to drop the ticker to a much slower cadence when the agent
has been idle for minutes and let the watch request a snapshot on screen-wake.
