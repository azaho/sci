"""Random-access byte sources used by the verifiers.

Verification reads bounded ranges and streams large regions in chunks, so a
64 MiB bundle is never held in memory more than one chunk at a time.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Final, Protocol, runtime_checkable

__all__ = [
    "CHUNK_SIZE",
    "ByteSource",
    "FileSource",
    "MemorySource",
    "PathSource",
    "SliceSource",
    "as_source",
    "iter_chunks",
]

CHUNK_SIZE: Final = 1 << 20


@runtime_checkable
class ByteSource(Protocol):
    """Read-only random access to a finite byte string."""

    @property
    def size(self) -> int: ...

    def read_at(self, offset: int, length: int) -> bytes:
        """Return exactly ``length`` bytes at ``offset``; raise on short reads."""
        ...


class MemorySource:
    """A :class:`ByteSource` over bytes already in memory."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self._data = memoryview(data).cast("B") if not isinstance(data, bytes) else data

    @property
    def size(self) -> int:
        return len(self._data)

    def read_at(self, offset: int, length: int) -> bytes:
        _check_range(offset, length, len(self._data))
        return bytes(self._data[offset : offset + length])


class FileSource:
    """A :class:`ByteSource` over a regular file, opened once.

    The size is captured at open time; reads beyond it fail. Use as a context
    manager or call :meth:`close`.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._file: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - closed by close()
        self._size = os.fstat(self._file.fileno()).st_size
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return self._size

    def read_at(self, offset: int, length: int) -> bytes:
        _check_range(offset, length, self._size)
        if hasattr(os, "pread"):
            data = os.pread(self._file.fileno(), length, offset)
        else:  # pragma: no cover - Windows
            with self._lock:
                self._file.seek(offset)
                data = self._file.read(length)
        if len(data) != length:
            raise OSError(f"short read from {self.path}: file changed during verification")
        return data

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> FileSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PathSource:
    """A :class:`ByteSource` that opens a file for each read.

    Suitable for loose bundles with many files (no descriptor is held open).
    The size is captured at construction; a changed file causes ``OSError``.
    """

    __slots__ = ("_size", "path")

    def __init__(self, path: str | os.PathLike[str], size: int | None = None) -> None:
        self.path = Path(path)
        self._size = self.path.stat().st_size if size is None else size

    @property
    def size(self) -> int:
        return self._size

    def read_at(self, offset: int, length: int) -> bytes:
        _check_range(offset, length, self._size)
        with open(self.path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size != self._size:
                raise OSError(f"{self.path} changed during verification")
            handle.seek(offset)
            data = handle.read(length)
        if len(data) != length:
            raise OSError(f"short read from {self.path}")
        return data


class SliceSource:
    """A window of another source."""

    __slots__ = ("_base", "_length", "_offset")

    def __init__(self, base: ByteSource, offset: int, length: int) -> None:
        _check_range(offset, length, base.size)
        self._base, self._offset, self._length = base, offset, length

    @property
    def size(self) -> int:
        return self._length

    def read_at(self, offset: int, length: int) -> bytes:
        _check_range(offset, length, self._length)
        return self._base.read_at(self._offset + offset, length)


def _check_range(offset: int, length: int, size: int) -> None:
    if offset < 0 or length < 0 or offset > size or length > size - offset:
        raise ValueError(f"read of {length} bytes at {offset} exceeds source size {size}")


def iter_chunks(
    source: ByteSource, offset: int = 0, length: int | None = None, chunk_size: int = CHUNK_SIZE
) -> Iterator[bytes]:
    """Yield the range ``[offset, offset + length)`` in bounded chunks."""
    end = source.size if length is None else offset + length
    _check_range(offset, end - offset, source.size)
    position = offset
    while position < end:
        step = min(chunk_size, end - position)
        yield source.read_at(position, step)
        position += step


def as_source(value: ByteSource | bytes | bytearray | memoryview) -> ByteSource:
    """Wrap bytes-like values; pass sources through."""
    if isinstance(value, bytes | bytearray | memoryview):
        return MemorySource(value)
    return value
