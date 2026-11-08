"""``sci spec``: print the SCI protocol specification bundled with this CLI."""

from __future__ import annotations

import argparse

from pysci import PROTOCOL_VERSION
from pysci.specification import (
    PROTOCOL_SPEC_SHA256,
    protocol_specification,
    specification_section,
    specification_sections,
)

from ..context import Context
from ..errors import CliError
from ..output import Result
from ..spec import Command, arg


def run(args: argparse.Namespace, ctx: Context) -> Result:
    if args.list and args.section:
        raise CliError("conflicting_options", "--list and --section cannot be combined")
    result = Result("spec")
    result.data = {"protocol": PROTOCOL_VERSION, "sha256": PROTOCOL_SPEC_SHA256}
    if args.list:
        sections = specification_sections()
        result.data["sections"] = [{"number": n, "title": t} for n, t in sections]
        for number, title in sections:
            result.say(f"{'  ' * number.count('.')}{number}  {title}")
        return result
    if args.section:
        text = specification_section(args.section.rstrip("."))
        if text is None:
            raise CliError(
                "unknown_section",
                f"the protocol has no section {args.section}; run `sci spec --list`",
                observed=args.section,
            )
        result.data["section"] = args.section.rstrip(".")
    else:
        text = protocol_specification()
    result.data["text"] = text
    result.lines.extend(text.rstrip("\n").split("\n"))
    return result


COMMAND = Command(
    name="spec",
    usage_hint="spec [--section N | --list] [--json]",
    summary="Print the SCI protocol specification.",
    description="""
Prints the complete SCI protocol specification (Markdown) that this CLI and
its pysci library implement. The text is bundled with the installed package
and checked against the hash pinned by the conformance corpus, so it matches
the installed version exactly and needs no network access.

Use --section to print one numbered section with its subsections, for
example `--section 4` for publication metadata or `--section 4.4` for
citations. --list prints the section numbers and titles.
""",
    args=(
        arg("--section", metavar="N", help="print only section N, e.g. 4 or 4.4"),
        arg("--list", action="store_true", help="list numbered sections and their titles"),
    ),
    outputs="Specification Markdown, one section, or the section list; --json adds its SHA-256.",
    files="None.",
    network="None.",
    exit_codes={2: "unknown section, or --list combined with --section"},
    examples=("sci spec --section 4", "sci spec --list", "sci spec > sci-protocol.md"),
    run=run,
)
