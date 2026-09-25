"""Hermes Watch: bridge Hermes approvals, questions and live stats to Wear OS.

Two halves in one package:

* :mod:`hermes_watch.daemon` + :mod:`hermes_watch.hub` -- the bridge daemon that
  holds watch connections, blocking approvals, and the stats snapshot.
* :mod:`hermes_watch.plugin` -- the Hermes plugin that feeds the daemon from
  inside a live agent process.

The wire contract is :mod:`hermes_watch.protocol`, documented in
``docs/protocol.md``. The Wear OS client in ``watch/`` implements the same
contract in Kotlin.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
