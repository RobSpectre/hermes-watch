"""Hermes Watch: approvals, questions and live stats on a Wear OS watch.

The watch is a Hermes gateway platform, not a sidecar. Three pieces, each with
one job:

* :mod:`hermes_watch.platform` -- the adapter. Hosts the watch WebSocket, pushes
  stats, renders approval buttons and question chips, and slides its prompts
  into Hermes' own prompt tokens and choice sets.
* :mod:`hermes_watch.plugin` -- the Hermes plugin. Reports lifecycle events and
  the exact provider-call measurements, and routes approvals to the watch for
  sessions that have no adapter of their own (a CLI session).
* :mod:`hermes_watch.stats` -- the readout. Reads the session store and the
  model metadata; never invents a number it did not measure.

:mod:`hermes_watch.live` is where the first two meet when they share a process.
:mod:`hermes_watch.protocol` is the wire contract, documented in
``docs/protocol.md``; the Wear OS client in ``watch/`` implements the same
contract in Kotlin.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
