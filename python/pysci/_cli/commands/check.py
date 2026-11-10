"""``sci check``: validate an unsigned draft without signing it."""

from __future__ import annotations

import argparse
from typing import Any

from pysci.draft import DraftReport, check_draft

from ..checks import issue_lines
from ..context import Context
from ..errors import CliError, Exit, exit_for_outcome, issue_json, issue_record
from ..output import Result
from ..spec import Command, arg
from ..storage.files import temp_names


def draft_report_json(report: DraftReport) -> dict[str, Any]:
    baseline: dict[str, Any]
    if report.baseline is None:
        baseline = {"status": "not_performed", "reason": "planned sizes could not be computed"}
    else:
        baseline = {
            "status": "eligible" if report.baseline.eligible else "not_eligible",
            "reasons": [issue_json(r) for r in report.baseline.reasons],
        }
    return {
        "valid": report.valid,
        "outcome": str(report.outcome),
        "files": len(report.files),
        "planned_manifest_bytes": report.manifest_bytes,
        "planned_zip_bytes": report.zip_bytes,
        "metadata": None
        if report.metadata is None
        else {"title": report.metadata.title, "display": [d.path for d in report.metadata.display]},
        "baseline": baseline,
        "signed_files_ignored": report.signed,
        "issues": [issue_json(i) for i in report.issues],
    }


def describe(result: Result, report: DraftReport) -> None:
    data = draft_report_json(report)
    result.say(f"files:            {data['files']}")
    result.say(f"planned manifest: {data['planned_manifest_bytes']} bytes")
    result.say(f"planned ZIP:      {data['planned_zip_bytes']} bytes")
    if report.valid:
        result.say("protocol checks:  passed (§§2-4)")
    else:
        result.say(f"protocol checks:  {len(report.issues)} problem(s)")
        result.lines.extend(issue_lines(data["issues"], "    "))
    baseline = data["baseline"]
    result.say(f"baseline:         {baseline['status']}")
    result.lines.extend(issue_lines(baseline.get("reasons", []), "    "))
    if report.signed:
        result.say("note: manifest.json/manifest.json.sig present and ignored")


def warn_display(result: Result, report: DraftReport) -> None:
    """Report display images that may not render; they do not change the exit code."""
    for issue in report.display_warnings:
        result.warn(f"{issue.file}: display image does not decode: {issue.observed}")


def run(args: argparse.Namespace, ctx: Context) -> Result:
    draft = ctx.resolve(args.dir)
    if not draft.is_dir():
        raise CliError("draft_missing", f"{draft} is not a directory", Exit.USAGE, file=str(draft))
    report = check_draft(draft, ignore=temp_names(draft))
    result = Result("check")
    result.data = {"path": str(draft), **draft_report_json(report)}
    describe(result, report)
    warn_display(result, report)
    if report.issues:
        result.exit_code = exit_for_outcome(report.outcome)
        result.errors.extend(issue_record(i) for i in report.issues)
    elif (
        report.baseline is not None and not report.baseline.eligible and not args.allow_nonbaseline
    ):
        result.exit_code = Exit.REFUSED
        result.errors.extend(issue_record(i) for i in report.baseline.reasons)
    return result


COMMAND = Command(
    name="check",
    usage_hint="check DIR [--allow-nonbaseline] [--json]",
    summary="Check an unsigned draft without signing it.",
    description="""
Validates a draft under SCI protocol §§2-4: file paths and conflicts,
required files, sci.json fields, README/LICENSE text rules, and absence of
private-key material. Calculates the planned manifest and ZIP sizes with the
signing serializer and evaluates baseline eligibility (§2.1). All
independent problems are reported together, each with its file or JSON
field, requirement, and observed value.
""",
    args=(
        arg("dir", metavar="DIR", help="draft directory (sci.json, README.md, LICENSE, files/)"),
        arg(
            "--allow-nonbaseline",
            action="store_true",
            help="exit 0 for a protocol-valid draft that is outside the baseline",
        ),
    ),
    outputs="Planned sizes, protocol problems, and baseline eligibility with reasons.",
    files="None; reads no private key.",
    network="None.",
    exit_codes={
        3: "the draft fails protocol checks",
        5: "files could not be read or a local limit was exceeded",
        6: "the draft is valid but not baseline (signing would refuse without override)",
    },
    examples=("sci check ./draft",),
    run=run,
)
