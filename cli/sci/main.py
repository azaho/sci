from __future__ import annotations
import argparse
import sys
from importlib.metadata import version
from typing import Any
from pysci import Invalid, Incomplete, Unsupported
from sci.output import CommandError, emit
from sci.commands import (
    check,
    license,
    sign,
    verify,
    read,
    pull,
    publish,
    submit,
    keygen,
    claim,
    revoke,
    timestamp,
    index,
    folders,
    search,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sci",
        description="SCI signed research publications (protocol 0.1 draft).",
        epilog="Local: sci keygen; sci license draft; sci sign draft --no-timestamp --output campaign; sci verify campaign/ID.zip. Online: sci publish campaign --to https://github.com/OWNER/REPO; sci submit ID --archive URL --to https://sciscape.io.",
    )
    p.add_argument("--version", action="version", version="sci-cli " + version("sci-cli"))
    sub = p.add_subparsers(dest="command", required=True)
    for command in (
        check,
        license,
        sign,
        verify,
        read,
        pull,
        publish,
        submit,
        keygen,
        claim,
        revoke,
        timestamp,
        index,
        folders,
        search,
    ):
        command.configure(sub)
    return p


def dispatch(args: argparse.Namespace) -> Any:
    return args.handler(args)


def main() -> None:
    try:
        result = dispatch(parser().parse_args())
        emit(result)
        sys.exit(result.get("exit_code", 0) if isinstance(result, dict) else 0)
    except (Invalid, Unsupported, Incomplete, CommandError, OSError, ValueError) as exc:
        code = (
            exc.exit_code
            if isinstance(exc, CommandError)
            else 4
            if isinstance(exc, Unsupported)
            else 5
            if isinstance(exc, (Incomplete, OSError))
            else 3
        )
        emit(
            {
                "error": {
                    "code": getattr(exc, "code", type(exc).__name__.lower()),
                    "message": str(exc),
                    "retryable": code == 5,
                }
            }
        )
        sys.exit(code)


if __name__ == "__main__":
    main()
