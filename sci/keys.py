"""Ed25519 identities: PEM files, public JWKs, raw keys, and fingerprints.

A public key is an identity. Identities are compared by their raw 32-byte
public keys; the fingerprint is ``ed25519:`` followed by the lowercase hex
SHA-256 of those raw bytes.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

RAW_KEY_BYTES = 32
SIGNATURE_BYTES = 64
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")


class KeyError_(ValueError):
    """Invalid key material or key file."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Decode canonical, unpadded base64url. Reject padding, whitespace, and
    encodings with nonzero trailing bits."""
    if not isinstance(text, str) or not _B64URL_RE.fullmatch(text):
        raise KeyError_("expected unpadded base64url text without whitespace")
    if len(text) % 4 == 1:
        raise KeyError_("invalid base64url length")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise KeyError_("invalid base64url text") from exc
    if b64url_encode(data) != text:
        raise KeyError_("noncanonical base64url text")
    return data


def raw_public_key(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint_raw(raw: bytes) -> str:
    if len(raw) != RAW_KEY_BYTES:
        raise KeyError_("an Ed25519 public key has exactly 32 bytes")
    return "ed25519:" + hashlib.sha256(raw).hexdigest()


def fingerprint(key: Ed25519PublicKey) -> str:
    return fingerprint_raw(raw_public_key(key))


def public_key_from_raw(raw: bytes) -> Ed25519PublicKey:
    if len(raw) != RAW_KEY_BYTES:
        raise KeyError_("an Ed25519 public key has exactly 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def public_key_from_b64url(text: str) -> Ed25519PublicKey:
    """Load the base64url ``x`` value used in manifests and whitelist helpers."""
    raw = b64url_decode(text)
    if len(raw) != RAW_KEY_BYTES:
        raise KeyError_("the decoded public key must have exactly 32 bytes")
    return public_key_from_raw(raw)


def jwk_from_public_key(key: Ed25519PublicKey) -> dict:
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(raw_public_key(key))}


def public_key_from_jwk(jwk: object) -> Ed25519PublicKey:
    """Load a public Ed25519 JWK with exactly ``kty``, ``crv``, and ``x``."""
    if not isinstance(jwk, dict):
        raise KeyError_("public_key must be a JSON object")
    if "d" in jwk:
        raise KeyError_("public_key must not contain private key material ('d')")
    if set(jwk) != {"kty", "crv", "x"}:
        extra = sorted(set(jwk) - {"kty", "crv", "x"})
        missing = sorted({"kty", "crv", "x"} - set(jwk))
        raise KeyError_(
            "public_key must have exactly kty, crv, and x"
            + (f"; unknown: {', '.join(extra)}" if extra else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
        )
    if jwk["kty"] != "OKP":
        raise KeyError_("public_key.kty must be 'OKP'")
    if jwk["crv"] != "Ed25519":
        raise KeyError_("public_key.crv must be 'Ed25519'")
    if not isinstance(jwk["x"], str):
        raise KeyError_("public_key.x must be a string")
    try:
        return public_key_from_b64url(jwk["x"])
    except KeyError_ as exc:
        raise KeyError_(f"public_key.x: {exc}") from exc


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def public_key_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise KeyError_(f"cannot read private key {path}: {exc.strerror}") from exc
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise KeyError_(f"{path} is not an unencrypted PKCS#8 PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyError_(f"{path} is not an Ed25519 private key")
    return key


def load_public_key_pem(data: bytes, source: str = "public key") -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as exc:
        raise KeyError_(f"{source} is not a SubjectPublicKeyInfo PEM public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise KeyError_(f"{source} is not an Ed25519 public key")
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise KeyError_(f"cannot read public key {path}: {exc.strerror}") from exc
    return load_public_key_pem(data, str(path))


def public_key_path_for(private_path: Path) -> Path:
    """``key.pem`` -> ``key.pub.pem``; a name without ``.pem`` gets ``.pub.pem`` appended."""
    private_path = Path(private_path)
    name = private_path.name
    if name.endswith(".pem"):
        return private_path.with_name(name[: -len(".pem")] + ".pub.pem")
    return private_path.with_name(name + ".pub.pem")


def write_new_key_pair(private_path: Path) -> tuple[Path, Path, Ed25519PrivateKey]:
    """Create a key pair without overwriting either file."""
    private_path = Path(private_path)
    public_path = public_key_path_for(private_path)
    for p in (private_path, public_path):
        if p.exists() or p.is_symlink():
            raise KeyError_(f"{p} already exists; keygen never overwrites key files")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_private_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(private_path, flags, 0o600)
    try:
        os.write(fd, private_key_pem(key))
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(private_path, 0o600)
    try:
        fd = os.open(public_path, flags, 0o644)
        try:
            os.write(fd, public_key_pem(key.public_key()))
        finally:
            os.close(fd)
    except OSError:
        private_path.unlink(missing_ok=True)
        raise
    return private_path, public_path, key


def sign(key: Ed25519PrivateKey, data: bytes) -> bytes:
    return key.sign(data)


def verify_signature(key: Ed25519PublicKey, signature: bytes, data: bytes) -> bool:
    if len(signature) != SIGNATURE_BYTES:
        return False
    try:
        key.verify(signature, data)
    except InvalidSignature:
        return False
    return True
