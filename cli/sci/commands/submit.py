"""``sci submit``: register hosted publications with an indexer (§8.1.4)."""

from __future__ import annotations

import argparse
from typing import Any

from pysci.accounts import normalize_account
from pysci.submission import build_submission_request

from ..context import Context
from ..errors import CliError, ErrorRecord, Exit, combine_exits
from ..output import Result
from ..spec import Command, arg
from ..storage.inputs import collect_signed_inputs, invalid_exit
from .common import archive_urls, index_client, looks_like_id, parse_id


def run(args: argparse.Namespace, ctx: Context) -> Result:
    archives = archive_urls(args.archive)
    if not archives:
        raise CliError(
            "missing_archive",
            "submit requires at least one --archive descriptor URL; files must already be hosted",
        )
    account = None
    if args.account:
        try:
            account = normalize_account(args.account[0], args.account[1])
        except ValueError as exc:
            raise CliError(
                "invalid_account", f"--account: {exc}", Exit.USAGE, observed=" ".join(args.account)
            ) from None
    digests: list[str] = []
    paths = []
    for target in args.targets:
        if looks_like_id(target):
            digest = parse_id(target)
            if digest not in digests:
                digests.append(digest)
        else:
            paths.append(ctx.resolve(target))
    invalid: list[dict[str, Any]] = []
    if paths:
        inputs, invalid, _ = collect_signed_inputs(paths)
        for item in inputs:
            if item.digest not in digests:
                digests.append(item.digest)
    client = index_client(ctx, args.to)
    result = Result("submit")
    result.data = {
        "index": client.origin,
        "descriptors": archives,
        "account": account.to_json() if account else None,
        "invalid_candidates": invalid,
        "submissions": [],
    }
    if not digests:
        result.exit_code = invalid_exit(invalid) if invalid else Exit.USAGE
        result.errors.append(ErrorRecord("no_valid_inputs", "no valid publications to submit"))
        return result
    client.discover(ctx.deadline(args.timeout))
    exits: list[int] = []
    for digest in digests:
        request = build_submission_request(digest, archives, account)
        outcome = client.submit(request, ctx.deadline(args.timeout))
        exits.append(outcome["exit_code"])
        entry = {k: v for k, v in outcome.items() if k != "exit_code"}
        entry["id"] = "sha256:" + digest
        result.data["submissions"].append(entry)
        _describe(result, entry)
        if "error" in entry:
            error = entry["error"]
            result.errors.append(
                ErrorRecord(
                    error["code"],
                    f"sha256:{digest}: {error['message']}",
                    error["retryable"],
                )
            )
    if invalid:
        exits.append(invalid_exit(invalid))
        for candidate in invalid:
            result.say(f"not sent (invalid): {candidate['path']} ({candidate['status']})")
    result.exit_code = combine_exits(exits)
    result.partial = result.exit_code == Exit.INCOMPLETE and Exit.OK in exits
    return result


def _describe(result: Result, entry: dict[str, Any]) -> None:
    status = entry["status"]
    if status == "registered":
        body = entry.get("response", {})
        service = body.get("status")
        line = f"{entry['id']}: registered"
        if isinstance(service, str):
            line += f" (service status: {service})"
        result.say(line)
        if body.get("publication_url"):
            result.say(f"    {body['publication_url']}")
        if body.get("message"):
            result.say(f"    {body['message']}")
        if service is None:
            result.say("    registration confirms lookup only; catalog admission is not implied")
    else:
        message = entry.get("error", {}).get("message", "")
        result.say(f"{entry['id']}: {status} - {message}")


COMMAND = Command(
    name="submit",
    usage_hint=(
        "submit ID_OR_PATH ... --archive URL [--archive URL ...] [--to INDEX] "
        "[--account TYPE ID] [--json]"
    ),
    summary="Register hosted publications with an indexer.",
    description="""
Sends one publication submission (SCI protocol §8.1.4) per ID to the index's
submit_url, naming the archive descriptors that host it. Accepts full IDs
or local signed inputs (a ZIP, a signed directory, or a folder of *.zip
bundles; each is verified first and invalid candidates are not sent). Files
must already be hosted (see `sci publish`); submission uploads nothing.

Protocol success confirms location registration only; service fields such as
Sciscape's status, publication_url, accepted_at, reason, and message are
reported as returned. Temporary failures are retried with backoff and
Retry-After; refusals are not retried. A lost response is reported as
outcome_unknown. Adding or renewing an account claim permits resubmitting an
unchanged bundle for catalog admission.
""",
    args=(
        arg(
            "targets",
            metavar="ID_OR_PATH",
            nargs="+",
            help="publication IDs or local signed inputs",
        ),
        arg(
            "--archive", metavar="URL", action="append", help="archive descriptor URL (repeatable)"
        ),
        arg("--to", metavar="INDEX", help="index origin (default: configured index)"),
        arg(
            "--account",
            nargs=2,
            metavar=("TYPE", "ID"),
            help="unsigned account hint (domain, github, or huggingface)",
        ),
    ),
    outputs="One result per ID (registered, refused, failed, or outcome_unknown) with the "
    "service response.",
    files="None.",
    network="Index descriptor GET, then one paced POST per publication (at most one per second).",
    exit_codes={
        3: "a local input failed verification, or the index rejected the content",
        4: "the index has no submit_url or rejected an unsupported schema",
        5: "a temporary failure, outcome_unknown, or a mix of successes and failures",
        6: "the index refused a submission by policy (e.g. not_baseline)",
    },
    examples=(
        "sci submit ./campaign --to https://sciscape.io --archive "
        "https://raw.githubusercontent.com/lab/research/main/sci/archive.json",
    ),
    run=run,
    timeout=True,
)
