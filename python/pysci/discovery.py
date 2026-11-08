from __future__ import annotations
import re
from typing import Any
from urllib.parse import urlsplit, unquote, quote
from .validation import Invalid, check_schema, strict_json, exact_keys


def absolute(value: Any) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise Invalid("absolute URL required")
    try:
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise Invalid("absolute URL without credentials required")
        _ = parsed.port
    except ValueError as exc:
        raise Invalid("invalid URL") from exc
    if re.search(r"%(?![a-fA-F0-9]{2})", value) or "\\" in value:
        raise Invalid("invalid URL encoding")
    return value


def descriptor(raw: bytes, kind: str) -> dict[str, Any]:
    o = strict_json(raw)
    check_schema(o, "sci-" + kind + "/0.1")
    rules = (
        {
            "publication_lookup_url": {"id"},
            "revocation_lookup_url": {"x", "file"},
            "timestamp_lookup_url": {"id"},
            "submit_url": set(),
            "policy_url": set(),
            "docs_url": set(),
        }
        if kind == "index"
        else {
            "bundle_url": {"id", "id2"},
            "entry_url": {"id", "id2", "+path"},
            "blob_url": {"sha256"},
            "list_url": set(),
        }
    )
    if (
        kind == "index"
        and not {"publication_lookup_url", "revocation_lookup_url", "timestamp_lookup_url"}
        <= o.keys()
    ):
        raise Invalid("missing index route")
    if kind == "archive" and not ("bundle_url" in o or "entry_url" in o):
        raise Invalid("archive has no manifest route")
    for name, allowed in rules.items():
        if name not in o:
            continue
        value = o[name]
        if not isinstance(value, str):
            raise Invalid("template must be string")
        expressions = re.findall(r"\{([^{}]*)\}", value)
        plain = re.sub(r"\{[^{}]*\}", "x", value)
        if "{" in plain or "}" in plain or not set(expressions) <= allowed:
            raise Invalid("unsupported template expression")
        if kind == "index" and not allowed <= set(expressions):
            raise Invalid("missing required expression")
        absolute(plain)
    return dict(o)


def expand(template: str, **values: str) -> str:
    def replace(match: re.Match[str]) -> str:
        expr = match[1]
        value = values[expr.lstrip("+")]
        return quote(value, safe="/._-" if expr.startswith("+") else "")

    return re.sub(r"\{([^{}]+)\}", replace, template)


def submission(raw: bytes) -> dict[str, Any]:
    o = strict_json(raw)
    exact_keys(o, ["id", "descriptors"], ["account"])
    if not isinstance(o["id"], str) or not re.fullmatch("[0-9a-f]{64}", o["id"]):
        raise Invalid("bare publication digest required")
    urls = o["descriptors"]
    if not isinstance(urls, list) or not urls:
        raise Invalid("nonempty descriptors required")
    for value in urls:
        absolute(value)
    if len(set(urls)) != len(urls):
        raise Invalid("duplicate descriptor")
    if "account" in o:
        exact_keys(o["account"], ["type", "id"])
        if not all(isinstance(v, str) for v in o["account"].values()):
            raise Invalid("account strings required")
    return dict(o)


def citation_key(target: str) -> str:
    if target.startswith("doi:"):
        return "doi:" + unquote(target[4:], errors="strict").casefold()
    return target


def submission_response(
    raw: bytes, status: int, content_type: str, requested_id: str
) -> dict[str, Any]:
    if content_type.split(";", 1)[0] != "application/json":
        raise Invalid("submission response content type")
    o = strict_json(raw)
    if not isinstance(o, dict):
        raise Invalid("submission response object")
    if status in (200, 201):
        if o.get("id") != requested_id:
            raise Invalid("submission response ID")
    else:
        if not 400 <= status <= 599:
            raise Invalid("submission response status")
        error = o.get("error")
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
            or type(error.get("retryable")) is not bool
        ):
            raise Invalid("submission error contract")
    return dict(o)


def lookup_sources(raw: bytes, status: int) -> list[str]:
    if status == 404:
        return []
    if status != 200:
        from .validation import Incomplete

        raise Incomplete("publication lookup unavailable")
    o = strict_json(raw)
    if not isinstance(o, dict) or not isinstance(o.get("descriptors"), list):
        raise Invalid("lookup descriptor array")
    return [absolute(url) for url in o["descriptors"]]
