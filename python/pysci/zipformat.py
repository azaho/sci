"""The canonical single-file bundle ZIP (protocol §2.2).

:func:`parse_zip_layout` checks every structural rule of §2.2 except CRC-32
values and entry ordering against the manifest (those need entry data and
the manifest; see :mod:`pysci.bundle`). It reads only the end record, the
central directory, and each local header, checking bounds before every read,
and verifies that headers and data account for every byte.

:func:`write_canonical_zip` produces the exact bytes defined by §2.2 and
:func:`canonical_zip_size` evaluates the §2.1 size formula.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from .errors import Incomplete, Invalid, Issue, Outcome
from .source import ByteSource, iter_chunks

__all__ = [
    "BASELINE_ZIP_LIMIT",
    "SupportsWrite",
    "ZipEntry",
    "canonical_zip_size",
    "crc32_of",
    "parse_zip_layout",
    "write_canonical_zip",
]

BASELINE_ZIP_LIMIT: Final = 64 * 1024 * 1024

_LOCAL_SIG: Final = 0x04034B50
_CENTRAL_SIG: Final = 0x02014B50
_EOCD_SIG: Final = 0x06054B50
_LOCAL: Final = struct.Struct("<IHHHHHIIIHH")
_CENTRAL: Final = struct.Struct("<IHHHHHHIIIHHHHHII")
_EOCD: Final = struct.Struct("<IHHHHIIH")
_VERSION: Final = 10  # version 1.0
_MADE_BY: Final = 0x000A  # MS-DOS host, version 1.0
_DOS_DATE: Final = 0x0021  # 1980-01-01
_DOS_TIME: Final = 0x0000
_MAX_U16: Final = 0xFFFF
_MAX_U32: Final = 0xFFFFFFFF
ENTRY_OVERHEAD: Final = _LOCAL.size + _CENTRAL.size  # 76 bytes, plus 2 * path length
EOCD_SIZE: Final = _EOCD.size  # 22


@dataclass(frozen=True, slots=True)
class ZipEntry:
    """Location of one stored entry inside a structurally valid ZIP."""

    name: bytes
    local_offset: int
    data_offset: int
    size: int
    crc32: int

    @property
    def path(self) -> str:
        """The entry name decoded as ASCII (validated by the parser)."""
        return self.name.decode("ascii")


def _invalid(code: str, requirement: str, observed: object = None) -> Invalid:
    return Invalid(
        Issue(
            code,
            requirement,
            section="2.2",
            file="bundle.zip",
            observed=None if observed is None else str(observed),
        )
    )


def parse_zip_layout(
    source: ByteSource,
    *,
    max_entries: int = _MAX_U16 - 1,
    max_central_bytes: int = 64 * 1024 * 1024,
) -> list[ZipEntry]:
    """Parse and check the §2.2 layout without reading entry data.

    Raises:
        Invalid: any layout, field, or consistency violation.
        Incomplete: the central directory exceeds a local limit.
    """
    size = source.size
    if size < EOCD_SIZE:
        raise _invalid(
            "zip_truncated",
            "a ZIP ends with a 22-byte end-of-central-directory record",
            f"{size} bytes",
        )
    eocd_offset = size - EOCD_SIZE
    sig, disk, cd_disk, count_disk, count_total, cd_size, cd_offset, comment_len = _EOCD.unpack(
        source.read_at(eocd_offset, EOCD_SIZE)
    )
    if sig != _EOCD_SIG:
        raise _invalid(
            "zip_end_record",
            "one end-of-central-directory record must end at the last byte, with an empty comment",
        )
    if comment_len != 0:
        raise _invalid("zip_comment", "the archive comment must be empty", comment_len)
    if disk != 0 or cd_disk != 0:
        raise _invalid("zip_disk_number", "all disk-number fields must be zero")
    if _MAX_U16 in (count_disk, count_total) or _MAX_U32 in (cd_size, cd_offset):
        raise _invalid("zip64", "ZIP64 records and sentinel values are prohibited")
    if count_disk != count_total:
        raise _invalid(
            "zip_entry_count",
            "both entry-count fields must give the actual count",
            f"{count_disk} != {count_total}",
        )
    if cd_offset > eocd_offset or cd_size != eocd_offset - cd_offset:
        raise _invalid(
            "zip_central_extent",
            "the central directory must lie between the last "
            "entry data and the end record, with its stated offset and length",
        )
    if count_total > max_entries:
        raise Incomplete(
            Issue(
                "zip_entry_limit",
                "ZIP entry count exceeds the local limit",
                section="6",
                file="bundle.zip",
                observed=str(count_total),
                outcome=Outcome.INCOMPLETE,
            )
        )
    if cd_size > max_central_bytes:
        raise Incomplete(
            Issue(
                "zip_central_limit",
                "central directory exceeds the local limit",
                section="6",
                file="bundle.zip",
                observed=str(cd_size),
                outcome=Outcome.INCOMPLETE,
            )
        )
    central = source.read_at(cd_offset, cd_size)
    records = _parse_central(central, count_total)
    return _check_locals(source, records, cd_offset)


@dataclass(frozen=True, slots=True)
class _Central:
    name: bytes
    crc32: int
    size: int
    offset: int
    fields: tuple[int, ...]  # version needed .. uncompressed size, shared with local header


def _parse_central(central: bytes, count: int) -> list[_Central]:
    records: list[_Central] = []
    position = 0
    for index in range(count):
        if len(central) - position < _CENTRAL.size:
            raise _invalid(
                "zip_central_truncated",
                "central directory must contain one header per entry",
                f"entry {index}",
            )
        (
            sig,
            made_by,
            needed,
            flags,
            method,
            mtime,
            mdate,
            crc,
            csize,
            usize,
            name_len,
            extra_len,
            comment_len,
            disk_start,
            internal,
            external,
            offset,
        ) = _CENTRAL.unpack_from(central, position)
        if sig != _CENTRAL_SIG:
            raise _invalid(
                "zip_central_signature", "central file header signature expected", f"entry {index}"
            )
        _check_common(index, needed, flags, method, mtime, mdate, csize, usize)
        if made_by != _MADE_BY:
            raise _invalid(
                "zip_version_made_by", "version made by must be 0x000A", f"0x{made_by:04X}"
            )
        if extra_len != 0 or comment_len != 0:
            raise _invalid(
                "zip_extra_field", "extra fields and file comments must be empty", f"entry {index}"
            )
        if disk_start != 0:
            raise _invalid("zip_disk_number", "all disk-number fields must be zero")
        if internal != 0 or external != 0:
            raise _invalid(
                "zip_attributes",
                "internal and external file attributes must be zero",
                f"entry {index}",
            )
        if offset == _MAX_U32:
            raise _invalid("zip64", "ZIP64 sentinel values are prohibited")
        start = position + _CENTRAL.size
        if name_len > len(central) - start:
            raise _invalid(
                "zip_central_truncated", "file name exceeds the central directory", f"entry {index}"
            )
        name = central[start : start + name_len]
        records.append(
            _Central(
                name,
                crc,
                usize,
                offset,
                (needed, flags, method, mtime, mdate, crc, csize, usize, name_len, extra_len),
            )
        )
        position = start + name_len
    if position != len(central):
        raise _invalid(
            "zip_unaccounted_bytes",
            "central directory length must match its headers exactly",
            f"{len(central) - position} extra bytes",
        )
    return records


def _check_common(
    index: int, needed: int, flags: int, method: int, mtime: int, mdate: int, csize: int, usize: int
) -> None:
    if needed != _VERSION:
        raise _invalid("zip_version_needed", "version needed to extract must be 1.0 (10)", needed)
    if flags != 0:
        raise _invalid(
            "zip_flags",
            "general-purpose flags must be zero (no data descriptors)",
            f"0x{flags:04X}",
        )
    if method != 0:
        raise _invalid("zip_method", "compression method must be 0 (stored)", method)
    if mtime != _DOS_TIME:
        raise _invalid("zip_mod_time", "last-modification time must be 0x0000", f"0x{mtime:04X}")
    if mdate != _DOS_DATE:
        raise _invalid("zip_mod_date", "last-modification date must be 0x0021", f"0x{mdate:04X}")
    if _MAX_U32 in (csize, usize):
        raise _invalid("zip64", "ZIP64 sentinel values are prohibited")
    if csize != usize:
        raise _invalid(
            "zip_size_mismatch",
            "stored entries have equal compressed and uncompressed sizes",
            f"entry {index}",
        )


def _check_locals(
    source: ByteSource, records: Sequence[_Central], cd_offset: int
) -> list[ZipEntry]:
    entries: list[ZipEntry] = []
    position = 0
    for index, record in enumerate(records):
        if record.offset != position:
            raise _invalid(
                "zip_local_offset",
                "each central entry must point to its local "
                "header, stored consecutively from byte zero",
                f"entry {index}: offset {record.offset}, expected {position}",
            )
        if cd_offset - position < _LOCAL.size:
            raise _invalid(
                "zip_local_truncated", "local header exceeds the entry region", f"entry {index}"
            )
        (sig, *fields) = _LOCAL.unpack(source.read_at(position, _LOCAL.size))
        if sig != _LOCAL_SIG:
            raise _invalid(
                "zip_local_signature", "local file header signature expected", f"entry {index}"
            )
        needed, flags, method, mtime, mdate, _crc, csize, usize, _nlen, _xlen = fields
        _check_common(index, needed, flags, method, mtime, mdate, csize, usize)
        if tuple(fields) != record.fields:
            raise _invalid(
                "zip_header_mismatch",
                "fields shared by local and central headers must agree byte for byte",
                f"entry {index}",
            )
        name_start = position + _LOCAL.size
        name_len = len(record.name)
        if name_len > cd_offset - name_start:
            raise _invalid(
                "zip_local_truncated", "file name exceeds the entry region", f"entry {index}"
            )
        if source.read_at(name_start, name_len) != record.name:
            raise _invalid(
                "zip_header_mismatch", "local and central file names must agree", f"entry {index}"
            )
        data_offset = name_start + name_len
        if record.size > cd_offset - data_offset:
            raise _invalid(
                "zip_data_truncated",
                "entry data must end before the central directory",
                f"entry {index}",
            )
        if not record.name.isascii():
            raise _invalid("zip_name", "entry names must be ASCII paths", record.name)
        entries.append(ZipEntry(record.name, position, data_offset, record.size, record.crc32))
        position = data_offset + record.size
    if position != cd_offset:
        raise _invalid(
            "zip_unaccounted_bytes",
            "local headers and data must account for every byte before the central directory",
            f"{cd_offset - position} unaccounted bytes",
        )
    return entries


def crc32_of(source: ByteSource) -> int:
    """CRC-32 of a whole source, streamed."""
    crc = 0
    for chunk in iter_chunks(source):
        crc = zlib.crc32(chunk, crc)
    return crc


class SupportsWrite(Protocol):
    def write(self, data: bytes, /) -> object: ...


def canonical_zip_size(entries: Iterable[tuple[str, int]]) -> int:
    """Exact canonical ZIP length: ``22 + sum(file_bytes + 76 + 2 * path_bytes)`` (§2.1).

    ``entries`` must include ``manifest.json`` and ``manifest.json.sig``.
    """
    return EOCD_SIZE + sum(
        size + ENTRY_OVERHEAD + 2 * len(path.encode("utf-8")) for path, size in entries
    )


def write_canonical_zip(sink: SupportsWrite, entries: Sequence[tuple[str, ByteSource]]) -> int:
    """Write the canonical ZIP for entries already in §2.2 order; return its length.

    Each source is read twice (CRC pass, then copy) in bounded chunks. The
    caller is responsible for the entry order (manifest, signature, then
    listed paths in ascending ASCII order); see
    :func:`pysci.bundle.canonical_entries`.

    Raises:
        ValueError: the entries cannot be represented in classic ZIP.
    """
    if len(entries) >= _MAX_U16:
        raise ValueError("too many entries for classic ZIP")
    central = bytearray()
    position = 0
    for name, source in entries:
        name_bytes = name.encode("ascii")
        size = source.size
        if size >= _MAX_U32 or len(name_bytes) > _MAX_U16:
            raise ValueError(f"entry {name!r} cannot be represented without ZIP64")
        crc = crc32_of(source)
        header = _LOCAL.pack(
            _LOCAL_SIG, _VERSION, 0, 0, _DOS_TIME, _DOS_DATE, crc, size, size, len(name_bytes), 0
        )
        sink.write(header + name_bytes)
        for chunk in iter_chunks(source):
            sink.write(chunk)
        central += _CENTRAL.pack(
            _CENTRAL_SIG,
            _MADE_BY,
            _VERSION,
            0,
            0,
            _DOS_TIME,
            _DOS_DATE,
            crc,
            size,
            size,
            len(name_bytes),
            0,
            0,
            0,
            0,
            0,
            position,
        )
        central += name_bytes
        position += _LOCAL.size + len(name_bytes) + size
        if position >= _MAX_U32:
            raise ValueError("archive too large for classic ZIP")
    sink.write(bytes(central))
    sink.write(_EOCD.pack(_EOCD_SIG, 0, 0, len(entries), len(entries), len(central), position, 0))
    return position + len(central) + EOCD_SIZE
