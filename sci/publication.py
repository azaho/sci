"""Byte-preserving publication verification. ZIPs are opaque streams here."""

import datetime as dt
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from cryptography.exceptions import InvalidSignature

from sci.errors import SCIError, invalid
from sci.keys import fingerprint, jwk_key, load_private, load_public, public_jwk, raw_public
from sci.validation import (
    JSON_LIMIT,
    LICENSE,
    PAYLOAD_LIMIT,
    compact,
    strict_json,
    validate_manifest,
    validate_metadata,
    validate_png,
    validate_readme,
)


@dataclass(frozen=True)
class Publication:
    manifest_bytes: bytes
    sci_bytes: bytes
    signature: bytes
    manifest: dict
    metadata: dict

    @property
    def uuid(self):
        return self.manifest["uuid"]

    @property
    def manifest_sha256(self):
        return hashlib.sha256(self.manifest_bytes).hexdigest()

    @property
    def fingerprint(self):
        return fingerprint(raw_public(jwk_key(self.manifest["public_key"])))

    @property
    def originals(self):
        return {
            "manifest.json": self.manifest_bytes, "sci.json": self.sci_bytes,
            "manifest.json.sig": self.signature, "LICENSE": LICENSE,
        }

    @property
    def descriptors(self):
        entries = dict(self.manifest["files"])
        for name in ("manifest.json", "manifest.json.sig"):
            data = self.originals[name]
            entries[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return entries


def check_bytes(data: bytes, descriptor: dict, name: str):
    if len(data) != descriptor["bytes"] or hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
        raise invalid(name, "bytes do not match the signed size and SHA-256")


def verify_documents(manifest_bytes: bytes, sci_bytes: bytes, signature: bytes) -> Publication:
    if len(manifest_bytes) + len(sci_bytes) > JSON_LIMIT:
        raise SCIError("sci.json + manifest.json exceed 65,536 bytes", "too_large", 413)
    manifest = strict_json(manifest_bytes, "manifest.json")
    if len(signature) != 64:
        raise SCIError("manifest.json.sig must contain exactly 64 raw bytes", "invalid_signature", 403)
    key = jwk_key(manifest.get("public_key"))
    try:
        key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise SCIError("Invalid manifest signature", "invalid_signature", 403) from exc
    validate_manifest(manifest)
    check_bytes(sci_bytes, manifest["files"]["sci.json"], "sci.json")
    metadata = strict_json(sci_bytes, "sci.json")
    validate_metadata(metadata)
    return Publication(manifest_bytes, sci_bytes, signature, manifest, metadata)


def verify_stream(stream: BinaryIO, entry: dict, name: str):
    digest = hashlib.sha256()
    total = 0
    buffered = bytearray() if name in {"README.md", "headline_figure.png", "LICENSE"} else None
    while block := stream.read(1024 * 1024):
        total += len(block)
        if total > entry["bytes"]:
            raise invalid(name, "actual bytes exceed signed size")
        digest.update(block)
        if buffered is not None:
            buffered.extend(block)
    if total != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
        raise invalid(name, "bytes do not match the signed size and SHA-256")
    if name == "README.md":
        validate_readme(bytes(buffered))
    elif name == "headline_figure.png":
        validate_png(bytes(buffered))
    elif name == "LICENSE" and bytes(buffered) != LICENSE:
        raise invalid("LICENSE", "does not match the bundled version 1 license")


def verify_files(publication: Publication, opener: Callable[[str], BinaryIO]):
    for name, entry in publication.manifest["files"].items():
        with opener(name) as stream:
            verify_stream(stream, entry, name)


def read_limited(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SCIError(f"Missing regular publication file: {path}")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise SCIError(f"{path.name} exceeds {maximum} bytes", "too_large", 413)
    return data


def verify_publication(folder: Path | str, public_key: Path | None = None) -> Publication:
    folder = Path(folder)
    manifest = read_limited(folder / "manifest.json", JSON_LIMIT)
    metadata = read_limited(folder / "sci.json", JSON_LIMIT - len(manifest))
    signature = read_limited(folder / "manifest.json.sig", 64)
    publication = verify_documents(manifest, metadata, signature)
    if public_key is not None:
        if raw_public(load_public(public_key)) != raw_public(jwk_key(publication.manifest["public_key"])):
            raise SCIError("Signer does not match the trusted public key", "untrusted_signer", 403)
    def opener(name):
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise SCIError(f"Missing regular publication file: {path}")
        return path.open("rb")
    verify_files(publication, opener)
    return publication


def _atomic_write(path: Path, data: bytes):
    fd, temporary = tempfile.mkstemp(prefix=".sci-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sign_publication(folder: Path | str, private_key: Path) -> Publication:
    # Packaging is intentionally imported only by the authoring command.
    from sci.preview import package, prepared_preview

    folder = Path(folder)
    metadata = read_limited(folder / "sci.json", JSON_LIMIT)
    validate_metadata(strict_json(metadata, "sci.json"))
    validate_readme(read_limited(folder / "README.md", 1_000_000))
    key = load_private(private_key)
    license_path = folder / "LICENSE"
    if license_path.exists() or license_path.is_symlink():
        if read_limited(license_path, len(LICENSE)) != LICENSE:
            raise SCIError("LICENSE differs from SCI's fixed terms. Move it aside; do not discard notices.")
    else:
        with license_path.open("xb") as out:
            out.write(LICENSE)
    figure = folder / "headline_figure.png"
    if figure.exists() or figure.is_symlink():
        validate_png(read_limited(figure, 2_000_000))
    manifest = {
        "schema_version": 1, "uuid": str(uuid.uuid4()),
        "public_key": public_jwk(key.public_key()),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "files": {},
    }
    archive = folder / "files.zip"
    if archive.is_symlink():
        raise SCIError("files.zip must not be a symlink")
    if (folder / "files").exists() or (folder / "files").is_symlink():
        manifest["files_tree"] = package(folder / "files", archive)
    elif archive.exists():
        if archive.stat().st_size > PAYLOAD_LIMIT:
            raise SCIError("files.zip exceeds 50,000,000 bytes")
        manifest["files_tree"] = prepared_preview(archive)
    for name in ("sci.json", "README.md", "LICENSE", "files.zip", "headline_figure.png"):
        path = folder / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise SCIError(f"Expected regular file: {path}")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest["files"][name] = {"sha256": digest, "bytes": path.stat().st_size}
    validate_manifest(manifest)
    while len(compact(manifest)) + len(metadata) > JSON_LIMIT:
        preview = manifest.get("files_tree")
        if not preview or not preview["paths"]:
            raise SCIError("Metadata exceeds 65,536 bytes; reduce author metadata or citations", "too_large", 413)
        preview["paths"].pop()
        preview["truncated"] = True
    manifest_bytes = compact(manifest)
    signature = key.sign(manifest_bytes)
    publication = verify_documents(manifest_bytes, metadata, signature)
    # Verify again before installing the generated identity. Inputs can change during packaging.
    verify_files(publication, lambda name: (folder / name).open("rb"))
    _atomic_write(folder / "manifest.json", manifest_bytes)
    _atomic_write(folder / "manifest.json.sig", signature)
    return publication
