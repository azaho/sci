"""Run the SCI command as ``python -m pysci``."""

from __future__ import annotations

from ._cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
