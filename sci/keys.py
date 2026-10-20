"""Ed25519 keys; identities are hashes of raw public-key bytes."""

import base64
import hashlib
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sci.errors import SCIError, invalid


def raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def encode_public(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_public(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise invalid("public_key.x", "expected canonical unpadded base64url of 32 bytes")
    raw = base64.urlsafe_b64decode(value + "=")
    if len(raw) != 32 or encode_public(raw) != value:
        raise invalid("public_key.x", "noncanonical or incorrect-size Ed25519 key")
    return raw


def public_jwk(key: Ed25519PublicKey) -> dict:
    return {"kty": "OKP", "crv": "Ed25519", "x": encode_public(raw_public(key))}


def jwk_key(jwk: dict) -> Ed25519PublicKey:
    if not isinstance(jwk, dict) or set(jwk) != {"kty", "crv", "x"}:
        raise invalid("public_key", "requires exactly kty, crv, x; no private material")
    if jwk["kty"] != "OKP" or jwk["crv"] != "Ed25519":
        raise invalid("public_key", "requires an OKP Ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(decode_public(jwk["x"]))


def fingerprint(raw: bytes) -> str:
    return "ed25519:" + hashlib.sha256(raw).hexdigest()


def load_private(path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError, OSError) as exc:
        raise SCIError(f"Cannot read private key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise invalid("private key", "expected Ed25519 PKCS#8 PEM")
    return key


def load_public(path: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (ValueError, TypeError, OSError) as exc:
        raise SCIError(f"Cannot read public key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise invalid("public key", "expected Ed25519 SubjectPublicKeyInfo PEM")
    return key


def public_path(private: Path) -> Path:
    return private.with_suffix(".pub.pem") if private.suffix == ".pem" else Path(str(private) + ".pub.pem")


def keygen(path: Path) -> tuple[Path, str]:
    public = public_path(path)
    if path.exists() or path.is_symlink() or public.exists() or public.is_symlink():
        raise SCIError(f"Refusing to overwrite either key: {path}, {public}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    # Exclusive creation also protects against another keygen process.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
        with public.open("xb") as out:
            out.write(key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return public, fingerprint(raw_public(key.public_key()))
