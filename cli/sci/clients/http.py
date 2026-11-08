"""HTTP with injectable transports, operation deadlines, bounded retries, and pacing.

The default transport uses :mod:`urllib.request`. Tests and embedding
programs pass any object implementing :class:`Transport`.
"""

from __future__ import annotations

import contextlib
import email.utils
import io
import json
import os
import random
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from pysci.errors import SciError
from pysci.jsonstrict import JsonValue, parse_json

from ..errors import CliError, Exit

READ_CHUNK = 1 << 16
DEFAULT_TIMEOUT = 120.0
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 6
BACKOFF_BASE = 0.5
BACKOFF_CAP = 20.0


class Stream(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | Path | None = None
    unredirected: dict[str, str] = field(default_factory=dict)
    """Headers (e.g. credentials) that must not follow redirects to other hosts."""

    def body_bytes(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, Path):
            return self.body.read_bytes()
        return self.body


@dataclass(slots=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    """Lower-cased header names."""
    stream: Stream
    url: str = ""


class TransportError(Exception):
    """The request could not be completed (connection, DNS, TLS, or timeout)."""

    def __init__(self, message: str, *, sent: bool = True) -> None:
        super().__init__(message)
        self.sent = sent


class Transport(Protocol):
    def send(self, request: HttpRequest, timeout: float) -> RawResponse: ...


class UrllibTransport:
    """Transport over :mod:`urllib.request`. Follows redirects for GET/HEAD.

    Credentials go into unredirected headers, so they are not forwarded to a
    redirect target.
    """

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener()

    def send(self, request: HttpRequest, timeout: float) -> RawResponse:
        data: Any = None
        headers = dict(request.headers)
        if isinstance(request.body, Path):
            data = open(request.body, "rb")  # noqa: SIM115 - closed below
            headers["Content-Length"] = str(request.body.stat().st_size)
        elif request.body is not None:
            data = request.body
        req = urllib.request.Request(request.url, data=data, headers=headers, method=request.method)
        for name, value in request.unredirected.items():
            req.add_unredirected_header(name, value)
        try:
            response = self._opener.open(req, timeout=max(timeout, 0.001))
            return RawResponse(
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response,
                response.url,
            )
        except urllib.error.HTTPError as exc:
            return RawResponse(
                exc.code,
                {k.lower(): v for k, v in (exc.headers or {}).items()},
                exc if exc.fp is not None else io.BytesIO(b""),
                request.url,
            )
        except (TimeoutError, urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            sent = not isinstance(reason, socket.gaierror | ConnectionRefusedError)
            raise TransportError(f"{type(reason).__name__}: {reason}", sent=sent) from None
        finally:
            if hasattr(data, "close"):
                data.close()


# --------------------------------------------------------------------------- deadline


class Deadline:
    """An operation deadline including retries (``--timeout``)."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return self._end - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self, what: str) -> float:
        left = self.remaining()
        if left <= 0:
            raise CliError(
                "deadline_exceeded",
                f"operation deadline of {self.seconds:g}s exceeded during {what}",
                Exit.INCOMPLETE,
                retryable=True,
            )
        return left


# --------------------------------------------------------------------------- pacing


class Pacer:
    """At most one paced request per ``interval`` seconds across processes.

    Applies to timestamp authority requests and index writes from this
    machine (FreeTSA usage policy: one request per second). The last request
    time is kept in ``<state>/pace`` under an advisory lock.
    """

    interval: float = 1.0

    def __init__(
        self,
        path: Path | None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._sleep = sleep
        self._wall = wall
        self._last = 0.0

    def wait(self) -> None:
        if self.path is None:
            self._wait_local()
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self._wait_local()
            return
        try:
            _lock(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64)
            try:
                last = float(raw.decode("ascii").strip() or 0)
            except ValueError:
                last = 0.0
            last = max(last, self._last)
            delay = last + self.interval - self._wall()
            if delay > 0:
                self._sleep(min(delay, self.interval))
            now = f"{self._wall():.6f}".encode("ascii")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, now)
            self._last = self._wall()
        finally:
            os.close(fd)

    def _wait_local(self) -> None:
        delay = self._last + self.interval - self._wall()
        if delay > 0:
            self._sleep(min(delay, self.interval))
        self._last = self._wall()


def _lock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: in-process pacing only
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


# --------------------------------------------------------------------------- client


@dataclass(slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    attempts: int = 1

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def content_type(self) -> str:
        return (self.header("content-type") or "").split(";", 1)[0].strip().lower()

    def json(self, what: str) -> JsonValue:
        try:
            return parse_json(self.body, file=what)
        except SciError as exc:
            raise CliError(
                "invalid_response",
                f"{what} is not valid JSON: {exc.issue.requirement}",
                Exit.INVALID,
                url=self.url,
            ) from None

    def json_object(self, what: str) -> dict[str, JsonValue]:
        value = self.json(what)
        if not isinstance(value, dict):
            raise CliError(
                "invalid_response", f"{what} must be a JSON object", Exit.INVALID, url=self.url
            )
        return value


class ResponseTooLarge(Exception):
    pass


class NetworkError(CliError):
    """A request failed at the transport level after the permitted retries."""

    def __init__(self, url: str, message: str, *, lost_response: bool) -> None:
        super().__init__(
            "network_error",
            f"request to {_redact(url)} failed: {message}",
            Exit.INCOMPLETE,
            retryable=True,
            url=_redact(url),
        )
        self.lost_response = lost_response


def _redact(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class HttpClient:
    """Requests with deadlines, retries (backoff, jitter, Retry-After), and limits."""

    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str,
        pacer: Pacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.transport = transport
        self.user_agent = user_agent
        self.pacer = pacer or Pacer(None, sleep=sleep)
        self.sleep = sleep
        self.rng = rng or random.Random(secrets.randbits(64))

    def request(
        self,
        method: str,
        url: str,
        *,
        deadline: Deadline,
        headers: Mapping[str, str] | None = None,
        body: bytes | Path | None = None,
        auth: str | None = None,
        max_bytes: int = 1 << 20,
        sink: BinaryIO | None = None,
        retry: bool = True,
        paced: bool = False,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
        should_retry: Callable[[Response], bool] | None = None,
    ) -> Response:
        """Send a request; return the final response (any status).

        Temporary failures (transport errors and ``retry_statuses``) are
        retried with bounded exponential backoff and jitter, honouring
        ``Retry-After``, until the attempt limit or the deadline. Policy and
        validation refusals (other statuses) are returned without retry.
        With ``sink`` the body is streamed there instead of into memory.

        Raises:
            NetworkError: the transport failed on the last permitted attempt.
            CliError: the response exceeded ``max_bytes`` (exit 5).
        """
        attempt = 0
        all_headers = {"User-Agent": self.user_agent, **(headers or {})}
        unredirected = {"Authorization": auth} if auth else {}
        while True:
            attempt += 1
            left = deadline.check(f"{method} {_redact(url)}")
            if paced:
                self.pacer.wait()
                left = deadline.check(f"{method} {_redact(url)}")
            request = HttpRequest(method, url, dict(all_headers), body, dict(unredirected))
            try:
                raw = self.transport.send(request, left)
            except TransportError as exc:
                if not retry or attempt >= MAX_ATTEMPTS:
                    raise NetworkError(url, str(exc), lost_response=exc.sent) from None
                if not self._backoff(attempt, None, deadline):
                    raise NetworkError(url, str(exc), lost_response=exc.sent) from None
                continue
            try:
                response = self._read(raw, url, max_bytes, sink, attempt)
            except TransportError as exc:
                if (
                    not retry
                    or attempt >= MAX_ATTEMPTS
                    or not self._backoff(attempt, None, deadline)
                ):
                    raise NetworkError(url, str(exc), lost_response=True) from None
                continue
            temporary = response.status in retry_statuses or (
                should_retry is not None and should_retry(response)
            )
            if not temporary or not retry or attempt >= MAX_ATTEMPTS:
                return response
            wait = retry_after_seconds(response.header("retry-after"))
            if not self._backoff(attempt, wait, deadline):
                return response

    def _backoff(self, attempt: int, retry_after: float | None, deadline: Deadline) -> bool:
        delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempt - 1))
        delay = self.rng.uniform(delay / 2, delay)
        if retry_after is not None:
            delay = retry_after + self.rng.uniform(0, 0.25)
        if delay >= deadline.remaining():
            return False
        self.sleep(delay)
        return True

    def _read(
        self, raw: RawResponse, url: str, max_bytes: int, sink: BinaryIO | None, attempt: int
    ) -> Response:
        total = 0
        chunks: list[bytes] = []
        if sink is not None:
            sink.seek(0)
            sink.truncate()
        stream_ok = 200 <= raw.status < 300
        try:
            while True:
                try:
                    chunk = raw.stream.read(READ_CHUNK)
                except (TimeoutError, OSError, ValueError) as exc:
                    raise TransportError(f"response interrupted: {exc}") from None
                if not chunk:
                    break
                total += len(chunk)
                limit = max_bytes if stream_ok else min(max_bytes, 1 << 20)
                if total > limit:
                    raise CliError(
                        "response_too_large",
                        f"response from {_redact(url)} exceeds the local limit of {limit} bytes",
                        Exit.INCOMPLETE,
                        url=_redact(url),
                    )
                if sink is not None and stream_ok:
                    sink.write(chunk)
                else:
                    chunks.append(chunk)
        finally:
            with contextlib.suppress(Exception):
                raw.stream.close()
        return Response(raw.status, raw.headers, b"".join(chunks), raw.url or url, attempt)

    # convenience -------------------------------------------------------------

    def get(self, url: str, *, deadline: Deadline, **kwargs: Any) -> Response:
        return self.request("GET", url, deadline=deadline, **kwargs)

    def post_json(
        self, url: str, document: object, *, deadline: Deadline, **kwargs: Any
    ) -> Response:
        body = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}) or {})
        return self.request("POST", url, deadline=deadline, headers=headers, body=body, **kwargs)


def multipart(parts: list[tuple[str, str | None, bytes, str]]) -> tuple[bytes, str]:
    """Encode ``(name, filename, data, content_type)`` parts as multipart/form-data."""
    boundary = "sci-" + secrets.token_hex(16)
    out = io.BytesIO()
    for name, filename, data, content_type in parts:
        out.write(f"--{boundary}\r\n".encode("ascii"))
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out.write(f"Content-Disposition: {disposition}\r\n".encode("ascii"))
        out.write(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode("ascii"))
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def status_error(response: Response, what: str) -> CliError:
    """A CliError for an unexpected HTTP status (5xx/429 temporary, others invalid)."""
    temporary = response.status in RETRY_STATUSES
    record: dict[str, Any] = {"status": response.status, "url": _redact(response.url)}
    retry_after = response.header("retry-after")
    if retry_after:
        record["retry_after"] = retry_after
    return CliError(
        "temporary_failure" if temporary else "unexpected_status",
        f"{what} returned HTTP {response.status}",
        Exit.INCOMPLETE if temporary else Exit.INVALID,
        retryable=temporary,
        **record,
    )
