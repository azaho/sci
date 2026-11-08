from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pysci import prepare
from pysci.bundle import read_directory


def run(args: argparse.Namespace) -> dict[str, Any]:
    # An ephemeral public key has exactly the same serialized length as the selected key.
    # Draft checks do not read a user's private key or write outputs.
    b = prepare(read_directory(Path(args.path), draft=True), Ed25519PrivateKey.generate())
    return {"valid": True, "baseline": b.baseline, "planned_zip_bytes": len(b.zip())}


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "check",
        help="Validate a draft offline; writes nothing and reads no private key.",
        description="Validate a draft offline; writes nothing and reads no private key.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci check ./draft",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.set_defaults(handler=run)
