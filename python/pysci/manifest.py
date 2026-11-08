"""The signed manifest (protocol §3) and manifest checks (§6.1)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import ed25519
from .errors import Invalid, Issue, IssueCollector
from .ids import parse_publication_id
from .jsonstrict import MAX_SAFE_INTEGER, JsonInt, JsonObject, json_pointer, json_type_name
from .jsonstrict import parse_json_object as _parse_json_object
from .keys import PublicKey, parse_public_jwk, public_key_of
from .paths import check_paths, path_sort_key
from .schemas import PUBLICATION_SCHEMA, check_schema

__all__ = [
    "MANIFEST_PATH",
    "REQUIRED_FILES",
    "SIGNATURE_PATH",
    "TIMESTAMP_SIDECAR_PATH",
    "FileEntry",
    "Manifest",
    "build_manifest",
    "parse_manifest",
    "serialize_manifest",
    "sign_manifest",
    "verify_manifest",
]

MANIFEST_PATH: Final = "manifest.json"
SIGNATURE_PATH: Final = "manifest.json.sig"
TIMESTAMP_SIDECAR_PATH: Final = "manifest.json.sig.tsr"
METADATA_PATH: Final = "sci.json"
README_PATH: Final = "README.md"
LICENSE_PATH: Final = "LICENSE"
REQUIRED_FILES: Final = (METADATA_PATH, README_PATH)
_ROOT_FILES: Final = frozenset({METADATA_PATH, README_PATH, LICENSE_PATH})
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_BYTES_LEXEME: Final = re.compile(r"0|[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One manifest ``files`` member."""

    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """A parsed manifest. ``raw`` is the exact signed byte string."""

    raw: bytes
    public_key: PublicKey
    files: Mapping[str, FileEntry]

    @property
    def digest(self) -> str:
        """Bare SHA-256 hex digest of the manifest bytes."""
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def publication_id(self) -> str:
        """``sha256:<digest>`` identifier (§2)."""
        return "sha256:" + self.digest

    def sorted_paths(self) -> list[str]:
        """Listed paths in ZIP order (ascending ASCII bytes)."""
        return sorted(self.files, key=path_sort_key)


def parse_manifest(data: bytes, *, file: str = MANIFEST_PATH) -> Manifest:
    """Check JSON encoding, fields, types, values, and paths (§3, §3.1, §7).

    The signature is not checked; use :func:`verify_manifest`.

    Raises:
        Invalid: one or more violations (all independent field issues are reported).
        Unsupported: the schema string is unknown.
    """
    record = _parse_json_object(data, file=file)
    check_schema(record, PUBLICATION_SCHEMA, file=file)
    issues = IssueCollector()
    _check_members(record, ("schema", "public_key", "files"), file, issues)
    public_key = None
    if "public_key" in record:
        public_key = parse_public_jwk(record["public_key"], file=file, collector=issues)
    files = _parse_files(record.get("files"), "files" in record, file, issues)
    issues.raise_if_any()
    assert public_key is not None
    assert files is not None
    return Manifest(raw=bytes(data), public_key=public_key, files=files)


def _check_members(
    record: JsonObject, required: tuple[str, ...], file: str, issues: IssueCollector
) -> None:
    missing = [name for name in required if name not in record]
    extra = sorted(set(record) - set(required))
    if missing:
        issues.add(
            "manifest_fields",
            "manifest requires schema, public_key, and files",
            section="3",
            file=file,
            observed=missing,
        )
    if extra:
        issues.add(
            "manifest_fields",
            "manifest permits only schema, public_key, and files",
            section="3",
            file=file,
            observed=extra,
        )


def _parse_files(
    value: object, present: bool, file: str, issues: IssueCollector
) -> dict[str, FileEntry] | None:
    if not present:
        return None
    if not isinstance(value, dict):
        issues.add(
            "manifest_files_type",
            "files must be a JSON object",
            section="3",
            file=file,
            field="/files",
            observed=json_type_name(value),
        )
        return None
    entries: dict[str, FileEntry] = {}
    start = len(issues)
    for path, entry in value.items():
        where = json_pointer("files", path)
        if not isinstance(entry, dict):
            issues.add(
                "manifest_entry_type",
                "each files member must be an object",
                section="3",
                file=file,
                field=where,
                observed=json_type_name(entry),
            )
            continue
        if set(entry) != {"sha256", "bytes"}:
            issues.add(
                "manifest_entry_fields",
                "each entry has exactly sha256 and bytes",
                section="3",
                file=file,
                field=where,
                observed=sorted(entry),
            )
        digest = entry.get("sha256")
        if not (isinstance(digest, str) and _SHA256_HEX.fullmatch(digest)):
            issues.add(
                "manifest_sha256",
                "sha256 must be 64 lowercase hexadecimal characters",
                section="3",
                file=file,
                field=where + "/sha256",
                observed=digest,
            )
        size = entry.get("bytes")
        if not (
            type(size) is JsonInt
            and _BYTES_LEXEME.fullmatch(size.lexeme)
            and 0 <= size <= MAX_SAFE_INTEGER
        ):
            observed = size.lexeme if isinstance(size, JsonInt) else size
            issues.add(
                "manifest_bytes",
                "bytes must be a digits-only integer from 0 through 9007199254740991",
                section="3",
                file=file,
                field=where + "/bytes",
                observed=getattr(size, "lexeme", observed),
            )
        if len(issues) == start:
            assert isinstance(digest, str)
            assert isinstance(size, int)
            entries[path] = FileEntry(sha256=digest, bytes=int(size))
    for required in REQUIRED_FILES:
        if required not in value:
            issues.add(
                "manifest_required_file",
                f"files must list {required}",
                section="3",
                file=file,
                field="/files",
                observed=required,
            )
    for path in value:
        if path not in _ROOT_FILES and not path.startswith("files/"):
            issues.add(
                "manifest_path_location",
                "entries other than sci.json, README.md, and LICENSE must be under files/",
                section="3",
                file=file,
                field=json_pointer("files", path),
                observed=path,
            )
    check_paths(value.keys(), collector=issues, file=file, field_prefix="/files")
    return entries if len(issues) == start else None


def verify_manifest(
    manifest_bytes: bytes,
    signature: bytes,
    *,
    expected_id: str | None = None,
    file: str = MANIFEST_PATH,
) -> Manifest:
    """Complete the §6.1 manifest checks.

    Checks the JSON encoding, fields, and paths; computes the publication
    identifier and compares it with ``expected_id`` (any accepted ID spelling);
    then checks the public key and signature over the original bytes.

    Raises:
        Invalid, Unsupported: see :func:`parse_manifest`; also a mismatched
            identifier or signature failure.
    """
    manifest = parse_manifest(manifest_bytes, file=file)
    if expected_id is not None:
        expected = parse_publication_id(expected_id, allow_bare=True)
        if manifest.digest != expected:
            raise Invalid(
                Issue(
                    "publication_id_mismatch",
                    "manifest hash must equal the requested publication identifier",
                    section="6.1",
                    file=file,
                    observed=manifest.publication_id,
                )
            )
    ed25519.verify(manifest.public_key.raw, signature, manifest.raw, file=SIGNATURE_PATH)
    return manifest


def build_manifest(public_key: PublicKey, files: Mapping[str, FileEntry]) -> bytes:
    """Serialize a manifest with the stable SCI serializer.

    Members appear as ``schema``, ``public_key``, ``files``; file paths are in
    ascending ASCII byte order; output is compact UTF-8 JSON with no
    insignificant whitespace. Paths are validated before serialization.

    Raises:
        Invalid: a path, hash, or size would make the manifest invalid.
    """
    issues = IssueCollector()
    check_paths(files.keys(), collector=issues, field_prefix="/files")
    for required in REQUIRED_FILES:
        if required not in files:
            issues.add("manifest_required_file", f"files must list {required}", section="3")
    for path, entry in files.items():
        if path not in _ROOT_FILES and not path.startswith("files/"):
            issues.add(
                "manifest_path_location",
                "entries other than sci.json, README.md, and LICENSE must be under files/",
                section="3",
                observed=path,
            )
        if not _SHA256_HEX.fullmatch(entry.sha256) or not 0 <= entry.bytes <= MAX_SAFE_INTEGER:
            issues.add("manifest_entry", "invalid file hash or size", section="3", observed=path)
    issues.raise_if_any()
    return serialize_manifest(public_key, files)


def serialize_manifest(public_key: PublicKey, files: Mapping[str, FileEntry]) -> bytes:
    """The stable serializer used by :func:`build_manifest`, without validation.

    Useful for planning sizes of drafts that still have problems.
    Non-ASCII characters are written as JSON escapes.
    """
    document = {
        "schema": PUBLICATION_SCHEMA,
        "public_key": public_key.to_jwk(),
        "files": {
            path: {"sha256": files[path].sha256, "bytes": files[path].bytes}
            for path in sorted(files, key=path_sort_key)
        },
    }
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def sign_manifest(manifest_bytes: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """Sign exact manifest bytes after checking the embedded key matches."""
    manifest = parse_manifest(manifest_bytes)
    if manifest.public_key != public_key_of(private_key):
        raise ValueError("private key does not match the manifest public_key")
    return ed25519.sign(private_key, manifest_bytes)
