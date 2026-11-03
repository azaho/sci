"""Request reading, multipart parsing, client identification, and rate limiting."""

from __future__ import annotations

import threading
import time

from starlette.requests import Request

from .errors import ApiError


class RateLimiter:
    """Per-client token bucket for publishing requests (in memory, per process)."""

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, client: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(client, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self.buckets[client] = (tokens - 1, now)
                ok, retry = True, 0
            else:
                self.buckets[client] = (tokens, now)
                ok, retry = False, int((1 - tokens) / self.rate) + 1
            if len(self.buckets) > 10_000:
                cutoff = now - 600
                self.buckets = {k: v for k, v in self.buckets.items() if v[1] > cutoff}
        return ok, retry



async def read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "too_large", f"The request body exceeds {limit} bytes.")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "too_large", f"The request body exceeds {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)



def parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    from python_multipart.multipart import MultipartParser, parse_options_header

    ctype, params = parse_options_header(content_type)
    if ctype != b"multipart/form-data" or b"boundary" not in params:
        raise ApiError(400, "invalid_request", "Use multipart/form-data with fields sci, manifest, and signature.")
    parts: dict[str, bytes] = {}
    state = {"headers": {}, "name": None, "data": [], "hname": b"", "hvalue": b""}

    def on_part_begin():
        state["headers"] = {}
        state["data"] = []

    def on_header_field(data, start, end):
        state["hname"] += data[start:end]

    def on_header_value(data, start, end):
        state["hvalue"] += data[start:end]

    def on_header_end():
        state["headers"][state["hname"].lower()] = state["hvalue"]
        state["hname"], state["hvalue"] = b"", b""

    def on_part_data(data, start, end):
        state["data"].append(data[start:end])

    def on_part_end():
        disp = state["headers"].get(b"content-disposition", b"")
        _, dparams = parse_options_header(disp)
        name = dparams.get(b"name", b"").decode("utf-8", "replace")
        if name not in ("sci", "manifest", "signature"):
            raise ApiError(400, "invalid_request", f"Unexpected form field {name!r}; send only sci, manifest, and signature.")
        if name in parts:
            raise ApiError(400, "invalid_request", f"Form field {name!r} appears more than once.")
        parts[name] = b"".join(state["data"])

    parser = MultipartParser(
        params[b"boundary"],
        callbacks={
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
    try:
        parser.write(body)
        parser.finalize()
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(400, "invalid_request", f"Malformed multipart body: {exc}") from exc
    missing = [n for n in ("sci", "manifest", "signature") if n not in parts]
    if missing:
        raise ApiError(400, "invalid_request", f"Missing form field(s): {', '.join(missing)}.")
    return parts



def client_id(request: Request, header: str | None) -> str:
    if header:
        v = request.headers.get(header)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

