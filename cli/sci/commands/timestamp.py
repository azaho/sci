"""``sci timestamp``: obtain or submit timestamp proofs."""

from __future__ import annotations

import argparse
from pathlib import Path

from pysci.bundle import Scope, verify_directory, verify_zip
from pysci.manifest import TIMESTAMP_SIDECAR_PATH
from pysci.records import verify_revocation

from ..checks import proof_json
from ..clients.tsa import read_sidecar, verify_sidecar_bytes
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit, exit_for_outcome, issue_record
from ..output import Result
from ..spec import Command, arg
from ..storage.inputs import find_sidecar, sidecar_target
from .common import index_client, trust
from .revoke import RECORD, SIDECAR, SIGNATURE, submit_revocation
from .sign import timestamp_step, tsa_url
from .verify import signature_bytes


def _target(path: Path) -> tuple[str, bytes, Path, str | None, Result | None]:
    """(kind, signature, sidecar path, publication digest, failure result)."""
    if path.is_dir() and (path / RECORD).exists():
        record = (path / RECORD).read_bytes()
        signature = (path / SIGNATURE).read_bytes() if (path / SIGNATURE).exists() else b""
        check = verify_revocation(record, signature)
        if not check.ok:
            failed = Result("timestamp", exit_code=exit_for_outcome(check.outcome))
            failed.errors.extend(issue_record(i) for i in check.issues)
            failed.say("the revocation record failed verification (§6.7); no proof requested")
            return "revocation", b"", path / SIDECAR, None, failed
        return "revocation", signature, path / SIDECAR, None, None
    if path.is_dir():
        kind = "directory"
        verification = verify_directory(path, scope=Scope.MANIFEST)
    else:
        kind = "zip"
        verification = verify_zip(path, scope=Scope.MANIFEST)
    if not verification.ok or verification.manifest is None:
        failed = Result("timestamp", exit_code=exit_for_outcome(verification.outcome))
        failed.errors.extend(issue_record(i) for i in verification.issues)
        failed.say("manifest checks failed (§6.1); no proof requested")
        return kind, b"", path, None, failed
    digest = verification.manifest.digest
    return kind, signature_bytes(path, kind), sidecar_target(path, kind, digest), digest, None


def submit(args: argparse.Namespace, ctx: Context, path: Path) -> Result:
    if path.is_dir() and (path / RECORD).exists():
        result = submit_revocation(ctx, path, args.to, args.timeout)
        result.command = "timestamp"
        return result
    kind, signature, _, digest, failed = _target(path)
    if failed is not None:
        return failed
    assert digest is not None
    sidecar = find_sidecar(path, kind, digest)
    data = read_sidecar(sidecar) if sidecar else None
    if not data:
        raise CliError(
            "no_proofs",
            f"no timestamp sidecar found for {path}; obtain one with `sci timestamp {path}`",
            Exit.USAGE,
        )
    local = verify_sidecar_bytes(data, signature, trust(ctx))
    client = index_client(ctx, args.to)
    _, outcome = client.submit_multipart(
        "timestamp_submit_url",
        "timestamp submission",
        [
            ("id", None, digest.encode("ascii"), "text/plain"),
            (TIMESTAMP_SIDECAR_PATH, TIMESTAMP_SIDECAR_PATH, data, "application/octet-stream"),
        ],
        ctx.deadline(args.timeout),
    )
    if outcome["exit_code"] == Exit.OK:
        body = outcome["response"]
        count = body.get("retained_count")
        if body.get("id") != digest or not isinstance(count, int) or isinstance(count, bool):
            outcome = {
                "status": "failed",
                "exit_code": Exit.INVALID,
                "error": ErrorRecord(
                    "invalid_response",
                    "timestamp submission response needs the submitted id and an integer "
                    "retained_count",
                ).to_json(),
                "response": body,
            }
    result = Result("timestamp", exit_code=outcome["exit_code"])
    result.data = {
        "mode": "submit",
        "publication_id": "sha256:" + digest,
        "index": client.origin,
        "sidecar": str(sidecar),
        "local_checks": {
            "status": str(local.outcome),
            "proofs": [proof_json(p) for p in local.proofs],
        },
        **{k: v for k, v in outcome.items() if k != "exit_code"},
    }
    if "error" in outcome:
        error = outcome["error"]
        result.errors.append(ErrorRecord(error["code"], error["message"], error["retryable"]))
        result.say(f"timestamp submission failed: {error['message']}")
    else:
        body = outcome["response"]
        result.say(
            f"submitted {local.proofs.__len__()} proof(s) for sha256:{digest} to {client.origin}; "
            f"retained: {body['retained_count']}"
        )
    return result


def run(args: argparse.Namespace, ctx: Context) -> Result:
    if args.submit and args.tsa:
        raise CliError("conflicting_options", "--submit cannot be combined with --tsa")
    if args.to and not args.submit:
        raise CliError("invalid_arguments", "--to applies only to --submit")
    path = ctx.resolve(args.path)
    if not path.exists():
        raise CliError("input_missing", f"{path} does not exist", Exit.USAGE, file=str(path))
    if args.submit:
        return submit(args, ctx, path)
    kind, signature, sidecar, digest, failed = _target(path)
    if failed is not None:
        return failed
    url = tsa_url(ctx, args.tsa)
    result = Result("timestamp")
    result.data = {
        "mode": "create",
        "target": kind,
        "path": str(path),
        "publication_id": None if digest is None else "sha256:" + digest,
    }
    ok = timestamp_step(ctx, result, sidecar, signature, url, args.timeout)
    ts = result.data["timestamp"]
    if not ok:
        result.exit_code = Exit.INCOMPLETE if ts["error"]["code"] != "sidecar_malformed" else 3
        result.say(f"timestamp failed: {ts['error']['message']}")
        return result
    result.say(f"proof {ts['status']}: {ts['sidecar']}")
    proof = ts["proof"]
    result.say(f"genTime: {proof.get('gen_time')}  TSA: {proof.get('tsa') or url}")
    result.say(
        f"trust: {proof['result']}"
        + (f" ({proof['issues'][0]['message']})" if proof["issues"] else "")
    )
    return result


COMMAND = Command(
    name="timestamp",
    usage_hint="timestamp PATH [--tsa URL | --submit [--to URL]] [--json]",
    summary="Obtain or submit timestamp proofs.",
    description="""
Proof creation: PATH is a signed publication directory, a ZIP, or a
revocation directory. The target signature is verified first (§6.1 or §6.7),
then an RFC 3161 proof over SHA-256 of the signature bytes is requested from
the configured TSA (default FreeTSA) or --tsa URL. The reply's structure,
status, imprint, and nonce are checked; trust checks are reported
separately. The complete DER reply is appended without duplicates to
manifest.json.sig.tsr, <digest>.tsr beside a ZIP, or revocation.json.sig.tsr,
written atomically. A malformed existing sidecar is preserved and the
command stops.

--submit sends existing publication proofs to the index's timestamp
submission endpoint (Sciscape `timestamp_submit_url`); the publication must be
admitted to its catalog. For a revocation directory, --submit sends the proofs
with the revocation record (as `sci revoke --submit`).
""",
    args=(
        arg("path", metavar="PATH", help="signed directory, ZIP, or revocation directory"),
        arg(
            "--tsa", metavar="URL", help="timestamp authority (default: config tsa_url or FreeTSA)"
        ),
        arg("--submit", action="store_true", help="submit existing proofs to an index"),
        arg("--to", metavar="URL", help="index origin for --submit (default: configured index)"),
    ),
    outputs="The proof's genTime, TSA, and trust result, or the index's retention result.",
    files="Creates or appends the timestamp sidecar next to PATH; --submit writes none.",
    network="One paced request to the TSA, or one paced POST to the index with --submit.",
    exit_codes={
        3: "the target fails verification, or the existing sidecar is malformed",
        4: "the index does not support timestamp submission",
        5: "the TSA or index request did not complete",
        6: "the index refused the proofs by policy",
    },
    examples=("sci timestamp ./draft", "sci timestamp ./campaign/<digest>.zip --submit"),
    run=run,
    timeout=True,
)
