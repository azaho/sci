"""``sci revoke``: create or submit a key revocation (§5.3)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pysci.manifest import MANIFEST_PATH, METADATA_PATH
from pysci.records import create_revocation, verify_revocation

from ..checks import proof_json
from ..clients.tsa import read_sidecar, verify_sidecar_bytes
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit, exit_for_outcome, issue_record
from ..output import Result
from ..spec import Command, arg
from ..storage.files import StoreOutcome, store_bytes
from ..storage.keys import require_outside
from .common import KEY_ARG_HELP, index_client, signing_key, trust

RECORD = "revocation.json"
SIGNATURE = "revocation.json.sig"
SIDECAR = "revocation.json.sig.tsr"

WARNING = """Revoking a key is permanent. After an index verifies the revocation it rejects
new catalog admissions for this key, and verified account associations end.
Anyone holding the record and signature can submit them: keep them private
until revocation is intended."""


def _inside_publication(directory: Path) -> Path | None:
    for candidate in [directory, *directory.parents]:
        if (candidate / MANIFEST_PATH).exists():
            return candidate
    if (directory / METADATA_PATH).exists():
        return directory
    return None


def create(args: argparse.Namespace, ctx: Context) -> Result:
    output = ctx.resolve(args.output)
    publication = _inside_publication(output.absolute())
    if publication is not None:
        raise CliError(
            "inside_publication",
            f"{output} is inside the publication directory {publication}; write revocations "
            "elsewhere",
            Exit.USAGE,
        )
    key_path, key, warnings = signing_key(ctx, args)
    require_outside(key_path, output)
    result = Result("revoke")
    result.warnings.extend(warnings)
    if ctx.interactive() and not args.yes:
        ctx.stderr.write(WARNING + "\nType 'revoke' to create the record: ")
        ctx.stderr.flush()
        answer = ctx.stdin.readline().strip()
        if answer != "revoke":
            raise CliError("cancelled", "revocation was not created", Exit.USAGE)
    record = create_revocation(key)
    check = verify_revocation(record.record, record.signature)
    assert check.ok
    assert check.public_key is not None
    output.mkdir(parents=True, exist_ok=True)
    statuses: dict[str, str] = {}
    for name, data in ((RECORD, record.record), (SIGNATURE, record.signature)):
        stored = store_bytes(output / name, data)
        if stored.outcome is StoreOutcome.CONFLICT:
            raise CliError(
                "destination_conflict",
                f"{output / name} exists with different bytes; it was left unchanged",
                Exit.INVALID,
                file=str(output / name),
            )
        statuses[name] = str(stored.outcome)
    result.data = {
        "x": check.public_key.x,
        "public_key": check.public_key.to_jwk(),
        "files": {RECORD: str(output / RECORD), SIGNATURE: str(output / SIGNATURE)},
        "status": statuses,
        "submitted": False,
    }
    result.say(f"revocation record for x={check.public_key.x}:")
    result.say(f"  {output / RECORD}")
    result.say(f"  {output / SIGNATURE}")
    result.say("Not submitted. Keep these files private until you intend to revoke the key.")
    result.say(f"Optionally timestamp them (sci timestamp {output}), then submit with:")
    result.say(f"  sci revoke --submit {output}")
    return result


def submit_revocation(ctx: Context, directory: Path, to: str | None, timeout: float) -> Result:
    result = Result("revoke")
    try:
        record = (directory / RECORD).read_bytes()
        signature = (directory / SIGNATURE).read_bytes()
    except OSError as exc:
        raise CliError(
            "revocation_missing",
            f"{directory} must contain {RECORD} and {SIGNATURE}: {exc}",
            Exit.USAGE,
        ) from None
    check = verify_revocation(record, signature)
    if not check.ok or check.public_key is None:
        result.exit_code = exit_for_outcome(check.outcome)
        result.errors.extend(issue_record(i) for i in check.issues)
        result.data = {"path": str(directory), "verification": str(check.outcome)}
        result.say(f"revocation in {directory} failed verification (§6.7)")
        return result
    parts: list[tuple[str, str | None, bytes, str]] = [
        (RECORD, RECORD, record, "application/json"),
        (SIGNATURE, SIGNATURE, signature, "application/octet-stream"),
    ]
    sidecar = read_sidecar(directory / SIDECAR)
    local_proofs: dict[str, Any] | None = None
    if sidecar is not None:
        parts.append((SIDECAR, SIDECAR, sidecar, "application/octet-stream"))
        verification = verify_sidecar_bytes(sidecar, signature, trust(ctx))
        local_proofs = {
            "status": str(verification.outcome),
            "proofs": [proof_json(p) for p in verification.proofs],
        }
    client = index_client(ctx, to)
    deadline = ctx.deadline(timeout)
    response, outcome = client.submit_multipart(
        "revocation_url", "key revocation submission", parts, deadline
    )
    x = check.public_key.x
    if outcome["exit_code"] == Exit.OK:
        body = outcome["response"]
        if body.get("x") != x or body.get("status") != "revoked":
            outcome = {
                "status": "failed",
                "exit_code": Exit.INVALID,
                "error": ErrorRecord(
                    "invalid_response",
                    "revocation response must contain the submitted x and status 'revoked'",
                    observed=repr({k: body.get(k) for k in ("x", "status")}),
                ).to_json(),
                "response": body,
            }
    result.data = {
        "x": x,
        "index": client.origin,
        "timestamp_sidecar": local_proofs or {"status": "absent"},
        **{k: v for k, v in outcome.items() if k != "exit_code"},
    }
    result.exit_code = outcome["exit_code"]
    if "error" in outcome:
        error = outcome["error"]
        result.errors.append(ErrorRecord(error["code"], error["message"], error["retryable"]))
        result.say(f"revocation for x={x} not confirmed by {client.origin}: {error['message']}")
    else:
        result.say(f"revocation for x={x} recorded by {client.origin} (status: revoked)")
        extra = {k: v for k, v in outcome.get("response", {}).items() if k not in ("x", "status")}
        if extra:
            result.say(f"service results: {extra}")
    del response
    return result


def run(args: argparse.Namespace, ctx: Context) -> Result:
    if (args.output is None) == (args.submit is None):
        raise CliError("invalid_arguments", "use exactly one of --output DIR or --submit DIR")
    if args.output is not None:
        if args.to:
            raise CliError("invalid_arguments", "--to applies only to --submit")
        return create(args, ctx)
    return submit_revocation(ctx, ctx.resolve(args.submit), args.to, args.timeout)


COMMAND = Command(
    name="revoke",
    usage_hint="revoke (--output DIR | --submit DIR) [--to URL] [--key PATH] [--yes] [--json]",
    summary="Create or submit a key revocation.",
    description="""
--output DIR creates revocation.json and revocation.json.sig (SCI protocol
§5.3) for the selected key, outside any publication directory. Revocation is
permanent. Interactive use explains the effect and asks for confirmation;
noninteractive invocation is an explicit request to create the record. The
record is never submitted by --output.

--submit DIR verifies the record (§6.7) and sends it, with any
revocation.json.sig.tsr, to the index's revocation submission endpoint
(Sciscape `revocation_url`).
""",
    args=(
        arg("--output", metavar="DIR", help="create the revocation record in DIR"),
        arg("--submit", metavar="DIR", help="submit the revocation record found in DIR"),
        arg("--to", metavar="URL", help="index origin for --submit (default: configured index)"),
        arg("--key", metavar="PATH", help=KEY_ARG_HELP),
        arg("--yes", action="store_true", help="skip the interactive confirmation"),
    ),
    outputs="The revoked key's x and file paths, or the index's submission result.",
    files="--output writes DIR/revocation.json and DIR/revocation.json.sig; --submit writes none.",
    network="--output: none. --submit: one paced POST to the index (after descriptor discovery).",
    exit_codes={
        3: "the revocation fails verification, or DIR holds a different record",
        4: "the index does not support revocation submission",
        5: "the submission did not complete (temporary failure or outcome_unknown)",
        6: "the index refused the submission by policy",
    },
    examples=("sci revoke --output ~/sci-revocation", "sci revoke --submit ~/sci-revocation"),
    run=run,
    timeout=True,
)
