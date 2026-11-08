"""Bounded, offline publication operations. Nothing is extracted during verification."""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import validation as v


@dataclass(frozen=True)
class Limits:
    bundle_bytes: int = 128 * 1024 * 1024
    files: int = 4096
    json_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class Bundle:
    id: str
    manifest: dict[str, Any]
    metadata: dict[str, Any]
    entries: dict[str, bytes]
    baseline: bool

    def zip(self) -> bytes:
        return v.build_zip(
            [
                (p, self.entries[p])
                for p in ["manifest.json", "manifest.json.sig", *sorted(self.manifest["files"])]
            ]
        )

    @property
    def signer(self) -> str:
        return str(self.manifest["public_key"]["x"])


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def public_jwk(key: Ed25519PrivateKey) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).rstrip(b"=").decode(),
    }


def read_regular(path: Path, limit: int) -> bytes:
    """O_NOFOLLOW plus fstat binds checks to the opened file, including changes during read."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise v.Invalid(f"not a regular file: {path.name}")
        if before.st_size > limit:
            raise v.Incomplete(f"file exceeds operation budget: {path.name}")
        raw = f.read(limit + 1)
        after = os.fstat(f.fileno())
    if len(raw) > limit:
        raise v.Incomplete("read budget exceeded")
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise v.Incomplete("input changed while reading")
    return raw


def read_directory(root: Path, limits: Limits = Limits(), draft: bool = False) -> dict[str, bytes]:
    if root.is_symlink():
        raise v.Invalid("symlink input directory")
    data: dict[str, bytes] = {}
    budget = limits.bundle_bytes
    for parent, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(parent) / name).is_symlink():
                raise v.Invalid("symlink directory")
        for name in names:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json.sig.tsr" or (
                draft and relative in ("manifest.json", "manifest.json.sig")
            ):
                continue
            if len(data) >= limits.files:
                raise v.Incomplete("file count budget exceeded")
            data[relative] = read_regular(path, budget)
            budget -= len(data[relative])
    return data


def verify(
    entries: dict[str, bytes], expected_id: str | None = None, limits: Limits = Limits()
) -> Bundle:
    if sum(map(len, entries.values())) > limits.bundle_bytes or len(entries) > limits.files:
        raise v.Incomplete("bundle work limit")
    try:
        raw, sig = entries["manifest.json"], entries["manifest.json.sig"]
        m = v.check_manifest(raw, sig)
        pid = v.sha(raw)
        if (
            expected_id is not None
            and expected_id.removeprefix("sci:").removeprefix("sha256:") != pid
        ):
            raise v.Invalid("expected publication ID mismatch")
        payload = {
            p: b
            for p, b in entries.items()
            if p not in ("manifest.json", "manifest.json.sig", "manifest.json.sig.tsr")
        }
        if payload.keys() != m["files"].keys():
            raise v.Invalid("file inventory differs from signed manifest")
        metadata = v.complete_checks(raw, sig, m, payload)
        return Bundle(
            pid,
            m,
            metadata,
            {"manifest.json": raw, "manifest.json.sig": sig, **payload},
            v.is_baseline(raw, m, metadata, payload),
        )
    except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise v.Invalid(f"malformed bundle: {exc}") from exc


def load_bundle(path: Path, expected_id: str | None = None, limits: Limits = Limits()) -> Bundle:
    if path.is_dir():
        return verify(read_directory(path, limits), expected_id, limits)
    raw = read_regular(path, limits.bundle_bytes)
    try:
        mb, sig, _, data = v.check_zip(raw)
    except (ValueError, TypeError, KeyError) as exc:
        raise v.Invalid(str(exc)) from exc
    return verify({"manifest.json": mb, "manifest.json.sig": sig, **data}, expected_id, limits)


def prepare(entries: dict[str, bytes], key: Ed25519PrivateKey) -> Bundle:
    """Prepare entirely in memory; the caller owns atomic persistence."""
    for p, raw in entries.items():
        if b"PRIVATE KEY-----" in raw or b'"d"' in raw[:65536] and b'"OKP"' in raw[:65536]:
            raise v.Invalid(f"recognized private-key material in {p}")
    manifest = {
        "schema": "sci-publication/0.1",
        "public_key": public_jwk(key),
        "files": {p: {"sha256": v.sha(b), "bytes": len(b)} for p, b in sorted(entries.items())},
    }
    raw = canonical_json(manifest)
    return verify({"manifest.json": raw, "manifest.json.sig": key.sign(raw), **entries})
