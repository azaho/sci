"""Create and submit signed Sciscape opinions with an existing SCI signing key."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pysci.accounts import normalize_account
from pysci.keys import public_key_of

from ..clients.votes import (
    MAX_RECORD_BYTES,
    MAX_REVISION,
    Vote,
    create_vote,
    current_vote,
    verify_vote,
)
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit
from ..output import Result
from ..spec import Command, arg
from ..storage.files import StoreOutcome, store_bytes
from ..storage.keys import require_outside
from .common import KEY_ARG_HELP, index_client, parse_id, signing_key


def _save(directory: Path, vote: Vote) -> None:
    try:
        for name, data in (("vote.json", vote.record), ("vote.json.sig", vote.signature)):
            if store_bytes(directory / name, data).outcome is StoreOutcome.CONFLICT:
                raise CliError(
                    "destination_conflict", f"{directory / name} has different bytes", Exit.INVALID
                )
    except OSError as exc:
        raise CliError(
            "vote_write_failed", f"cannot save signed vote: {exc}", Exit.INCOMPLETE
        ) from None


def _load(directory: Path) -> Vote:
    try:
        with (directory / "vote.json").open("rb") as f:
            record = f.read(MAX_RECORD_BYTES + 1)
        with (directory / "vote.json.sig").open("rb") as f:
            signature = f.read(65)
    except OSError as exc:
        raise CliError("vote_missing", f"cannot read signed vote: {exc}", Exit.USAGE) from None
    return verify_vote(record, signature)


def run(args: argparse.Namespace, ctx: Context) -> Result:
    choices = [args.up, args.down, args.withdraw]
    if args.submit:
        if (
            args.target
            or any(choices)
            or args.output
            or args.revision is not None
            or args.reason is not None
            or args.review
            or args.key
            or args.refresh
        ):
            raise CliError(
                "conflicting_options", "--submit accepts only --to, --account, --timeout and --json"
            )
    elif not args.target or sum(choices) != 1:
        raise CliError(
            "invalid_arguments",
            "supply a publication ID and exactly one of --up, --down, --withdraw",
        )
    if args.refresh and (args.revision is not None or args.output):
        raise CliError(
            "conflicting_options", "--refresh cannot be combined with --revision or --output"
        )
    if args.output and (args.revision is None or args.to):
        raise CliError(
            "invalid_arguments", "offline --output requires --revision and cannot be used with --to"
        )
    if args.revision is not None and not 1 <= args.revision <= MAX_REVISION:
        raise CliError("invalid_arguments", f"--revision must be from 1 to {MAX_REVISION}")
    hint: bytes | None = None
    if args.account:
        try:
            account = normalize_account(args.account[0], args.account[1])
            if account.type != "domain":
                raise ValueError("votes require a registrable domain account")
            hint = json.dumps(account.to_json(), separators=(",", ":")).encode()
        except ValueError as exc:
            raise CliError("invalid_account", str(exc), Exit.USAGE) from None
    result = Result("vote")
    if args.submit:
        directory = ctx.resolve(args.submit)
        vote = _load(directory)
    else:
        target = "sci:sha256:" + parse_id(args.target)
        review = "sci:sha256:" + parse_id(args.review) if args.review else None
        value = 1 if args.up else -1 if args.down else 0
        key_path, key, warnings = signing_key(ctx, args)
        result.warnings.extend(warnings)
        # Validate local inputs before any network requests.
        candidate = create_vote(key, target, value, args.revision or 1, args.reason, review)
        previous = None
        if args.revision is None:
            previous = current_vote(
                index_client(ctx, args.to),
                target[11:],
                public_key_of(key).x,
                ctx.deadline(args.timeout),
            )
        if (
            not args.refresh
            and previous is not None
            and (previous.value, previous.reason, previous.review)
            == (
                value,
                args.reason,
                review,
            )
        ):
            vote = previous  # Repeat the exact signed opinion without manufacturing another event.
        else:
            revision = (
                args.revision
                if args.revision is not None
                else (previous.revision + 1 if previous else 1)
            )
            if revision > MAX_REVISION:
                raise CliError(
                    "revision_exhausted", "vote revision range is exhausted", Exit.INVALID
                )
            vote = (
                candidate
                if revision == candidate.revision
                else create_vote(key, target, value, revision, args.reason, review)
            )
        directory = ctx.resolve(args.output) if args.output else ctx.home / "votes" / vote.id
        require_outside(key_path, directory)
        _save(directory, vote)
    result.data = {
        "id": vote.id,
        "target": vote.target,
        "value": vote.value,
        "revision": vote.revision,
        "path": str(directory),
        "submitted": False,
    }
    if args.output:
        result.say(f"Signed vote saved to {directory}; not submitted.")
        result.say(f"Submit unchanged with: sci vote --submit {directory} --to INDEX")
        return result
    parts: list[tuple[str, str | None, bytes, str]] = [
        ("vote.json", "vote.json", vote.record, "application/json"),
        ("vote.json.sig", "vote.json.sig", vote.signature, "application/octet-stream"),
    ]
    if hint is not None:
        parts.append(("account.json", "account.json", hint, "application/json"))
    try:
        client = index_client(ctx, args.to)
        response, outcome = client.submit_multipart(
            "vote_url", "signed votes", parts, ctx.deadline(args.timeout)
        )
    except CliError as exc:
        result.exit_code = exc.exit_code
        result.errors.extend(exc.records)
        result.say(f"Vote saved at {directory}; submission not confirmed.")
        return result
    del response
    result.data.update(index=client.origin)
    result.exit_code = outcome["exit_code"]
    result.data.update({k: v for k, v in outcome.items() if k != "exit_code"})
    if result.exit_code == Exit.OK:
        body = outcome["response"]
        if (
            body.get("id") != vote.id
            or not isinstance(body.get("revision"), int)
            or isinstance(body.get("revision"), bool)
            or body.get("revision") != vote.revision
            or type(body.get("created")) is not bool
        ):
            result.exit_code = Exit.INVALID
            result.errors.append(
                ErrorRecord("invalid_response", "vote response does not confirm the signed event")
            )
        else:
            result.data["submitted"] = True
            result.say(
                f"Vote {vote.value:+d} for {vote.target} recorded by {client.origin} "
                f"(revision {vote.revision})."
            )
    elif "error" in outcome:
        error = outcome["error"]
        result.errors.append(ErrorRecord(error["code"], error["message"], error["retryable"]))
    if not result.data["submitted"]:
        result.say(f"Submission not confirmed. Signed vote retained at {directory}.")
        result.say(
            f"Retry the exact event with: sci vote --submit {directory} --to {client.origin}"
        )
        if any(e.code == "vote_conflict" for e in result.errors):
            result.say(
                "Another revision exists. Inspect the current opinion before issuing a new vote; "
                "this command did not overwrite it."
            )
    return result


COMMAND = Command(
    name="vote",
    usage_hint="vote ID (--up | --down | --withdraw) [--reason TEXT] [--review ID] [--to INDEX]",
    summary="Sign and submit an upvote, downvote, or withdrawal to Sciscape.",
    description="""
Uses your existing SCI key to sign sciscape-vote/0.1 JSON and its raw Ed25519
signature. This is an optional Sciscape service extension, not a change to
SCI 0.1. The private key stays local. Domain eligibility is index policy;
--account domain NAME is an unsigned discovery hint, not a signed vote field.

With no --revision, fetch and verify the current signed opinion, then use
the next revision (or retry the identical opinion unchanged). Concurrent
conflicts are reported without automatically overwriting another opinion.
--refresh creates a new revision even for an unchanged opinion, so the index
can attach renewed or newly discovered domain evidence.
--reason is optional nonblank text, at most 1,000 Unicode code points.
--review is an optional reference to a separately published SCI review;
publish full reviews through the usual sign/publish/submit workflow.

--output DIR --revision N signs offline without submission. --submit DIR
verifies and submits saved vote.json and vote.json.sig without needing a
private key. Every online-created vote is saved under <state>/votes/<hash>
before upload, so a lost response can be retried with exactly the same bytes.
Zero (--withdraw) remains a signed revision, preventing replay of older votes.
""",
    args=(
        arg("target", nargs="?", metavar="ID", help="target SCI publication identifier"),
        arg("--up", action="store_true", help="recommend the publication"),
        arg("--down", action="store_true", help="recommend against the publication"),
        arg(
            "--refresh", action="store_true", help="issue a new revision to refresh domain evidence"
        ),
        arg("--withdraw", action="store_true", help="withdraw your existing vote"),
        arg("--reason", metavar="TEXT", help="optional signed explanation"),
        arg("--review", metavar="ID", help="optional signed reference to a review publication"),
        arg(
            "--revision",
            type=int,
            metavar="N",
            help="explicit revision; otherwise look up the current vote",
        ),
        arg("--output", metavar="DIR", help="save offline; requires --revision; does not submit"),
        arg("--submit", metavar="DIR", help="verify and submit an already signed vote"),
        arg("--to", metavar="INDEX", help="index origin (default: configured index)"),
        arg("--account", nargs=2, metavar=("TYPE", "ID"), help="unsigned domain discovery hint"),
        arg("--key", metavar="PATH", help=KEY_ARG_HELP),
    ),
    outputs="Signed vote hash, target, value, revision, local path, and submission outcome.",
    files=(
        "Writes vote.json and vote.json.sig to --output DIR or <state>/votes/<hash>; "
        "never overwrites different bytes. --submit writes none."
    ),
    network=(
        "--output: none. Otherwise index discovery, current-vote GET unless "
        "--revision/--submit, and a paced multipart POST."
    ),
    exit_codes={
        3: "invalid signed vote or response",
        4: "index lacks voting capability",
        5: "conflict, temporary failure, or unknown outcome",
        6: "index refused the vote",
    },
    examples=(
        "sci vote sha256:<digest> --up --to https://sciscape.io --account domain lab.example",
        "sci vote sha256:<digest> --down --reason 'Evaluation leakage' --review sha256:<review>",
        "sci vote sha256:<digest> --withdraw",
    ),
    run=run,
    timeout=True,
)
