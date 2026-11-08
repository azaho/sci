"""``sci publish``: store signed bundles in local folders or external archives."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..clients.credentials import github_token, huggingface_token
from ..clients.github import GitHubArchive
from ..clients.hosting import Prepared
from ..clients.huggingface import HuggingFaceArchive
from ..context import Context
from ..destinations import Destination, resolve_destinations
from ..errors import CliError, ErrorRecord, Exit, combine_exits
from ..output import Result
from ..spec import Command, arg
from ..storage.files import InputChanged, sha256_file, store_no_clobber
from ..storage.inputs import (
    SignedInput,
    collect_signed_inputs,
    invalid_exit,
    reader_files,
    write_zip,
)
from ..storage.local_publish import publish_to_folder, remember_folder


def _snapshot(item: SignedInput, workdir: Path) -> SignedInput:
    """Copy a verified input's canonical ZIP (and sidecar) into the work directory."""
    target = workdir / f"{item.digest}.zip"
    try:
        store_no_clobber(target, lambda handle: write_zip(item, handle), item.zip_sha256)
    except InputChanged:
        raise CliError(
            "input_changed",
            f"{item.path} changed after verification",
            Exit.INCOMPLETE,
            retryable=True,
        ) from None
    sidecar = item.sidecar
    if sidecar is not None:
        shutil.copyfile(sidecar, workdir / f"{item.digest}.tsr")
    return SignedInput(target, "zip", item.result)


def _plan_folder(item: SignedInput, destination: Destination) -> str:
    assert destination.path is not None
    target = destination.path / f"{item.digest}.zip"
    if not target.exists():
        return "write"
    return "keep identical" if sha256_file(target) == item.zip_sha256 else "conflict"


def run(args: argparse.Namespace, ctx: Context) -> Result:
    destinations = resolve_destinations(args.to or [], list(ctx.config.folders), ctx.cwd)
    paths = [ctx.resolve(p) for p in args.paths]
    inputs, invalid, duplicates = collect_signed_inputs(paths)
    result = Result("publish")
    result.data = {
        "destinations": [d.display for d in destinations],
        "publications": [i.publication_id for i in inputs],
        "invalid_candidates": invalid,
        "duplicates": duplicates,
        "results": [],
    }
    for candidate in invalid:
        first = candidate["issues"][0]["message"] if candidate["issues"] else ""
        result.say(f"not published (invalid): {candidate['path']} - {first}")
    if not inputs:
        result.exit_code = invalid_exit(invalid) if invalid else Exit.USAGE
        result.errors.append(ErrorRecord("no_valid_inputs", "no valid signed publications found"))
        return result

    if args.dry_run:
        plan: list[dict[str, Any]] = []
        for destination in destinations:
            for item in inputs:
                action = (
                    _plan_folder(item, destination)
                    if destination.kind == "folder"
                    else "upload reader files and bundle (repository must be public"
                    + ("; created if missing)" if args.create else ")")
                )
                plan.append(
                    {
                        "destination": destination.display,
                        "publication_id": item.publication_id,
                        "action": action,
                    }
                )
                result.say(f"{destination.display}: {item.publication_id}: {action}")
        result.data["plan"] = plan
        result.data["dry_run"] = True
        if invalid:
            result.exit_code = Exit.INCOMPLETE
            result.partial = True
        return result

    exits: list[int] = [invalid_exit(invalid)] if invalid else []
    workdir = Path(tempfile.mkdtemp(prefix="sci-publish-"))
    try:
        snapshots = [_snapshot(item, workdir) for item in inputs]
        prepared: list[Prepared] | None = None
        for destination in destinations:
            if destination.kind == "folder":
                assert destination.path is not None
                stored_any = False
                for item in snapshots:
                    report, code = publish_to_folder(item, destination.path)
                    exits.append(code)
                    result.data["results"].append(report)
                    stored_any = stored_any or code == Exit.OK
                    if code == Exit.OK:
                        result.say(
                            f"{destination.display}: {item.publication_id}: {report['bundle']}"
                        )
                    else:
                        error = report["error"]
                        result.errors.append(
                            ErrorRecord(error["code"], error["message"], error["retryable"])
                        )
                        result.say(f"{destination.display}: {item.publication_id}: failed")
                if stored_any and remember_folder(ctx.config, destination.path):
                    ctx.save_config()
                continue
            if prepared is None:
                prepared = []
                for item in snapshots:
                    reader = reader_files(item)
                    sidecar = item.sidecar
                    if sidecar is not None:
                        reader["manifest.json.sig.tsr"] = sidecar.read_bytes()
                    prepared.append(Prepared(item, item.path, reader))
            _hosted(ctx, args, destination, prepared, result, exits)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    result.exit_code = combine_exits(exits)
    result.partial = result.exit_code == Exit.INCOMPLETE and Exit.OK in exits
    return result


def _hosted(
    ctx: Context,
    args: argparse.Namespace,
    destination: Destination,
    prepared: list[Prepared],
    result: Result,
    exits: list[int],
) -> None:
    assert destination.owner is not None
    assert destination.repo is not None
    try:
        archive: GitHubArchive | HuggingFaceArchive
        if destination.kind == "github":
            token = github_token(ctx.env, ctx.runner)
            archive = GitHubArchive(
                ctx.http,
                destination.owner,
                destination.repo,
                token,
                notice=lambda message: print(message, file=ctx.stderr, flush=True),
            )
        else:
            token = huggingface_token(ctx.env)
            archive = HuggingFaceArchive(ctx.http, destination.owner, destination.repo, token)
        summary, reports = archive.publish(prepared, create=args.create, timeout=args.timeout)
    except CliError as exc:
        exits.append(exc.exit_code)
        result.errors.append(exc.records[0])
        for pub in prepared:
            result.data["results"].append(
                {
                    "publication_id": pub.item.publication_id,
                    "destination": destination.display,
                    "kind": destination.kind,
                    "status": "failed",
                    "error": exc.records[0].to_json(),
                }
            )
        result.say(f"{destination.display}: failed - {exc.records[0].message}")
        return
    availability = summary["availability"]
    for report in reports:
        entry = report.to_json()
        entry.update({"destination": destination.display, "kind": destination.kind})
        result.data["results"].append(entry)
        exits.append(report.exit_code if report.status != "hosted" else Exit.OK)
        if report.error is not None:
            result.errors.append(report.error)
        result.say(f"{destination.display}: {report.publication_id}: {report.status}")
    result.data.setdefault("archives", []).append({"destination": destination.display, **summary})
    result.say(f"  descriptor: {summary['descriptor_url']}")
    if not availability["confirmed"]:
        exits.append(Exit.INCOMPLETE)
        result.errors.append(
            ErrorRecord(
                "availability_unconfirmed",
                f"{destination.display}: public availability of reader files is not yet confirmed "
                "(CDN caches may lag); retry later",
                True,
            )
        )
    elif not availability["reader_cors"]:
        result.warn(f"{destination.display}: reader files lack CORS headers for browser readers")
    bundle_cors = [c for c in availability["checks"] if c["label"] == "bundle"]
    if bundle_cors and not all(c.get("cors") for c in bundle_cors):
        result.warn(
            f"{destination.display}: bundle downloads lack CORS headers; browser readers use "
            "entry_url instead"
        )


COMMAND = Command(
    name="publish",
    usage_hint=(
        "publish PATH ... --to DESTINATION [--to DESTINATION ...] [--create] [--dry-run] [--json]"
    ),
    summary="Store signed bundles in local folders or external archives.",
    description="""
PATH is a signed ZIP, a signed directory, or a folder of *.zip bundles. Each
candidate is verified completely (SCI protocol §6.4) and deduplicated by ID;
invalid candidates are reported and not published. Publishing never
contacts an indexer (see `sci submit`).

Destinations (repeat --to; equivalent addresses are merged):
  DIR                                   store <digest>.zip (+ <digest>.tsr)
  @local                                all known folders
  https://github.com/OWNER/REPO         reader files on main + release assets
  https://huggingface.co/datasets/OWNER/REPO   reader files + ZIPs (Git LFS)
SSH forms (git@github.com:OWNER/REPO.git, git@hf.co:datasets/OWNER/REPO) and
ssh:// URLs are accepted; file or branch URLs, queries, and fragments are not.

Local folders: <digest>.zip is written atomically; identical files are kept and
conflicting bytes rejected. Hosted archives: the repository must be public
(or created public with --create; visibility is never changed). Reader files
(manifest, signature, sci.json, README, LICENSE, display files, timestamp
sidecar) go to sci/publications/<id2>/<id>/ on main; sci/archive.json and a
sorted sci/publications.txt are maintained in batched commits without
force-pushing. GitHub stores <id>.zip as an asset of the shared release
sci-<id2>, named for the first two ID characters (never marked latest);
Hugging Face stores
bundles/<id2>/<id>.zip via LFS. Retries compare remote digests and reuse
matching content. Credentials: `gh auth login` or GH_TOKEN/GITHUB_TOKEN;
`hf auth login` or HF_TOKEN.
""",
    args=(
        arg(
            "paths",
            metavar="PATH",
            nargs="+",
            help="signed ZIP, signed directory, or campaign folder",
        ),
        arg(
            "--to",
            metavar="DESTINATION",
            action="append",
            help="destination (repeatable, required)",
        ),
        arg("--create", action="store_true", help="create a missing hosted repository as public"),
        arg("--dry-run", action="store_true", help="list the plan without writes or uploads"),
    ),
    outputs="""One result per publication and destination (stored/hosted/failed, reused or
uploaded files), hosted descriptor URLs, and public-availability and CORS
checks.""",
    files="""Writes <digest>.zip and <digest>.tsr in folder destinations and remembers those
folders in config.json. Hosted destinations change only their repositories.""",
    network="None for folders. GitHub REST API / Hugging Face Hub API uploads for hosted "
    "destinations, then unauthenticated availability checks.",
    exit_codes={
        3: "an input failed verification, or a destination holds conflicting bytes",
        5: "an upload or availability check did not complete, or results are mixed",
        6: "the provider refused the operation (permissions)",
    },
    examples=(
        "sci publish ./campaign --to ./mirror --to https://github.com/lab/research --create",
    ),
    run=run,
    timeout=True,
)
