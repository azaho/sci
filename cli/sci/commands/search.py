from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from pysci import load_bundle, Invalid
from pysci.validation import strict_json
from sci import config
from sci.clients.network import Network, index
from sci.output import CommandError


def search(args: argparse.Namespace) -> dict[str, Any]:
    sources = args.sources or ["."]
    c = config.read()
    expanded = []
    for s in sources:
        if s == "@local":
            expanded.extend(c.get("folders", []))
        elif s == "@index":
            if not c.get("index"):
                raise CommandError("missing_index", "Configure an index")
            expanded.append(c["index"])
        else:
            expanded.append(s)
    expanded = list(
        dict.fromkeys(s if s.startswith("https://") else str(Path(s).resolve()) for s in expanded)
    )
    if args.limit < 1 or args.limit > 1000:
        raise CommandError("limit", "Limit must be 1–1000")
    if args.domain and any(not s.startswith("https://") for s in expanded):
        raise CommandError("domain_scope", "--domain requires remote-only scope")
    groups = []
    local: dict[str, Any] = {}
    errors = []
    config.home().mkdir(parents=True, exist_ok=True)
    cache = config.home() / "catalog.sqlite"
    try:
        db = sqlite3.connect(cache)
        db.execute("pragma quick_check").fetchall()
        db.execute(
            "create table if not exists bundles(path text primary key, identity text, data text)"
        )
    except sqlite3.DatabaseError:
        if cache.exists():
            cache.rename(cache.with_suffix(".corrupt"))
        db = sqlite3.connect(cache)
        db.execute("create table bundles(path text primary key, identity text, data text)")
    try:
        for source in expanded:
            try:
                if source.startswith("https://"):
                    net = Network(args.timeout)
                    d = index(net, source)
                    if d.get("catalog_schema") != "sciscape_catalog_v1":
                        raise CommandError("unsupported_capability", "Unknown catalog schema", 4)
                    params = {"order": "accepted_at.desc,id.desc", "limit": str(args.limit)}
                    if args.query:
                        params["search_text"] = "plfts(simple)." + args.query
                    if args.category:
                        params["categories"] = "cs." + json.dumps(
                            [dict(zip(["scheme", "code"], args.category))]
                        )
                    if args.key_x:
                        params["public_key->>x"] = "eq." + args.key_x
                    if args.domain:
                        params["admission_accounts"] = "cs." + json.dumps(
                            [{"type": "domain", "id": args.domain}]
                        )
                    rows = strict_json(
                        net.request(
                            d["catalog_url"] + "/publications?" + urlencode(params),
                            limit=4 * 1024 * 1024,
                        )
                    )
                    if not isinstance(rows, list):
                        raise Invalid("catalog response must be an array")
                    groups.append(
                        {"source": source, "verification": "catalog_metadata", "results": rows}
                    )
                    continue
                folder = Path(source)
                if not folder.is_dir():
                    raise Invalid("folder unavailable")
                current = []
                for path in sorted(folder.glob("*.zip")):
                    current.append(str(path))
                    try:
                        st = path.lstat()
                        identity = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
                        row = db.execute(
                            "select data from bundles where path=? and identity=?",
                            (str(path), identity),
                        ).fetchone()
                        if row:
                            obj = json.loads(row[0])
                        else:
                            b = load_bundle(path)
                            obj = {
                                "id": b.id,
                                "metadata": b.metadata,
                                "signer": b.signer,
                                "readme": b.entries["README.md"].decode(),
                            }
                            with db:
                                db.execute(
                                    "insert or replace into bundles values(?,?,?)",
                                    (str(path), identity, json.dumps(obj)),
                                )
                        if args.key_x and obj["signer"] != args.key_x:
                            continue
                        if args.category and dict(
                            zip(["scheme", "code"], args.category)
                        ) not in obj["metadata"].get("categories", []):
                            continue
                        haystack = " ".join(
                            [
                                obj["metadata"]["title"],
                                obj["metadata"]["abstract"],
                                *obj["metadata"].get("keywords", []),
                                obj["readme"],
                            ]
                        ).casefold()
                        if not all(t in haystack for t in args.query.casefold().split()):
                            continue
                        if obj["id"] in local:
                            local[obj["id"]]["paths"].append(str(path))
                        else:
                            local[obj["id"]] = {
                                "id": obj["id"],
                                "metadata": obj["metadata"],
                                "signer": obj["signer"],
                                "paths": [str(path)],
                            }
                    except Exception as exc:
                        errors.append({"path": str(path), "error": str(exc)})
                with db:
                    for (p,) in db.execute("select path from bundles").fetchall():
                        if Path(p).parent == folder and p not in current:
                            db.execute("delete from bundles where path=?", (p,))
                if args.sources:
                    config.remember(folder)
            except Exception as exc:
                errors.append({"source": source, "error": str(exc)})
    finally:
        db.close()
    if any(not s.startswith("https://") for s in expanded):
        groups.insert(
            0,
            {
                "source": "local",
                "verification": "complete",
                "results": [local[k] for k in sorted(local)[: args.limit]],
                "more": len(local) > args.limit,
            },
        )
    return {"groups": groups, "errors": errors, "exit_code": 5 if errors else 0}
