# Setup

Three things to install: the bridge daemon, the Hermes plugin, and the watch app.

## 1. Bridge daemon

Requires Python 3.10+.

```bash
git clone https://github.com/RobSpectre/hermes-watch.git
cd hermes-watch
python -m venv .venv && . .venv/bin/activate
pip install ./bridge
hermes-watch-bridge doctor      # checks paths, ports, plugin install
hermes-watch-bridge serve
```

`serve` prints the address to type on the watch and the first four and last four
characters of the pairing token. The token itself lives at
`$HERMES_HOME/hermes-watch/token` (mode 0600) and is printed in full by
`hermes-watch-bridge token --show`.

Run it wherever the agent runs. It reads `$HERMES_HOME/state.db` read-only and
listens on two ports:

| Port | Default | Bound to | Who talks to it |
|---|---|---|---|
| watch | 8787 | `0.0.0.0` (your LAN) | the watch app |
| ingest | 8788 | `127.0.0.1` only | the Hermes plugin |

Keep the bridge running with your init system of choice — it is a plain
foreground process with no daemonisation of its own. A systemd user unit is the
usual answer:

```ini
# ~/.config/systemd/user/hermes-watch.service
[Unit]
Description=Hermes Watch bridge
After=network-online.target

[Service]
ExecStart=%h/hermes-watch/.venv/bin/hermes-watch-bridge serve
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now hermes-watch
```

## 2. Hermes plugin

```bash
hermes plugins install RobSpectre/hermes-watch --enable
```

That clones the repo into `~/.hermes/plugins/hermes-watch/`. The repository root
is the plugin (`plugin.yaml` + an `__init__.py` that delegates into `bridge/`), so
there is no build step. If you would rather not use `hermes plugins install`, a
`git clone` into `~/.hermes/plugins/hermes-watch/` plus adding the name to
`plugins.enabled` in `~/.hermes/config.yaml` does the same thing.

Then select the approval transport:

```yaml
# ~/.hermes/config.yaml
security:
  approval:
    transport: pixel-watch
    transport_fallback: builtin
```

* `transport: pixel-watch` — approvals are presented on the watch.
* `transport_fallback: builtin` — **do not skip this.** Without it a failed
  transport denies by default, so an approval you never saw would be refused
  silently. With it, Hermes shows its normal terminal prompt whenever the watch
  is not connected.

Restart Hermes (the gateway restarts for messaging surfaces; a new CLI session
is enough for the CLI).

Verify:

```bash
hermes hooks list                       # outbound/shell hooks, for context
hermes-watch-bridge status              # bridge up?
hermes-watch-bridge stats               # the numbers the watch will show
python bridge/tools/fake_watch.py       # a terminal stand-in for the watch
```

## 3. Watch app

The app is not published to any store. Build it:

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
`watch/` directly.

Then on the watch: open **Hermes Watch**, enter the bridge host, port (default
8787) and pairing token, and tap Connect. The pairing screen is also reachable
from the stats screen's bridge chip if you need to re-pair.

### Notification permission

Wear OS 3+ asks for `POST_NOTIFICATIONS` on first launch. Approvals arrive as
notifications, so denying it means the app can only show approvals you open
yourself — which defeats the purpose.

## Troubleshooting

| Symptom | Check |
|---|---|
| Watch says "reconnecting…" forever | `hermes-watch-bridge status`; confirm the watch is on the same network and the host is the bridge machine's LAN IP (`serve` prints it). |
| "bridge too new" on the watch | Protocol mismatch: update the app or the bridge. The error names both versions. |
| No notifications at all | Notification permission; also `adb shell dumpsys notification --noredact \| grep hermes`. |
| Approvals never appear on the watch | `hermes plugins list` shows the plugin enabled; `hermes-watch-bridge status` shows `watches: 1`. If watches is 0, the plugin deliberately raises `WatchUnavailable` and Hermes uses its built-in prompt instead of stalling. |
| tok/s shows `—` | No live measurement yet. The plugin reports it from `post_api_request`; check the plugin is enabled and the bridge's ingest port matches. |
| context shows `—` | The model's context window is unknown. Set `model.context_length` in `config.yaml` to pin it, and the percentage will appear. |
| Stats frozen | The bridge is not receiving frames: `curl -H "Authorization: Bearer $(hermes-watch-bridge token --show)" http://127.0.0.1:8788/healthz`. |

## Battery

A watch holding a socket is a watch spending battery. Current behaviour:

* One WebSocket, kept alive by the bridge's own pings (30 s). No polling.
* Stats are pushed every 5 s while the agent is idle and every 1.5 s while it is
  busy, and the tile renders from a cached snapshot with no socket at all.
* Frames are tiny — a snapshot is well under 2 KB — so the radio wakes briefly
  rather than transferring a payload.

Expected cost is in the low single-digit percent per day on a device that is
otherwise idle, but this has **not** been measured on real hardware yet; treat
any number as unverified until someone reports one. The obvious next lever, not
yet implemented, is to drop the ticker to a much slower cadence when the agent
has been idle for minutes and let the watch request a snapshot on screen-wake.
