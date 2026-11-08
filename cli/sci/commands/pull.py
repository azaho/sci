from __future__ import annotations
from sci.storage.keys import require_signer
import argparse
import tempfile
from pathlib import Path
from typing import Any
from pysci import Invalid, load_bundle
from sci.storage.files import atomic
from sci.storage.bundles import pack
from sci.output import result


def run(args: argparse.Namespace) -> dict[str, Any]:
    from sci.clients.network import digest, retrieve

    pid = digest(args.path)
    dest = Path(args.output or pid)
    if not args.zip and dest.exists():
        if not dest.is_dir() or dest.is_symlink():
            raise Invalid("pull destination must be a directory")
        if any(dest.iterdir()):
            existing = load_bundle(dest, pid)
            require_signer(existing, args.public_key)
            return result(existing)
    b = retrieve(pid, args.archive, args.source, args.timeout)
    require_signer(b, args.public_key)
    if args.zip:
        pack(b, dest)
    else:
        if dest.is_symlink():
            raise Invalid("symlink destination")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".sci-pull-") as tmp:
            staged = Path(tmp) / "bundle"
            staged.mkdir()
            for p, raw in b.entries.items():
                atomic(staged / p, raw)
            if dest.exists():
                dest.rmdir()
            staged.rename(dest)
    return result(b)


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "pull",
        help="Retrieve and verify a complete publication online before saving it locally.",
        description="Retrieve and verify a complete publication online before saving it locally.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci pull sha256:ID --archive https://archive.example/sci/archive.json --zip --output ./campaign",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.add_argument("--output")
    c.add_argument("--public-key")
    c.add_argument("--check-timestamp-online", action="store_true")
    c.add_argument("--archive", action="append", default=[])
    c.add_argument("--in", dest="source")
    c.add_argument("--zip", action="store_true")
    c.set_defaults(handler=run)
