"""``sci verify``: verify a signed directory or ZIP."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pysci.bundle import VerificationResult
from pysci.manifest import SIGNATURE_PATH
from pysci.source import FileSource

from ..checks import (
    baseline_check,
    bundle_check,
    checks,
    describe_checks,
    issue_lines,
    timestamps_check,
)
from ..clients.tsa import read_sidecar, verify_sidecar_bytes
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit, exit_for_outcome, issue_record
from ..output import Result
from ..spec import Command, arg
from ..storage.inputs import find_sidecar, verify_input, zip_manifest_and_signature
from ..storage.keys import load_public_key_file
from .common import trust_with_warnings


def signature_bytes(path: Path, kind: str) -> bytes:
    if kind == "directory":
        return (path / SIGNATURE_PATH).read_bytes()
    with FileSource(path) as source:
        return zip_manifest_and_signature(source)[1]


def check_timestamps(
    ctx: Context,
    path: Path,
    kind: str,
    result: VerificationResult,
    *,
    online: bool,
    timeout: float | None,
    warnings: list[str],
) -> dict[str, Any]:
    if result.manifest is None:
        return {
            "status": "not_performed",
            "reason": "timestamp checks require completed manifest checks (§6.8)",
        }
    sidecar = find_sidecar(path, kind, result.manifest.digest)
    if sidecar is None:
        return timestamps_check(None, source=None, online=online)
    data = read_sidecar(sidecar) or b""
    deadline = ctx.deadline(timeout) if online else None
    trust_config, trust_warnings = trust_with_warnings(ctx, online=online, deadline=deadline)
    warnings.extend(trust_warnings)
    verification = verify_sidecar_bytes(data, signature_bytes(path, kind), trust_config)
    return timestamps_check(verification, source=str(sidecar), online=online)


def run(args: argparse.Namespace, ctx: Context) -> Result:
    path = ctx.resolve(args.path)
    if not path.exists():
        raise CliError("input_missing", f"{path} does not exist", Exit.USAGE, file=str(path))
    expected = None
    if args.public_key:
        expected = load_public_key_file(ctx.resolve(args.public_key))
    kind, verification = verify_input(path)
    result = Result("verify")
    bundle = bundle_check(verification)
    exit_code = exit_for_outcome(verification.outcome)
    key_check: dict[str, Any] | None = None
    if expected is not None:
        if verification.manifest is None:
            key_check = {"status": "not_performed"}
        else:
            match = verification.manifest.public_key == expected
            key_check = {"status": "match" if match else "mismatch", "expected": expected.x}
            if not match:
                exit_code = Exit.INVALID
                result.errors.append(
                    ErrorRecord(
                        "public_key_mismatch",
                        "signer does not match --public-key",
                        file="manifest.json",
                        field="/public_key/x",
                        requirement=f"x = {expected.x}",
                        observed=verification.manifest.public_key.x,
                    )
                )
    timestamps = check_timestamps(
        ctx,
        path,
        kind,
        verification,
        online=args.check_timestamp_online,
        timeout=args.timeout,
        warnings=result.warnings,
    )
    report = checks(bundle=bundle, baseline=baseline_check(verification), timestamps=timestamps)
    result.data = {
        "path": str(path),
        "kind": kind,
        "publication_id": verification.publication_id,
        "signer": verification.manifest.public_key.to_jwk() if verification.manifest else None,
        "public_key_check": key_check,
        "checks": report,
    }
    result.errors.extend(issue_record(i) for i in verification.issues)
    result.exit_code = exit_code
    if verification.publication_id:
        result.say(f"publication: {verification.publication_id}")
    if verification.manifest is not None:
        result.say(f"signer x:    {verification.manifest.public_key.x}")
    if key_check is not None:
        result.say(f"public key:  {key_check['status']}")
    result.say("checks:")
    result.lines.extend(describe_checks(report))
    if bundle["issues"]:
        result.lines.extend(issue_lines(bundle["issues"], "    "))
    result.say("Offline verification does not establish current account associations or the")
    result.say("absence of key revocation.")
    return result


COMMAND = Command(
    name="verify",
    usage_hint="verify PATH [--public-key PATH] [--check-timestamp-online] [--json]",
    summary="Verify a signed directory or ZIP.",
    description="""
Runs the complete bundle checks of SCI protocol §6.4 (ZIP layout, manifest,
signature, metadata, every file hash, and inventory). Baseline eligibility
is reported separately; valid non-baseline bundles pass. Timestamp proofs in
manifest.json.sig.tsr (directories) or <digest>.tsr (beside a ZIP) are
checked under §6.8 and reported separately; a failed proof never
invalidates the publication. Associations and revocation are reported as
not performed.
""",
    args=(
        arg("path", metavar="PATH", help="signed directory or ZIP (any file name)"),
        arg("--public-key", metavar="PATH", help="require the signer to match this public PEM key"),
        arg(
            "--check-timestamp-online",
            action="store_true",
            help="fetch current CRLs for timestamp trust checks (otherwise offline)",
        ),
    ),
    outputs="""Publication ID, signer, and the checks object: bundle, baseline, timestamps,
associations, revocation (each with status; skipped checks are not_performed).""",
    files="None.",
    network="None, unless --check-timestamp-online fetches CRLs (FreeTSA and configured URLs).",
    exit_codes={
        3: "a protocol, signature, or content check failed, or --public-key does not match",
        4: "an unsupported schema",
        5: "a check could not be completed (unreadable input or local limit)",
    },
    examples=("sci verify ./campaign/<digest>.zip --public-key ~/.sci/key.pub.pem",),
    run=run,
    timeout=True,
)
