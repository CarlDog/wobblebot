"""Lurker CLI — an alias for ``cli/observe``, today; future home for advisor commentary.

Run as a module::

    python -m wobblebot.cli.lurker          # same effect as: python -m wobblebot.cli.observe
    python -m wobblebot.cli.lurker --symbols BTC/USD,ETH/USD,DOGE/USD

Today this is a one-line delegate to ``wobblebot.cli.observe`` — pure
data collection, no engine, no LLM, no orders. Same flags, same
config, same behavior.

The ``lurker`` name exists for operator muscle memory and as the
planned home for Phase 3.4-ish "lurker mode": observer + advisor
commentary on the live market without trading. When that lands,
``cli/observe`` stays the bare data-collection variant for operators
who want only price snapshots, and ``cli/lurker`` becomes the richer
"watch the market with LLM commentary" entry point.
"""

from __future__ import annotations

from wobblebot.cli.observe import main

if __name__ == "__main__":
    raise SystemExit(main())
