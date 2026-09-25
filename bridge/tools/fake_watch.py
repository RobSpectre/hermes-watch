#!/usr/bin/env python3
"""A terminal stand-in for the Wear OS app.

Useful when developing the bridge or the app without a watch on your wrist: it
connects to the bridge's watch socket, prints every frame the watch would
render, and (with ``--answer``) approves or denies the first approval it sees.

    hermes-watch-bridge serve                       # terminal 1
    python tools/fake_watch.py --token "$TOKEN"     # terminal 2
    python tools/fake_watch.py --answer once        # answers and exits

Kept dependency-free apart from aiohttp, which the bridge already requires.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_watch import protocol as p  # noqa: E402
from hermes_watch.settings import load_config, load_token  # noqa: E402


def render(frame: dict) -> str:
    kind = frame.get("type")
    if kind == p.S_HELLO:
        return f"hello: bridge {frame['bridge_version']} protocol {frame['protocol']} profile {frame['profile']}"
    if kind == p.S_EVENT:
        return f"EVENT {frame['event']:<22} {json.dumps(frame.get('payload', {}))[:110]}"
    if kind == p.S_STATS:
        live = frame.get("live", {})
        parts = []
        if "session" in frame and frame["session"]:
            session = frame["session"]
            rate = session["tok_per_s"]
            parts.append(
                f"{session['title'] or session['id']}  "
                f"{rate['live'] if rate['live'] is not None else '—'} tok/s  "
                f"{session['tokens']['total']:,} tok  "
                f"{session['elapsed_s']:.0f}s"
            )
        if frame.get("context"):
            context = frame["context"]
            remaining = context["remaining_pct"]
            parts.append(
                f"context {remaining:.1f}% left" if remaining is not None else "context — (window unknown)"
            )
        if live:
            parts.append(f"state {live.get('agent_state')}")
        return "STATS " + " | ".join(parts)
    return f"{kind}: {json.dumps(frame)[:120]}"


async def run(args: argparse.Namespace) -> int:
    config = load_config()
    token = args.token or load_token() or ""
    url = f"ws://{args.host}:{args.port}/v1/watch?token={token}&device={args.device}"
    answered = False
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            print(f"connected to {url.split('token=')[0]}token=***")
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    break
                frame = json.loads(message.data)
                print(render(frame))
                if (
                    args.answer
                    and not answered
                    and frame.get("type") == p.S_EVENT
                    and frame.get("event") == p.E_APPROVAL_REQUESTED
                ):
                    answered = True
                    choice = args.answer
                    print(f"  -> answering {frame['id']} with {choice!r}")
                    await ws.send_str(json.dumps({"v": 1, "type": "answer", "id": frame["id"], "choice": choice}))
                    if args.exit_after_answer:
                        await asyncio.sleep(0.5)
                        return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="default: watch_port from bridge config")
    parser.add_argument("--token", help="pairing token (default: from $HERMES_HOME/hermes-watch/token)")
    parser.add_argument("--device", default="fake-watch")
    parser.add_argument("--answer", choices=p.APPROVAL_CHOICES, help="answer the first approval with this choice")
    parser.add_argument("--exit-after-answer", action="store_true")
    args = parser.parse_args()
    if args.port is None:
        args.port = load_config().watch_port
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
