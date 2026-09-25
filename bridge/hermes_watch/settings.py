"""Local settings for the watch platform.

There is no shared secret here on purpose. Access to the watch socket is
granted by Hermes' own pairing store: a device connects with a stable id, sends
one message, and the gateway's standard unauthorized-DM path answers with a
pairing code the owner approves on the host with ``hermes pairing approve
pixel_watch <code>``. That is strictly better than a token pasted onto a watch
keyboard, and it means there is no credential file in this package to leak,
rotate, or forget.

What is left is a small amount of non-secret configuration: which address the
listener binds and how long a prompt waits. It exists as a file only so the CLI
(``hermes-watch stats``) and the gateway agree on defaults when the gateway
config is not the thing being edited.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .stats import hermes_home

DIR_NAME = "hermes-watch"

#: ``config.yaml`` key (``gateway.platforms.pixel_watch``) and the string the
#: pairing store keys on. Defined here because both the plugin entry point and
#: the adapter need it, and this module must stay importable without pulling in
#: the gateway's platform machinery.
PLATFORM_NAME = "pixel_watch"
PLATFORM_LABEL = "Pixel Watch"
DEFAULT_WATCH_PORT = 8787


@dataclass
class BridgeConfig:
    """Non-secret settings shared by the adapter and the CLI."""

    watch_host: str = "0.0.0.0"
    watch_port: int = DEFAULT_WATCH_PORT
    label: str = "pixel-watch"
    #: Seconds an unanswered watch prompt waits before failing closed.
    approval_timeout_s: float = 300.0

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def config_dir(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / DIR_NAME


def config_path(home: Optional[Path] = None) -> Path:
    return config_dir(home) / "config.json"


def _gateway_extra(home: Optional[Path] = None) -> dict[str, Any]:
    """The ``extra`` block for this platform out of Hermes' own config.

    Read defensively and without importing the gateway: this module is imported
    by the CLI and by the plugin, neither of which should fail because a config
    file is malformed or a YAML parser is missing. Returning ``{}`` just means
    the defaults below apply.

    It matters because the adapter binds whatever ``extra`` says: the plugin's
    approval transport and the CLI's doctor both have to agree with it, or a CLI
    approval would be posted to a port nothing listens on.
    """
    path = (home or hermes_home()) / "config.yaml"
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    data: Any = None
    try:
        from ruamel.yaml import YAML  # Hermes' own parser, when available

        data = YAML(typ="safe").load(raw)
    except Exception:
        try:
            import yaml

            data = yaml.safe_load(raw)
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    gateway = data.get("gateway")
    platforms = gateway.get("platforms") if isinstance(gateway, dict) else None
    entry = platforms.get(PLATFORM_NAME) if isinstance(platforms, dict) else None
    extra = entry.get("extra") if isinstance(entry, dict) else None
    return extra if isinstance(extra, dict) else {}


def load_config(home: Optional[Path] = None) -> BridgeConfig:
    """Load config, tolerating its absence. Environment overrides win.

    Precedence, lowest first: dataclass defaults, the legacy ``config.json``,
    the gateway's ``config.yaml`` (what the adapter actually binds), then the
    ``HERMES_WATCH_*`` environment.
    """
    path = config_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    known = set(BridgeConfig().__dataclass_fields__)
    config = BridgeConfig(**{k: v for k, v in data.items() if k in known})
    extra = _gateway_extra(home)
    for key, attr, cast in (
        ("host", "watch_host", str),
        ("port", "watch_port", int),
        ("approval_timeout_s", "approval_timeout_s", float),
    ):
        if key in extra:
            try:
                setattr(config, attr, cast(extra[key]))
            except (TypeError, ValueError):
                pass
    for env, attr, cast in (
        ("HERMES_WATCH_HOST", "watch_host", str),
        ("HERMES_WATCH_WATCH_PORT", "watch_port", int),
        ("HERMES_WATCH_LABEL", "label", str),
        ("HERMES_WATCH_APPROVAL_TIMEOUT_S", "approval_timeout_s", float),
    ):
        raw = os.environ.get(env)
        if raw:
            try:
                setattr(config, attr, cast(raw))
            except ValueError:
                pass
    return config


def save_config(config: BridgeConfig, home: Optional[Path] = None) -> Path:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def watch_url(home: Optional[Path] = None) -> str:
    """Base URL of the running watch listener, for the CLI and the plugin."""
    config = load_config(home)
    env = os.environ.get("HERMES_WATCH_URL")
    if env:
        return env.rstrip("/")
    host = "127.0.0.1" if config.watch_host in ("0.0.0.0", "::") else config.watch_host
    return f"http://{host}:{config.watch_port}"
