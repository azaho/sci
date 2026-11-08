from __future__ import annotations
import argparse
from typing import Any
from sci import config


def run(a: argparse.Namespace) -> dict[str, Any]:
    c = config.read()
    if a.url:
        from sci.clients.network import origin

        c["index"] = origin(a.url)
        config.save(c)
    return {"index": c.get("index")}


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "index",
        help="Show or save the default HTTPS index origin offline. Writes config.json only when a URL is supplied.",
        description="Show or save the default HTTPS index origin offline. Writes config.json only when a URL is supplied.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci index https://sciscape.io",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("url", nargs="?")
    c.set_defaults(handler=run)
