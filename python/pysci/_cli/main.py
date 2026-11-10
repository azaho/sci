"""The ``sci`` command: argument parsing, dispatch, and error reporting."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from pysci import PROTOCOL_VERSION
from pysci import __version__ as pysci_version

from . import __version__
from .clients.http import Transport
from .context import Context, run_command
from .errors import EXIT_MEANINGS, CliError, Exit
from .output import Result, render
from .spec import add_command

DRAFT_GUIDE = """\
write a publication (complete rules: `sci spec --section 2` and `--section 4`):
  draft/
    sci.json     required metadata, max 64 KiB
    README.md    required main text, nonblank UTF-8 Markdown, max 1 MiB
    LICENSE      written by `sci license` (CC BY 4.0 text/figures/data, MIT code)
    files/       optional code, data, figures; max 1,000 files, ZIP max 64 MiB
  sci.json example:
    {
      "title": "Spike timing predicts choice in mouse V1 across three datasets",
      "abstract": "Question, approach, main finding, and main limitation.",
      "categories": [{"scheme": "arxiv", "code": "q-bio.NC"}],
      "keywords": ["spike timing", "decision making"],
      "display": [{"path": "files/accuracy.png", "caption": "Accuracy by time bin."}],
      "citations": [
        {"target": "sci:sha256:<64 hex digits>", "relations": ["uses"],
         "reason": "We reuse its preprocessing code."},
        {"target": "doi:10.48550/arXiv.1706.03762"},
        {"target": "https://example.org/data/release-3"}
      ]
    }
  Required: title (max 200 characters) and abstract (max 2,500). Optional:
  categories (max 3 {scheme, code}; arxiv codes such as cs.LG, q-bio.NC),
  keywords (max 20, each max 64 characters), display (max 3 paths under
  files/ shown first; caption max 2,000), and citations (max 1,000). Display
  files must be static .png images: max 2 MiB, 8,000,000 pixels, and 8,192
  pixels per side (other formats can stay in files/ but not in display).
  Omit unused fields; null and other fields are rejected.
  Line breaks and tabs are allowed only in abstract, citation reason, README.md,
  and LICENSE. File paths use only ASCII letters, digits, ".", "_", and "-"
  per component (no spaces), max 255 bytes.
  Citations: target is "sci:sha256:<digest>" (the ID printed by `sci sign`),
  "doi:<DOI>" (no https://doi.org/ prefix), or an http(s) URL. Optional
  relations: uses, reviews, supports, challenges, supersedes; optional reason
  (max 1,000). Citations are signed: add them before signing.
  `sci sign` adds manifest.json and manifest.json.sig to the draft. Changing
  any file afterwards needs `sci sign --replace`, which creates a new ID. To
  correct a published result, sign a new publication that cites the old one
  with "relations": ["supersedes"].
"""

DESCRIPTION = f"""\
sci: sign, verify, publish, and find SCI research publications (protocol {PROTOCOL_VERSION}).

A publication is a signed folder or <digest>.zip with README.md, sci.json
(metadata and citations), an optional LICENSE, and files/. Its ID is the
SHA-256 of manifest.json: sha256:<digest>. Everything verifies offline.

local campaign (offline):
  sci keygen                                   create ~/.sci/key.pem (owner-only)
  sci license ./draft                          select the SCI baseline license
  sci check ./draft                            validate and size the draft
  sci sign ./draft --no-timestamp --output ./campaign
  sci verify ./campaign/<digest>.zip           complete bundle checks
  sci search "neural decoding" --in ./campaign
  sci search --in ./campaign --limit 50        list publications, then --cursor TOKEN
  sci read ./campaign/<digest>.zip             verified metadata and README
  sci read ./campaign/<digest>.zip --file files/data.csv --output data.csv
                                               reuse one verified file
  sci publish ./campaign --to ./mirror         copy verified ZIPs to a folder

remote publishing:
  sci sign ./draft --output ./campaign         sign + FreeTSA timestamp
  sci publish ./campaign --to https://github.com/lab/research --create
  sci index https://sciscape.io                choose an index (no default)
  sci submit ./campaign --archive \\
      https://github.com/lab/research
  sci search "neural decoding" --in @index     catalog metadata, newest first
  sci pull sha256:<digest>                     download and verify completely
"""

EPILOG = (
    "Run `sci COMMAND --help` for arguments, outputs, file changes, network use, exit codes,\n"
    "and an example. Add --json to any command for a machine-readable result object.\n"
    "State lives in ~/.sci (override with SCI_HOME): config.json, key.pem, catalog.sqlite.\n"
    "Jobs on one machine can share it; keep it on a local disk, not a network mount.\n\n"
    "exit codes:\n" + "\n".join(f"  {code}  {text}" for code, text in EXIT_MEANINGS.items())
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(self, message)


class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser


def _deliver(write: Callable[[], object], out: TextIO, err: TextIO) -> None:
    """Write output. A reader that has gone away (`sci ... | head`) does not change the result."""
    try:
        write()
        out.flush()
        err.flush()
    except BrokenPipeError:
        # Point the closed streams at /dev/null so interpreter shutdown cannot fail on them.
        devnull = os.open(os.devnull, os.O_WRONLY)
        for stream in (out, err):
            with contextlib.suppress(OSError, ValueError):
                os.dup2(devnull, stream.fileno())
        os.close(devnull)


def build_parser() -> argparse.ArgumentParser:
    from .commands import COMMANDS

    parser = _Parser(
        prog="sci",
        description=DESCRIPTION + "\n" + DRAFT_GUIDE,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"sci {__version__} (pysci {pysci_version}, SCI protocol {PROTOCOL_VERSION})",
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="COMMAND", title="commands", parser_class=_Parser
    )
    for command in COMMANDS:
        add_command(subparsers, command)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    runner: Callable[[Sequence[str], float], str | None] | None = None,
    cwd: Path | None = None,
) -> int:
    """Run the CLI; return the exit code. Keyword arguments allow embedding and tests."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    wants_json = "--json" in args_list
    try:
        parsed: list[argparse.Namespace] = []

        def parse() -> None:
            with contextlib.redirect_stdout(out):
                parsed.append(parser.parse_args(args_list))

        _deliver(parse, out, err)
        if not parsed:  # --help or --version, and the reader closed the pipe
            return 0
        args = parsed[0]
    except _UsageError as exc:
        command = getattr(exc.parser, "prog", "sci").removeprefix("sci").strip() or "sci"
        if wants_json:
            result = Result(command, exit_code=Exit.USAGE)
            result.errors.append(CliError("usage", str(exc)).records[0])
            _deliver(lambda: render(result, as_json=True, stdout=out, stderr=err), out, err)
        else:
            usage_parser = exc.parser
            message = f"{usage_parser.prog}: error: {exc}\n"

            def report_usage() -> None:
                usage_parser.print_usage(err)
                err.write(message)

            _deliver(report_usage, out, err)
        return int(Exit.USAGE)
    except SystemExit as exc:  # --help and --version
        _deliver(lambda: None, out, err)
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    if args.command is None:
        _deliver(lambda: parser.print_help(out), out, err)
        return int(Exit.USAGE)
    import time

    ctx = Context(
        env=dict(os.environ if env is None else env),
        stdout=out,
        stderr=err,
        stdin=stdin or sys.stdin,
        transport=transport,
        sleep=sleep or time.sleep,
        runner=runner or run_command,
        cwd=cwd or Path.cwd(),
    )
    try:
        result = args.handler(args, ctx)
    except CliError as exc:
        result = Result(args.command_name, exit_code=exc.exit_code, data=exc.result)
        result.errors.extend(exc.records)
    except KeyboardInterrupt:
        result = Result(args.command_name, exit_code=Exit.INCOMPLETE)
        result.errors.append(
            CliError("interrupted", "interrupted; completed items were kept").records[0]
        )
    _deliver(lambda: render(result, as_json=args.json, stdout=out, stderr=err), out, err)
    return int(result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
