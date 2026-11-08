"""Publication verification: manifest, file, metadata, and complete bundle checks (§6.1-6.4).

Entry points:

* :func:`verify_zip` -- a single-file ZIP bundle (any file name);
* :func:`verify_loose` -- loose files given as a path -> bytes/source mapping;
* :func:`verify_directory` / :func:`verify_path` -- local files on disk;
* :func:`verify_file` -- one listed file against a verified manifest (§6.2).

Results never raise for protocol failures; they return a
:class:`VerificationResult` whose :attr:`~VerificationResult.outcome`
distinguishes ``invalid`` from ``unsupported`` and ``incomplete`` (§6).
Verification never extracts, decodes, or executes payload bytes before they
pass their hash checks; large files are streamed in bounded chunks.
"""

from __future__ import annotations

import hashlib
import os
import stat
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from .baseline import BaselineReport, evaluate_baseline
from .errors import Incomplete, Invalid, Issue, IssueCollector, Outcome, SciError
from .manifest import (
    LICENSE_PATH,
    MANIFEST_PATH,
    METADATA_PATH,
    README_PATH,
    SIGNATURE_PATH,
    TIMESTAMP_SIDECAR_PATH,
    FileEntry,
    Manifest,
    verify_manifest,
)
from .metadata import Metadata, parse_metadata
from .paths import path_sort_key
from .png import PngStructure, display_problem, is_display_candidate
from .source import ByteSource, FileSource, MemorySource, PathSource, as_source, iter_chunks
from .text import TextFileValidator
from .zipformat import (
    BASELINE_ZIP_LIMIT,
    SupportsWrite,
    ZipEntry,
    canonical_zip_size,
    parse_zip_layout,
    write_canonical_zip,
)

__all__ = [
    "Scope",
    "VerificationLimits",
    "VerificationResult",
    "canonical_entries",
    "extract_verified",
    "manifest_zip_size",
    "read_zip_entry",
    "verify_directory",
    "verify_file",
    "verify_loose",
    "verify_path",
    "verify_zip",
    "write_bundle_zip",
]

_SIGNATURE_LIMIT: Final = 64


class Scope(StrEnum):
    """How much of a publication to verify."""

    MANIFEST = "manifest"
    """§6.1 only (plus the planned ZIP size); payloads may be absent."""
    COMPLETE = "complete"
    """§6.4 complete bundle checks."""


@dataclass(frozen=True, slots=True)
class VerificationLimits:
    """Local resource limits. Exceeding one yields ``incomplete``, never ``invalid``.

    The defaults accommodate every baseline publication with large margins
    (§2.1 requires services to complete checks for baseline publications).
    """

    max_input_bytes: int | None = 4 * 1024**3
    """Total input size (the ZIP, or the sum of loose files); None for unlimited."""
    max_manifest_bytes: int = 16 * 1024**2
    max_metadata_bytes: int = 16 * 1024**2
    max_entries: int = 65534
    max_central_directory_bytes: int = 64 * 1024**2


DEFAULT_LIMITS: Final = VerificationLimits()


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of a publication verification.

    ``baseline`` is a boolean only after complete successful verification;
    otherwise None. ``zip_bytes`` is the canonical ZIP size (§2.1 formula)
    once the manifest is verified. ``zip_sha256`` is the SHA-256 of the
    canonical ZIP when it was computed (always for ZIP input).
    """

    outcome: Outcome
    scope: Scope
    issues: tuple[Issue, ...] = ()
    publication_id: str | None = None
    manifest: Manifest | None = None
    metadata: Metadata | None = None
    baseline: bool | None = None
    baseline_report: BaselineReport | None = None
    zip_bytes: int | None = None
    zip_sha256: str | None = None
    checked_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.VALID

    @property
    def within_baseline_zip_limit(self) -> bool | None:
        return None if self.zip_bytes is None else self.zip_bytes <= BASELINE_ZIP_LIMIT


def manifest_zip_size(manifest: Manifest, signature_bytes: int = 64) -> int:
    """§2.1 ZIP size computed from the manifest alone."""
    sizes = [(MANIFEST_PATH, len(manifest.raw)), (SIGNATURE_PATH, signature_bytes)]
    sizes.extend((path, entry.bytes) for path, entry in manifest.files.items())
    return canonical_zip_size(sizes)


# --------------------------------------------------------------------------- helpers


def _incomplete(
    code: str, requirement: str, observed: object = None, file: str | None = None
) -> Incomplete:
    return Incomplete(
        Issue(
            code,
            requirement,
            section="6",
            file=file,
            observed=None if observed is None else str(observed),
            outcome=Outcome.INCOMPLETE,
        )
    )


def _check_budget(total: int, limits: VerificationLimits) -> None:
    if limits.max_input_bytes is not None and total > limits.max_input_bytes:
        raise _incomplete(
            "input_budget_exceeded",
            "input exceeds the local verification budget",
            f"{total} bytes > {limits.max_input_bytes}",
        )


def _read_small(source: ByteSource, limit: int, file: str) -> bytes:
    if source.size > limit:
        raise _incomplete(
            "local_size_limit",
            f"{file} exceeds the local size limit",
            f"{source.size} bytes > {limit}",
            file=file,
        )
    return source.read_at(0, source.size)


class _FileDigest:
    """Streaming SHA-256 (and optional CRC-32) for one entry, with optional teeing."""

    def __init__(self, crc: bool) -> None:
        self.sha = hashlib.sha256()
        self.crc: int | None = 0 if crc else None
        self.sinks: list[Callable[[bytes], object]] = []

    def update(self, chunk: bytes) -> None:
        self.sha.update(chunk)
        if self.crc is not None:
            self.crc = zlib.crc32(chunk, self.crc)
        for sink in self.sinks:
            sink(chunk)


class _PayloadChecks:
    """Collects metadata bytes and text-file validation while payloads stream."""

    def __init__(self, manifest: Manifest, limits: VerificationLimits) -> None:
        self.manifest = manifest
        self.limits = limits
        self.metadata_chunks: list[bytes] = []
        self.text: dict[str, TextFileValidator] = {
            README_PATH: TextFileValidator(file=README_PATH, nonblank=True)
        }
        if LICENSE_PATH in manifest.files:
            self.text[LICENSE_PATH] = TextFileValidator(file=LICENSE_PATH)
        self.license_sha256: str | None = None
        self.png: dict[str, PngStructure] = {}

    def attach(self, path: str, digest: _FileDigest) -> None:
        if path == METADATA_PATH:
            size = self.manifest.files[path].bytes
            if size > self.limits.max_metadata_bytes:
                raise _incomplete(
                    "local_size_limit", "sci.json exceeds the local size limit", size, file=path
                )
            digest.sinks.append(self.metadata_chunks.append)
        elif path in self.text:
            digest.sinks.append(self.text[path].feed)
        elif is_display_candidate(path, self.manifest.files[path].bytes):
            # Display paths are known only after sci.json, which can follow the files.
            self.png[path] = PngStructure()
            digest.sinks.append(self.png[path].feed)

    def display_problems(self, metadata: Metadata) -> dict[str, str]:
        problems: dict[str, str] = {}
        for item in metadata.display:
            size = self.manifest.files[item.path].bytes
            problem = display_problem(item.path, size, self.png.get(item.path))
            if problem is not None:
                problems[item.path] = problem
        return problems

    def finish(self, issues: IssueCollector) -> Metadata | None:
        for validator in self.text.values():
            issues.extend(validator.finish())
        metadata = None
        try:
            metadata = parse_metadata(b"".join(self.metadata_chunks), self.manifest.files.keys())
        except SciError as exc:
            issues.extend(exc.issues)
        return metadata


def _compare_digest(
    path: str, entry: FileEntry, size: int, digest: _FileDigest, issues: IssueCollector
) -> bool:
    if size != entry.bytes:
        issues.add(
            "file_size_mismatch",
            "byte count must equal the manifest bytes value",
            section="6.2",
            file=path,
            observed=f"{size} != {entry.bytes}",
        )
        return False
    actual = digest.sha.hexdigest()
    if actual != entry.sha256:
        issues.add(
            "file_hash_mismatch",
            "SHA-256 must equal the manifest sha256 value",
            section="6.2",
            file=path,
            observed=actual,
        )
        return False
    return True


def _finish(
    manifest: Manifest,
    signature_size: int,
    payload: _PayloadChecks,
    issues: IssueCollector,
    zip_sha256: str | None,
    checked: Iterable[str],
) -> VerificationResult:
    metadata = payload.finish(issues) if not issues else None
    zip_bytes = manifest_zip_size(manifest, signature_size)
    if issues:
        return _failure(issues.issues, Scope.COMPLETE, manifest, zip_bytes)
    assert metadata is not None
    license_entry = manifest.files.get(LICENSE_PATH)
    report = evaluate_baseline(
        zip_bytes=zip_bytes,
        file_count=len(manifest.files),
        display_items=len(metadata.display),
        manifest_bytes=len(manifest.raw),
        metadata_bytes=manifest.files[METADATA_PATH].bytes,
        readme_bytes=manifest.files[README_PATH].bytes,
        license_sha256=None if license_entry is None else license_entry.sha256,
        display_problems=payload.display_problems(metadata),
    )
    return VerificationResult(
        outcome=Outcome.VALID,
        scope=Scope.COMPLETE,
        publication_id=manifest.publication_id,
        manifest=manifest,
        metadata=metadata,
        baseline=report.eligible,
        baseline_report=report,
        zip_bytes=zip_bytes,
        zip_sha256=zip_sha256,
        checked_files=tuple(checked),
    )


def _failure(
    issues: Iterable[Issue],
    scope: Scope,
    manifest: Manifest | None = None,
    zip_bytes: int | None = None,
) -> VerificationResult:
    issue_tuple = tuple(issues)
    collector = IssueCollector(list(issue_tuple))
    return VerificationResult(
        outcome=collector.outcome,
        scope=scope,
        issues=issue_tuple,
        publication_id=None if manifest is None else manifest.publication_id,
        manifest=manifest,
        zip_bytes=zip_bytes,
    )


def _manifest_only(manifest: Manifest, signature_size: int) -> VerificationResult:
    return VerificationResult(
        outcome=Outcome.VALID,
        scope=Scope.MANIFEST,
        publication_id=manifest.publication_id,
        manifest=manifest,
        zip_bytes=manifest_zip_size(manifest, signature_size),
    )


def _guard(scope: Scope, run: Callable[[], VerificationResult]) -> VerificationResult:
    try:
        return run()
    except SciError as exc:
        return _failure(exc.issues, scope)
    except OSError as exc:
        return _failure(
            [
                Issue(
                    "input_unavailable",
                    "input could not be read completely",
                    section="6",
                    observed=str(exc),
                    outcome=Outcome.INCOMPLETE,
                )
            ],
            scope,
        )


# --------------------------------------------------------------------------- ZIP


def verify_zip(
    bundle: ByteSource | bytes | str | os.PathLike[str],
    *,
    expected_id: str | None = None,
    scope: Scope = Scope.COMPLETE,
    limits: VerificationLimits = DEFAULT_LIMITS,
) -> VerificationResult:
    """Verify a ZIP bundle under §2.2 and §6.4 (or §6.1 with ``Scope.MANIFEST``).

    ``bundle`` may be a path, bytes, or a :class:`~pysci.source.ByteSource`.
    The file name is irrelevant (§2.2: renamed ZIPs remain verifiable).
    """
    if isinstance(bundle, str | os.PathLike):
        try:
            source = FileSource(bundle)
        except OSError as exc:
            return _failure(
                [
                    Issue(
                        "input_unavailable",
                        "bundle could not be opened",
                        section="6",
                        observed=str(exc),
                        outcome=Outcome.INCOMPLETE,
                    )
                ],
                scope,
            )
        with source:
            return _guard(scope, lambda: _verify_zip(source, expected_id, scope, limits))
    src = as_source(bundle)
    return _guard(scope, lambda: _verify_zip(src, expected_id, scope, limits))


def _verify_zip(
    source: ByteSource, expected_id: str | None, scope: Scope, limits: VerificationLimits
) -> VerificationResult:
    _check_budget(source.size, limits)
    entries = parse_zip_layout(
        source, max_entries=limits.max_entries, max_central_bytes=limits.max_central_directory_bytes
    )
    names = [entry.name for entry in entries]
    if names[:2] != [MANIFEST_PATH.encode(), SIGNATURE_PATH.encode()]:
        raise Invalid(
            Issue(
                "zip_first_entries",
                "the first two entries must be manifest.json and manifest.json.sig",
                section="2.2",
                file="bundle.zip",
                observed=str([n.decode("ascii", "replace") for n in names[:2]]),
            )
        )
    manifest_entry, signature_entry = entries[0], entries[1]
    if manifest_entry.size > limits.max_manifest_bytes:
        raise _incomplete(
            "local_size_limit",
            "manifest.json exceeds the local size limit",
            manifest_entry.size,
            file=MANIFEST_PATH,
        )
    manifest_bytes = source.read_at(manifest_entry.data_offset, manifest_entry.size)
    signature = source.read_at(
        signature_entry.data_offset, min(signature_entry.size, _SIGNATURE_LIMIT + 1)
    )
    _check_crc(manifest_bytes, manifest_entry)
    if signature_entry.size <= _SIGNATURE_LIMIT:
        _check_crc(signature, signature_entry)
    manifest = verify_manifest(manifest_bytes, signature, expected_id=expected_id)
    if scope is Scope.MANIFEST:
        return _manifest_only(manifest, signature_entry.size)
    _check_zip_inventory(entries[2:], manifest)
    return _scan_zip(source, entries, manifest, limits)


def _check_crc(data: bytes, entry: ZipEntry) -> None:
    if zlib.crc32(data) != entry.crc32:
        raise Invalid(
            Issue("zip_crc", "CRC-32 must match the entry data", section="2.2", file=entry.path)
        )


def _check_zip_inventory(entries: list[ZipEntry], manifest: Manifest) -> None:
    issues = IssueCollector()
    paths = [entry.path for entry in entries]
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            issues.add(
                "zip_duplicate_entry",
                "each listed file appears exactly once",
                section="2.2",
                file=path,
            )
        seen.add(path)
    extra = sorted(seen - set(manifest.files))
    missing = sorted(set(manifest.files) - seen)
    if extra:
        issues.add(
            "bundle_extra_file",
            "the bundle must contain only listed files plus the manifest and signature",
            section="3",
            observed=extra,
        )
    if missing:
        issues.add(
            "bundle_missing_file",
            "every listed file must be present",
            section="3",
            observed=missing,
        )
    if not issues and paths != manifest.sorted_paths():
        issues.add(
            "zip_entry_order",
            "listed entries must follow ascending ASCII path order",
            section="2.2",
            observed=paths[:5],
        )
    issues.raise_if_any()


def _scan_zip(
    source: ByteSource, entries: list[ZipEntry], manifest: Manifest, limits: VerificationLimits
) -> VerificationResult:
    """One sequential pass: whole-ZIP SHA-256, per-entry CRC-32 and SHA-256, text checks."""
    whole = hashlib.sha256()
    payload = _PayloadChecks(manifest, limits)
    issues = IssueCollector()
    position = 0
    for index, entry in enumerate(entries):
        whole.update(source.read_at(position, entry.data_offset - position))
        digest = _FileDigest(crc=True)
        if index >= 2:
            payload.attach(entry.path, digest)
        for chunk in iter_chunks(source, entry.data_offset, entry.size):
            whole.update(chunk)
            digest.update(chunk)
        position = entry.data_offset + entry.size
        if digest.crc != entry.crc32:
            issues.add(
                "zip_crc", "CRC-32 must match the entry data", section="2.2", file=entry.path
            )
            continue
        if index >= 2:
            _compare_digest(entry.path, manifest.files[entry.path], entry.size, digest, issues)
    for chunk in iter_chunks(source, position, source.size - position):
        whole.update(chunk)
    return _finish(
        manifest,
        entries[1].size,
        payload,
        issues,
        whole.hexdigest(),
        [entry.path for entry in entries[2:]],
    )


# --------------------------------------------------------------------------- loose


def verify_loose(
    files: Mapping[str, ByteSource | bytes],
    *,
    expected_id: str | None = None,
    scope: Scope = Scope.COMPLETE,
    limits: VerificationLimits = DEFAULT_LIMITS,
    compute_zip_sha256: bool = False,
) -> VerificationResult:
    """Verify loose bundle files given as ``{relative path: bytes or source}``.

    The optional ``manifest.json.sig.tsr`` sidecar is excluded from the
    inventory (§6.4). With ``compute_zip_sha256`` the canonical ZIP is
    serialized into a hash (not stored) after successful verification.
    """
    sources = {path: as_source(value) for path, value in files.items()}
    return _guard(
        scope, lambda: _verify_loose(sources, expected_id, scope, limits, compute_zip_sha256)
    )


def _verify_loose(
    sources: dict[str, ByteSource],
    expected_id: str | None,
    scope: Scope,
    limits: VerificationLimits,
    compute_zip_sha256: bool,
) -> VerificationResult:
    _check_budget(sum(source.size for source in sources.values()), limits)
    missing = [p for p in (MANIFEST_PATH, SIGNATURE_PATH) if p not in sources]
    if missing:
        raise Invalid(
            Issue(
                "bundle_missing_file",
                "manifest.json and manifest.json.sig are required",
                section="2",
                observed=str(missing),
            )
        )
    manifest_bytes = _read_small(sources[MANIFEST_PATH], limits.max_manifest_bytes, MANIFEST_PATH)
    signature_source = sources[SIGNATURE_PATH]
    signature = signature_source.read_at(0, min(signature_source.size, _SIGNATURE_LIMIT + 1))
    manifest = verify_manifest(manifest_bytes, signature, expected_id=expected_id)
    if scope is Scope.MANIFEST:
        return _manifest_only(manifest, signature_source.size)
    present = set(sources) - {MANIFEST_PATH, SIGNATURE_PATH, TIMESTAMP_SIDECAR_PATH}
    issues = IssueCollector()
    extra = sorted(present - set(manifest.files))
    absent = sorted(set(manifest.files) - present)
    if extra:
        issues.add(
            "bundle_extra_file",
            "the bundle must contain only listed files plus the manifest and signature",
            section="3",
            observed=extra,
        )
    if absent:
        issues.add(
            "bundle_missing_file", "every listed file must be present", section="3", observed=absent
        )
    issues.raise_if_any()
    payload = _PayloadChecks(manifest, limits)
    for path in manifest.sorted_paths():
        source = sources[path]
        entry = manifest.files[path]
        if source.size != entry.bytes:  # size check before reading (§6.2)
            issues.add(
                "file_size_mismatch",
                "byte count must equal the manifest bytes value",
                section="6.2",
                file=path,
                observed=f"{source.size} != {entry.bytes}",
            )
            continue
        digest = _FileDigest(crc=False)
        payload.attach(path, digest)
        for chunk in iter_chunks(source):
            digest.update(chunk)
        _compare_digest(path, entry, source.size, digest, issues)
    zip_sha256 = None
    if compute_zip_sha256 and not issues:
        hasher = _HashSink()
        write_canonical_zip(
            hasher,
            canonical_entries(manifest_bytes, signature, {p: sources[p] for p in manifest.files}),
        )
        zip_sha256 = hasher.hexdigest()
    return _finish(
        manifest, signature_source.size, payload, issues, zip_sha256, manifest.sorted_paths()
    )


class _HashSink:
    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._hash.update(data)
        return len(data)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _scan_directory(root: Path) -> tuple[dict[str, ByteSource], list[Issue]]:
    """Map every regular file under ``root`` to a source; report other file types."""
    sources: dict[str, ByteSource] = {}
    problems: list[Issue] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as iterator:
            for item in iterator:
                relative = Path(item.path).relative_to(root).as_posix()
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(item.path))
                elif stat.S_ISREG(info.st_mode):
                    sources[relative] = PathSource(item.path, info.st_size)
                else:
                    problems.append(
                        Issue(
                            "not_regular_file",
                            "bundle files must be regular "
                            "files (no symbolic links or special files)",
                            section="6.4",
                            file=relative,
                        )
                    )
    return sources, problems


def verify_directory(
    directory: str | os.PathLike[str],
    *,
    expected_id: str | None = None,
    scope: Scope = Scope.COMPLETE,
    limits: VerificationLimits = DEFAULT_LIMITS,
    compute_zip_sha256: bool = False,
) -> VerificationResult:
    """Verify a signed directory of loose files (symbolic links are rejected)."""
    try:
        sources, problems = _scan_directory(Path(directory))
    except OSError as exc:
        return _failure(
            [
                Issue(
                    "input_unavailable",
                    "directory could not be read",
                    section="6",
                    observed=str(exc),
                    outcome=Outcome.INCOMPLETE,
                )
            ],
            scope,
        )
    if problems and scope is Scope.COMPLETE:
        return _failure(problems, scope)
    if scope is Scope.MANIFEST:
        sources = {k: v for k, v in sources.items() if k in (MANIFEST_PATH, SIGNATURE_PATH)}
    return verify_loose(
        sources,
        expected_id=expected_id,
        scope=scope,
        limits=limits,
        compute_zip_sha256=compute_zip_sha256,
    )


def verify_path(
    path: str | os.PathLike[str],
    *,
    expected_id: str | None = None,
    scope: Scope = Scope.COMPLETE,
    limits: VerificationLimits = DEFAULT_LIMITS,
) -> VerificationResult:
    """Verify a directory (loose files) or a file (ZIP bundle)."""
    if Path(path).is_dir():
        return verify_directory(path, expected_id=expected_id, scope=scope, limits=limits)
    return verify_zip(path, expected_id=expected_id, scope=scope, limits=limits)


# --------------------------------------------------------------------------- single files


def read_zip_entry(
    bundle: ByteSource | bytes,
    path: str,
    *,
    expected_id: str | None = None,
    limits: VerificationLimits = DEFAULT_LIMITS,
) -> tuple[Manifest, bytes]:
    """Read one listed file from a ZIP after §6.1 and §6.2 checks (no complete check).

    Only the end record, central directory, local headers, manifest,
    signature, and the requested entry are read, so ``bundle`` may be a
    range-request-backed :class:`~pysci.source.ByteSource`. This does not
    establish that the whole bundle is valid (§2.2: verify completely before
    unpacking the whole bundle).

    Raises:
        Invalid, Unsupported, Incomplete: a failed or impossible check.
    """
    source = as_source(bundle)
    entries = parse_zip_layout(
        source,
        max_entries=limits.max_entries,
        max_central_bytes=limits.max_central_directory_bytes,
    )
    if [e.name for e in entries[:2]] != [MANIFEST_PATH.encode(), SIGNATURE_PATH.encode()]:
        raise Invalid(
            Issue(
                "zip_first_entries",
                "the first two entries must be manifest.json and manifest.json.sig",
                section="2.2",
                file="bundle.zip",
            )
        )
    if entries[0].size > limits.max_manifest_bytes:
        raise _incomplete(
            "local_size_limit",
            "manifest.json exceeds the local size limit",
            entries[0].size,
            file=MANIFEST_PATH,
        )
    manifest_bytes = source.read_at(entries[0].data_offset, entries[0].size)
    signature = source.read_at(entries[1].data_offset, min(entries[1].size, _SIGNATURE_LIMIT + 1))
    manifest = verify_manifest(manifest_bytes, signature, expected_id=expected_id)
    matches = [e for e in entries[2:] if e.name == path.encode("utf-8")]
    if path not in manifest.files or len(matches) != 1:
        raise Invalid(
            Issue(
                "file_not_listed",
                "requested path must be a listed ZIP entry",
                section="6.2",
                file=path,
            )
        )
    entry = matches[0]
    if entry.size != manifest.files[path].bytes:
        raise Invalid(
            Issue(
                "file_size_mismatch",
                "byte count must equal the manifest bytes value",
                section="6.2",
                file=path,
            )
        )
    data = source.read_at(entry.data_offset, entry.size)
    verify_file(manifest, path, data)
    return manifest, data


def verify_file(manifest: Manifest, path: str, data: ByteSource | bytes) -> None:
    """§6.2 file checks for one retrieved file against a verified manifest.

    Raises:
        Invalid: the path is not listed, or its size or hash differs.
    """
    entry = manifest.files.get(path)
    if entry is None:
        raise Invalid(
            Issue(
                "file_not_listed",
                "requested path must be a manifest files key",
                section="6.2",
                file=path,
            )
        )
    source = as_source(data)
    issues = IssueCollector()
    if source.size != entry.bytes:
        issues.add(
            "file_size_mismatch",
            "byte count must equal the manifest bytes value",
            section="6.2",
            file=path,
            observed=f"{source.size} != {entry.bytes}",
        )
    else:
        digest = _FileDigest(crc=False)
        for chunk in iter_chunks(source):
            digest.update(chunk)
        _compare_digest(path, entry, source.size, digest, issues)
    issues.raise_if_any()


# --------------------------------------------------------------------------- writing


def canonical_entries(
    manifest_bytes: bytes,
    signature: bytes,
    files: Mapping[str, ByteSource | bytes],
) -> list[tuple[str, ByteSource]]:
    """Entries in §2.2 order: manifest, signature, then listed paths by ASCII bytes."""
    ordered: list[tuple[str, ByteSource]] = [
        (MANIFEST_PATH, MemorySource(manifest_bytes)),
        (SIGNATURE_PATH, MemorySource(signature)),
    ]
    ordered.extend((path, as_source(files[path])) for path in sorted(files, key=path_sort_key))
    return ordered


def write_bundle_zip(
    sink: SupportsWrite,
    manifest_bytes: bytes,
    signature: bytes,
    files: Mapping[str, ByteSource | bytes],
) -> int:
    """Write the canonical ZIP (§2.2) to ``sink`` (an object with ``write``); return its size.

    ``files`` must contain exactly the manifest's listed paths. Verify the
    bundle first; this function only serializes.
    """
    return write_canonical_zip(sink, canonical_entries(manifest_bytes, signature, files))


def extract_verified(
    bundle: ByteSource | bytes | str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    expected_id: str | None = None,
    limits: VerificationLimits = DEFAULT_LIMITS,
) -> VerificationResult:
    """Verify a ZIP completely, then write its entries under ``destination``.

    Files are written only if verification succeeds, only inside
    ``destination`` (§6.4), with exclusive creation (existing files are never
    overwritten). Directories are created as needed. Returns the verification
    result; on failure nothing is written.
    """
    result = verify_zip(bundle, expected_id=expected_id, limits=limits)
    if not result.ok:
        return result
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    opened: FileSource | None = None
    if isinstance(bundle, str | os.PathLike):
        opened = FileSource(bundle)
        source: ByteSource = opened
    else:
        source = as_source(bundle)
    try:
        entries = parse_zip_layout(source)
        for entry in entries:
            target = (root / entry.path).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"entry escapes destination: {entry.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with open(target, "xb") as handle:
                for chunk in iter_chunks(source, entry.data_offset, entry.size):
                    digest.update(chunk)
                    handle.write(chunk)
            listed = result.manifest.files.get(entry.path) if result.manifest else None
            if listed is not None and digest.hexdigest() != listed.sha256:
                raise OSError(f"{entry.path} changed after verification")
    finally:
        if opened is not None:
            opened.close()
    return result
