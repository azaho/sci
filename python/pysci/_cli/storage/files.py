"""Local file operations: atomic writes, no-clobber stores, and recoverable replacement."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

TEMP_MARK = ".sci-tmp-"
"""Prefix of temporary and backup names; never a final output name."""

CHUNK = 1 << 20


def temp_name(target: Path) -> Path:
    return target.with_name(f"{TEMP_MARK}{secrets.token_hex(6)}-{target.name}")


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` through a temporary file and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = temp_name(path)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    _fsync_dir(path.parent)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class StoreOutcome(StrEnum):
    WRITTEN = "written"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class StoreResult:
    outcome: StoreOutcome
    path: Path
    sha256: str
    existing_sha256: str | None = None


class InputChanged(Exception):
    """Bytes written did not match the expected digest (input changed)."""


def store_no_clobber(
    target: Path, write: Callable[[Any], object], expected_sha256: str
) -> StoreResult:
    """Store bytes produced by ``write`` at ``target`` unless a file is already there.

    An identical existing file is kept; a different one is a conflict and is
    left untouched. The new file is written to a temporary name, hashed, and
    linked into place without replacing anything (rename fallback if links
    are unsupported). Raises :class:`InputChanged` if the produced bytes do
    not hash to ``expected_sha256``.
    """
    if target.exists():
        existing = sha256_file(target)
        outcome = StoreOutcome.UNCHANGED if existing == expected_sha256 else StoreOutcome.CONFLICT
        return StoreResult(outcome, target, expected_sha256, existing)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = temp_name(target)
    try:
        hasher = _HashingWriter(open(temp, "xb"))  # noqa: SIM115 - closed below
        try:
            write(hasher)
            hasher.handle.flush()
            os.fsync(hasher.handle.fileno())
        finally:
            hasher.handle.close()
        if hasher.hexdigest() != expected_sha256:
            raise InputChanged(f"{target.name}: bytes changed after verification")
        try:
            os.link(temp, target)
        except FileExistsError:
            existing = sha256_file(target)
            outcome = (
                StoreOutcome.UNCHANGED if existing == expected_sha256 else StoreOutcome.CONFLICT
            )
            return StoreResult(outcome, target, expected_sha256, existing)
        except OSError:
            if target.exists():
                existing = sha256_file(target)
                outcome = (
                    StoreOutcome.UNCHANGED if existing == expected_sha256 else StoreOutcome.CONFLICT
                )
                return StoreResult(outcome, target, expected_sha256, existing)
            os.replace(temp, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)
    _fsync_dir(target.parent)
    return StoreResult(StoreOutcome.WRITTEN, target, expected_sha256)


def store_bytes(target: Path, data: bytes) -> StoreResult:
    return store_no_clobber(target, lambda h: h.write(data), hashlib.sha256(data).hexdigest())


class _HashingWriter:
    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self._hash = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._hash.update(data)
        return self.handle.write(data)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def copy_file_to(handle: BinaryIO, source: Path) -> None:
    with open(source, "rb") as src:
        shutil.copyfileobj(src, handle, CHUNK)


# --------------------------------------------------------------------------- replacement


def recover_replacements(directory: Path) -> list[str]:
    """Restore files left by an interrupted :func:`replace_together`.

    Backups (``.sci-tmp-bak-<name>``) are restored when their final file is
    absent, then all temporary files in ``directory`` are removed. Returns the
    restored names.
    """
    restored: list[str] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return restored
    for name in names:
        if name.startswith(TEMP_MARK + "bak-"):
            final = directory / name[len(TEMP_MARK + "bak-") :]
            if not final.exists():
                os.replace(directory / name, final)
                restored.append(final.name)
    for name in os.listdir(directory):
        if name.startswith(TEMP_MARK):
            with contextlib.suppress(OSError):
                os.unlink(directory / name)
    return restored


def temp_names(directory: Path) -> set[str]:
    try:
        return {n for n in os.listdir(directory) if n.startswith(TEMP_MARK)}
    except OSError:
        return set()


@contextlib.contextmanager
def replace_together(directory: Path, files: Mapping[str, bytes | None]) -> Iterator[None]:
    """Replace several files in ``directory`` as one recoverable operation.

    ``files`` maps names to new bytes (or None to remove). New bytes are
    written to temporary names; existing files are renamed to backups; the
    new files are renamed into place. The body of the ``with`` block then
    checks the result: if it raises, the backups are restored. Otherwise the
    backups are deleted. An interruption leaves backups that
    :func:`recover_replacements` restores.
    """
    temps: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    placed: list[str] = []
    try:
        for name, data in files.items():
            if data is None:
                continue
            temp = directory / f"{TEMP_MARK}new-{name}"
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temps[name] = temp
        for name in files:
            final = directory / name
            if final.exists():
                backup = directory / f"{TEMP_MARK}bak-{name}"
                os.replace(final, backup)
                backups[name] = backup
        for name, temp in temps.items():
            os.replace(temp, directory / name)
            placed.append(name)
        _fsync_dir(directory)
        yield
    except BaseException:
        for name in placed:
            with contextlib.suppress(OSError):
                os.unlink(directory / name)
        for name, backup in backups.items():
            with contextlib.suppress(OSError):
                os.replace(backup, directory / name)
        for temp in temps.values():
            with contextlib.suppress(OSError):
                os.unlink(temp)
        raise
    for backup in backups.values():
        with contextlib.suppress(OSError):
            os.unlink(backup)
    _fsync_dir(directory)


def is_within(path: Path, directory: Path) -> bool:
    """True if ``path`` is ``directory`` or inside it (after resolving)."""
    try:
        resolved = path.resolve()
        base = directory.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def directory_is_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except FileNotFoundError:
        return True
