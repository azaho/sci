"""Manifest, file and metadata scopes; never imply complete bundle verification."""

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from . import validation as v
from .bundle import Limits, read_regular


@dataclass(frozen=True)
class Reading:
    id: str
    manifest: dict[str, Any]
    entries: dict[str, bytes]
    metadata: dict[str, Any] | None
    scope: str


def read_verified(
    get: Callable[[str, int], bytes],
    *,
    expected_id: str | None = None,
    file: str | None = None,
    listing: bool = False,
    limits: Limits = Limits(),
) -> Reading:
    budget = limits.bundle_bytes

    def bounded(path: str, limit: int) -> bytes:
        nonlocal budget
        if limit > budget:
            raise v.Incomplete("read work limit")
        raw = get(path, limit)
        if len(raw) > min(limit, budget):
            raise v.Incomplete("read work limit")
        budget -= len(raw)
        return raw

    raw = bounded("manifest.json", min(limits.json_bytes, budget))
    sig = bounded("manifest.json.sig", 64)
    manifest = v.check_manifest(raw, sig)
    pid = v.sha(raw)
    if expected_id is not None and pid != expected_id:
        raise v.Invalid("publication ID mismatch")
    entries = {"manifest.json": raw, "manifest.json.sig": sig}
    metadata = None
    if listing:
        return Reading(pid, manifest, entries, None, "manifest")
    paths = (
        [file]
        if file is not None
        else ["sci.json", "README.md", *(["LICENSE"] if "LICENSE" in manifest["files"] else [])]
    )
    for path in paths:
        if path not in manifest["files"]:
            raise v.Invalid("requested path is not listed in manifest")
        entry = manifest["files"][path]
        data = bounded(path, entry["bytes"])
        if len(data) != entry["bytes"] or v.sha(data) != entry["sha256"]:
            raise v.Invalid("file check " + path)
        entries[path] = data
    if file is None:
        metadata = v.check_sci(entries["sci.json"], manifest["files"])
        v.text_file(entries["README.md"], nonblank=True)
        if "LICENSE" in entries:
            v.text_file(entries["LICENSE"])
    return Reading(pid, manifest, entries, metadata, "file" if file is not None else "metadata")


def read_local(
    path: Path, *, file: str | None = None, listing: bool = False, limits: Limits = Limits()
) -> Reading:
    if path.is_dir():
        if path.is_symlink():
            raise v.Invalid("symlink input directory")

        def get(name: str, limit: int) -> bytes:
            target = path / name
            if any(p.is_symlink() for p in target.parents if p != path.parent):
                raise v.Invalid("symlink input parent")
            return read_regular(target, limit)
    else:
        raw = read_regular(path, limits.bundle_bytes)
        mb, sig, _, payload = v.check_zip(raw)
        entries = {"manifest.json": mb, "manifest.json.sig": sig, **payload}

        def get(name: str, limit: int) -> bytes:
            return entries[name]

    return read_verified(get, file=file, listing=listing, limits=limits)
