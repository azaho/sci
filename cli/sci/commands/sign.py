"""``sci sign``: sign a draft, timestamp it, and optionally store its ZIP in a campaign folder."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pysci.bundle import verify_directory, verify_loose
from pysci.der import frame_values
from pysci.draft import BaselineRefused, check_draft, load_draft, sign_draft
from pysci.errors import SciError
from pysci.keys import public_key_of
from pysci.manifest import MANIFEST_PATH, SIGNATURE_PATH, TIMESTAMP_SIDECAR_PATH
from pysci.source import ByteSource

from ..checks import proof_json
from ..clients.tsa import FREETSA_URL, append_sidecar, read_sidecar, request_proof
from ..config import normalize_url
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit, exit_for_outcome, issue_record
from ..output import Result
from ..spec import Command, arg
from ..storage.files import recover_replacements, replace_together, temp_names
from ..storage.inputs import SignedInput
from ..storage.keys import require_outside
from ..storage.local_publish import publish_to_folder, remember_folder
from .check import describe, draft_report_json
from .common import KEY_ARG_HELP, signing_key, trust


def tsa_url(ctx: Context, explicit: str | None) -> str:
    if explicit:
        return normalize_url(explicit, what="TSA URL")
    return ctx.config.tsa_url or FREETSA_URL


def _verify_in_place(draft: Path, manifest: bytes, signature: bytes, expected: str) -> None:
    sources, _, _ = load_draft(draft, temp_names(draft))
    files: dict[str, ByteSource | bytes] = {
        **sources,
        MANIFEST_PATH: manifest,
        SIGNATURE_PATH: signature,
    }
    result = verify_loose(files, expected_id=expected)
    if not result.ok:
        raise CliError(
            "draft_changed",
            "draft files changed during signing; the previous files were restored",
            Exit.INCOMPLETE,
            retryable=True,
        )


def timestamp_step(
    ctx: Context, result: Result, sidecar: Path, signature: bytes, url: str, timeout: float
) -> bool:
    """Request one proof and append it; on failure record a partial result."""
    deadline = ctx.deadline(timeout)
    try:
        trust_config = trust(ctx)
        proof = request_proof(ctx.http, url, signature, trust_config, deadline)
        appended = append_sidecar(sidecar, proof.response)
    except CliError as exc:
        result.data["timestamp"] = {
            "status": "failed",
            "tsa": url,
            "error": exc.records[0].to_json(),
        }
        result.errors.extend(exc.records)
        return False
    result.data["timestamp"] = {
        "status": "appended" if appended else "duplicate",
        "tsa": url,
        "sidecar": str(sidecar),
        "proof": proof_json(proof.verification),
    }
    return True


def run(args: argparse.Namespace, ctx: Context) -> Result:
    draft = ctx.resolve(args.dir)
    if not draft.is_dir():
        raise CliError("draft_missing", f"{draft} is not a directory", Exit.USAGE, file=str(draft))
    if args.no_timestamp and args.tsa:
        raise CliError("conflicting_options", "--tsa cannot be combined with --no-timestamp")
    url = None if args.no_timestamp else tsa_url(ctx, args.tsa)
    output = ctx.resolve(args.output) if args.output else None
    restored = recover_replacements(draft)
    key_path, key, warnings = signing_key(ctx, args)
    require_outside(key_path, draft, *([output] if output else []))

    result = Result("sign")
    result.warnings.extend(warnings)
    if restored:
        result.warn(f"restored files from an interrupted signing operation: {restored}")
    report = check_draft(draft, ignore=temp_names(draft))
    result.data = {"path": str(draft), "check": draft_report_json(report)}
    if report.issues:
        describe(result, report)
        result.exit_code = exit_for_outcome(report.outcome)
        result.errors.extend(issue_record(i) for i in report.issues)
        return result
    assert report.baseline is not None
    if not report.baseline.eligible and not args.allow_nonbaseline:
        describe(result, report)
        result.exit_code = Exit.REFUSED
        result.errors.append(
            ErrorRecord(
                "baseline_refused",
                "the draft does not meet the baseline; pass --allow-nonbaseline to sign a "
                "protocol-valid non-baseline publication",
            )
        )
        result.errors.extend(issue_record(i) for i in report.baseline.reasons)
        return result

    selected = public_key_of(key)
    signed_before = (draft / MANIFEST_PATH).exists() or (draft / SIGNATURE_PATH).exists()
    status = "signed"
    if signed_before:
        existing = verify_directory(draft)
        if (
            existing.ok
            and existing.manifest is not None
            and existing.manifest.public_key == selected
        ):
            status = "unchanged"
            publication_id = existing.manifest.publication_id
            signature = (draft / SIGNATURE_PATH).read_bytes()
        elif not args.replace:
            raise CliError(
                "signature_exists",
                f"{draft} has a signature that no longer matches its files or the selected key; "
                "pass --replace to re-sign (this discards timestamp proofs for the old signature)",
                Exit.USAGE,
            )
        else:
            status = "replaced"
    if status != "unchanged":
        try:
            signed = sign_draft(
                draft, key, allow_nonbaseline=args.allow_nonbaseline, ignore=temp_names(draft)
            )
        except BaselineRefused as exc:  # pragma: no cover - checked above
            raise CliError("baseline_refused", str(exc), Exit.REFUSED) from None
        except SciError as exc:
            raise CliError.from_sci(exc, "signing failed") from None
        replacements: dict[str, bytes | None] = {
            MANIFEST_PATH: signed.manifest_bytes,
            SIGNATURE_PATH: signed.signature,
        }
        if status == "replaced":
            replacements[TIMESTAMP_SIDECAR_PATH] = None
        with replace_together(draft, replacements):
            _verify_in_place(draft, signed.manifest_bytes, signed.signature, signed.publication_id)
        publication_id = signed.publication_id
        signature = signed.signature
    result.data.update(
        {
            "status": status,
            "publication_id": publication_id,
            "signer": selected.to_jwk(),
            "baseline": report.baseline.eligible,
        }
    )
    verb = {"signed": "Signed", "replaced": "Re-signed", "unchanged": "Unchanged signature"}[status]
    result.say(f"{verb}: {publication_id}")
    if status == "replaced":
        result.say("previous timestamp proofs were discarded with the replaced signature")
    if not report.baseline.eligible:
        result.say("baseline: not eligible (signed with --allow-nonbaseline)")

    sidecar = draft / TIMESTAMP_SIDECAR_PATH
    timestamp_ok = True
    if url is None:
        result.data["timestamp"] = {"status": "skipped", "reason": "--no-timestamp"}
        result.say("timestamp: skipped (--no-timestamp)")
    else:
        existing_proofs = read_sidecar(sidecar)
        framed = frame_values(existing_proofs) if existing_proofs else None
        if status == "unchanged" and framed is not None and framed.values and framed.complete:
            result.data["timestamp"] = {"status": "existing", "sidecar": str(sidecar)}
            result.say(f"timestamp: existing sidecar kept ({len(framed.values)} proof(s))")
        else:
            timestamp_ok = timestamp_step(ctx, result, sidecar, signature, url, args.timeout)
            ts = result.data["timestamp"]
            if timestamp_ok:
                trust_result = ts["proof"]["result"]
                result.say(f"timestamp: {ts['status']} from {url} (trust: {trust_result})")
            else:
                result.say(f"timestamp: failed ({ts['error']['message']})")
                result.say(f"retry later with: sci timestamp {draft}")

    pack_exit: int = Exit.OK
    if output is not None:
        verified = verify_directory(draft, expected_id=publication_id, compute_zip_sha256=True)
        if not verified.ok:
            raise CliError(
                "draft_changed",
                "signed files changed before packing",
                Exit.INCOMPLETE,
                retryable=True,
            )
        item = SignedInput(draft, "directory", verified)
        stored, pack_exit = publish_to_folder(item, output)
        result.data["output"] = stored
        if pack_exit == Exit.OK:
            if remember_folder(ctx.config, output):
                ctx.save_config()
            result.say(f"stored: {stored['path']} ({stored['bundle']})")
        else:
            error: dict[str, Any] = stored["error"]
            result.errors.append(ErrorRecord(error["code"], error["message"], error["retryable"]))
            result.say(f"packing failed; the signed directory {draft} is preserved")
    if not timestamp_ok:
        result.exit_code = Exit.INCOMPLETE
        result.partial = True
    if pack_exit != Exit.OK:
        result.exit_code = pack_exit
        result.partial = True
    return result


COMMAND = Command(
    name="sign",
    usage_hint=(
        "sign DIR [--output DIR] [--key PATH] [--no-timestamp | --tsa URL] [--replace] "
        "[--allow-nonbaseline] [--json]"
    ),
    summary="Sign a draft and optionally save its ZIP in a campaign folder.",
    description="""
Applies the `check` draft checks (rejecting symbolic links, special files, and
private-key material), builds manifest.json with sorted paths and a stable
serializer, signs it (SCI protocol §5.1), and verifies the result (§6.4)
before keeping it. manifest.json and manifest.json.sig are replaced together
through a recoverable operation; if the files change while signing, the
previous files are restored.

An unchanged draft signed by the same key keeps its manifest bytes and ID.
Otherwise --replace is required, and the timestamp proofs of the replaced
signature are discarded.

Unless --no-timestamp is given, the signature is timestamped by the
configured TSA (default FreeTSA, https://freetsa.org/tsr; no automatic
fallback). A timestamp failure keeps the signed publication and returns a
partial result; retry with `sci timestamp DIR`.

--output DIR also stores <digest>.zip (and <digest>.tsr) in a campaign folder
under the local publishing rules of `sci publish`.
""",
    args=(
        arg("dir", metavar="DIR", help="draft directory to sign in place"),
        arg("--output", metavar="DIR", help="campaign folder that receives <digest>.zip"),
        arg("--key", metavar="PATH", help=KEY_ARG_HELP),
        arg("--no-timestamp", action="store_true", help="skip timestamping (signing is offline)"),
        arg(
            "--tsa",
            metavar="URL",
            help="timestamp authority URL (default: config tsa_url, else https://freetsa.org/tsr)",
        ),
        arg("--replace", action="store_true", help="replace an existing, different signature"),
        arg(
            "--allow-nonbaseline",
            action="store_true",
            help="sign a protocol-valid publication that is outside the baseline",
        ),
    ),
    outputs="Publication ID, signer, baseline eligibility, timestamp result, and stored ZIP path.",
    files="""Writes DIR/manifest.json and DIR/manifest.json.sig; appends to
DIR/manifest.json.sig.tsr; with --output writes <digest>.zip and <digest>.tsr in
that folder and remembers the folder in config.json.""",
    network="""One request to the TSA unless --no-timestamp (paced to at most one per
second together with index writes). Otherwise offline.""",
    exit_codes={
        3: "the draft fails protocol checks, or --output holds a conflicting ZIP",
        5: "signed, but timestamping or packing did not complete (ID is reported)",
        6: "baseline refusal (use --allow-nonbaseline for protocol-valid drafts)",
    },
    examples=("sci license ./draft && sci sign ./draft --no-timestamp --output ./campaign",),
    run=run,
    timeout=True,
)
