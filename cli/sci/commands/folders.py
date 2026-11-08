from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from pysci import Invalid
from sci import config
from sci.output import CommandError


def run(a: argparse.Namespace) -> dict[str, Any]:
    if bool(a.action) != bool(a.folder):
        raise CommandError("folder_required", "Supply an action and folder together")
    c = config.read()
    if a.action:
        path = Path(a.folder).expanduser().resolve()
        if a.action == "add":
            if not path.is_dir():
                raise Invalid("folder does not exist")
            config.remember(path)
        else:
            c["folders"] = [f for f in c.get("folders", []) if f != str(path)]
            config.save(c)
    return {
        "folders": [
            {"path": f, "available": Path(f).is_dir()} for f in config.read().get("folders", [])
        ]
    }


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "folders",
        help="List, add or forget local campaign folders offline; never deletes publication files.",
        description="List, add or forget local campaign folders offline; never deletes publication files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci folders add ./campaign",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("action", nargs="?", choices=["add", "remove"])
    c.add_argument("folder", nargs="?")
    c.set_defaults(handler=run)
