"""Archive retrieval (§8.2): descriptors, individual files, ZIP ranges, and complete bundles."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pysci.bundle import (
    DEFAULT_LIMITS,
    VerificationLimits,
    VerificationResult,
    read_zip_entry,
    verify_file,
    verify_zip,
)
from pysci.discovery import ArchiveDescriptor, parse_archive_descriptor
from pysci.errors import Incomplete, Issue, Outcome, SciError
from pysci.manifest import MANIFEST_PATH, SIGNATURE_PATH, TIMESTAMP_SIDECAR_PATH, Manifest
from pysci.manifest import verify_manifest as _verify_manifest
from pysci.source import FileSource
from pysci.zipformat import parse_zip_layout

from ..errors import CliError, ErrorRecord, Exit, exit_for_outcome
from .http import Deadline, HttpClient, NetworkError, status_error

DESCRIPTOR_LIMIT = 1 << 20
BLOCK = 64 * 1024
MAX_RANGE_REQUESTS = 4096
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


class SourceFailure(Exception):
    """One route or source could not supply verified bytes."""

    def __init__(self, record: ErrorRecord, exit_code: int) -> None:
        super().__init__(record.message)
        self.record = record
        self.exit_code = Exit(exit_code)


def _from_cli(exc: CliError, route: str) -> SourceFailure:
    record = exc.records[0]
    return SourceFailure(
        ErrorRecord(record.code, record.message, record.retryable, extra={"route": route}),
        exc.exit_code,
    )


def _from_sci(exc: SciError, route: str) -> SourceFailure:
    issue = exc.issue
    return SourceFailure(
        ErrorRecord(
            issue.code,
            issue.requirement,
            exc.outcome is Outcome.INCOMPLETE,
            file=issue.file,
            field=issue.field,
            observed=issue.observed,
            section=issue.section,
            extra={"route": route},
        ),
        exit_for_outcome(exc.outcome),
    )


class NoRangeSupport(Exception):
    pass


class HttpRangeSource:
    """A :class:`pysci.source.ByteSource` backed by single-range GET requests.

    Blocks are cached; contiguous missing blocks are fetched together. The
    number of requests is bounded; exceeding it raises ``Incomplete``.
    """

    def __init__(self, http: HttpClient, url: str, deadline: Deadline) -> None:
        self.http = http
        self.url = url
        self.deadline = deadline
        self.requests = 0
        self._blocks: dict[int, bytes] = {}
        response = self._get(f"bytes=-{BLOCK}")
        start, end, total = self._content_range(
            response.header("content-range"), len(response.body)
        )
        self._size = total
        self._store(start, response.body)
        del end

    @property
    def size(self) -> int:
        return self._size

    def _get(self, value: str) -> Any:
        if self.requests >= MAX_RANGE_REQUESTS:
            raise Incomplete(
                Issue(
                    "range_request_limit",
                    "ZIP range retrieval exceeded the local request limit",
                    section="6",
                    outcome=Outcome.INCOMPLETE,
                )
            )
        self.requests += 1
        try:
            response = self.http.get(
                self.url, deadline=self.deadline, headers={"Range": value}, max_bytes=8 * BLOCK
            )
        except CliError as exc:
            if exc.code == "response_too_large":
                raise NoRangeSupport() from None
            raise
        if response.status == 200:
            raise NoRangeSupport()
        if response.status != 206:
            raise status_error(response, "bundle range request")
        return response

    def _content_range(self, header: str | None, length: int) -> tuple[int, int, int]:
        match = _CONTENT_RANGE.fullmatch((header or "").strip())
        if match is None or match.group(3) == "*":
            raise NoRangeSupport()
        start, end, total = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if end < start or end - start + 1 != length or end >= total:
            raise CliError(
                "invalid_range_response",
                "Content-Range does not match the returned bytes",
                Exit.INCOMPLETE,
                retryable=True,
            )
        return start, end, total

    def _store(self, start: int, data: bytes) -> None:
        # Cache only whole aligned blocks (and the final partial block).
        position = start
        offset = 0
        while offset < len(data):
            block = position // BLOCK
            block_start = block * BLOCK
            block_end = min(block_start + BLOCK, self._size)
            if position == block_start and len(data) - offset >= block_end - block_start:
                self._blocks[block] = data[offset : offset + block_end - block_start]
                offset += block_end - block_start
                position = block_end
            else:
                skip = min(block_end - position, len(data) - offset)
                offset += skip
                position += skip

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self._size:
            raise ValueError("read beyond the end of the bundle")
        if length == 0:
            return b""
        first, last = offset // BLOCK, (offset + length - 1) // BLOCK
        block = first
        while block <= last:
            if block in self._blocks:
                block += 1
                continue
            run_end = block
            while run_end + 1 <= last and run_end + 1 not in self._blocks:
                run_end += 1
            start = block * BLOCK
            end = min((run_end + 1) * BLOCK, self._size) - 1
            if (end - start + 1) > 8 * BLOCK:
                end = start + 8 * BLOCK - 1
                run_end = block + 7
            response = self._get(f"bytes={start}-{end}")
            got_start, _, total = self._content_range(
                response.header("content-range"), len(response.body)
            )
            if got_start != start or total != self._size:
                raise CliError(
                    "invalid_range_response",
                    "range response does not match the request",
                    Exit.INCOMPLETE,
                    retryable=True,
                )
            self._store(start, response.body)
            block = run_end + 1
        out = b"".join(self._blocks[b] for b in range(first, last + 1))
        begin = offset - first * BLOCK
        return out[begin : begin + length]


@dataclass
class ArchiveSource:
    """One archive descriptor offering a publication with an expected ID."""

    http: HttpClient
    descriptor_url: str
    digest: str
    workdir: Path
    limits: VerificationLimits = DEFAULT_LIMITS
    routes: list[str] = field(default_factory=list)
    _descriptor: ArchiveDescriptor | None = None
    _manifest: Manifest | None = None
    _signature: bytes | None = None
    _zip: Path | None = None
    _range: HttpRangeSource | None = None
    _range_failed: bool = False
    _file: FileSource | None = None

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _open_zip(self, path: Path) -> FileSource:
        if self._file is None:
            self._file = FileSource(path)
        return self._file

    def descriptor(self, deadline: Deadline) -> ArchiveDescriptor:
        if self._descriptor is None:
            try:
                response = self.http.get(
                    self.descriptor_url, deadline=deadline, max_bytes=DESCRIPTOR_LIMIT
                )
            except CliError as exc:
                raise _from_cli(exc, "descriptor") from None
            if response.status != 200:
                raise _from_cli(status_error(response, "archive descriptor"), "descriptor")
            try:
                self._descriptor = parse_archive_descriptor(response.body, file=self.descriptor_url)
            except SciError as exc:
                raise _from_sci(exc, "descriptor") from None
        return self._descriptor

    # ------------------------------------------------------------------ raw routes

    def _fetch(self, url: str, deadline: Deadline, max_bytes: int, route: str) -> bytes | None:
        try:
            response = self.http.get(url, deadline=deadline, max_bytes=max_bytes)
        except CliError as exc:
            if exc.code == "response_too_large":
                raise SourceFailure(
                    ErrorRecord(
                        "file_size_mismatch",
                        "retrieved file is larger than its manifest entry",
                        extra={"route": route},
                    ),
                    Exit.INVALID,
                ) from None
            raise _from_cli(exc, route) from None
        if response.status == 404:
            return None
        if response.status != 200:
            raise _from_cli(status_error(response, route), route)
        return response.body

    def entry(self, path: str, deadline: Deadline, max_bytes: int) -> bytes | None:
        url = self.descriptor(deadline).entry(self.digest, path)
        if url is None:
            return None
        return self._fetch(url, deadline, max_bytes, "entry_url")

    # ------------------------------------------------------------------ manifest

    def manifest(self, deadline: Deadline) -> Manifest:
        """Verified manifest (§6.1): entry routes, then ZIP ranges, then the complete ZIP."""
        if self._manifest is not None:
            return self._manifest
        failures: list[SourceFailure] = []
        descriptor = self.descriptor(deadline)
        if descriptor.entry_url is not None:
            try:
                manifest_bytes = self.entry(MANIFEST_PATH, deadline, self.limits.max_manifest_bytes)
                signature = self.entry(SIGNATURE_PATH, deadline, 65)
                if manifest_bytes is not None and signature is not None:
                    self._manifest = self._verify(manifest_bytes, signature, "entry_url")
                    self._signature = signature
                    self.routes.append("entry_url")
                    return self._manifest
            except SourceFailure as exc:
                failures.append(exc)
        if descriptor.bundle_url is not None:
            try:
                source = self._zip_source(deadline)
                if source is not None:
                    entries = parse_zip_layout(source)
                    manifest_bytes = source.read_at(entries[0].data_offset, entries[0].size)
                    signature = source.read_at(entries[1].data_offset, min(entries[1].size, 65))
                    self._manifest = self._verify(manifest_bytes, signature, "bundle_url")
                    self._signature = signature
                    return self._manifest
            except SciError as exc:
                failures.append(_from_sci(exc, "bundle_url"))
            except SourceFailure as exc:
                failures.append(exc)
            except CliError as exc:
                failures.append(_from_cli(exc, "bundle_url"))
        if failures:
            raise failures[-1]
        raise SourceFailure(
            ErrorRecord(
                "not_available",
                "the archive does not serve this publication's manifest",
                extra={"descriptor": self.descriptor_url},
            ),
            Exit.INCOMPLETE,
        )

    def _verify(self, manifest_bytes: bytes, signature: bytes, route: str) -> Manifest:
        try:
            return _verify_manifest(manifest_bytes, signature, expected_id=self.digest)
        except SciError as exc:
            raise _from_sci(exc, route) from None

    def signature(self, deadline: Deadline) -> bytes:
        self.manifest(deadline)
        assert self._signature is not None
        return self._signature

    # ------------------------------------------------------------------ ZIP access

    def _zip_source(self, deadline: Deadline) -> HttpRangeSource | FileSource | None:
        if self._zip is not None:
            return self._open_zip(self._zip)
        url = self.descriptor(deadline).bundle(self.digest)
        if url is None:
            return None
        if self._range is not None:
            return self._range
        if not self._range_failed:
            try:
                self._range = HttpRangeSource(self.http, url, deadline)
                self.routes.append("bundle_url ranges")
                return self._range
            except NoRangeSupport:
                self._range_failed = True
        path = self.download_zip(deadline)
        return self._open_zip(path) if path is not None else None

    def download_zip(self, deadline: Deadline) -> Path | None:
        """Download the complete ZIP to a temporary file (within the input limit)."""
        if self._zip is not None:
            return self._zip
        url = self.descriptor(deadline).bundle(self.digest)
        if url is None:
            return None
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - kept for the operation
            dir=self.workdir, prefix=".sci-tmp-", suffix=".zip", delete=False
        )
        try:
            response = self.http.get(
                url,
                deadline=deadline,
                sink=handle,
                max_bytes=self.limits.max_input_bytes or (1 << 40),
            )
        except CliError as exc:
            handle.close()
            raise _from_cli(exc, "bundle_url") from None
        handle.close()
        if response.status == 404:
            return None
        if response.status != 200:
            raise _from_cli(status_error(response, "bundle download"), "bundle_url")
        self._zip = Path(handle.name)
        self.routes.append("bundle_url")
        return self._zip

    def verify_complete_zip(self, deadline: Deadline) -> tuple[Path, VerificationResult] | None:
        path = self.download_zip(deadline)
        if path is None:
            return None
        result = verify_zip(path, expected_id=self.digest, limits=self.limits)
        return path, result

    # ------------------------------------------------------------------ files

    def file(self, path: str, deadline: Deadline) -> bytes:
        """One listed file after §6.1 and §6.2: entry/blob routes, ZIP ranges, complete ZIP."""
        manifest = self.manifest(deadline)
        entry = manifest.files.get(path)
        if entry is None:
            raise SourceFailure(
                ErrorRecord(
                    "file_not_listed",
                    "requested path must be a manifest files key",
                    file=path,
                    section="6.2",
                ),
                Exit.INVALID,
            )
        descriptor = self.descriptor(deadline)
        failures: list[SourceFailure] = []
        routes: list[tuple[str, str | None]] = [
            ("entry_url", descriptor.entry(self.digest, path)),
            ("blob_url", descriptor.blob(entry.sha256)),
        ]
        for route, url in routes:
            if url is None:
                continue
            try:
                data = self._fetch(url, deadline, entry.bytes, route)
                if data is None:
                    continue
                verify_file(manifest, path, data)
                self.routes.append(route)
                return data
            except SciError as exc:
                failures.append(_from_sci(exc, route))
            except SourceFailure as exc:
                failures.append(exc)
        if descriptor.bundle_url is not None:
            try:
                source = self._zip_source(deadline)
                if source is not None:
                    _, data = read_zip_entry(source, path, expected_id=self.digest)
                    return data
            except SciError as exc:
                failures.append(_from_sci(exc, "bundle_url"))
            except SourceFailure as exc:
                failures.append(exc)
            except CliError as exc:
                failures.append(_from_cli(exc, "bundle_url"))
        if failures:
            raise failures[-1]
        raise SourceFailure(
            ErrorRecord("not_available", "no route serves this file", file=path),
            Exit.INCOMPLETE,
        )

    def sidecar(self, deadline: Deadline) -> bytes | None:
        try:
            return self.entry(TIMESTAMP_SIDECAR_PATH, deadline, 16 << 20)
        except SourceFailure:
            return None


def failure_exit(failures: list[SourceFailure]) -> Exit:
    """No source succeeded: invalid only if every source served failing content."""
    codes = {f.exit_code for f in failures}
    if not failures:
        return Exit.INCOMPLETE
    if codes == {Exit.INVALID}:
        return Exit.INVALID
    if codes == {Exit.UNSUPPORTED}:
        return Exit.UNSUPPORTED
    return Exit.INCOMPLETE


__all__ = [
    "ArchiveSource",
    "HttpRangeSource",
    "NetworkError",
    "SourceFailure",
    "failure_exit",
]
