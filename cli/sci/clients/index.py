"""Index client: descriptor discovery (§8.1), lookups, submissions, and catalog queries."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

from pysci.discovery import (
    IndexDescriptor,
    is_absolute_url,
    parse_index_descriptor,
    parse_publication_lookup,
    parse_timestamp_lookup,
)
from pysci.errors import Outcome, SciError
from pysci.jsonstrict import JsonValue
from pysci.submission import SubmissionRequest, parse_submission_response
from pysci.templates import UrlTemplate, parse_template

from ..errors import CliError, ErrorRecord, Exit
from .http import Deadline, HttpClient, NetworkError, Response, status_error

DESCRIPTOR_LIMIT = 1 << 20
CATALOG_SCHEMA = "sciscape_catalog_v1"
API_SCHEMA = "sciscape-api/0.1"
CATALOG_FIELDS = "id,title,abstract,public_key,accepted_at"


def descriptor_url(origin: str) -> str:
    return origin.rstrip("/") + "/.well-known/sci/index.json"


def unsupported(capability: str, origin: str, detail: str) -> CliError:
    return CliError(
        "unsupported_capability",
        f"index {origin} does not support {capability}: {detail}",
        Exit.UNSUPPORTED,
        capability=capability,
    )


@dataclass(frozen=True, slots=True)
class Index:
    origin: str
    descriptor: IndexDescriptor

    def extra_url(self, name: str) -> str | None:
        value = self.descriptor.extra.get(name)
        return value if isinstance(value, str) and is_absolute_url(value) else None

    def extra_str(self, name: str) -> str | None:
        value = self.descriptor.extra.get(name)
        return value if isinstance(value, str) else None

    def publication_page(self, digest: str) -> str | None:
        template = self.extra_str("publication_url")
        if template is None:
            return None
        try:
            parsed: UrlTemplate = parse_template(template)
            return parsed.expand({"id": digest})
        except (ValueError, KeyError, SciError):
            return None


def classify_service_error(status: int, code: str, retryable: bool) -> Exit:
    """Map a submission or Sciscape API error to an exit code."""
    if code == "unsupported_schema":
        return Exit.UNSUPPORTED
    if retryable or status in (408, 409, 425, 429) or status >= 500:
        return Exit.INCOMPLETE
    if status in (403, 413) or code in ("policy_refused", "not_baseline"):
        return Exit.REFUSED
    return Exit.INVALID


class IndexClient:
    def __init__(self, http: HttpClient, origin: str) -> None:
        self.http = http
        self.origin = origin
        self._index: Index | None = None

    def discover(self, deadline: Deadline) -> Index:
        if self._index is not None:
            return self._index
        url = descriptor_url(self.origin)
        response = self.http.get(url, deadline=deadline, max_bytes=DESCRIPTOR_LIMIT)
        if response.status != 200:
            raise status_error(response, f"index descriptor {url}")
        try:
            descriptor = parse_index_descriptor(response.body, file=url)
        except SciError as exc:
            error = CliError.from_sci(exc, f"index descriptor {url} is not usable")
            raise error from None
        self._index = Index(self.origin, descriptor)
        return self._index

    # ------------------------------------------------------------------ §8.1.1-8.1.3

    def lookup(self, digest: str, deadline: Deadline) -> list[str]:
        index = self.discover(deadline)
        url = index.descriptor.publication_lookup(digest)
        response = self.http.get(url, deadline=deadline, max_bytes=DESCRIPTOR_LIMIT)
        try:
            return parse_publication_lookup(response.status, response.body)
        except SciError as exc:
            if exc.outcome is Outcome.INCOMPLETE:
                raise status_error(response, "publication lookup") from None
            raise CliError.from_sci(exc, "publication lookup response is invalid") from None

    def timestamps(self, digest: str, deadline: Deadline) -> bytes | None:
        index = self.discover(deadline)
        url = index.descriptor.timestamp_lookup(digest)
        response = self.http.get(url, deadline=deadline, max_bytes=16 << 20)
        try:
            lookup = parse_timestamp_lookup(response.status, response.body)
        except SciError:
            raise status_error(response, "timestamp lookup") from None
        return lookup.data

    # ------------------------------------------------------------------ §8.1.4

    def submit(self, request: SubmissionRequest, deadline: Deadline) -> dict[str, Any]:
        """Submit one publication; return a per-ID result with an exit code.

        Temporary failures and service errors marked retryable are retried
        with backoff. A lost response stays ``outcome_unknown`` unless a
        repeated request confirms the result.
        """
        index = self.discover(deadline)
        url = index.descriptor.submit_url
        if url is None:
            raise unsupported("publication submission", self.origin, "no submit_url")
        body = request.to_json_bytes()
        lost = False
        try:
            response = self.http.request(
                "POST",
                url,
                deadline=deadline,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=body,
                paced=True,
                should_retry=_retryable_error,
            )
        except NetworkError as exc:
            lost = exc.lost_response
            return {
                "id": request.id,
                "status": "outcome_unknown" if lost else "failed",
                "exit_code": Exit.INCOMPLETE,
                "error": ErrorRecord(
                    "outcome_unknown" if lost else "network_error",
                    (
                        "the submission response was lost; repeat the same request to "
                        "confirm the result"
                    )
                    if lost
                    else str(exc),
                    True,
                ).to_json(),
            }
        except CliError as exc:
            if exc.code == "deadline_exceeded":
                return {
                    "id": request.id,
                    "status": "failed",
                    "exit_code": Exit.INCOMPLETE,
                    "error": exc.records[0].to_json(),
                }
            raise
        try:
            parsed = parse_submission_response(
                response.status, response.header("content-type"), response.body, request.id
            )
        except SciError as exc:
            temporary = response.status >= 500 or response.status == 429
            return {
                "id": request.id,
                "status": "failed",
                "http_status": response.status,
                "exit_code": Exit.INCOMPLETE if temporary else Exit.INVALID,
                "error": ErrorRecord(
                    "invalid_response",
                    f"submission response violates §8.1.4: {exc.issue.requirement}",
                    temporary,
                    observed=exc.issue.observed,
                ).to_json(),
            }
        if parsed.error is not None:
            exit_code = classify_service_error(
                response.status, parsed.error.code, parsed.error.retryable
            )
            record = ErrorRecord(parsed.error.code, parsed.error.message, parsed.error.retryable)
            out: dict[str, Any] = {
                "id": request.id,
                "status": "refused" if exit_code == Exit.REFUSED else "failed",
                "http_status": response.status,
                "exit_code": exit_code,
                "error": record.to_json(),
                "response": parsed.body,
            }
            if response.header("retry-after"):
                out["retry_after"] = response.header("retry-after")
            return out
        return {
            "id": request.id,
            "status": "registered",
            "http_status": response.status,
            "exit_code": Exit.OK,
            "attempts": response.attempts,
            "response": parsed.body,
        }

    # ------------------------------------------------------------------ Sciscape extensions

    def _api(self, field: str, deadline: Deadline, capability: str) -> str:
        index = self.discover(deadline)
        if index.extra_str("api_schema") != API_SCHEMA:
            raise unsupported(capability, self.origin, f"api_schema is not {API_SCHEMA}")
        url = index.extra_url(field)
        if url is None:
            raise unsupported(capability, self.origin, f"no {field}")
        return url

    def submit_multipart(
        self,
        field: str,
        capability: str,
        parts: list[tuple[str, str | None, bytes, str]],
        deadline: Deadline,
    ) -> tuple[Response | None, dict[str, Any]]:
        from .http import multipart

        url = self._api(field, deadline, capability)
        body, content_type = multipart(parts)
        try:
            response = self.http.request(
                "POST",
                url,
                deadline=deadline,
                headers={"Content-Type": content_type, "Accept": "application/json"},
                body=body,
                paced=True,
                should_retry=_retryable_error,
            )
        except NetworkError as exc:
            status = "outcome_unknown" if exc.lost_response else "failed"
            return None, {
                "status": status,
                "exit_code": Exit.INCOMPLETE,
                "error": ErrorRecord(
                    status if exc.lost_response else "network_error",
                    "the response was lost; repeat the same request to confirm the result"
                    if exc.lost_response
                    else str(exc),
                    True,
                ).to_json(),
            }
        return response, service_result(response, capability)

    # ------------------------------------------------------------------ catalog

    def catalog(self, deadline: Deadline) -> tuple[str, Index]:
        index = self.discover(deadline)
        schema = index.extra_str("catalog_schema")
        url = index.extra_url("catalog_url")
        if url is None or schema != CATALOG_SCHEMA:
            raise unsupported(
                "catalog search",
                self.origin,
                f"requires catalog_url with catalog_schema {CATALOG_SCHEMA} (found {schema!r})",
            )
        return url.rstrip("/"), index

    def search(
        self,
        deadline: Deadline,
        *,
        query: str | None,
        category: tuple[str, str] | None,
        domain: str | None,
        key_x: str | None,
        after: tuple[str, str] | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool, Index]:
        base, index = self.catalog(deadline)
        params: list[tuple[str, str]] = [("select", CATALOG_FIELDS)]
        if query:
            params.append(("search_text", f"plfts(simple).{query}"))
        if category is not None:
            value = json.dumps(
                [{"scheme": category[0], "code": category[1]}], separators=(",", ":")
            )
            params.append(("categories", f"cs.{value}"))
        if domain is not None:
            value = json.dumps([{"type": "domain", "id": domain}], separators=(",", ":"))
            params.append(("admission_accounts", f"cs.{value}"))
        if key_x is not None:
            params.append(("public_key->>x", f"eq.{key_x}"))
        if after is not None:
            time, last = after
            params.append(
                ("or", f"(accepted_at.lt.{time},and(accepted_at.eq.{time},id.lt.{last}))")
            )
        params.append(("order", "accepted_at.desc,id.desc"))
        params.append(("limit", str(limit + 1)))
        url = f"{base}/publications?" + urllib.parse.urlencode(params, safe="(),.>-:")
        response = self.http.get(
            url,
            deadline=deadline,
            headers={"Accept": "application/json", "Accept-Profile": CATALOG_SCHEMA},
            max_bytes=16 << 20,
        )
        if response.status != 200:
            error = status_error(response, "catalog query")
            detail = _postgrest_message(response)
            if detail:
                error.records[0] = ErrorRecord(
                    error.code, f"{error.records[0].message}: {detail}", error.records[0].retryable
                )
            raise error
        rows = response.json("catalog response")
        if not isinstance(rows, list):
            raise CliError(
                "invalid_response", "catalog response must be a JSON array", Exit.INVALID
            )
        results = [_catalog_row(row, index) for row in rows[: limit + 1]]
        return results[:limit], len(results) > limit, index


def _retryable_error(response: Response) -> bool:
    if response.content_type != "application/json":
        return False
    try:
        record = json.loads(response.body)
    except ValueError:
        return False
    error = record.get("error") if isinstance(record, dict) else None
    return isinstance(error, dict) and error.get("retryable") is True


def _postgrest_message(response: Response) -> str | None:
    try:
        record = json.loads(response.body)
    except ValueError:
        return None
    if isinstance(record, dict) and isinstance(record.get("message"), str):
        return str(record["message"])[:300]
    return None


def service_result(response: Response, capability: str) -> dict[str, Any]:
    """Interpret a Sciscape custom-write response (success object or error object)."""
    try:
        record = response.json_object(f"{capability} response")
    except CliError as exc:
        temporary = response.status >= 500 or response.status == 429
        return {
            "status": "failed",
            "http_status": response.status,
            "exit_code": Exit.INCOMPLETE if temporary else Exit.INVALID,
            "error": exc.records[0].to_json(),
        }
    if 200 <= response.status < 300:
        return {
            "status": "ok",
            "http_status": response.status,
            "exit_code": Exit.OK,
            "response": record,
        }
    error = record.get("error")
    if (
        isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and isinstance(error.get("message"), str)
        and isinstance(error.get("retryable"), bool)
    ):
        code, message, retryable = (
            str(error["code"]),
            str(error["message"]),
            bool(error["retryable"]),
        )
        exit_code = classify_service_error(response.status, code, retryable)
        return {
            "status": "refused" if exit_code == Exit.REFUSED else "failed",
            "http_status": response.status,
            "exit_code": exit_code,
            "error": ErrorRecord(code, message, retryable).to_json(),
        }
    temporary = response.status >= 500 or response.status == 429
    return {
        "status": "failed",
        "http_status": response.status,
        "exit_code": Exit.INCOMPLETE if temporary else Exit.INVALID,
        "error": ErrorRecord(
            "invalid_response", f"{capability} returned HTTP {response.status}", temporary
        ).to_json(),
    }


def _catalog_row(row: JsonValue, index: Index) -> dict[str, Any]:
    def bad(field: str, value: object) -> CliError:
        return CliError(
            "invalid_response",
            f"catalog row field {field} has an unexpected type",
            Exit.INVALID,
            field=f"/{field}",
            observed=repr(value)[:120],
        )

    if not isinstance(row, dict):
        raise bad("", row)
    digest = row.get("id")
    if not isinstance(digest, str) or len(digest) != 64:
        raise bad("id", digest)
    for name in ("title", "abstract"):
        if not isinstance(row.get(name), str):
            raise bad(name, row.get(name))
    accepted = row.get("accepted_at")
    if not isinstance(accepted, str):
        raise bad("accepted_at", accepted)
    key = row.get("public_key")
    if not isinstance(key, dict) or not isinstance(key.get("x"), str):
        raise bad("public_key", key)
    return {
        "id": "sha256:" + digest,
        "title": row["title"],
        "abstract": row["abstract"],
        "signer": key,
        "accepted_at": accepted,
        "publication_url": index.publication_page(digest),
    }
