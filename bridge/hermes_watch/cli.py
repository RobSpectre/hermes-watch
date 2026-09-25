"""``hermes-watch`` -- inspect what the watch sees, and check the setup.

The listener is not run from here any more: it is a Hermes gateway platform, so
``hermes gateway run`` (or the installed service) hosts it. What is left is the
tooling a human needs when something looks wrong -- the same stats document the
watch renders, the session list, the listener's health, and a one-pass check of
the things that actually break.

Plain argparse and plain text output, because most of this ends up pasted into
an issue or read over SSH.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .client import AdapterClient
from .settings import (
    PLATFORM_NAME,
    config_path,
    hermes_home,
    load_config,
    watch_url,
)
from .stats import StatsEngine, find_recent_sessions, state_db_path

BAR_WIDTH = 28


def _fmt_int(value: Optional[int]) -> str:
    return "—" if value is None else f"{value:,}"


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _bar(pct: Optional[float], width: int = BAR_WIDTH) -> str:
    if pct is None:
        return "[" + "?" * width + "]"
    filled = int(round(width * max(min(pct, 100.0), 0.0) / 100.0))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _local_addresses() -> list[str]:
    """Best-effort LAN addresses, so setup can show what to type on the watch."""
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except Exception:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        if address not in addresses:
            addresses.insert(0, address)
    except Exception:
        pass
    return addresses


def _pairing_dir() -> Path:
    """Where Hermes keeps pairing state. Mirrors ``gateway.pairing`` without
    importing the gateway into a CLI process."""
    override = os.environ.get("HERMES_PAIRING_DIR")
    if override:
        return Path(override)
    home = hermes_home()
    candidate = home / "platforms" / "pairing"
    legacy = home / "pairing"
    return candidate if candidate.exists() or not legacy.exists() else legacy


def _approved_devices() -> list[str]:
    path = _pairing_dir() / f"{PLATFORM_NAME}-approved.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return sorted(str(key) for key in data)
    except Exception:
        return []


# --- commands ---------------------------------------------------------------


def cmd_stats(args: argparse.Namespace) -> int:
    """Print the same document the watch renders, as text."""
    engine = StatsEngine(db_path=Path(args.db) if args.db else None)
    snapshot = engine.snapshot(session_id=args.session)
    session = snapshot["session"]
    context = snapshot["context"]
    usage = snapshot["usage"]

    print(f"hermes home   {snapshot['hermes_home']}")
    print(f"session db    {state_db_path()}")
    print()
    if not session:
        print("no session in the store yet")
    else:
        print(f"session       {session['id']}  ({session['source']})")
        print(f"title         {session['title'] or '(untitled)'}")
        print(f"model         {session['model'] or 'unknown'}")
        print(f"elapsed       {_fmt_seconds(session['elapsed_s'])}"
              f"{'' if session['ended'] else '  (live)'}")
        print(f"messages      {session['message_count']}   tools {session['tool_call_count']}"
              f"   api calls {session['api_call_count']}")
        tokens = session["tokens"]
        print(f"tokens        in {_fmt_int(tokens['input'])}"
              f"  out {_fmt_int(tokens['output'])}"
              f"  cached {_fmt_int(tokens['cached_read'])}"
              f"  total {_fmt_int(tokens['total'])}")
        rate = session["tok_per_s"]
        live = rate["live"]
        average = rate["session_avg"]
        if live is not None:
            live_text = f"{live} (measured over provider call latency)"
        else:
            live_text = "— (no live measurement: nothing has reported one yet)"
        average_text = f"{average} (wall clock, {rate['session_avg_source']})" if average is not None else "—"
        print(f"tok/s         {live_text}")
        print(f"              session avg {average_text}")
    print()
    print(f"context {_bar(context['remaining_pct'])}"
          f"  {('%.1f%% left' % context['remaining_pct']) if context['remaining_pct'] is not None else 'unknown'}")
    print(f"  used        {_fmt_int(context['used_tokens'])}"
          f"   window {_fmt_int(context['window_tokens'])}"
          f"   remaining {_fmt_int(context['remaining_tokens'])}")
    print(f"  source      {context['source'] or 'unavailable'}")
    print()
    print(f"last {usage['window_days']} days  {usage['session_count']} sessions"
          f"  {usage['api_calls']} api calls  ${usage['cost_usd']:.4f}")
    print(f"  tokens      total {_fmt_int(usage['tokens']['total'])}"
          f"  (in {_fmt_int(usage['tokens']['input'])}"
          f" / out {_fmt_int(usage['tokens']['output'])}"
          f" / cached {_fmt_int(usage['tokens']['cached_read'])})")
    for model in usage["by_model"][:5]:
        print(f"    {model['model'][:38]:38}  {_fmt_int(model['tokens']['total']):>12}"
              f"  ${model['cost_usd']:.4f}")
    if usage["by_day"]:
        peak = max(day["tokens"] for day in usage["by_day"]) or 1
        print("  by day")
        for day in usage["by_day"][-14:]:
            width = int(round(20 * day["tokens"] / peak))
            print(f"    {day['date']}  {'#' * width:<20} {_fmt_int(day['tokens'])}")
    if args.json:
        print()
        print(json.dumps(snapshot, indent=2))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    for row in find_recent_sessions(limit=args.limit, db_path=Path(args.db) if args.db else None):
        ended = "" if row["ended_at"] else "  (live)"
        print(
            f"{row['id']}  {row['source']:8}  {(row['title'] or '(untitled)')[:34]:34}"
            f"  in {row['input_tokens']:>8,}  out {row['output_tokens']:>7,}{ended}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Ask the running listener what it is doing."""
    url = args.url or watch_url()
    health = AdapterClient(url).health()
    if health is None:
        print(f"no watch listener at {url}")
        print("the listener lives in the gateway now:")
        print("  hermes gateway run          # foreground")
        print("  hermes gateway status       # is the service up?")
        return 1
    watches = health.get("watches") or []
    print(f"listener      {url}")
    print(f"platform      {PLATFORM_NAME} {health.get('version', '?')}  up {health.get('uptime_s', '?')}s")
    print(f"agent state   {health.get('state', '?')}")
    print(f"watches       {len(watches)} connected")
    for watch in watches:
        print(
            f"  {watch.get('device_id', '?')[:28]:28}  {watch.get('label') or '-':16}"
            f"  authorized={watch.get('authorized')}  {watch.get('connected_s', '?')}s"
        )
    pending = health.get("pending") or []
    if pending:
        print(f"pending       {len(pending)}")
        for item in pending:
            print(f"  {item.get('id', '?')}  {item.get('kind', '?'):8}  {item.get('remaining_s', '?')}s left")
    return 0


def cmd_pairing(args: argparse.Namespace) -> int:
    """Show pairing state without needing the gateway up."""
    approved = _approved_devices()
    directory = _pairing_dir()
    pending_path = directory / f"{PLATFORM_NAME}-pending.json"
    print(f"pairing store   {directory}")
    print(f"approved        {len(approved)} device(s)")
    for device in approved:
        print(f"  {device}")
    if pending_path.exists():
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except Exception:
            pending = {}
        if pending:
            print(f"waiting         {len(pending)} request(s) -- approve on the host:")
            print(f"  hermes pairing list")
            print(f"  hermes pairing approve {PLATFORM_NAME} <code>")
    else:
        print("waiting         none")
    if not approved:
        print()
        print("A watch pairs by itself: connect the app, and it sends one message.")
        print("Hermes answers with an 8-character code, shown on the watch. Approve it")
        print(f"with: hermes pairing approve {PLATFORM_NAME} <code>")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """The standalone daemon is gone; point at what replaced it."""
    config = load_config()
    addresses = _local_addresses() or ["<this-host>"]
    print("This command is gone: the watch listener is a Hermes gateway platform now,")
    print("so there is no second process to run.")
    print()
    print("    1. enable it in ~/.hermes/config.yaml:")
    print()
    print("         gateway:")
    print("           platforms:")
    print(f"             {PLATFORM_NAME}:")
    print("               enabled: true")
    print()
    print("    2. start the gateway (or the installed service):")
    print()
    print("         hermes gateway run        # foreground")
    print("         hermes gateway install    # as a service")
    print()
    print(f"The watch connects to  ws://{addresses[0]}:{config.watch_port}/watch")
    for extra in addresses[1:]:
        print(f"                      ws://{extra}:{config.watch_port}/watch")
    print("Pairing is by code, not by token: connect, then approve the code the watch shows")
    print(f"with  hermes pairing approve {PLATFORM_NAME} <code>")
    return 2


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the things that actually break in practice, in one pass."""
    problems = 0
    home = hermes_home()
    print(f"HERMES_HOME        {home}{'' if os.environ.get('HERMES_HOME') else '  (default, env unset)'}")
    db = state_db_path()
    print(f"session store      {db}  {'ok' if db.exists() else 'MISSING'}")
    if not db.exists():
        problems += 1

    plugin_dir = home / "plugins" / "hermes-watch"
    print(f"plugin installed   {plugin_dir}  {'yes' if plugin_dir.exists() else 'no'}")
    if not plugin_dir.exists():
        print("                   install with: hermes plugins install RobSpectre/hermes-watch --enable")
        problems += 1

    config = load_config()
    print(f"config             {config_path()}  ({config.watch_host}:{config.watch_port})")
    approved = _approved_devices()
    print(f"paired devices     {len(approved)}" + (f"  ({', '.join(approved)})" if approved else ""))
    if not approved:
        print(f"                   pair a watch, then: hermes pairing approve {PLATFORM_NAME} <code>")

    url = watch_url()
    health = AdapterClient(url).health()
    print(f"listener           {url}  {'up' if health else 'DOWN'}")
    if health:
        watches = health.get("watches") or []
        print(f"                   state={health.get('state')}  watches={len(watches)}"
              f"  authorized={sum(1 for w in watches if w.get('authorized'))}")
    else:
        print("                   is the gateway running with this platform enabled?")
        print(f"                   gateway.platforms.{PLATFORM_NAME}.enabled in ~/.hermes/config.yaml")

    print()
    print("no problems found" if problems == 0 else f"{problems} problem(s) to fix")
    return 0 if problems == 0 else 1


# --- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-watch",
        description="Inspect the watch platform: stats, health, pairing.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="(removed) how to run the listener now")
    serve_parser.set_defaults(func=cmd_serve)

    stats_parser = sub.add_parser("stats", help="print the stats the watch would show")
    stats_parser.add_argument("--session", help="session id (default: most recently active)")
    stats_parser.add_argument("--db", help="path to a state.db")
    stats_parser.add_argument("--json", action="store_true", help="also dump the raw payload")
    stats_parser.set_defaults(func=cmd_stats)

    sessions_parser = sub.add_parser("sessions", help="list recent sessions")
    sessions_parser.add_argument("--limit", type=int, default=10)
    sessions_parser.add_argument("--db")
    sessions_parser.set_defaults(func=cmd_sessions)

    status_parser = sub.add_parser("status", help="ask the running listener for its health")
    status_parser.add_argument("--url", help="listener base url (default: from config)")
    status_parser.set_defaults(func=cmd_status)

    pairing_parser = sub.add_parser("pairing", help="show which watches are paired")
    pairing_parser.set_defaults(func=cmd_pairing)

    doctor_parser = sub.add_parser("doctor", help="check the setup end to end")
    doctor_parser.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
