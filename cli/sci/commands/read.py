from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from pysci import Invalid
from pysci.reading import read_local
from sci.output import CommandError
from sci.storage.files import atomic


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.output and not args.file:
        raise CommandError("output_requires_file", "Use --file PATH with --output FILE")
    if Path(args.path).exists():
        b = read_local(Path(args.path), file=args.file, listing=args.list)
    else:
        from sci.clients.reading import retrieve

        b = retrieve(
            args.path, args.archive, args.source, args.timeout, file=args.file, listing=args.list
        )
    out = {
        "id": "sha256:" + b.id,
        "verification": b.scope,
        "verified_files": list(b.entries),
        "baseline": "not_checked",
        "signer": b.manifest["public_key"]["x"],
        "timestamps": "not_checked",
        "associations": "not_checked",
        "revocation": "not_checked",
    }
    if args.list:
        return {**out, "files": b.manifest["files"]}
    path = args.file or "README.md"
    if path not in b.manifest["files"]:
        raise Invalid("requested path is not listed in manifest")
    raw = b.entries[path]
    if args.output:
        atomic(Path(args.output), raw)
        return {**out, "saved": args.output}
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise CommandError("binary_output", "Use --output FILE for binary data")
    lines = text.splitlines()
    if args.start_line < 1 or args.lines < 1:
        raise CommandError("line_range", "Line numbers and count must be positive")
    start, end = args.start_line - 1, args.start_line - 1 + args.lines
    return {
        **out,
        "metadata": b.metadata,
        "text": "\n".join(lines[start:end]),
        "next_line": end + 1 if end < len(lines) else None,
        "truncated": end < len(lines),
    }


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "read",
        help="Read verified local files offline, or retrieve an ID online. --output saves original file bytes.",
        description="Read verified local files offline, or retrieve an ID online. --output saves original file bytes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci read ./draft --file README.md --lines 200",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    group = c.add_mutually_exclusive_group()
    group.add_argument("--file")
    group.add_argument("--list", action="store_true")
    c.add_argument("--output")
    c.add_argument("--start-line", type=int, default=1)
    c.add_argument("--lines", type=int, default=200)
    c.add_argument("--archive", action="append", default=[])
    c.add_argument("--in", dest="source")
    c.set_defaults(handler=run)
