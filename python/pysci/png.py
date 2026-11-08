"""PNG display files in baseline publications (protocol §2.1) and the §6.2 decode check.

:class:`PngStructure` is the required baseline check. It streams, keeps a few
bytes of state, and does not verify CRCs or decode. :func:`png_decode_problem`
is the additional check that §6.2 recommends for publishing tools.
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

__all__ = [
    "DISPLAY_MAX_BYTES",
    "DISPLAY_MAX_DIMENSION",
    "DISPLAY_MAX_PIXELS",
    "PngStructure",
    "display_problem",
    "is_display_candidate",
    "png_decode_problem",
]

DISPLAY_MAX_BYTES: Final = 2 * 1024 * 1024
DISPLAY_MAX_PIXELS: Final = 8_000_000
DISPLAY_MAX_DIMENSION: Final = 8192

_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_ANIMATION: Final = frozenset({b"acTL", b"fcTL", b"fdAT"})
_CRITICAL: Final = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})
_DEPTHS: Final = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_CHANNELS: Final = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7: Final = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)
_STAGE_SIGNATURE, _STAGE_HEADER, _STAGE_CHUNKS, _STAGE_ENDED = range(4)
_NEEDED: Final = {_STAGE_SIGNATURE: 8, _STAGE_HEADER: 16, _STAGE_CHUNKS: 8}


def _has_png_extension(path: str) -> bool:
    return path[-4:].lower() == ".png"


def is_display_candidate(path: str, size: int) -> bool:
    """True if the path and size allow a baseline display file."""
    return _has_png_extension(path) and size <= DISPLAY_MAX_BYTES


class PngStructure:
    """Streaming §2.1 checks: signature, chunk layout, ``IHDR`` dimensions, no animation."""

    __slots__ = ("_failure", "_pending", "_skip", "_stage")

    def __init__(self) -> None:
        self._stage = _STAGE_SIGNATURE
        self._pending = bytearray()
        self._skip = 0
        self._failure: str | None = None

    def feed(self, data: bytes) -> None:
        offset = 0
        while offset < len(data) and self._failure is None:
            if self._skip:
                step = min(self._skip, len(data) - offset)
                self._skip -= step
                offset += step
            elif self._stage == _STAGE_ENDED:
                self._failure = "bytes follow the IEND chunk"
            else:
                needed = _NEEDED[self._stage]
                step = min(needed - len(self._pending), len(data) - offset)
                self._pending += data[offset : offset + step]
                offset += step
                if len(self._pending) == needed:
                    self._consume(bytes(self._pending))
                    self._pending.clear()

    def _consume(self, header: bytes) -> None:
        if self._stage == _STAGE_SIGNATURE:
            if header != _SIGNATURE:
                self._failure = "the file does not start with the PNG signature"
            self._stage = _STAGE_HEADER
            return
        size, kind = struct.unpack_from(">I4s", header)
        if kind in _ANIMATION:
            self._failure = "animated PNG chunks are not allowed"
        elif self._stage == _STAGE_HEADER:
            if kind != b"IHDR" or size != 13:
                self._failure = "the first chunk must be IHDR with 13 data bytes"
                return
            width, height = struct.unpack_from(">II", header, 8)
            if not (
                1 <= width <= DISPLAY_MAX_DIMENSION
                and 1 <= height <= DISPLAY_MAX_DIMENSION
                and width * height <= DISPLAY_MAX_PIXELS
            ):
                self._failure = f"{width} x {height} pixels is outside the display limits"
                return
            self._stage = _STAGE_CHUNKS
            self._skip = size - 8 + 4
        elif kind == b"IHDR":
            self._failure = "only one IHDR chunk is allowed"
        elif kind == b"IEND":
            if size:
                self._failure = "the IEND chunk must be empty"
            self._stage = _STAGE_ENDED
            self._skip = 4
        else:
            self._skip = size + 4

    def problem(self) -> str | None:
        """Why the bytes fed so far fail the checks, or None if they pass."""
        if self._failure is not None:
            return self._failure
        if self._stage != _STAGE_ENDED or self._skip or self._pending:
            return "the file ends before a complete IEND chunk"
        return None


def display_problem(path: str, size: int, structure: PngStructure | None) -> str | None:
    """Why a display file is outside the baseline, or None if it meets §2.1."""
    if not _has_png_extension(path):
        return "the extension is not .png"
    if size > DISPLAY_MAX_BYTES:
        return f"{size:,} bytes exceeds {DISPLAY_MAX_BYTES:,}"
    if structure is None:
        return "the file was not checked"
    return structure.problem()


def _expected_scanlines(width: int, height: int, depth: int, color: int, interlaced: bool) -> int:
    channels = _CHANNELS[color]
    total = 0
    for x, y, dx, dy in _ADAM7 if interlaced else ((0, 0, 1, 1),):
        columns = (width - x + dx - 1) // dx
        rows = (height - y + dy - 1) // dy
        if columns > 0 and rows > 0:
            total += ((columns * depth * channels + 7) // 8 + 1) * rows
    return total


def png_decode_problem(data: bytes) -> str | None:
    """Why a PNG that passed :class:`PngStructure` cannot be decoded, or None (§6.2).

    Checks CRCs, ``IHDR`` fields, required chunks, and the exact inflated size.
    Inflated data is bounded by the §2.1 pixel limit.
    """
    offset = 8
    idat = bytearray()
    idat_closed = False
    palette = False
    width = height = depth = color = interlace = 0
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        body = data[offset + 8 : offset + 8 + size]
        stored = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(kind + body) != stored:
            return f"chunk {kind.decode('latin-1')} has a bad CRC"
        if not kind[0] & 0x20 and kind not in _CRITICAL:
            return f"unknown critical chunk {kind.decode('latin-1')}"
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if depth not in _DEPTHS.get(color, ()):
                return f"invalid color type {color} with bit depth {depth}"
            if compression or filtering or interlace > 1:
                return "unsupported IHDR compression, filter, or interlace method"
        elif kind == b"PLTE":
            palette = True
        elif kind == b"IDAT":
            if idat_closed:
                return "IDAT chunks must be consecutive"
            idat += body
        elif idat:
            idat_closed = True
        offset += 12 + size
    if color == 3 and not palette:
        return "palette image without a PLTE chunk"
    if not idat:
        return "no IDAT chunk"
    expected = _expected_scanlines(width, height, depth, color, interlace == 1)
    inflater = zlib.decompressobj()
    try:
        pixels = inflater.decompress(bytes(idat), expected + 1)
        extra = inflater.decompress(inflater.unconsumed_tail, 1) if len(pixels) == expected else b""
    except zlib.error:
        return "image data is not a valid zlib stream"
    if len(pixels) != expected or extra:
        return "image data does not match the image dimensions"
    if not inflater.eof or inflater.unused_data:
        return "image data continues after the end of the zlib stream"
    return _filter_problem(pixels, width, height, depth, color, interlace == 1)


def _filter_problem(
    pixels: bytes, width: int, height: int, depth: int, color: int, interlaced: bool
) -> str | None:
    channels = _CHANNELS[color]
    position = 0
    for x, y, dx, dy in _ADAM7 if interlaced else ((0, 0, 1, 1),):
        columns = (width - x + dx - 1) // dx
        rows = (height - y + dy - 1) // dy
        if columns <= 0 or rows <= 0:
            continue
        stride = (columns * depth * channels + 7) // 8 + 1
        for _ in range(rows):
            if pixels[position] > 4:
                return "a scanline uses an unknown filter"
            position += stride
    return None
