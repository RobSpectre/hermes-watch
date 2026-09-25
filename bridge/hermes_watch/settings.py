"""Bridge settings and pairing token, stored under the Hermes home.

Layout (``$HERMES_HOME/hermes-watch/``)::

    config.json   non-secret settings (ports, bind addresses, label)
    token         the shared secret, mode 0600

The token is deliberately *not* in ``config.json`` and not in ``.env``: it is
generated on first use, never printed by default, and is the only thing
standing between your LAN and the ability to approve tool calls on your
machine. Rotate it with ``hermes-watch-bridge rotate-token``.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .stats import hermes_home

DIR_NAME = "hermes-watch"
TOKEN_BYTES = 32


@dataclass
class BridgeConfig:
    """Everything the daemon and the plugin need to find each other."""

    watch_host: str = "0.0.0.0"
    watch_port: int = 8787
    ingest_host: str = "127.0.0.1"
    ingest_port: int = 8788
    label: str = "pixel-watch"
    #: Seconds an unanswered watch approval waits before failing closed.
    approval_timeout_s: float = 300.0
    #: Notify the watch when the agent is waiting on a question it cannot answer.
    notify_questions: bool = True

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def config_dir(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / DIR_NAME


def config_path(home: Optional[Path] = None) -> Path:
    return config_dir(home) / "config.json"


def token_path(home: Optional[Path] = None) -> Path:
    return config_dir(home) / "token"


def load_config(home: Optional[Path] = None) -> BridgeConfig:
    """Load config, creating the directory. Environment overrides win."""
    path = config_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    known = {f for f in BridgeConfig().__dataclass_fields__}
    config = BridgeConfig(**{k: v for k, v in data.items() if k in known})
    if os.environ.get("HERMES_WATCH_WATCH_PORT"):
        config.watch_port = int(os.environ["HERMES_WATCH_WATCH_PORT"])
    if os.environ.get("HERMES_WATCH_INGEST_PORT"):
        config.ingest_port = int(os.environ["HERMES_WATCH_INGEST_PORT"])
    if os.environ.get("HERMES_WATCH_LABEL"):
        config.label = os.environ["HERMES_WATCH_LABEL"]
    return config


def save_config(config: BridgeConfig, home: Optional[Path] = None) -> Path:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def load_token(home: Optional[Path] = None) -> Optional[str]:
    env = os.environ.get("HERMES_WATCH_TOKEN")
    if env:
        return env.strip()
    path = token_path(home)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    return None


def ensure_token(home: Optional[Path] = None, *, rotate: bool = False) -> str:
    """Return the pairing token, creating (or replacing) it as needed."""
    path = token_path(home)
    if path.exists() and not rotate:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    # Create with 0600 before writing so the secret is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def ingest_url(home: Optional[Path] = None) -> str:
    config = load_config(home)
    return os.environ.get("HERMES_WATCH_URL") or f"http://{config.ingest_host}:{config.ingest_port}"
