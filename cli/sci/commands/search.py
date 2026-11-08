"""``sci search``: search local campaigns, remote catalogs, or both."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..clients.index import IndexClient
from ..config import CATALOG_FILE, normalize_origin
from ..context import Context
from ..errors import CliError, ErrorRecord, Exit
from ..output import Result
from ..spec import Command, arg
from ..storage.catalog import SEARCHED_FIELDS, Catalog, LocalHit, ScanReport, is_corrupt
from ..storage.local_publish import remember_folder

CURSOR_VERSION = 1


def _encode_cursor(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(token: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        state = json.loads(raw)
    except (binascii.Error, ValueError):
        raise CliError("invalid_cursor", "--cursor is not a continuation token") from None
    if not isinstance(state, dict) or state.get("v") != CURSOR_VERSION:
        raise CliError("invalid_cursor", "--cursor is not a continuation token from this version")
    return state


def _resolve_sources(
    ctx: Context, values: list[str] | None
) -> tuple[list[str], list[str], list[str]]:
    """Return (folders, index origins, explicitly named folders to remember)."""
    folders: list[str] = []
    origins: list[str] = []
    explicit: list[str] = []
    if not values:
        return [str(ctx.cwd.resolve())], [], []
    for value in values:
        if value == "@local":
            for folder in ctx.config.folders:
                if folder not in folders:
                    folders.append(folder)
        elif value == "@index":
            if not ctx.config.index:
                raise CliError(
                    "missing_index", "@index requires a configured index (`sci index URL`)"
                )
            if ctx.config.index not in origins:
                origins.append(ctx.config.index)
        elif "://" in value:
            origin = normalize_origin(value)
            if origin not in origins:
                origins.append(origin)
        elif value.startswith("@"):
            raise CliError("invalid_source", f"unknown source {value!r}; use @local or @index")
        else:
            path = ctx.resolve(value)
            folder = str(path.resolve()) if path.exists() else str(path.absolute())
            if folder not in folders:
                folders.append(folder)
            if path.is_dir():
                explicit.append(folder)
    return folders, origins, explicit


def run(args: argparse.Namespace, ctx: Context) -> Result:
    category = tuple(args.category) if args.category else None
    request: dict[str, Any] = {
        "q": args.query,
        "category": list(category) if category else None,
        "key_x": args.key_x,
        "domain": args.domain,
        "limit": args.limit,
    }
    positions: dict[str, Any] = {}
    explicit: list[str] = []
    if args.cursor:
        state = _decode_cursor(args.cursor)
        for name, value in request.items():
            limit_given = getattr(args, "limit_given", False)
            given = value if name != "limit" else (value if limit_given else None)
            if given is not None and given != state.get(name):
                raise CliError(
                    "cursor_conflict",
                    f"--cursor continues an earlier search; {name} differs from that search",
                )
            request[name] = state.get(name)
        if args.in_:
            folders_now, origins_now, _ = _resolve_sources(ctx, args.in_)
            if folders_now != state["folders"] or origins_now != state["origins"]:
                raise CliError("cursor_conflict", "--cursor continues different sources")
        folders, origins = list(state["folders"]), list(state["origins"])
        positions = dict(state.get("positions", {}))
    else:
        folders, origins, explicit = _resolve_sources(ctx, args.in_)
    query: str | None = request["q"]
    limit: int = request["limit"]
    if limit < 1:
        raise CliError("invalid_arguments", "--limit must be positive")
    if not query and not (request["category"] or request["key_x"] or request["domain"]):
        raise CliError("missing_query", "supply QUERY or a filter (--category, --key-x, --domain)")
    if request["domain"] and folders:
        raise CliError("invalid_arguments", "--domain requires remote-only scope (index sources)")
    if not folders and not origins:
        raise CliError("no_sources", "no sources selected (no known folders for @local?)")
    category_pair = tuple(request["category"]) if request["category"] else None

    result = Result("search")
    groups: list[dict[str, Any]] = []
    next_positions: dict[str, Any] = {}
    partial = False

    if folders and positions.get("local", "") != "done":
        group, after, more, group_partial = _local(
            ctx, folders, query, category_pair, request["key_x"], positions.get("local"), limit
        )
        groups.append(group)
        partial = partial or group_partial
        if more:
            next_positions["local"] = after
        for folder in explicit:
            if remember_folder(ctx.config, Path(folder)):
                ctx.save_config()
    for origin in origins:
        key = "index:" + origin
        if positions.get(key, "") == "done":
            continue
        group, after_remote, more = _remote(
            ctx, args, origin, query, category_pair, request, positions.get(key), limit
        )
        groups.append(group)
        if group.get("error"):
            partial = True
            error = group["error"]
            result.errors.append(ErrorRecord(error["code"], error["message"], error["retryable"]))
        if more:
            next_positions[key] = after_remote
    next_cursor = None
    if next_positions:
        state = {
            "v": CURSOR_VERSION,
            **request,
            "folders": folders,
            "origins": origins,
            "positions": {
                **dict.fromkeys(["local", *("index:" + o for o in origins)], "done"),
                **next_positions,
            },
        }
        if not folders:
            state["positions"].pop("local", None)
        next_cursor = _encode_cursor(state)
    result.data = {
        "query": query,
        "filters": {
            "category": request["category"],
            "key_x": request["key_x"],
            "domain": request["domain"],
        },
        "limit": limit,
        "groups": groups,
        "next_cursor": next_cursor,
        "partial": partial,
    }
    _describe(result, groups, next_cursor)
    if partial:
        failed = [g for g in groups if g.get("error")]
        if failed and len(failed) == len(groups) and len({g["error_exit"] for g in failed}) == 1:
            result.exit_code = failed[0]["error_exit"]
        else:
            result.exit_code = Exit.INCOMPLETE
            result.partial = True
    for group in groups:
        group.pop("error_exit", None)
    return result


def _local(
    ctx: Context,
    folders: list[str],
    query: str | None,
    category: tuple[str, ...] | None,
    key_x: str | None,
    after: str | None,
    limit: int,
) -> tuple[dict[str, Any], str | None, bool, bool]:
    terms = query.casefold().split() if query else []
    pair = (category[0], category[1]) if category else None

    def scan_and_search(catalog: Catalog) -> tuple[ScanReport, list[LocalHit], bool]:
        scan = catalog.scan([Path(f) for f in folders])
        hits, more = catalog.search(
            scan.available, terms, category=pair, key_x=key_x, after=after, limit=limit
        )
        return scan, hits, more

    try:
        catalog = Catalog(ctx.home / CATALOG_FILE)
        try:
            try:
                scan, hits, more = scan_and_search(catalog)
            except sqlite3.DatabaseError as exc:
                if not is_corrupt(exc):
                    raise
                catalog.reset()
                scan, hits, more = scan_and_search(catalog)
        finally:
            catalog.close()
    except sqlite3.Error as exc:
        raise CliError(
            "catalog_unavailable",
            f"the local search cache could not be used: {exc}",
            Exit.INCOMPLETE,
            retryable=True,
            file=str(ctx.home / CATALOG_FILE),
        ) from exc
    group: dict[str, Any] = {
        "source": "local",
        "kind": "local",
        "folders": folders,
        "searched_fields": list(SEARCHED_FIELDS),
        "verification": "complete bundle checks (§6.4) on each ZIP before indexing",
        "results": [hit.to_json() for hit in hits],
        "more": more,
        "invalid_candidates": scan.invalid,
        "unavailable_folders": scan.unavailable,
        "cache": {"verified": scan.verified, "reused": scan.reused, "rebuilt": catalog.rebuilt},
        "partial": bool(scan.unavailable),
    }
    last = hits[-1].publication_id if hits else after
    return group, last, more, bool(scan.unavailable)


def _remote(
    ctx: Context,
    args: argparse.Namespace,
    origin: str,
    query: str | None,
    category: tuple[str, ...] | None,
    request: dict[str, Any],
    after: Any,
    limit: int,
) -> tuple[dict[str, Any], Any, bool]:
    group: dict[str, Any] = {
        "source": origin,
        "kind": "index",
        "searched_fields": ["title", "abstract", "keywords"],
        "verification": "catalog metadata from the index; files were not retrieved or verified",
        "results": [],
        "more": False,
    }
    client = IndexClient(ctx.http, origin)
    try:
        rows, more, _ = client.search(
            ctx.deadline(args.timeout),
            query=query,
            category=(category[0], category[1]) if category else None,
            domain=request["domain"],
            key_x=request["key_x"],
            after=(after[0], after[1]) if isinstance(after, list) else None,
            limit=limit,
        )
    except CliError as exc:
        group["error"] = exc.records[0].to_json()
        group["error_exit"] = int(exc.exit_code)
        group["partial"] = True
        return group, None, False
    group["results"] = rows
    group["more"] = more
    last = [rows[-1]["accepted_at"], rows[-1]["id"].removeprefix("sha256:")] if rows else None
    return group, last, more


def _describe(result: Result, groups: list[dict[str, Any]], cursor: str | None) -> None:
    for group in groups:
        if group["kind"] == "local":
            result.say(f"== local: {', '.join(group['folders'])}")
        else:
            result.say(f"== {group['source']} (catalog metadata, not verified locally)")
        for hit in group["results"]:
            result.say(f"{hit['id']}  {hit['title']}")
            if group["kind"] == "local":
                for path in hit["paths"]:
                    result.say(f"    {path}")
            else:
                when = hit.get("accepted_at")
                page = hit.get("publication_url") or ""
                result.say(f"    accepted {when}  {page}".rstrip())
        if not group["results"]:
            result.say("(no results)")
        for candidate in group.get("invalid_candidates", []):
            first = candidate["issues"][0]["message"] if candidate["issues"] else ""
            result.say(f"invalid: {candidate['path']} ({candidate['status']}) {first}".rstrip())
        for folder in group.get("unavailable_folders", []):
            result.say(f"unavailable: {folder['folder']} ({folder['error']})")
        if group.get("error"):
            result.say(f"error: {group['error']['message']}")
        if group.get("more"):
            result.say("(more results available)")
    if cursor:
        result.say(f"continue with: --cursor {cursor}")


class _LimitAction(argparse.Action):
    def __call__(self, parser: Any, namespace: Any, values: Any, option: Any = None) -> None:
        setattr(namespace, self.dest, values)
        namespace.limit_given = True


COMMAND = Command(
    name="search",
    usage_hint=(
        "search [QUERY] [--in SOURCE ...] [--category SCHEME CODE] [--key-x X] [--domain DOMAIN] "
        "[--limit N] [--cursor TOKEN] [--json]"
    ),
    summary="Search local campaigns, remote catalogs, or both.",
    description="""
Sources (repeat --in to combine; duplicates are merged):
  DIR          immediate *.zip children of that folder
  https://...  catalog discovered through that index's descriptor
  @local       known folders (see `sci folders`)
  @index       the configured index
Without --in, the current folder is searched offline.

Local search verifies new or changed ZIPs completely (SCI protocol §6.4),
caches their metadata and README text in catalog.sqlite (keyed by verified
publication ID), and matches all case-folded query terms in title,
abstract, keywords, and README. Results are ordered by publication ID;
invalid candidates are reported separately. Remote search queries the index
catalog (Sciscape catalog_url, sciscape_catalog_v1) newest admissions first
and labels results as catalog metadata; nothing is downloaded or verified.
An archive without a catalog is not searchable. If a source fails, available
results are returned and marked partial.
""",
    args=(
        arg("query", metavar="QUERY", nargs="?", help="search terms (optional with a filter)"),
        arg(
            "--in",
            dest="in_",
            metavar="SOURCE",
            action="append",
            help="source to search (repeatable): folder, https:// index origin, @local, or @index",
        ),
        arg(
            "--category",
            nargs=2,
            metavar=("SCHEME", "CODE"),
            help="exact category filter, e.g. arxiv cs.LG",
        ),
        arg("--key-x", metavar="X", help="signer public key x filter"),
        arg("--domain", metavar="DOMAIN", help="admission-time domain association (remote only)"),
        arg(
            "--limit",
            type=int,
            default=20,
            metavar="N",
            action=_LimitAction,
            help="results per group",
        ),
        arg(
            "--cursor",
            metavar="TOKEN",
            help="continue a previous search (same sources, query, and filters)",
        ),
    ),
    outputs="""One local group and one group per index: source, searched fields, verification
status, results (ID, title, abstract, signer; local paths and citations, or
admission time and publication URL), and a continuation token.""",
    files="""Updates the search cache <state>/catalog.sqlite; remembers folders named with
--in in config.json.""",
    network="None for local sources. Index sources: descriptor and catalog GET requests.",
    exit_codes={
        4: "an index has no supported catalog (reported in its group)",
        5: "partial result: a source failed or a folder was unavailable",
    },
    examples=(
        'sci search "neural decoding" --in ./campaign-a --in ./campaign-b',
        'sci search "neural decoding" --in @local --in https://sciscape.io',
    ),
    run=run,
    timeout=True,
)
