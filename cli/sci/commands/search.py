from __future__ import annotations
import argparse
import base64
import json
import re
from pathlib import Path
from typing import Any
from sci import config
from sci.clients import catalog
from sci.clients.network import origin
from sci.storage.catalog import Catalog
from sci.output import CommandError


def sources(values: list[str]) -> list[str]:
    settings = config.read()
    expanded: list[str] = []
    for value in values:
        if value == "@local":
            expanded.extend(settings.get("folders", []))
        elif value == "@index":
            if not settings.get("index"):
                raise CommandError("missing_index", "Configure an index")
            expanded.append(settings["index"])
        else:
            expanded.append(value)
    return list(
        dict.fromkeys(
            origin(s) if "://" in s else str(Path(s).expanduser().resolve()) for s in expanded
        )
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    options = {
        "query": args.query or "",
        "limit": args.limit if args.limit is not None else 20,
        "category": args.category,
        "key_x": args.key_x,
        "domain": args.domain,
    }
    positions: dict[str, Any] = {}
    if args.cursor:
        try:
            if len(args.cursor) > 65536:
                raise ValueError()
            token = json.loads(
                base64.urlsafe_b64decode(args.cursor + "=" * (-len(args.cursor) % 4))
            )
            if (
                set(token) != {"schema", "sources", "options", "positions"}
                or token["schema"] != "sci-search/1"
            ):
                raise ValueError()
            expanded = token["sources"]
            previous = token["options"]
            positions = token["positions"]
            if (
                not isinstance(expanded, list)
                or not all(isinstance(s, str) for s in expanded)
                or not isinstance(positions, dict)
            ):
                raise ValueError()
            if (
                not isinstance(previous, dict)
                or len(set(expanded)) != len(expanded)
                or sources(expanded) != expanded
            ):
                raise ValueError()
            if set(positions) - {*expanded, "local"}:
                raise ValueError()
            for source, position in positions.items():
                if position == "done":
                    continue
                if source == "local":
                    if not isinstance(position, str) or not re.fullmatch("[0-9a-f]{64}", position):
                        raise ValueError()
                elif (
                    not isinstance(position, dict)
                    or set(position) != {"id", "time"}
                    or not all(isinstance(v, str) for v in position.values())
                ):
                    raise ValueError()
            if set(previous) != set(options):
                raise ValueError()
            for name in options:
                explicit = getattr(args, name)
                if explicit is not None and explicit != previous[name]:
                    raise CommandError("cursor_conflict", "Cursor query or filters differ")
            if args.sources and sources(args.sources) != expanded:
                raise CommandError("cursor_conflict", "Cursor sources differ")
            options = previous
        except (ValueError, KeyError, TypeError) as exc:
            raise CommandError("cursor", "Malformed search continuation token") from exc
    else:
        expanded = sources(args.sources or ["."])
    if type(options["limit"]) is not int or not 1 <= options["limit"] <= 99:
        raise CommandError("limit", "Limit must be 1–99")
    if not isinstance(options["query"], str) or any(
        options[n] is not None and not isinstance(options[n], str) for n in ("key_x", "domain")
    ):
        raise CommandError("cursor", "Invalid search filters")
    if options["category"] is not None and (
        not isinstance(options["category"], list)
        or len(options["category"]) != 2
        or not all(isinstance(x, str) for x in options["category"])
    ):
        raise CommandError("category", "Supply scheme and code")
    local_sources = [s for s in expanded if not s.startswith("https://")]
    if options["domain"] and local_sources:
        raise CommandError("domain_scope", "--domain requires remote-only scope")
    groups: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    next_positions = dict(positions)
    more = False
    if local_sources and positions.get("local") != "done":
        local: dict[str, Any] = {}
        cache = Catalog(config.home() / "catalog.sqlite")
        try:
            for source in local_sources:
                try:
                    rows, invalid = cache.scan(Path(source))
                    errors.extend(invalid)
                    if args.sources:
                        config.remember(Path(source))
                    for row in rows:
                        metadata = row["metadata"]
                        if options["key_x"] and row["signer"] != options["key_x"]:
                            continue
                        if options["category"] and dict(
                            zip(["scheme", "code"], options["category"])
                        ) not in metadata.get("categories", []):
                            continue
                        haystack = " ".join(
                            [
                                metadata["title"],
                                metadata["abstract"],
                                *metadata.get("keywords", []),
                                row["readme"],
                            ]
                        ).casefold()
                        if not all(t in haystack for t in options["query"].casefold().split()):
                            continue
                        if row["id"] not in local:
                            local[row["id"]] = {
                                "id": "sha256:" + row["id"],
                                "title": metadata["title"],
                                "abstract": metadata["abstract"],
                                "metadata": metadata,
                                "citations": metadata.get("citations", []),
                                "signer": row["signer"],
                                "paths": [],
                            }
                        local[row["id"]]["paths"].append(row["path"])
                except Exception as exc:
                    errors.append({"source": source, "error": str(exc)})
        finally:
            cache.close()
        after = positions.get("local", "")
        if not isinstance(after, str):
            raise CommandError("cursor", "Invalid local position")
        ids = [pid for pid in sorted(local) if pid > after]
        selected = ids[: options["limit"]]
        local_more = len(ids) > len(selected)
        next_positions["local"] = selected[-1] if local_more else "done"
        more |= local_more
        groups.append(
            {
                "source": "local",
                "sources": local_sources,
                "fields": ["title", "abstract", "keywords", "README"],
                "verification": "complete",
                "results": [local[pid] for pid in selected],
                "more": local_more,
            }
        )
    for source in expanded:
        if not source.startswith("https://") or positions.get(source) == "done":
            continue
        try:
            rows, position = catalog.page(source, options, positions.get(source), args.timeout)
            next_positions[source] = position or "done"
            more |= position is not None
            groups.append(
                {
                    "source": source,
                    "fields": ["title", "abstract", "keywords"],
                    "verification": "catalog_metadata",
                    "results": rows,
                    "more": position is not None,
                }
            )
        except Exception as exc:
            errors.append({"source": source, "error": str(exc)})
    cursor = None
    if more:
        payload = {
            "schema": "sci-search/1",
            "sources": expanded,
            "options": options,
            "positions": next_positions,
        }
        cursor = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
    return {"groups": groups, "errors": errors, "cursor": cursor, "exit_code": 5 if errors else 0}


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "search",
        help="Search verified local ZIPs or online catalog metadata. Default: current folder offline. Local search updates the cache and remembers explicitly selected folders.",
        description="Search verified local ZIPs or online catalog metadata. Default: current folder offline. Local search updates the cache and remembers explicitly selected folders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci search decoding --in ./campaign --limit 20",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("query", nargs="?", default=None)
    c.add_argument("--in", dest="sources", action="append")
    c.add_argument("--limit", type=int, default=None, help="Results per group, 1–99 (default: 20)")
    c.add_argument("--category", nargs=2, metavar=("SCHEME", "CODE"))
    c.add_argument("--key-x")
    c.add_argument("--domain")
    c.add_argument("--cursor", help="Continue the same sources, query and filters")
    c.set_defaults(handler=run)
