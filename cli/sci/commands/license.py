from __future__ import annotations
import argparse
from importlib.resources import files
from pathlib import Path
from pysci import Invalid
from sci.storage.files import atomic


def run(args: argparse.Namespace) -> dict[str, str]:
    root = Path(args.path)
    if not root.is_dir() or (root / "manifest.json").exists():
        raise Invalid("license requires an unsigned draft directory")
    atomic(root / "LICENSE", files("pysci").joinpath("sci-baseline-LICENSE.txt").read_bytes())
    return {"license": str(root / "LICENSE")}


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "license",
        help="Install the baseline license in an unsigned draft offline.",
        description="Install the baseline license in an unsigned draft offline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci license ./draft",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.set_defaults(handler=run)
