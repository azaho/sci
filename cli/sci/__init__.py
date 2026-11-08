"""The ``sci`` command-line tool for SCI publications, built on :mod:`pysci`."""

from __future__ import annotations

from importlib import metadata

try:
    __version__ = metadata.version("sci-cli")
except metadata.PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.1.0"

__all__ = ["__version__"]
