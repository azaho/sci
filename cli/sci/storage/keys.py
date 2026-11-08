from __future__ import annotations
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pysci.validation import jwk_key
import argparse
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pysci import Bundle, Invalid
from pysci.bundle import read_regular
from sci import config


def key_path(args: argparse.Namespace) -> Path:
    return Path(args.key or config.read().get("key", config.home() / "key.pem")).expanduser()


def key(args: argparse.Namespace) -> Ed25519PrivateKey:
    value = serialization.load_pem_private_key(read_regular(key_path(args), 16384), password=None)
    if not isinstance(value, Ed25519PrivateKey):
        raise Invalid("expected Ed25519 private key")
    return value


def require_signer(bundle: Bundle, public_path: str | None) -> None:
    if public_path:
        public = serialization.load_pem_public_key(read_regular(Path(public_path), 16384))
        if not isinstance(public, Ed25519PublicKey) or public.public_bytes_raw() != jwk_key(
            bundle.manifest["public_key"]
        ):
            raise Invalid("public key mismatch")
