from __future__ import annotations
import argparse
from typing import Any


def run(args: argparse.Namespace) -> dict[str, Any]:
    from sci.clients.tsa import timestamp

    return timestamp(args)


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "timestamp",
        help="Obtain a timestamp online or submit existing publication proofs. Append sidecars atomically without altering signed bytes.",
        description="Obtain a timestamp online or submit existing publication proofs. Append sidecars atomically without altering signed bytes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci timestamp ./draft",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    action = c.add_mutually_exclusive_group()
    action.add_argument("--tsa", help="TSA URL; default configured TSA or https://freetsa.org/tsr")
    action.add_argument("--submit", action="store_true")
    c.add_argument("--to")
    c.set_defaults(handler=run)
