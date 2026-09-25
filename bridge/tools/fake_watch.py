#!/usr/bin/env python3
"""A terminal stand-in for the Wear OS app.

Useful when developing the adapter or the app without a watch on your wrist: it
connects to the watch listener, prints every frame the watch would render, can
send a message (to trigger pairing), and answers the first approval it sees.

    # while the gateway (with the platform enabled) is running:
    python tools/fake_watch.py --device my-watch
    python tools/fake_watch.py --say hello          # trigger the pairing code
    python tools/fake_watch.py --answer once --exit-after-answer

The listener is part of the gateway now, so there is no daemon to start first.
Depends only on aiohttp, which Hermes already ships.
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
from hermes_watch.settings import load_config  # noqa: E402


def render(frame: dict) -> str:
    kind = frame.get("type")
    if kind == p.S_HELLO:
        return (
            f"hello: watch adapter {frame.get('bridge_version')} protocol {frame.get('protocol')} "
            f"profile {frame.get('profile')} authorized={frame.get('authorized')}"
        )
    if kind == p.S_EVENT:
        event = frame.get("event")
        payload = frame.get("payload", {})
        if event == "message":
            return f"MESSAGE {payload.get('text', '')}"
        if event == p.E_APPROVAL_REQUESTED:
            choices = payload.get("choices") or []
            body = payload.get("payload") or {}
            return (
                f"APPROVAL {frame.get('id')}  {body.get('command') or ''}  "
                f"choices={choices}  {payload.get('remaining_s')}s left"
            )
        if event == p.E_QUESTION_PENDING:
            return f"QUESTION {frame.get('id')}  {json.dumps(payload)[:110]}"
        return f"EVENT {event:<22} {json.dumps(payload)[:110]}"
    if kind in (p.S_STATS, p.S_SNAPSHOT):
        parts = []
        session = frame.get("session")
        if session:
            rate = session["tok_per_s"]
            live = rate["live"]
            parts.append(
                f"{session['title'] or session['id']}  "
                f"{live if live is not None else '—'} tok/s  "
                f"{session['tokens']['total']:,} tok  {session['elapsed_s']:.0f}s"
            )
        context = frame.get("context")
        if context:
            remaining = context["remaining_pct"]
            parts.append(f"context {remaining:.1f}% left" if remaining is not None
                         else "context — (window unknown)")
        usage = frame.get("usage")
        if usage:
            parts.append(f"30d {usage['tokens']['total']:,} tok / {usage['session_count']} sessions")
        live = frame.get("live") or {}
        if live:
            parts.append(f"state {live.get('agent_state')}")
        label = "SNAPSHOT" if kind == p.S_SNAPSHOT else "STATS"
        return f"{label} " + " | ".join(parts)
    return f"{kind}: {json.dumps(frame)[:120]}"


async def run(args: argparse.Namespace) -> int:
    config = load_config()
    port = args.port or config.watch_port
    url = f"http://{args.host}:{port}/watch"
    answered = False
    sent = False
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            print(f"connected to {url} as {args.device}")
            await ws.send_str(
                json.dumps({"v": 1, "type": p.C_HELLO, "device_id": args.device, "label": args.label})
            )
            if args.say:
                await ws.send_str(json.dumps({"v": 1, "type": p.C_TEXT, "text": args.say}))
                sent = True
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    break
                frame = json.loads(message.data)
                print(render(frame))
                if (
                    not sent
                    and frame.get("type") == p.S_HELLO
                    and args.pair
                    and not frame.get("authorized")
                ):
                    # One message from an unknown device is what makes Hermes
                    # answer with a pairing code.
                    sent = True
                    print("  -> sending a message to start pairing")
                    await ws.send_str(json.dumps({"v": 1, "type": p.C_TEXT, "text": args.pair_message}))
                if (
                    args.answer
                    and not answered
                    and frame.get("type") == p.S_EVENT
                    and frame.get("event") == p.E_APPROVAL_REQUESTED
                ):
                    answered = True
                    print(f"  -> answering {frame['id']} with {args.answer!r}")
                    await ws.send_str(
                        json.dumps({"v": 1, "type": p.C_ANSWER, "id": frame["id"], "choice": args.answer})
                    )
                    if args.exit_after_answer:
                        await asyncio.sleep(0.5)
                        return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="default: watch_port from config")
    parser.add_argument("--device", default="fake-watch", help="stable device id (pairing keys on this)")
    parser.add_argument("--label", default="Fake Watch", help="human name shown on the host")
    parser.add_argument("--say", help="send this message on connect")
    parser.add_argument("--pair", action="store_true", help="send a message when the listener says unpaired")
    parser.add_argument("--pair-message", default="hello", help="message used by --pair")
    parser.add_argument("--answer", choices=p.APPROVAL_CHOICES, help="answer the first approval with this choice")
    parser.add_argument("--exit-after-answer", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
