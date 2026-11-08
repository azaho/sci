"""Ed25519 public JWKs (RFC 8037) and key file helpers (§5.1)."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .ed25519 import check_public_key_point
from .errors import Invalid, Issue, IssueCollector
from .jsonstrict import json_type_name

__all__ = [
    "PublicKey",
    "generate_private_key",
    "load_private_key_pem",
    "load_public_key_pem",
    "looks_like_private_key",
    "parse_public_jwk",
    "private_key_to_pem",
    "public_key_of",
    "x_to_bytes",
]

_X_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def x_to_bytes(x: str) -> bytes | None:
    """Decode a canonical unpadded base64url ``x``; None if noncanonical."""
    if not _X_PATTERN.fullmatch(x):
        return None
    raw = base64.urlsafe_b64decode(x + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != x:
        return None
    return raw


@dataclass(frozen=True, slots=True)
class PublicKey:
    """An Ed25519 public key; ``raw`` holds the 32 encoded bytes."""

    raw: bytes

    def __post_init__(self) -> None:
        if len(self.raw) != 32:
            raise ValueError("Ed25519 public keys are 32 bytes")

    @property
    def x(self) -> str:
        """Canonical unpadded base64url JWK ``x`` value."""
        return base64.urlsafe_b64encode(self.raw).rstrip(b"=").decode("ascii")

    def to_jwk(self) -> dict[str, str]:
        """The public JWK object with exactly ``kty``, ``crv``, and ``x``."""
        return {"kty": "OKP", "crv": "Ed25519", "x": self.x}

    @classmethod
    def from_x(cls, x: str) -> PublicKey:
        """Parse a canonical ``x`` value. Raises :class:`Invalid`."""
        raw = x_to_bytes(x)
        if raw is None:
            raise Invalid(
                Issue(
                    "jwk_x_encoding",
                    "x must be canonical unpadded base64url of 32 bytes",
                    section="5.1",
                    observed=x,
                )
            )
        return cls(raw)

    def to_pem(self) -> bytes:
        """SubjectPublicKeyInfo PEM encoding."""
        return Ed25519PublicKey.from_public_bytes(self.raw).public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def to_cryptography(self) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(self.raw)


def parse_public_jwk(
    value: object,
    *,
    file: str | None = None,
    field: str = "/public_key",
    collector: IssueCollector | None = None,
    check_point: bool = True,
) -> PublicKey | None:
    """Validate a public JWK under §5.1.

    With a ``collector`` issues are appended and None is returned on failure;
    without one the first failure raises :class:`Invalid`.
    """
    local = IssueCollector()
    result = _parse_jwk(value, file, field, local, check_point)
    if collector is not None:
        collector.extend(local.issues)
        return result
    local.raise_if_any()
    return result


def _parse_jwk(
    value: object, file: str | None, field: str, issues: IssueCollector, check_point: bool
) -> PublicKey | None:
    if not isinstance(value, dict):
        issues.add(
            "jwk_type",
            "public key must be a JWK object",
            section="5.1",
            file=file,
            field=field,
            observed=json_type_name(value),
        )
        return None
    extra = sorted(set(value) - {"kty", "crv", "x"})
    missing = sorted({"kty", "crv", "x"} - set(value))
    if extra:
        issues.add(
            "jwk_fields",
            "only kty, crv, and x are permitted in a public key",
            section="5.1",
            file=file,
            field=field,
            observed=extra,
        )
    if missing:
        issues.add(
            "jwk_fields",
            "public key requires kty, crv, and x",
            section="5.1",
            file=file,
            field=field,
            observed=missing,
        )
    if "kty" in value and value["kty"] != "OKP":
        issues.add(
            "jwk_kty",
            'kty must be "OKP"',
            section="5.1",
            file=file,
            field=field + "/kty",
            observed=value["kty"],
        )
    if "crv" in value and value["crv"] != "Ed25519":
        issues.add(
            "jwk_crv",
            'crv must be "Ed25519"',
            section="5.1",
            file=file,
            field=field + "/crv",
            observed=value["crv"],
        )
    x = value.get("x")
    raw: bytes | None = None
    if "x" in value:
        raw = x_to_bytes(x) if isinstance(x, str) else None
        if raw is None:
            issues.add(
                "jwk_x_encoding",
                "x must be canonical unpadded base64url of 32 bytes",
                section="5.1",
                file=file,
                field=field + "/x",
                observed=x,
            )
    if issues or raw is None:
        return None
    if check_point:
        try:
            check_public_key_point(raw, file=file)
        except Invalid as exc:
            issues.extend(exc.issues)
            return None
    return PublicKey(raw)


def generate_private_key() -> Ed25519PrivateKey:
    """Create a new Ed25519 private key."""
    return Ed25519PrivateKey.generate()


def public_key_of(private_key: Ed25519PrivateKey) -> PublicKey:
    """The :class:`PublicKey` matching ``private_key``."""
    return PublicKey(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def private_key_to_pem(private_key: Ed25519PrivateKey, password: bytes | None = None) -> bytes:
    """PKCS#8 PEM, optionally encrypted with ``password``."""
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )


def load_private_key_pem(data: bytes, password: bytes | None = None) -> Ed25519PrivateKey:
    """Load a PKCS#8 PEM Ed25519 private key. Raises ValueError for other key types."""
    key = serialization.load_pem_private_key(data, password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    return key


def load_public_key_pem(data: bytes) -> PublicKey:
    """Load a SubjectPublicKeyInfo PEM Ed25519 public key."""
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return PublicKey(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


_PRIVATE_MARKERS: Final = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"PuTTY-User-Key-File-",
)


def looks_like_private_key(data: bytes) -> bool:
    """Heuristic: does ``data`` contain recognizable private-key material?

    Detects PEM/OpenSSH/PuTTY private keys and private JWKs (a ``"d"`` member
    next to ``"kty"``). Publishing tools use this to keep signing keys out of
    bundles (§2).
    """
    if any(marker in data for marker in _PRIVATE_MARKERS):
        return True
    return bool(
        re.search(rb'"kty"\s*:', data) and re.search(rb'"d"\s*:\s*"[A-Za-z0-9_-]{20,}', data)
    )
