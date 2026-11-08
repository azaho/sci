from __future__ import annotations
from pathlib import Path
from pysci import Bundle, Invalid
from pysci.bundle import read_regular
from sci import config
from sci.storage.files import atomic


def pack(b: Bundle, destination: Path, source: Path | None = None) -> None:
    atomic(destination / (b.id + ".zip"), b.zip())
    if source is not None:
        sidecar = (
            source / "manifest.json.sig.tsr" if source.is_dir() else source.with_name(b.id + ".tsr")
        )
        if sidecar.exists():
            atomic(destination / (b.id + ".tsr"), read_regular(sidecar, 4 * 1024 * 1024))
    config.remember(destination)


def candidates(path: Path) -> list[Path]:
    found = (
        [path]
        if path.is_file() or (path / "manifest.json").exists()
        else sorted(path.glob("*.zip"))
    )
    if not found:
        raise Invalid("no signed publications")
    return found
