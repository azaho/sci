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
  sci read ./campaign/<digest>.zip             verified metadata and README
  sci publish ./campaign --to ./mirror         copy verified ZIPs to a folder

remote publishing:
  sci sign ./draft --output ./campaign         sign + FreeTSA timestamp
  sci publish ./campaign --to https://github.com/lab/research --create
  sci index https://sciscape.io                choose an index (no default)
  sci submit ./campaign --archive \\
      https://raw.githubusercontent.com/lab/research/main/sci/archive.json
  sci search "neural decoding" --in @index     catalog metadata, newest first
  sci pull sha256:<digest>                     download and verify completely
"""

EPILOG = (
    "Run `sci COMMAND --help` for arguments, outputs, file changes, network use, exit codes,\n"
    "and an example. Add --json to any command for a machine-readable result object.\n"
    "State lives in ~/.sci (override with SCI_HOME): config.json, key.pem, catalog.sqlite.\n\n"
    "exit codes:\n" + "\n".join(f"  {code}  {text}" for code, text in EXIT_MEANINGS.items())
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(self, message)


class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser


def build_parser() -> argparse.ArgumentParser:
    from .commands import COMMANDS

    parser = _Parser(
        prog="sci",
        description=DESCRIPTION,
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
        with contextlib.redirect_stdout(out):
            args = parser.parse_args(args_list)
    except _UsageError as exc:
        command = getattr(exc.parser, "prog", "sci").removeprefix("sci").strip() or "sci"
        if wants_json:
            result = Result(command, exit_code=Exit.USAGE)
            result.errors.append(CliError("usage", str(exc)).records[0])
            render(result, as_json=True, stdout=out, stderr=err)
        else:
            exc.parser.print_usage(err)
            err.write(f"{exc.parser.prog}: error: {exc}\n")
        return int(Exit.USAGE)
    except SystemExit as exc:  # --help and --version
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    if args.command is None:
        parser.print_help(out)
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
    render(result, as_json=args.json, stdout=out, stderr=err)
    return int(result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
