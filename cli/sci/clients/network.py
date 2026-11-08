from __future__ import annotations
import re
import math
import random
import time
from typing import Any
from urllib.parse import urlsplit, urljoin
import httpx
from pysci import Bundle, Incomplete, Invalid, verify
from pysci import validation as v
from pysci.discovery import absolute, descriptor, expand, lookup_sources
from sci import config
from sci.output import CommandError

MAX_BYTES = 128 * 1024 * 1024


def digest(value: str) -> str:
    value = value.removeprefix("sci:").removeprefix("sha256:")
    if not re.fullmatch("[0-9a-f]{64}", value):
        raise Invalid("full lowercase SHA-256 ID required")
    return value


def origin(value: str) -> str:
    absolute(value)
    p = urlsplit(value)
    if p.scheme != "https" or p.path not in ("", "/") or p.query or p.fragment:
        raise Invalid("index must be an HTTPS origin")
    return value.rstrip("/")


class Network:
    def __init__(self, timeout: float = 120) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise CommandError("timeout", "Timeout must be positive")
        self.deadline = time.monotonic() + timeout

    def request(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        limit: int = MAX_BYTES,
    ) -> bytes:
        for attempt in range(4):
            absolute(url)
            if urlsplit(url).scheme != "https":
                raise Invalid("remote operations require HTTPS")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise Incomplete("operation deadline exceeded")
            try:
                with httpx.stream(
                    method,
                    url,
                    content=data,
                    headers=headers,
                    timeout=min(remaining, 30),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and method == "GET":
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code == 404:
                        raise CommandError("no_known_sources", "Source not found", 5)
                    if response.status_code in (429, 500, 502, 503, 504):
                        try:
                            delay = max(
                                0.0,
                                float(
                                    response.headers.get(
                                        "retry-after", 2**attempt + random.random()
                                    )
                                ),
                            )
                        except ValueError:
                            from email.utils import parsedate_to_datetime

                            delay = max(
                                0.0,
                                parsedate_to_datetime(response.headers["retry-after"]).timestamp()
                                - time.time(),
                            )
                        if not math.isfinite(delay):
                            raise Incomplete("invalid retry delay")
                        if time.monotonic() + delay >= self.deadline:
                            raise Incomplete("retry exceeds deadline")
                        time.sleep(delay)
                        continue
                    if response.status_code >= 400:
                        code = {400: 2, 403: 6, 409: 5, 413: 5, 422: 3}.get(response.status_code, 5)
                        raise CommandError("remote_refused", f"HTTP {response.status_code}", code)
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        if len(chunks) + len(chunk) > limit:
                            raise Incomplete("download budget exceeded")
                        if time.monotonic() >= self.deadline:
                            raise Incomplete("operation deadline exceeded")
                        chunks.extend(chunk)
                    return bytes(chunks)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise Incomplete("network unavailable") from exc
        raise Incomplete("request retries exhausted")


def index(network: Network, source: str | None = None) -> dict[str, Any]:
    value = source or config.read().get("index")
    if not value:
        raise CommandError("missing_index", "Configure an index with sci index URL")
    return descriptor(
        network.request(origin(value) + "/.well-known/sci/index.json", limit=65536), "index"
    )


def retrieve(value: str, archives: list[str], source: str | None, timeout: float) -> Bundle:
    pid = digest(value)
    net = Network(timeout)
    if not archives:
        desc = index(net, source)
        archives = lookup_sources(
            net.request(expand(desc["publication_lookup_url"], id=pid), limit=65536), 200
        )
    if not archives:
        raise CommandError("no_known_sources", "No known archive sources", 5)
    failures = []
    for url in archives:
        try:
            d = descriptor(net.request(url, limit=65536), "archive")
            if "bundle_url" in d:
                raw = net.request(expand(d["bundle_url"], id=pid, id2=pid[:2]))
                mb, sig, _, payload = v.check_zip(raw)
                return verify({"manifest.json": mb, "manifest.json.sig": sig, **payload}, pid)

            def entry(path: str) -> bytes:
                return net.request(expand(d["entry_url"], id=pid, id2=pid[:2], path=path))

            mb, sig = entry("manifest.json"), entry("manifest.json.sig")
            m = v.check_manifest(mb, sig)
            if v.sha(mb) != pid:
                raise Invalid("publication ID mismatch")
            if sum(e["bytes"] for e in m["files"].values()) > MAX_BYTES:
                raise Incomplete("declared bytes exceed retrieval budget")
            payload = {p: entry(p) for p in m["files"]}
            return verify({"manifest.json": mb, "manifest.json.sig": sig, **payload}, pid)
        except (Invalid, Incomplete, CommandError) as exc:
            failures.append(str(exc))
    raise Incomplete("All sources failed: " + "; ".join(failures))
