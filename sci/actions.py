"""Signed action records (DESIGN.md "Signed action records").

An action is a small JSON payload signed by one key. The signature covers
``SCI-ACTION\\0v1\\0`` followed by the exact payload bytes, so an action
signature can never be confused with a publication signature. The server
keeps the original bytes and signature; derived tables are views of them.

Only the ``profile`` action exists so far. A profile record carries the
whole profile, not a change to one field, and names the revision it
replaces, so the current profile is always the latest accepted record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import keys
from . import publication as pub
from .publication import PublicationError

SIGNING_PREFIX = b"SCI-ACTION\x00v1\x00"
VERSION = 1
MAX_ACTION_BYTES = 8_192
MAX_NAME = 64
MAX_BIO = 500

_FINGERPRINT_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_COMMON_FIELDS = {"version", "type", "id", "audience", "signer"}
_PROFILE_FIELDS = _COMMON_FIELDS | {"expected_revision", "name", "bio"}


@dataclass(frozen=True)
class Action:
    payload: dict
    payload_bytes: bytes
    public_key_b64: str
    signature: bytes

    @property
    def id(self) -> str:
        return self.payload["id"]

    @property
    def type(self) -> str:
        return self.payload["type"]

    @property
    def signer(self) -> str:
        return self.payload["signer"]


def same_audience(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def profile_payload(*, signer: str, audience: str, expected_revision: int, name: str, bio: str | None) -> dict:
    doc = {
        "version": VERSION,
        "type": "profile",
        "id": pub.new_uuid4(),
        "audience": audience.rstrip("/"),
        "signer": signer,
        "expected_revision": expected_revision,
        "name": name,
    }
    if bio:
        doc["bio"] = bio
    validate_payload(doc)
    return doc


def validate_payload(doc: dict) -> dict:
    if doc.get("version") != VERSION:
        raise PublicationError(f"unsupported action version; expected {VERSION}", "version", "unsupported_schema")
    if doc.get("type") != "profile":
        raise PublicationError("unknown action type; supported: profile", "type")
    pub._check_fields(doc, _PROFILE_FIELDS, _PROFILE_FIELDS - {"bio"}, "action")
    pub.check_uuid4(doc["id"], "id")
    pub.check_text(doc["audience"], "audience", max_chars=512)
    if not isinstance(doc["signer"], str) or not _FINGERPRINT_RE.fullmatch(doc["signer"]):
        raise PublicationError("must be ed25519: followed by 64 lowercase hex characters", "signer")
    rev = doc["expected_revision"]
    if type(rev) is not int or rev < 0:
        raise PublicationError("must be a non-negative integer", "expected_revision")
    pub.check_text(doc["name"], "name", max_chars=MAX_NAME)
    if "bio" in doc:
        pub.check_text(doc["bio"], "bio", max_chars=MAX_BIO, multiline=True)
    return doc


def sign(private_key, payload: dict) -> dict:
    """Return the request body for ``POST /actions``."""
    data = pub.compact_json_bytes(payload)
    return {
        "payload": keys.b64url_encode(data),
        "signatures": [{
            "public_key": keys.b64url_encode(keys.raw_public_key(private_key.public_key())),
            "signature": keys.b64url_encode(keys.sign(private_key, SIGNING_PREFIX + data)),
        }],
    }


def verify(body: dict) -> Action:
    """Verify a ``POST /actions`` body: one signer whose key matches ``signer``."""
    pub._check_fields(body, {"payload", "signatures"}, {"payload", "signatures"}, "request")
    sigs = body["signatures"]
    if not isinstance(sigs, list) or len(sigs) != 1 or not isinstance(sigs[0], dict):
        raise PublicationError("must contain exactly one signature object", "signatures")
    pub._check_fields(sigs[0], {"public_key", "signature"}, {"public_key", "signature"}, "signatures[0]")
    try:
        data = keys.b64url_decode(body["payload"])
        public_key = keys.public_key_from_b64url(sigs[0]["public_key"])
        signature = keys.b64url_decode(sigs[0]["signature"])
    except keys.KeyError_ as exc:
        raise PublicationError(str(exc), "request") from exc
    if len(data) > MAX_ACTION_BYTES:
        raise PublicationError(f"exceeds {MAX_ACTION_BYTES} bytes", "payload", "too_large")
    doc = validate_payload(pub.parse_strict_json(data, "payload"))
    if keys.fingerprint(public_key) != doc["signer"]:
        raise PublicationError("the public key does not match the signer fingerprint", "signer", "invalid_signature")
    if not keys.verify_signature(public_key, signature, SIGNING_PREFIX + data):
        raise PublicationError("the signature does not verify", "signatures", "invalid_signature")
    return Action(doc, data, sigs[0]["public_key"], signature)
