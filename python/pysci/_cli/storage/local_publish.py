"""Local publication: store verified ``<digest>.zip`` bundles and sidecars in folders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..clients.tsa import merge_proofs, read_sidecar
from ..config import Config
from ..errors import ErrorRecord, Exit
from .files import InputChanged, StoreOutcome, atomic_write_bytes, store_bytes, store_no_clobber
from .inputs import SignedInput, write_zip


def publish_to_folder(item: SignedInput, folder: Path) -> tuple[dict[str, Any], int]:
    """Store one verified bundle; return (report, exit code).

    Identical existing bundles are kept; conflicting bytes are rejected and
    left untouched. Timestamp sidecars are copied as ``<digest>.tsr`` and
    merged without duplicating proofs.
    """
    report: dict[str, Any] = {
        "publication_id": item.publication_id,
        "destination": str(folder),
        "kind": "folder",
    }
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{item.digest}.zip"
        stored = store_no_clobber(target, lambda handle: write_zip(item, handle), item.zip_sha256)
    except InputChanged as exc:
        report["status"] = "failed"
        report["error"] = ErrorRecord("input_changed", str(exc), True).to_json()
        return report, Exit.INCOMPLETE
    except OSError as exc:
        report["status"] = "failed"
        report["error"] = ErrorRecord("write_failed", f"{folder}: {exc}", True).to_json()
        return report, Exit.INCOMPLETE
    report["path"] = str(target)
    report["bundle"] = str(stored.outcome)
    if stored.outcome is StoreOutcome.CONFLICT:
        report["status"] = "failed"
        report["error"] = ErrorRecord(
            "destination_conflict",
            f"{target} exists with different bytes; it was left unchanged",
            file=str(target),
            observed=f"sha256 {stored.existing_sha256}",
            requirement=f"sha256 {item.zip_sha256}",
        ).to_json()
        return report, Exit.INVALID
    report["status"] = "stored"
    sidecar = item.sidecar
    if sidecar is not None:
        report["timestamp_sidecar"] = _copy_sidecar(sidecar, folder / f"{item.digest}.tsr")
    return report, Exit.OK


def _copy_sidecar(source: Path, target: Path) -> dict[str, Any]:
    if source.exists() and target.exists() and os.path.samefile(source, target):
        return {"path": str(target), "status": "unchanged"}
    data = read_sidecar(source) or b""
    existing = read_sidecar(target)
    if existing is None:
        store_bytes(target, data)
        return {"path": str(target), "status": "written"}
    if existing == data:
        return {"path": str(target), "status": "unchanged"}
    merged, problems = merge_proofs(existing, data)
    if problems:
        return {
            "path": str(target),
            "status": "preserved",
            "problems": [p.to_json() for p in problems],
        }
    if merged != existing:
        atomic_write_bytes(target, merged)
        return {"path": str(target), "status": "merged"}
    return {"path": str(target), "status": "unchanged"}


def remember_folder(config: Config, folder: Path) -> bool:
    """Add a folder's canonical absolute path to the known folders; True if added."""
    canonical = str(folder.resolve())
    if canonical in config.folders:
        return False
    config.folders.append(canonical)
    return True
