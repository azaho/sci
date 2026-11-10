"""``sci pull``: save a complete verified publication."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pysci.bundle import VerificationResult, extract_verified, verify_directory
from pysci.der import frame_values
from pysci.errors import Outcome
from pysci.keys import PublicKey
from pysci.manifest import MANIFEST_PATH, SIGNATURE_PATH, TIMESTAMP_SIDECAR_PATH
from pysci.timestamps import ProofResult

from ..checks import baseline_check, bundle_check, checks, describe_checks, timestamps_check
from ..clients.archive import ArchiveSource, SourceFailure, failure_exit
from ..clients.index import IndexClient
from ..clients.tsa import merge_proofs, read_sidecar, verify_sidecar_bytes
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit, exit_for_outcome
from ..output import Result
from ..retrieval import resolve_sources
from ..spec import Command, arg
from ..storage.files import atomic_write_bytes, directory_is_empty
from ..storage.inputs import SignedInput, signature_of
from ..storage.keys import load_public_key_file
from ..storage.local_publish import publish_to_folder, remember_folder
from .common import parse_id, trust


def _existing(output: Path, digest: str, zip_mode: bool) -> VerificationResult | None:
    """A complete verified copy already at the destination, else None; conflicts raise."""
    if zip_mode:
        target = output / f"{digest}.zip"
        if not target.exists():
            return None
        from pysci.bundle import verify_zip

        result = verify_zip(target, expected_id=digest)
        if result.ok:
            return result
        raise CliError(
            "destination_conflict",
            f"{target} exists but is not a verified copy of sha256:{digest}",
            Exit.INVALID,
            file=str(target),
        )
    if not output.exists():
        return None
    if not output.is_dir():
        raise CliError("destination_conflict", f"{output} exists and is not a directory")
    if directory_is_empty(output):
        return None
    result = verify_directory(output, expected_id=digest)
    if result.ok:
        return result
    raise CliError(
        "destination_not_empty",
        f"{output} is not empty and is not a complete verified copy of sha256:{digest}",
        Exit.USAGE,
        file=str(output),
    )


def _retrieve(
    archive: ArchiveSource, ctx: Context, deadline: Any, workdir: Path
) -> tuple[str, Path, VerificationResult]:
    """Retrieve a complete bundle from one archive: complete ZIP, else individual files."""
    descriptor = archive.descriptor(deadline)
    if descriptor.bundle_url is not None:
        got = archive.verify_complete_zip(deadline)
        if got is not None:
            path, result = got
            if not result.ok:
                record = ErrorRecord(
                    result.issues[0].code if result.issues else "invalid",
                    result.issues[0].requirement if result.issues else "bundle failed checks",
                    result.outcome is Outcome.INCOMPLETE,
                    extra={"route": "bundle_url"},
                )
                raise SourceFailure(record, exit_for_outcome(result.outcome))
            return "zip", path, result
    manifest = archive.manifest(deadline)
    loose = Path(tempfile.mkdtemp(dir=workdir, prefix=".sci-tmp-loose-"))
    (loose / MANIFEST_PATH).write_bytes(manifest.raw)
    (loose / SIGNATURE_PATH).write_bytes(archive.signature(deadline))
    for member in manifest.sorted_paths():
        data = archive.file(member, deadline)
        target = loose / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    result = verify_directory(loose, expected_id=manifest.digest, compute_zip_sha256=True)
    if not result.ok:
        raise SourceFailure(
            ErrorRecord(
                "invalid", "assembled bundle failed complete checks", extra={"route": "entry_url"}
            ),
            exit_for_outcome(result.outcome),
        )
    return "directory", loose, result


def _proofs(
    ctx: Context,
    archives: list[ArchiveSource],
    index: IndexClient | None,
    digest: str,
    deadline: Any,
    signature: bytes,
    warnings: list[str],
) -> tuple[bytes | None, dict[str, Any]]:
    combined = b""
    sources: list[str] = []
    for archive in archives:
        data = archive.sidecar(deadline)
        if data:
            combined, problems = merge_proofs(combined, data)
            sources.append(archive.descriptor_url)
            warnings.extend(f"proofs from {archive.descriptor_url}: {p.message}" for p in problems)
    if index is not None:
        try:
            data = index.timestamps(digest, deadline)
        except CliError as exc:
            warnings.append(f"timestamp lookup failed: {exc}")
            data = None
        if data:
            combined, problems = merge_proofs(combined, data)
            sources.append(f"{index.origin} timestamp lookup")
            warnings.extend(f"index proofs: {p.message}" for p in problems)
    if not combined:
        return None, timestamps_check(
            None,
            source=None,
            online=False,
            absent_reason="no proofs were retrieved from the sources",
        )
    verification = verify_sidecar_bytes(combined, signature, trust(ctx))
    kept = b"".join(
        combined[p.offset : p.offset + p.length]
        for p in verification.proofs
        if p.result is not ProofResult.INVALID
    )
    report = timestamps_check(verification, source=", ".join(sources), online=False)
    report["kept_proofs"] = len(frame_values(kept).values) if kept else 0
    return kept or None, report


def run(args: argparse.Namespace, ctx: Context) -> Result:
    digest = parse_id(args.id)
    expected_key: PublicKey | None = None
    if args.public_key:
        expected_key = load_public_key_file(ctx.resolve(args.public_key))
    zip_mode = bool(args.zip)
    if zip_mode:
        output = ctx.resolve(args.output or ".")
    else:
        output = ctx.resolve(args.output) if args.output else ctx.cwd / digest
    result = Result("pull")
    result.data = {"publication_id": "sha256:" + digest, "output": str(output)}
    existing = _existing(output, digest, zip_mode)
    if existing is not None and existing.manifest is not None:
        if expected_key is not None and existing.manifest.public_key != expected_key:
            raise CliError(
                "public_key_mismatch", "signer does not match --public-key", Exit.INVALID
            )
        result.data.update(
            {
                "status": "already_present",
                "checks": checks(bundle=bundle_check(existing), baseline=baseline_check(existing)),
            }
        )
        result.say(f"sha256:{digest} is already present and verified at {output}")
        return result

    deadline = ctx.deadline(args.timeout)
    sources = resolve_sources(
        ctx, digest, archives=args.archive, index_url=args.in_, deadline=deadline
    )
    parent = output if zip_mode else output.parent
    parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=parent, prefix=".sci-tmp-pull-"))
    archives: list[ArchiveSource] = []
    try:
        failures: list[SourceFailure] = []
        retrieved: tuple[str, Path, VerificationResult] | None = None
        used: ArchiveSource | None = None
        for url in sources.descriptors:
            archive = ArchiveSource(ctx.http, url, digest, workdir)
            archives.append(archive)
            try:
                retrieved = _retrieve(archive, ctx, deadline, workdir)
                used = archive
                break
            except SourceFailure as exc:
                exc.record.extra["descriptor"] = url
                failures.append(exc)
            except CliError as exc:
                if exc.code == "deadline_exceeded":
                    raise
                failures.append(SourceFailure(exc.records[0], exc.exit_code))
        if retrieved is None or used is None:
            code = failure_exit(failures)
            error = CliError(
                "retrieval_failed",
                f"no source provided a complete verified copy of sha256:{digest}",
                code,
                retryable=code == Exit.INCOMPLETE,
            )
            error.records.extend(f.record for f in failures)
            raise error
        kind, path, verification = retrieved
        assert verification.manifest is not None
        if expected_key is not None and verification.manifest.public_key != expected_key:
            raise CliError(
                "public_key_mismatch",
                "signer does not match --public-key",
                Exit.INVALID,
                field="/public_key/x",
                observed=verification.manifest.public_key.x,
                requirement=f"x = {expected_key.x}",
            )
        item = SignedInput(path, "zip" if kind == "zip" else "directory", verification)
        proofs, ts_report = _proofs(
            ctx, archives, sources.index, digest, deadline, signature_of(item), result.warnings
        )
        if zip_mode:
            stored, store_code = publish_to_folder(item, output)
            result.data["stored"] = stored
            if store_code != Exit.OK:
                error = stored["error"]
                raise CliError(error["code"], error["message"], store_code)
            if remember_folder(ctx.config, output):
                ctx.save_config()
            if proofs:
                tsr = output / f"{digest}.tsr"
                merged, _ = merge_proofs(read_sidecar(tsr) or b"", proofs)
                atomic_write_bytes(tsr, merged)
                ts_report["written"] = str(tsr)
            result.say(f"saved {stored['path']} ({stored['bundle']})")
        else:
            staging = workdir / "final"
            if kind == "zip":
                extracted = extract_verified(path, staging, expected_id=digest)
                if not extracted.ok:
                    raise CliError(
                        "extraction_failed", "bundle changed during extraction", Exit.INCOMPLETE
                    )
            else:
                os.rename(path, staging)
            if proofs:
                atomic_write_bytes(staging / TIMESTAMP_SIDECAR_PATH, proofs)
                ts_report["written"] = str(output / TIMESTAMP_SIDECAR_PATH)
            if output.exists():
                output.rmdir()  # empty, checked above
            os.rename(staging, output)
            result.say(f"saved sha256:{digest} to {output}")
        result.data.update(
            {
                "status": "retrieved",
                "source": {"descriptor": used.descriptor_url, "routes": used.routes},
                "source_failures": [f.record.to_json() for f in failures],
                "signer": verification.manifest.public_key.to_jwk(),
                "checks": checks(
                    bundle=bundle_check(verification),
                    baseline=baseline_check(verification),
                    timestamps=ts_report,
                ),
            }
        )
        result.lines.extend(describe_checks(result.data["checks"]))
        if failures:
            result.warn(f"{len(failures)} source(s) failed before {used.descriptor_url} succeeded")
        return result
    finally:
        for archive in archives:
            archive.close()
        with contextlib.suppress(OSError):
            shutil.rmtree(workdir)


COMMAND = Command(
    name="pull",
    usage_hint=(
        "pull ID [--output DIR] [--zip] [--in URL | --archive ARCHIVE ...] "
        "[--public-key PATH] [--json]"
    ),
    summary="Save a complete verified publication.",
    description="""
Finds sources through the configured index or --in URL (SCI protocol
§8.1.1), or uses repeated --archive descriptor URLs or GitHub/Hugging Face
repository addresses without an index. Each
source is tried in turn against the same expected ID. The complete ZIP
(bundle_url) is preferred; otherwise files are fetched individually
(entry_url, blob_url). Downloads are verified completely (§6.4) in a
temporary location before extraction. No catalog admission is required.

The default output is ./<digest>/. An existing complete verified copy is
kept; other nonempty destinations are rejected. --zip stores <digest>.zip in
--output DIR (default: current folder) under the local publishing rules.
Timestamp proofs are retrieved from archive entry_url and the index's
timestamp lookup and reported separately; proof retrieval failure does not
fail the download.
""",
    args=(
        arg("id", metavar="ID", help="publication ID: sha256:<digest> or sci:sha256:<digest>"),
        arg("--output", metavar="DIR", help="destination directory (default: ./<digest>/)"),
        arg(
            "--zip", action="store_true", help="store <digest>.zip in --output DIR instead of files"
        ),
        arg("--in", dest="in_", metavar="URL", help="index origin (default: configured index)"),
        arg(
            "--archive",
            metavar="ARCHIVE",
            action="append",
            help="descriptor URL or GitHub/Hugging Face repository address (repeatable)",
        ),
        arg("--public-key", metavar="PATH", help="require the signer to match this public PEM key"),
    ),
    outputs="Destination, source used, source failures, and the checks object.",
    files="""Creates the destination directory (or <digest>.zip and <digest>.tsr with --zip);
temporary files use .sci-tmp- names and are removed.""",
    network="Index lookup (unless --archive), archive descriptor and file or bundle GETs, "
    "and timestamp lookups.",
    exit_codes={
        3: "every source served content failing verification, or the signer does not match",
        4: "unsupported descriptor or bundle schema",
        5: "no known sources (index 404 or empty list), sources unreachable, or deadline",
    },
    examples=("sci pull sha256:<digest> --in https://sciscape.io --output ./paper",),
    run=run,
    timeout=True,
)
