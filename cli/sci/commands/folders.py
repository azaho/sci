"""``sci folders``: manage the folders selected by ``@local``."""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path

from ..context import Context
from ..errors import CliError, Exit
from ..output import Result
from ..spec import Command, arg


def run(args: argparse.Namespace, ctx: Context) -> Result:
    config = ctx.config
    result = Result("folders")
    if args.action is None:
        if args.dir is not None:
            raise CliError("invalid_arguments", "use `sci folders add DIR` or `remove DIR`")
        folders = [{"path": f, "available": Path(f).is_dir()} for f in config.folders]
        result.data = {"folders": folders}
        if not folders:
            result.say("no known folders")
        for entry in folders:
            state = "available" if entry["available"] else "unavailable"
            result.say(f"{entry['path']}  ({state})")
        return result
    if args.dir is None:
        raise CliError("invalid_arguments", f"`sci folders {args.action}` requires DIR")
    if args.action == "add":
        folder = ctx.resolve(args.dir)
        if not folder.is_dir():
            raise CliError("folder_missing", f"{folder} is not an existing directory", Exit.USAGE)
        canonical = str(folder.resolve())
        changed = canonical not in config.folders
        if changed:
            config.folders.append(canonical)
            ctx.save_config()
        result.data = {"path": canonical, "status": "added" if changed else "unchanged"}
        result.say(f"{canonical}: {result.data['status']}")
        return result
    raw = ctx.resolve(args.dir)
    candidates = {str(raw.absolute())}
    with contextlib.suppress(OSError):
        candidates.add(str(raw.resolve()))
    removed = [f for f in config.folders if f in candidates]
    if removed:
        config.folders = [f for f in config.folders if f not in candidates]
        ctx.save_config()
    result.data = {
        "path": removed[0] if removed else str(raw),
        "status": "removed" if removed else "unknown",
    }
    result.say(
        f"{result.data['path']}: "
        + ("forgotten (files were not deleted)" if removed else "not a known folder")
    )
    return result


COMMAND = Command(
    name="folders",
    usage_hint="folders [add DIR | remove DIR] [--json]",
    summary="Manage the folders selected by @local.",
    description="""
Without arguments, lists known folders and whether each is available. `add`
saves an existing folder's canonical absolute path; `remove` forgets a path
without deleting any files. Folders are also remembered after an explicit
local search (`search --in DIR`) and after bundle writes by `sign --output`,
`pull --zip`, and `publish`. The computer is never scanned for campaigns.
""",
    args=(
        arg("action", metavar="ACTION", nargs="?", choices=("add", "remove"), help="add or remove"),
        arg("dir", metavar="DIR", nargs="?", help="folder path"),
    ),
    outputs="The known folders and their availability, or the change made.",
    files="`add` and `remove` update config.json; no bundle files are touched.",
    network="None.",
    exit_codes={2: "DIR is missing or not a directory"},
    examples=("sci folders add ./campaign", "sci folders"),
    run=run,
)
