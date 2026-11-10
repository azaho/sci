"""``sci index``: show or save the default index origin."""

from __future__ import annotations

import argparse

from ..config import CONFIG_FILE, normalize_origin
from ..context import Context
from ..output import Result
from ..spec import Command, arg


def run(args: argparse.Namespace, ctx: Context) -> Result:
    result = Result("index")
    config = ctx.config
    if args.url is None:
        result.data = {"index": config.index, "config": str(ctx.home / CONFIG_FILE)}
        result.say(config.index or "no index configured (set one with `sci index URL`)")
        return result
    origin = normalize_origin(args.url)
    changed = config.index != origin
    config.index = origin
    if changed:
        ctx.save_config()
    result.data = {
        "index": origin,
        "status": "saved" if changed else "unchanged",
        "config": str(ctx.home / CONFIG_FILE),
    }
    result.say(f"index: {origin} ({'saved' if changed else 'unchanged'})")
    return result


COMMAND = Command(
    name="index",
    usage_hint="index [URL] [--json]",
    summary="Show or save the default index origin.",
    description="""
Without URL, shows the configured index. With URL, saves that HTTPS origin
(for example https://sciscape.io) as the default for `pull`, `read`,
`submit`, `timestamp --submit`, `revoke --submit`, and `search --in @index`.
The origin is not contacted. No public index is selected by default.
""",
    args=(arg("url", metavar="URL", nargs="?", help="HTTPS index origin to save"),),
    outputs="The configured index origin.",
    files="With URL, updates config.json in the state directory (~/.sci or $SCI_HOME).",
    network="None.",
    exit_codes={2: "URL is not an HTTPS origin, or config.json is invalid"},
    examples=("sci index https://sciscape.io",),
    run=run,
)
