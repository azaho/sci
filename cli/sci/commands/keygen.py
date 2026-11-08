from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pysci import Invalid
from pysci.bundle import public_jwk
from sci.storage.files import atomic
from sci.storage.keys import key_path


def run(args: argparse.Namespace) -> dict[str, Any]:
    private = key_path(args)
    public = (
        private.with_name(private.stem + ".pub.pem")
        if private.suffix == ".pem"
        else Path(str(private) + ".pub.pem")
    )
    if private.exists() or public.exists():
        raise Invalid("key outputs already exist")
    k = Ed25519PrivateKey.generate()
    atomic(
        private,
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    atomic(
        public,
        k.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    return {
        "public_key": public_jwk(k),
        "x": public_jwk(k)["x"],
        "private_key_path": str(private),
        "public_key_path": str(public),
    }


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "keygen",
        help="Create an Ed25519 key pair offline with an owner-only private PEM; refuses existing outputs.",
        description="Create an Ed25519 key pair offline with an owner-only private PEM; refuses existing outputs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci keygen --key ./keys/key.pem",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("--key", help="Private PEM path; default SCI_HOME/key.pem")
    c.set_defaults(handler=run)
