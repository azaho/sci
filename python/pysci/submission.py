"""Publication submission contract (protocol §8.1.4)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from .accounts import Account
from .discovery import is_absolute_url
from .errors import Invalid, Issue, IssueCollector
from .ids import is_digest, parse_publication_id
from .jsonstrict import JsonObject, json_type_name, parse_json_object

__all__ = [
    "SubmissionError",
    "SubmissionRequest",
    "SubmissionResponse",
    "build_submission_request",
    "parse_submission_request",
    "parse_submission_response",
]

_FILE = "submission"


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    id: str
    descriptors: tuple[str, ...]
    account: Account | None = None

    def to_json_bytes(self) -> bytes:
        document: dict[str, object] = {"id": self.id, "descriptors": list(self.descriptors)}
        if self.account is not None:
            document["account"] = self.account.to_json()
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SubmissionError:
    """The §8.1.4 error object."""

    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class SubmissionResponse:
    """A conforming response: success (``id`` set) or error (``error`` set).

    ``body`` keeps the full decoded object so service-specific fields can be
    reported; protocol success confirms location registration only, not
    catalog admission.
    """

    status: int
    id: str | None
    error: SubmissionError | None
    body: JsonObject

    @property
    def success(self) -> bool:
        return self.error is None


def build_submission_request(
    publication_id: str, descriptors: Sequence[str], account: Account | None = None
) -> SubmissionRequest:
    """Build a request from any accepted ID spelling. Validates the result."""
    request = SubmissionRequest(
        parse_publication_id(publication_id, allow_bare=True), tuple(descriptors), account
    )
    parse_submission_request(request.to_json_bytes())
    return request


def parse_submission_request(data: bytes) -> SubmissionRequest:
    """Validate a request body (§8.1.4). Raises :class:`~pysci.errors.Invalid` with all issues."""
    record = parse_json_object(data, file=_FILE)
    issues = IssueCollector()
    missing = [name for name in ("id", "descriptors") if name not in record]
    extra = sorted(set(record) - {"id", "descriptors", "account"})
    if missing:
        issues.add(
            "submission_fields",
            "request requires id and descriptors",
            section="8.1.4",
            file=_FILE,
            observed=missing,
        )
    if extra:
        issues.add(
            "submission_fields",
            "request permits only id, descriptors, and account",
            section="8.1.4",
            file=_FILE,
            observed=extra,
        )
    publication = record.get("id")
    if "id" in record and not (isinstance(publication, str) and is_digest(publication)):
        issues.add(
            "submission_id",
            "id must be 64 lowercase hexadecimal characters without a prefix",
            section="8.1.1",
            file=_FILE,
            field="/id",
            observed=publication,
        )
    descriptors = record.get("descriptors")
    if "descriptors" in record:
        if not isinstance(descriptors, list) or not descriptors:
            issues.add(
                "submission_descriptors",
                "descriptors must be a nonempty array",
                section="8.1.4",
                file=_FILE,
                field="/descriptors",
                observed=json_type_name(descriptors),
            )
        else:
            for index, url in enumerate(descriptors):
                if not (isinstance(url, str) and is_absolute_url(url)):
                    issues.add(
                        "submission_descriptors",
                        "each descriptor must be an absolute URL string",
                        section="8.1.4",
                        file=_FILE,
                        field=f"/descriptors/{index}",
                        observed=url,
                    )
            if len({json.dumps(u) for u in descriptors}) != len(descriptors):
                issues.add(
                    "submission_descriptors",
                    "descriptors must be distinct",
                    section="8.1.4",
                    file=_FILE,
                    field="/descriptors",
                )
    account = None
    if "account" in record:
        value = record["account"]
        if not (
            isinstance(value, dict)
            and set(value) == {"type", "id"}
            and all(isinstance(v, str) for v in value.values())
        ):
            issues.add(
                "submission_account",
                "account must have exactly two string fields, type and id",
                section="5.2.1",
                file=_FILE,
                field="/account",
                observed=sorted(value) if isinstance(value, dict) else json_type_name(value),
            )
        else:
            account = Account(str(value["type"]), str(value["id"]))
    issues.raise_if_any()
    assert isinstance(publication, str)
    assert isinstance(descriptors, list)
    return SubmissionRequest(publication, tuple(str(u) for u in descriptors), account)


def parse_submission_response(
    status: int, content_type: str | None, body: bytes, requested_id: str
) -> SubmissionResponse:
    """Validate a response against the §8.1.4 contract.

    Success is ``200``/``201`` with an object whose ``id`` equals the
    requested digest; errors are ``4xx``/``5xx`` with an ``error`` object
    holding string ``code``, string ``message``, and Boolean ``retryable``.
    Both require ``application/json``.

    Raises:
        Invalid: the response violates the contract (e.g. wrong status class,
            content type, ID, or error shape).
    """

    def fail(code: str, requirement: str, observed: object = None) -> Invalid:
        return Invalid(
            Issue(
                code,
                requirement,
                section="8.1.4",
                file=_FILE,
                observed=None if observed is None else str(observed),
            )
        )

    requested = parse_publication_id(requested_id, allow_bare=True)
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise fail("submission_content_type", "responses must use application/json", content_type)
    record = parse_json_object(body, file=_FILE)
    if status in (200, 201):
        if record.get("id") != requested:
            raise fail(
                "submission_response_id",
                "success response id must equal the requested digest",
                record.get("id"),
            )
        return SubmissionResponse(status, requested, None, record)
    if 400 <= status <= 599:
        error = record.get("error")
        if not (
            isinstance(error, dict)
            and isinstance(error.get("code"), str)
            and isinstance(error.get("message"), str)
            and type(error.get("retryable")) is bool
        ):
            raise fail(
                "submission_error_shape",
                "error responses need an error object with "
                "string code, string message, and Boolean retryable",
            )
        return SubmissionResponse(
            status,
            None,
            SubmissionError(str(error["code"]), str(error["message"]), bool(error["retryable"])),
            record,
        )
    raise fail(
        "submission_status", "responses use 200/201 for success or 4xx/5xx for errors", status
    )
