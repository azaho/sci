"""HTTP client behaviour: retries, backoff, Retry-After, deadlines, limits, and pacing."""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from sci.clients.http import (
    Deadline,
    HttpClient,
    HttpRequest,
    NetworkError,
    Pacer,
    RawResponse,
    TransportError,
    retry_after_seconds,
)
from sci.errors import CliError, Exit, combine_exits


class Scripted:
    def __init__(self, *steps: RawResponse | Exception) -> None:
        self.steps = list(steps)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest, timeout: float) -> RawResponse:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def reply(status: int, body: bytes = b"{}", **headers: str) -> RawResponse:
    return RawResponse(
        status, {k.replace("_", "-"): v for k, v in headers.items()}, io.BytesIO(body)
    )


def client(transport: Scripted, sleeps: list[float]) -> HttpClient:
    import random

    return HttpClient(transport, user_agent="test", sleep=sleeps.append, rng=random.Random(1))


def test_retries_temporary_statuses_with_backoff_and_jitter() -> None:
    sleeps: list[float] = []
    transport = Scripted(reply(503), reply(502), reply(200, b"ok"))
    response = client(transport, sleeps).get("https://x.example/a", deadline=Deadline(60))
    assert response.status == 200 and response.body == b"ok" and response.attempts == 3
    assert len(sleeps) == 2
    assert 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0


def test_retry_after_is_honoured_and_bounded_by_deadline() -> None:
    sleeps: list[float] = []
    transport = Scripted(reply(429, retry_after="3"), reply(200))
    assert client(transport, sleeps).get("https://x.example", deadline=Deadline(60)).status == 200
    assert 3 <= sleeps[0] <= 3.25
    # Retry-After beyond the deadline: stop and return the temporary failure
    sleeps.clear()
    transport = Scripted(reply(503, retry_after="100"), reply(200))
    response = client(transport, sleeps).get("https://x.example", deadline=Deadline(5))
    assert response.status == 503 and sleeps == [] and len(transport.steps) == 1


def test_no_retry_on_policy_or_validation_refusal() -> None:
    for status in (400, 403, 404, 413, 422):
        transport = Scripted(reply(status), reply(200))
        assert (
            client(transport, []).get("https://x.example", deadline=Deadline(60)).status == status
        )
        assert len(transport.requests) == 1


def test_transport_errors_retry_then_report_lost_response() -> None:
    sleeps: list[float] = []
    transport = Scripted(*[TransportError("reset", sent=True)] * 6)
    with pytest.raises(NetworkError) as info:
        client(transport, sleeps).request(
            "POST", "https://x.example", deadline=Deadline(600), body=b"{}"
        )
    assert info.value.lost_response and info.value.exit_code == Exit.INCOMPLETE
    assert len(transport.requests) == 6
    transport = Scripted(TransportError("dns", sent=False), reply(200))
    assert client(transport, []).get("https://x.example", deadline=Deadline(60)).status == 200


def test_deadline_and_size_limits() -> None:
    with pytest.raises(CliError) as info:
        client(Scripted(), []).get("https://x.example", deadline=Deadline(0))
    assert info.value.code == "deadline_exceeded" and info.value.exit_code == Exit.INCOMPLETE
    transport = Scripted(reply(200, b"x" * 100))
    with pytest.raises(CliError) as info:
        client(transport, []).get("https://x.example", deadline=Deadline(60), max_bytes=10)
    assert info.value.code == "response_too_large"


def test_credentials_are_unredirected_headers() -> None:
    transport = Scripted(reply(200))
    client(transport, []).get("https://x.example", deadline=Deadline(60), auth="Bearer t")
    request = transport.requests[0]
    assert request.unredirected == {"Authorization": "Bearer t"}
    assert "Authorization" not in request.headers


def test_retry_after_parsing() -> None:
    now = datetime(2026, 11, 7, tzinfo=UTC)
    assert retry_after_seconds("7") == 7
    assert retry_after_seconds(format_datetime(now + timedelta(seconds=30)), now) == 30
    assert retry_after_seconds("soon") is None


def test_pacer_spaces_requests_across_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Pacer, "interval", 1.0)
    clock = [1000.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    wall: Callable[[], float] = lambda: clock[0]  # noqa: E731
    first = Pacer(tmp_path / "pace", sleep=sleep, wall=wall)
    second = Pacer(tmp_path / "pace", sleep=sleep, wall=wall)
    first.wait()
    clock[0] += 0.3
    second.wait()
    assert sleeps == [pytest.approx(0.7)]


def test_combine_exits() -> None:
    assert combine_exits([]) == Exit.OK
    assert combine_exits([0, 0]) == Exit.OK
    assert combine_exits([0, 3]) == Exit.INCOMPLETE
    assert combine_exits([3, 6]) == Exit.INVALID
    assert combine_exits([6, 6]) == Exit.REFUSED
