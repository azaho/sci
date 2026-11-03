"""sci verify: verify a signed publication folder."""

from __future__ import annotations

from pathlib import Path

from .. import keys
from .. import publication as pub
from ..output import CliError, Out


def run(args, out: Out) -> int:
    trusted = None
    if args.public_key:
        try:
            trusted = keys.load_public_key(Path(args.public_key))
        except keys.KeyError_ as exc:
            raise CliError(str(exc), "key_error") from exc
    docs = pub.verify_folder(Path(args.folder), trusted)
    for name in pub.undeclared_files(Path(args.folder), docs):
        out.progress(f"warning: {name} is present but not part of the signed publication; sign again to include it")
    out.result(
        f"{docs.uuid}\n{docs.manifest.fingerprint}",
        {
            "uuid": docs.uuid,
            "fingerprint": docs.manifest.fingerprint,
            "manifest_sha256": docs.manifest_sha256,
            "trusted_key_matched": trusted is not None,
            "valid": True,
        },
    )
    return 0



def register(sub, common, raw) -> None:
    v = sub.add_parser(
        "verify", parents=[common], formatter_class=raw,
        help="verify a signed publication folder",
        description="Verify the manifest signature, both document schemas, the approved LICENSE, and every\n"
        "declared file's exact hash and size. Missing or changed files cause failure. Prints the\n"
        "UUID and the signing key fingerprint. No private key or network access.\n\n"
        "Without --public-key this proves integrity under the key embedded in the manifest, not\n"
        "who the publisher is. With --public-key PATH (an Ed25519 SubjectPublicKeyInfo PEM file),\n"
        "verification also requires that key.",
    )
    v.add_argument("folder")
    v.add_argument("--public-key", metavar="PATH", help="require this trusted signer public key (PEM)")
    v.set_defaults(func=run)
