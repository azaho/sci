"""HTTP helpers shared by CLI commands: requests, retries, the instance descriptor."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from . import publication as pub
from .output import CliError

USER_AGENT = f"sci/{__version__}"


class HttpResult:
    def __init__(self, status: int, body: bytes, headers):
        self.status = status
        self.body = body
        self.headers = headers

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


def http(method: str, url: str, *, data=None, headers=None, limit: int = 1 << 20, timeout: float = 60) -> HttpResult:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(limit + 1)
            return HttpResult(resp.status, body, resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read(1 << 16) if exc.fp else b""
        return HttpResult(exc.code, body, exc.headers)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(f"cannot reach {url}: {reason}", "network_error") from exc


def http_retrying(method: str, url: str, *, attempts: int = 12, out=None, **kw) -> HttpResult:
    """Retry 503 and 429 responses (finalization in progress, rate limits)."""
    import time

    for i in range(attempts):
        r = http(method, url, **kw)
        if r.status not in (429, 503) or i == attempts - 1:
            return r
        try:
            wait = min(30, max(1, int(r.headers.get("Retry-After", "2"))))
        except (TypeError, ValueError):
            wait = 2
        if out is not None:
            out.progress(f"Server busy (HTTP {r.status}); retrying in {wait} s")
        time.sleep(wait)
    return r


def fetch_descriptor(server: str) -> dict:
    url = pub.join_url(server, "sci-server.json")
    r = http("GET", url, limit=65_536)
    if r.status != 200:
        raise CliError(f"cannot load the instance descriptor {url} (HTTP {r.status})", "descriptor_error")
    try:
        d = pub.parse_strict_json(r.body, "sci-server.json")
    except pub.PublicationError as exc:
        raise CliError(f"invalid instance descriptor {url}: {exc}", "descriptor_error") from exc
    if d.get("schema_version") != 1:
        raise CliError(f"unsupported instance descriptor version at {url}", "descriptor_error")
    for k in ("publishing_api_url", "catalog_url", "archive_url"):
        v = d.get(k)
        if not isinstance(v, str) or urllib.parse.urlsplit(v).scheme not in ("http", "https"):
            raise CliError(f"instance descriptor field {k} must be an http(s) URL", "descriptor_error")
    return d


def api_error_message(r: HttpResult) -> tuple[str, str]:
    doc = r.json()
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        e = doc["error"]
        return str(e.get("code", "error")), str(e.get("message", ""))
    return "http_error", f"HTTP {r.status}"


def encode_multipart(fields: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "sci-" + os.urandom(16).hex()
    out = bytearray()
    for name, filename, data, ctype in fields:
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
