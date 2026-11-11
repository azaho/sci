"""Sciscape vote extension codec and current-opinion lookup (not an SCI 0.1 record)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci import ed25519
from pysci.errors import IssueCollector, SciError
from pysci.jsonstrict import JsonInt, parse_json_object
from pysci.keys import PublicKey, parse_public_jwk, public_key_of
from pysci.text import check_text

from ..errors import CliError, Exit
from .http import Deadline, status_error
from .index import API_SCHEMA, IndexClient, unsupported

MAX_RECORD_BYTES = 16 * 1024
MAX_REVISION = 2147483647
REFERENCE = re.compile(r"sci:sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class Vote:
    record: bytes
    signature: bytes
    public_key: PublicKey
    target: str
    value: int
    revision: int
    reason: str | None
    review: str | None

    @property
    def id(self) -> str:
        return hashlib.sha256(self.record).hexdigest()


def verify_vote(record: bytes, signature: bytes) -> Vote:
    """Verify the original bytes and strict extension fields using SCI primitives."""
    try:
        if len(record) > MAX_RECORD_BYTES:
            raise ValueError("vote.json exceeds 16 KiB")
        raw = parse_json_object(record)
        required = {"schema", "public_key", "target", "value", "revision"}
        if not required <= raw.keys() or raw.keys() - required - {"reason", "review"}:
            raise ValueError("missing or unknown vote fields")
        if raw["schema"] != "sciscape-vote/0.1":
            raise ValueError("unsupported vote schema")
        key = parse_public_jwk(raw["public_key"])
        assert key is not None
        target = raw["target"]
        if not isinstance(target, str) or not REFERENCE.fullmatch(target):
            raise ValueError("target must be a sci:sha256 publication reference")
        value, revision = raw["value"], raw["revision"]
        if not isinstance(value, JsonInt) or value.lexeme not in {"-1", "0", "1"}:
            raise ValueError("value must be -1, 0, or 1")
        if (
            not isinstance(revision, JsonInt)
            or not revision.lexeme.isascii()
            or not revision.lexeme.isdigit()
            or not 1 <= revision <= MAX_REVISION
        ):
            raise ValueError(f"revision must be an integer from 1 to {MAX_REVISION}")
        reason = raw.get("reason")
        if "reason" in raw:
            if not isinstance(reason, str):
                raise ValueError("reason must be text")
            issues = IssueCollector()
            check_text(
                reason, multiline=True, nonblank=True, max_code_points=1000, collector=issues
            )
            issues.raise_if_any()
        review = raw.get("review")
        if "review" in raw and (not isinstance(review, str) or not REFERENCE.fullmatch(review)):
            raise ValueError("review must be a sci:sha256 publication reference")
        ed25519.verify(key.raw, signature, record)
        return Vote(
            record,
            signature,
            key,
            target,
            int(value),
            int(revision),
            reason if isinstance(reason, str) else None,
            review if isinstance(review, str) else None,
        )
    except SciError as exc:
        raise CliError.from_sci(exc, "invalid signed vote") from None
    except ValueError as exc:
        raise CliError("invalid_vote", str(exc), Exit.INVALID) from None


def create_vote(
    key: Ed25519PrivateKey,
    target: str,
    value: int,
    revision: int,
    reason: str | None,
    review: str | None,
) -> Vote:
    body: dict[str, object] = {
        "schema": "sciscape-vote/0.1",
        "public_key": public_key_of(key).to_jwk(),
        "target": target,
        "value": value,
        "revision": revision,
    }
    if reason is not None:
        body["reason"] = reason
    if review is not None:
        body["review"] = review
    try:
        record = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise CliError(
            "invalid_vote", "vote text must contain valid Unicode scalar values", Exit.INVALID
        ) from None
    return verify_vote(record, ed25519.sign(key, record))


def current_vote(client: IndexClient, digest: str, key_x: str, deadline: Deadline) -> Vote | None:
    index = client.discover(deadline)
    if index.extra_str("api_schema") != API_SCHEMA or index.extra_url("vote_url") is None:
        raise unsupported("signed votes", client.origin, "requires sciscape-api/0.1 and vote_url")
    base, _ = client.catalog(deadline)
    params = urllib.parse.urlencode(
        {
            "publication_id": f"eq.{digest}",
            "key_x": f"eq.{key_x}",
            "select": "id,record_base64,signature_base64",
            "limit": "2",
        }
    )
    response = client.http.get(f"{base}/votes?{params}", deadline=deadline, max_bytes=64 * 1024)
    if response.status != 200:
        raise status_error(response, "current vote lookup")
    rows = response.json("current vote lookup")
    if not isinstance(rows, list) or len(rows) > 1:
        raise CliError("invalid_response", "expected zero or one current vote", Exit.INVALID)
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        raise CliError("invalid_response", "invalid current vote row", Exit.INVALID)
    record, signature = row.get("record_base64"), row.get("signature_base64")
    if not isinstance(record, str) or not isinstance(signature, str):
        raise CliError("invalid_response", "current vote must include signed bytes", Exit.INVALID)
    try:
        vote = verify_vote(
            base64.b64decode(record, validate=True), base64.b64decode(signature, validate=True)
        )
    except ValueError:
        raise CliError("invalid_response", "invalid vote base64", Exit.INVALID) from None
    if (
        vote.target != f"sci:sha256:{digest}"
        or vote.public_key.x != key_x
        or vote.id != row.get("id")
    ):
        raise CliError(
            "invalid_response",
            "current vote does not match the requested key and paper",
            Exit.INVALID,
        )
    return vote
