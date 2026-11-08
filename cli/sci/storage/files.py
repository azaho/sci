from __future__ import annotations
import os
import tempfile
from pathlib import Path
from pysci import Invalid


def atomic(path: Path, data: bytes, *, replace: bool = False, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Invalid(f"symlink output: {path}")
    if path.exists() and not replace:
        if path.read_bytes() == data:
            return
        raise Invalid(f"conflicting output: {path}")
    fd, temp = tempfile.mkstemp(prefix=".sci-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(temp, path)
        else:
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise Invalid(f"concurrent conflicting output: {path}")
    finally:
        Path(temp).unlink(missing_ok=True)
