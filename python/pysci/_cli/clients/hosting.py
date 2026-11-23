"""Shared logic for hosted archives (GitHub, Hugging Face): layout, planning, and checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pysci.discovery import parse_archive_descriptor, parse_publication_list
from pysci.errors import SciError
from pysci.manifest import TIMESTAMP_SIDECAR_PATH

from ..errors import CliError, ErrorRecord, Exit
from ..storage.inputs import SignedInput
from .http import Deadline, HttpClient
from .tsa import merge_proofs

ARCHIVE_PATH = "sci/archive.json"
LIST_PATH = "sci/publications.txt"


def publication_prefix(digest: str) -> str:
    return f"sci/publications/{digest[:2]}/{digest}"


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def descriptor_bytes(descriptor: dict[str, str]) -> bytes:
    return json.dumps(descriptor, indent=2).encode("utf-8") + b"\n"


@dataclass
class Prepared:
    """A verified publication ready for upload."""

    item: SignedInput
    zip_path: Path
    reader: dict[str, bytes]
    """Reader files keyed by bundle path, plus the sidecar if available."""

    @property
    def digest(self) -> str:
        return self.item.digest

    @property
    def zip_sha256(self) -> str:
        return self.item.zip_sha256

    @property
    def zip_size(self) -> int:
        return self.zip_path.stat().st_size

    def remote_files(self) -> dict[str, bytes]:
        prefix = publication_prefix(self.digest)
        return {f"{prefix}/{path}": data for path, data in self.reader.items()}


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """What a provider reports about an existing file."""

    size: int | None = None
    git_sha1: str | None = None
    sha256: str | None = None


def matches(remote: RemoteFile, data: bytes) -> bool | None:
    """True/False if a digest decides equality; None if the content must be downloaded."""
    if remote.sha256 is not None:
        return remote.sha256 == hashlib.sha256(data).hexdigest()
    if remote.git_sha1 is not None:
        return remote.git_sha1 == git_blob_sha1(data)
    if remote.size is not None and remote.size != len(data):
        return False
    return None


@dataclass
class ItemReport:
    publication_id: str
    status: str = "pending"
    reused: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    bundle: str | None = None
    error: ErrorRecord | None = None
    exit_code: int = Exit.OK
    warnings: list[str] = field(default_factory=list)

    def fail(self, record: ErrorRecord, exit_code: int) -> None:
        self.status = "failed"
        self.error = record
        self.exit_code = exit_code

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "publication_id": self.publication_id,
            "status": self.status,
            "reader_files_uploaded": self.uploaded,
            "reader_files_reused": self.reused,
            "bundle": self.bundle,
        }
        if self.error is not None:
            out["error"] = self.error.to_json()
        if self.warnings:
            out["warnings"] = self.warnings
        return out


def plan_reader_files(
    pub: Prepared,
    remote: dict[str, RemoteFile],
    download: Callable[[str], bytes | None],
    report: ItemReport,
) -> dict[str, bytes]:
    """Files to write for one publication; reuse matches, merge sidecars, flag conflicts."""
    writes: dict[str, bytes] = {}
    conflicts: list[str] = []
    for path, data in pub.remote_files().items():
        existing = remote.get(path)
        if existing is None:
            writes[path] = data
            continue
        same = matches(existing, data)
        current: bytes | None = None
        if same is None:
            current = download(path)
            same = current == data
        if same:
            report.reused.append(path)
            continue
        if path.endswith("/" + TIMESTAMP_SIDECAR_PATH):
            current = current if current is not None else download(path)
            merged, problems = merge_proofs(current or b"", data)
            if problems:
                report.warnings.append(
                    f"remote timestamp sidecar {path} is malformed and was preserved"
                )
            elif merged != current:
                writes[path] = merged
            else:
                report.reused.append(path)
            continue
        conflicts.append(path)
    if conflicts:
        report.fail(
            ErrorRecord(
                "destination_conflict",
                "the repository already holds different bytes at "
                + ", ".join(conflicts[:3])
                + ("..." if len(conflicts) > 3 else ""),
            ),
            Exit.INVALID,
        )
        return {}
    return writes


def check_descriptor(existing: bytes | None, wanted: dict[str, str]) -> bool:
    """True if ``sci/archive.json`` must be written; raise on a conflicting descriptor."""
    if existing is None:
        return True
    try:
        record = json.loads(existing)
    except ValueError:
        record = None
    if record == wanted:
        return False
    raise CliError(
        "destination_conflict",
        f"{ARCHIVE_PATH} exists with different routes; it was left unchanged",
        Exit.INVALID,
        file=ARCHIVE_PATH,
        observed=existing[:200].decode("utf-8", "replace"),
    )


def merged_list(existing: bytes | None, completed: list[str]) -> bytes:
    """Sorted ``sci/publications.txt`` with completed uploads added."""
    ids: set[str] = set()
    if existing:
        try:
            ids.update(parse_publication_list(existing))
        except SciError:
            raise CliError(
                "destination_conflict",
                f"{LIST_PATH} is malformed; it was left unchanged",
                Exit.INVALID,
                file=LIST_PATH,
            ) from None
    ids.update(completed)
    return "".join(f"{digest}\n" for digest in sorted(ids)).encode("ascii")


@dataclass(frozen=True, slots=True)
class AvailabilityCheck:
    """A public URL to GET after publishing."""

    label: Literal["descriptor", "manifest", "bundle"]
    url: str
    expected: bytes | None = None
    """Exact body to compare; None checks reachability only."""
    required: bool = True
    """Whether a failure fails confirmation and whether the check counts toward reader CORS."""


def availability_checks(
    descriptor: dict[str, str], descriptor_url: str, hosted: list[Prepared]
) -> list[AvailabilityCheck]:
    """The public URLs to confirm after publishing, expanded from the descriptor's templates."""
    raw = descriptor_bytes(descriptor)
    routes = parse_archive_descriptor(raw, file=ARCHIVE_PATH)
    checks = [AvailabilityCheck("descriptor", descriptor_url, raw)]
    for pub in hosted:
        entry = routes.entry(pub.digest, "manifest.json")
        bundle = routes.bundle(pub.digest)
        assert entry is not None, f"missing manifest route for {pub.digest}"
        assert bundle is not None, f"missing bundle route for {pub.digest}"
        checks.append(AvailabilityCheck("manifest", entry, pub.reader["manifest.json"]))
        checks.append(AvailabilityCheck("bundle", bundle, required=False))
    return checks


def confirm_public(
    http: HttpClient,
    checks: list[AvailabilityCheck],
    deadline: Deadline,
) -> dict[str, Any]:
    """Unauthenticated GETs of public URLs: availability, exact bytes, and CORS."""
    results: list[dict[str, Any]] = []
    ok = True
    reader_cors = True
    for check in checks:
        entry: dict[str, Any] = {"label": check.label, "url": check.url}
        try:
            headers = {"Origin": "https://example.org"}
            if check.expected is None:
                headers["Range"] = "bytes=0-0"
            response = http.get(
                check.url,
                deadline=deadline,
                headers=headers,
                max_bytes=max(len(check.expected or b""), 1) + 1024,
            )
            entry["status"] = response.status
            entry["available"] = response.status in (200, 206) and (
                check.expected is None or response.body == check.expected
            )
            entry["cors"] = (response.header("access-control-allow-origin") or "") in (
                "*",
                "https://example.org",
            )
        except CliError as exc:
            entry["available"] = False
            entry["error"] = exc.records[0].to_json()
        if check.required:
            ok = ok and entry["available"]
            reader_cors = reader_cors and bool(entry.get("cors"))
        results.append(entry)
    return {"confirmed": ok, "reader_cors": reader_cors, "checks": results}
