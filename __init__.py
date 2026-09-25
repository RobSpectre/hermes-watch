"""Hermes plugin shim.

This repository is two things at once: a Python distribution (``bridge/``) and a
drop-in Hermes plugin. Hermes' plugin loader imports *this* directory and calls
``register(ctx)``, so the entry point lives here and delegates to the real
implementation in ``bridge/hermes_watch/plugin.py``.

Install either way::

    hermes plugins install RobSpectre/hermes-watch --enable   # plugin path
    pip install ./bridge                                      # daemon + CLI path

The ``sys.path`` insertion is what lets the plugin work from a plain
``git clone`` into ``~/.hermes/plugins/`` with no install step.
"""

from __future__ import annotations

import pathlib
import sys

_BRIDGE_DIR = pathlib.Path(__file__).resolve().parent / "bridge"
if _BRIDGE_DIR.is_dir() and str(_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_DIR))

from hermes_watch.plugin import register  # noqa: E402

__all__ = ["register"]
