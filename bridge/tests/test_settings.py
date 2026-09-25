"""Config resolution: the adapter, the plugin and the CLI must agree.

If they disagree about the port, a CLI approval is posted to a port nothing
listens on and silently falls back to the terminal prompt. So the precedence
matters more than it looks: defaults, then the legacy JSON, then what the
gateway actually binds (``config.yaml``), then the environment.
"""

from __future__ import annotations

import json

from hermes_watch.settings import (
    DEFAULT_WATCH_PORT,
    PLATFORM_NAME,
    load_config,
    watch_url,
)


def write_gateway_config(home, port: int, host: str = "0.0.0.0") -> None:
    (home / "config.yaml").write_text(
        f"""\
gateway:
  platforms:
    {PLATFORM_NAME}:
      enabled: true
      extra:
        host: {host}
        port: {port}
""",
        encoding="utf-8",
    )


def write_legacy_json(home, **fields) -> None:
    directory = home / "hermes-watch"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(fields), encoding="utf-8")


def test_defaults_apply_when_nothing_is_configured(tmp_path):
    config = load_config(tmp_path)
    assert config.watch_port == DEFAULT_WATCH_PORT
    assert config.watch_host == "0.0.0.0"


def test_the_gateway_config_decides_the_port(tmp_path):
    """The adapter binds `extra`; everyone else has to read the same value."""
    write_gateway_config(tmp_path, port=9393)
    assert load_config(tmp_path).watch_port == 9393


def test_the_gateway_config_beats_the_legacy_json(tmp_path):
    write_legacy_json(tmp_path, watch_port=8787)
    write_gateway_config(tmp_path, port=9393)
    assert load_config(tmp_path).watch_port == 9393


def test_the_legacy_json_still_works_on_its_own(tmp_path):
    """A config written by the pre-gateway version must not be ignored."""
    write_legacy_json(tmp_path, watch_port=8788, watch_host="127.0.0.1")
    config = load_config(tmp_path)
    assert (config.watch_port, config.watch_host) == (8788, "127.0.0.1")


def test_the_environment_beats_everything(tmp_path, monkeypatch):
    write_legacy_json(tmp_path, watch_port=8787)
    write_gateway_config(tmp_path, port=9393)
    monkeypatch.setenv("HERMES_WATCH_WATCH_PORT", "9500")
    assert load_config(tmp_path).watch_port == 9500


def test_a_broken_config_file_is_not_a_crash(tmp_path):
    (tmp_path / "config.yaml").write_text("gateway:\n  platforms: [oops\n", encoding="utf-8")
    write_legacy_json(tmp_path, watch_port=8788)
    assert load_config(tmp_path).watch_port == 8788


def test_a_config_without_this_platform_is_fine(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "gateway:\n  platforms:\n    telegram:\n      enabled: true\n", encoding="utf-8"
    )
    assert load_config(tmp_path).watch_port == DEFAULT_WATCH_PORT


def test_a_nonsense_port_in_config_is_ignored(tmp_path):
    (tmp_path / "config.yaml").write_text(
        f"gateway:\n  platforms:\n    {PLATFORM_NAME}:\n      extra:\n        port: not-a-number\n",
        encoding="utf-8",
    )
    assert load_config(tmp_path).watch_port == DEFAULT_WATCH_PORT


def test_watch_url_never_names_a_wildcard_address(tmp_path):
    """0.0.0.0 means "every interface"; it is not somewhere you can POST."""
    write_gateway_config(tmp_path, port=9393, host="0.0.0.0")
    assert watch_url(tmp_path) == "http://127.0.0.1:9393"


def test_watch_url_follows_a_configured_interface(tmp_path):
    write_gateway_config(tmp_path, port=9393, host="192.168.1.10")
    assert watch_url(tmp_path) == "http://192.168.1.10:9393"


def test_watch_url_can_be_pointed_at_a_remote_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WATCH_URL", "http://watch.example.test:1234/")
    assert watch_url(tmp_path) == "http://watch.example.test:1234"
