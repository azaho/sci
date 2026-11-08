from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from pysci import load_bundle
from sci import config
from sci.output import CommandError
from sci.storage.bundles import pack, candidates


def run(args: argparse.Namespace) -> dict[str, Any]:
    from sci.clients.hosting import destination

    configured = config.read().get("folders", [])
    destinations = []
    for value in args.to:
        for item in configured if value == "@local" else [value]:
            if ":" in item:
                provider, repo = destination(item)
                item = (
                    "https://github.com/" + repo
                    if provider == "github"
                    else "https://huggingface.co/datasets/" + repo
                )
            else:
                item = str(Path(item).expanduser().resolve())
            if item not in destinations:
                destinations.append(item)
    if not destinations:
        raise CommandError("empty_destinations", "No publishing destination configured")
    inputs = candidates(Path(args.path))
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in inputs:
        try:
            b = load_bundle(src)
            if b.id in seen:
                continue
            seen.add(b.id)
        except Exception as exc:
            results.append({"input": str(src), "error": str(exc)})
            continue
        for dest in destinations:
            try:
                if "://" in dest or dest.startswith("git@"):
                    from sci.clients.hosting import host

                    location = host(b, dest, args.create, args.dry_run, src)
                elif args.dry_run:
                    location = str(Path(dest) / (b.id + ".zip"))
                else:
                    pack(b, Path(dest), src)
                    location = str(Path(dest) / (b.id + ".zip"))
                results.append(
                    {"id": "sha256:" + b.id, "destination": location, "dry_run": args.dry_run}
                )
            except Exception as exc:
                results.append({"id": "sha256:" + b.id, "destination": dest, "error": str(exc)})
    return {"results": results, "exit_code": 5 if any("error" in x for x in results) else 0}


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "publish",
        help="Store verified publications in folders or hosted archives; never submits to an index. --dry-run plans without writes or uploads.",
        description="Store verified publications in folders or hosted archives; never submits to an index. --dry-run plans without writes or uploads.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci publish ./campaign --to https://github.com/OWNER/REPO --create",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.add_argument("--to", action="append", required=True)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--create", action="store_true")
    c.set_defaults(handler=run)
