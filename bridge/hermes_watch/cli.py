"""``hermes-watch-bridge`` -- run the daemon, inspect stats, manage pairing.

Deliberately a plain argparse CLI with plain-text output, because most of its
output ends up pasted into an issue or read over SSH.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .settings import (
    config_path,
    ensure_token,
    hermes_home,
    ingest_url,
    load_config,
    load_token,
    save_config,
    token_path,
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
            live_text = "— (no live measurement yet: is the plugin installed?)"
        if average is not None:
            average_text = f"{average} (wall clock, {rate['session_avg_source']})"
        else:
            average_text = "—"
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


def cmd_token(args: argparse.Namespace) -> int:
    token = ensure_token(rotate=args.rotate)
    if args.rotate:
        print("pairing token rotated — re-pair every watch")
    if args.show:
        print(token)
    else:
        print(f"pairing token: {token[:4]}...{token[-4:]}  ({len(token)} chars, {token_path()})")
        print("pass --show to print it in full")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .client import BridgeClient

    client = BridgeClient(args.url or ingest_url(), token=load_token() or "")
    health = client.health()
    if health is None:
        print(f"bridge not reachable at {args.url or ingest_url()}")
        return 1
    print(json.dumps(health, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    from .daemon import serve
    from .hub import Hub

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = load_config()
    if args.watch_port:
        config.watch_port = args.watch_port
    if args.ingest_port:
        config.ingest_port = args.ingest_port
    if args.host:
        config.watch_host = args.host
    save_config(config)
    token = ensure_token()

    hub = Hub(StatsEngine(db_path=Path(args.db) if args.db else None))
    hub.bridge_version = __version__
    hub.profile = os.environ.get("HERMES_PROFILE") or "default"

    addresses = _local_addresses() or ["<this-host>"]
    print(f"hermes-watch bridge {__version__}")
    print(f"  watch socket   ws://{addresses[0]}:{config.watch_port}/v1/watch?token=<pairing-token>")
    for extra in addresses[1:]:
        print(f"                 ws://{extra}:{config.watch_port}/v1/watch?token=<pairing-token>")
    print(f"  plugin ingest  {ingest_url()}")
    print(f"  pairing token  {token[:4]}...{token[-4:]}   (hermes-watch-bridge token --show)")
    print(f"  session store  {state_db_path()}")
    print("  ctrl-c to stop")
    try:
        asyncio.run(
            serve(
                hub,
                watch_host=config.watch_host,
                watch_port=config.watch_port,
                ingest_host=config.ingest_host,
                ingest_port=config.ingest_port,
                ingest_token=token,
                watch_token=token,
            )
        )
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


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

    config = load_config()
    print(f"config             {config_path()}  ({config.watch_host}:{config.watch_port})")
    print(f"pairing token      {'present' if load_token() else 'MISSING'}")

    for name, host, port in (("watch port", config.watch_host, config.watch_port),
                             ("ingest port", config.ingest_host, config.ingest_port)):
        bind_host = "" if host == "0.0.0.0" else host
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            free = probe.connect_ex((bind_host or "127.0.0.1", port)) != 0
        finally:
            probe.close()
        print(f"{name:18} {host}:{port}  {'in use (bridge running?)' if not free else 'free'}")
    print()
    print("no problems found" if problems == 0 else f"{problems} problem(s) to fix")
    return 0 if problems == 0 else 1


# --- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-watch-bridge",
        description="Bridge Hermes approvals, questions and live stats to a Wear OS watch.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="run the bridge daemon")
    serve_parser.add_argument("--host", help="watch bind address (default from config: 0.0.0.0)")
    serve_parser.add_argument("--watch-port", type=int, help="watch WebSocket port (default 8787)")
    serve_parser.add_argument("--ingest-port", type=int, help="plugin ingest port (default 8788)")
    serve_parser.add_argument("--db", help="path to a state.db (default: $HERMES_HOME/state.db)")
    serve_parser.add_argument("-v", "--verbose", action="store_true")
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

    token_parser = sub.add_parser("token", help="show or rotate the pairing token")
    token_parser.add_argument("--show", action="store_true", help="print the token in full")
    token_parser.add_argument("--rotate", action="store_true", help="generate a new token")
    token_parser.set_defaults(func=cmd_token)

    status_parser = sub.add_parser("status", help="ask a running bridge for its health")
    status_parser.add_argument("--url", help="ingest base url (default: from config)")
    status_parser.set_defaults(func=cmd_status)

    doctor_parser = sub.add_parser("doctor", help="check the setup end to end")
    doctor_parser.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
