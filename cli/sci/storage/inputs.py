"""Signed local inputs: candidate discovery, verification, sidecars, and ZIP materialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pysci.bundle import (
    VerificationResult,
    read_zip_entry,
    verify_directory,
    verify_file,
    verify_zip,
    write_bundle_zip,
)
from pysci.errors import Outcome
from pysci.manifest import (
    LICENSE_PATH,
    MANIFEST_PATH,
    METADATA_PATH,
    README_PATH,
    SIGNATURE_PATH,
    TIMESTAMP_SIDECAR_PATH,
    Manifest,
)
from pysci.source import FileSource, PathSource
from pysci.zipformat import parse_zip_layout

from ..errors import CliError, Exit, issue_json
from .files import copy_file_to


@dataclass(frozen=True, slots=True)
class SignedInput:
    path: Path
    kind: Literal["zip", "directory"]
    result: VerificationResult

    @property
    def manifest(self) -> Manifest:
        assert self.result.manifest is not None
        return self.result.manifest

    @property
    def digest(self) -> str:
        return self.manifest.digest

    @property
    def publication_id(self) -> str:
        return self.manifest.publication_id

    @property
    def zip_sha256(self) -> str:
        assert self.result.zip_sha256 is not None
        return self.result.zip_sha256

    @property
    def sidecar(self) -> Path | None:
        return find_sidecar(self.path, self.kind, self.digest)


def find_sidecar(path: Path, kind: str, digest: str) -> Path | None:
    """Locate a timestamp sidecar: ``manifest.json.sig.tsr`` or ``<digest>.tsr``."""
    if kind == "directory":
        candidate = path / TIMESTAMP_SIDECAR_PATH
        return candidate if candidate.is_file() else None
    for candidate in (path.parent / f"{digest}.tsr", path.with_suffix(".tsr")):
        if candidate.is_file():
            return candidate
    return None


def sidecar_target(path: Path, kind: str, digest: str) -> Path:
    if kind == "directory":
        return path / TIMESTAMP_SIDECAR_PATH
    return path.parent / f"{digest}.tsr"


def verify_input(path: Path, expected_id: str | None = None) -> tuple[str, VerificationResult]:
    if path.is_dir():
        return "directory", verify_directory(path, expected_id=expected_id, compute_zip_sha256=True)
    return "zip", verify_zip(path, expected_id=expected_id)


def candidates(path: Path) -> list[Path]:
    """Shared input rules: a ZIP or signed directory, else immediate ``*.zip`` children."""
    if not path.exists():
        raise CliError("input_missing", f"{path} does not exist", Exit.USAGE, file=str(path))
    if path.is_file() or (path / MANIFEST_PATH).exists():
        return [path]
    found = sorted(p for p in path.iterdir() if p.suffix == ".zip" and p.is_file())
    if not found:
        raise CliError(
            "no_candidates",
            f"{path} is neither a signed publication nor a folder containing *.zip bundles",
            Exit.USAGE,
            file=str(path),
        )
    return found


def collect_signed_inputs(
    paths: list[Path],
) -> tuple[list[SignedInput], list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify every candidate (§6.4); return (valid deduplicated, invalid, duplicates)."""
    valid: list[SignedInput] = []
    invalid: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen: dict[str, SignedInput] = {}
    for root in paths:
        for candidate in candidates(root):
            kind, result = verify_input(candidate)
            if not result.ok or result.manifest is None:
                invalid.append(
                    {
                        "path": str(candidate),
                        "status": str(result.outcome),
                        "publication_id": result.publication_id,
                        "issues": [issue_json(i) for i in result.issues],
                    }
                )
                continue
            item = SignedInput(candidate, "zip" if kind == "zip" else "directory", result)
            if item.publication_id in seen:
                duplicates.append(
                    {
                        "path": str(candidate),
                        "publication_id": item.publication_id,
                        "same_as": str(seen[item.publication_id].path),
                    }
                )
                continue
            seen[item.publication_id] = item
            valid.append(item)
    return valid, invalid, duplicates


def invalid_exit(entries: list[dict[str, Any]]) -> Exit:
    statuses = {e["status"] for e in entries}
    if Outcome.UNSUPPORTED in statuses:
        return Exit.UNSUPPORTED
    if Outcome.INVALID in statuses:
        return Exit.INVALID
    return Exit.INCOMPLETE


def zip_manifest_and_signature(source: FileSource) -> tuple[bytes, bytes]:
    entries = parse_zip_layout(source)
    return (
        source.read_at(entries[0].data_offset, entries[0].size),
        source.read_at(entries[1].data_offset, entries[1].size),
    )


def signature_of(item: SignedInput) -> bytes:
    if item.kind == "directory":
        return (item.path / SIGNATURE_PATH).read_bytes()
    with FileSource(item.path) as source:
        return zip_manifest_and_signature(source)[1]


def read_member(item: SignedInput, path: str) -> bytes:
    """Return verified bytes of one bundle member (manifest, signature, or listed file)."""
    if item.kind == "directory":
        data = (item.path / path).read_bytes()
        if path == MANIFEST_PATH:
            if data != item.manifest.raw:
                raise CliError("input_changed", f"{item.path}: manifest changed", Exit.INCOMPLETE)
            return data
        if path != SIGNATURE_PATH:
            verify_file(item.manifest, path, data)
        return data
    with FileSource(item.path) as source:
        if path in (MANIFEST_PATH, SIGNATURE_PATH):
            manifest, signature = zip_manifest_and_signature(source)
            if manifest != item.manifest.raw:
                raise CliError("input_changed", f"{item.path}: manifest changed", Exit.INCOMPLETE)
            return manifest if path == MANIFEST_PATH else signature
        return read_zip_entry(source, path, expected_id=item.publication_id)[1]


def reader_paths(manifest: Manifest, display: list[str]) -> list[str]:
    """Reader files: manifest, signature, metadata, README, any license, and display files."""
    paths = [MANIFEST_PATH, SIGNATURE_PATH, METADATA_PATH, README_PATH]
    if LICENSE_PATH in manifest.files:
        paths.append(LICENSE_PATH)
    paths.extend(p for p in display if p in manifest.files and p not in paths)
    return paths


def reader_files(item: SignedInput) -> dict[str, bytes]:
    display = [d.path for d in item.result.metadata.display] if item.result.metadata else []
    return {path: read_member(item, path) for path in reader_paths(item.manifest, display)}


def write_zip(item: SignedInput, handle: BinaryIO) -> None:
    """Write the canonical ZIP bytes of a verified input to ``handle``."""
    if item.kind == "zip":
        copy_file_to(handle, item.path)
        return
    manifest_bytes = (item.path / MANIFEST_PATH).read_bytes()
    signature = (item.path / SIGNATURE_PATH).read_bytes()
    files = {p: PathSource(item.path / p) for p in item.manifest.files}
    write_bundle_zip(handle, manifest_bytes, signature, files)
