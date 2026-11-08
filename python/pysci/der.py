"""Minimal DER framing for timestamp files (protocol §6.8).

:func:`frame_values` splits concatenated DER values by tag and definite
minimal length. It stops at the first value that is not complete -- a
truncated tag or length, an indefinite or nonminimal length, a length beyond
the remaining bytes, or a length beyond a local bound -- and never scans
ahead to resynchronize.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "DerFraming",
    "encode_length",
    "frame_values",
    "read_header",
    "validate_der_structure",
]

MAX_LENGTH_OCTETS: Final = 8


@dataclass(frozen=True, slots=True)
class DerFraming:
    """Complete values found as ``(offset, length)`` pairs, and where parsing stopped.

    ``complete`` is True when the values cover the whole input and the input
    is nonempty.
    """

    values: tuple[tuple[int, int], ...]
    stop_offset: int
    total: int

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.stop_offset == self.total


def read_header(
    data: bytes | memoryview, offset: int, end: int | None = None, max_length: int | None = None
) -> tuple[int, int, int] | None:
    """Parse the tag and length at ``offset``.

    Returns ``(tag_first_octet, content_offset, content_length)`` for a
    complete definite-length value that fits before ``end``, else None.
    Multi-octet tags must be minimally encoded; lengths must use the minimal
    DER form.
    """
    limit = len(data) if end is None else end
    position = offset
    if position >= limit:
        return None
    first = data[position]
    position += 1
    if first & 0x1F == 0x1F:  # high-tag-number form
        leading = True
        while True:
            if position >= limit:
                return None
            octet = data[position]
            position += 1
            if leading and octet & 0x7F == 0:
                return None  # nonminimal tag number
            leading = False
            if not octet & 0x80:
                break
    if position >= limit:
        return None
    octet = data[position]
    position += 1
    if octet < 0x80:
        length = octet
    else:
        count = octet & 0x7F
        if count == 0 or count > MAX_LENGTH_OCTETS:
            return None  # indefinite length or beyond the local bound
        if count > limit - position:
            return None
        if data[position] == 0:
            return None  # leading zero octet: nonminimal
        length = int.from_bytes(bytes(data[position : position + count]), "big")
        position += count
        if length < 0x80:
            return None  # long form for a short length: nonminimal
    if max_length is not None and length > max_length:
        return None
    if length > limit - position:
        return None
    return first, position, length


def frame_values(data: bytes, *, max_value_length: int | None = None) -> DerFraming:
    """Delimit concatenated DER values without resynchronizing (§6.8)."""
    values: list[tuple[int, int]] = []
    position = 0
    while position < len(data):
        header = read_header(data, position, max_length=max_value_length)
        if header is None:
            break
        _, content, length = header
        end = content + length
        values.append((position, end - position))
        position = end
    return DerFraming(tuple(values), position, len(data))


def validate_der_structure(data: bytes, max_depth: int = 64) -> bool:
    """True if ``data`` is exactly one value whose nested framing is definite and minimal.

    Constructed values are descended recursively (up to ``max_depth``) and
    must be fully consumed by their children. Primitive contents are not
    interpreted.
    """
    view = memoryview(data)

    def walk(start: int, end: int, depth: int) -> bool:
        position = start
        while position < end:
            header = read_header(view, position, end)
            if header is None:
                return False
            first, content, length = header
            constructed = bool(first & 0x20)
            if constructed and (
                depth >= max_depth or not walk(content, content + length, depth + 1)
            ):
                return False
            position = content + length
        return True

    header = read_header(view, 0)
    if header is None or header[1] + header[2] != len(data):
        return False
    return walk(0, len(data), 0)


def encode_length(length: int) -> bytes:
    """DER definite-length octets for ``length``."""
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body
