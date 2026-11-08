"""Sciscape catalog queries; these responses are metadata, never bundle verification."""

from __future__ import annotations
import json
import re
from typing import Any
from urllib.parse import urlencode
from pysci import Invalid
from pysci.discovery import expand
from pysci.validation import strict_json, jwk_key, rfc3339
from sci.clients.network import Network, index
from sci.output import CommandError


def page(
    source: str, query: dict[str, Any], after: dict[str, str] | None, timeout: float
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    net = Network(timeout)
    descriptor = index(net, source)
    if descriptor.get("catalog_schema") != "sciscape_catalog_v1" or not isinstance(
        descriptor.get("catalog_url"), str
    ):
        raise CommandError("unsupported_capability", "Index has no supported catalog", 4)
    limit = query["limit"]
    params = {"order": "accepted_at.desc,id.desc", "limit": str(limit + 1)}
    if query["query"]:
        params["search_text"] = "plfts(simple)." + query["query"]
    if query["category"]:
        params["categories"] = "cs." + json.dumps(
            [dict(zip(["scheme", "code"], query["category"]))]
        )
    if query["key_x"]:
        params["public_key->>x"] = "eq." + query["key_x"]
    if query["domain"]:
        params["admission_accounts"] = "cs." + json.dumps(
            [{"type": "domain", "id": query["domain"]}]
        )
    if after:
        if not re.fullmatch(
            r"\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+]00:00)", after["time"]
        ) or not re.fullmatch("[0-9a-f]{64}", after["id"]):
            raise CommandError("cursor", "Invalid catalog cursor")
        params["or"] = (
            f"(accepted_at.lt.{after['time']},and(accepted_at.eq.{after['time']},id.lt.{after['id']}))"
        )
    raw = strict_json(
        net.request(
            descriptor["catalog_url"] + "/publications?" + urlencode(params), limit=4 * 1024 * 1024
        )
    )
    if not isinstance(raw, list) or len(raw) > limit + 1:
        raise Invalid("catalog page")
    for row in raw:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not re.fullmatch("[0-9a-f]{64}", row["id"])
        ):
            raise Invalid("catalog publication ID")
        if not all(isinstance(row.get(k), str) for k in ("title", "abstract", "accepted_at")):
            raise Invalid("catalog metadata fields")
        jwk_key(row.get("public_key"))
        rfc3339(row["accepted_at"])
    more = len(raw) > limit
    rows = raw[:limit]
    next_position = {"id": rows[-1]["id"], "time": rows[-1]["accepted_at"]} if more else None
    results = [
        {
            **row,
            "id": "sha256:" + row["id"],
            "signer": row["public_key"]["x"],
            "publication_url": expand(
                descriptor.get("publication_url", source + "/p/{id}"), id=row["id"]
            ),
        }
        for row in rows
    ]
    return results, next_position
