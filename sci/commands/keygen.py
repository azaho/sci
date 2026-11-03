"""sci keygen: create a signing key pair."""

from __future__ import annotations

from pathlib import Path

from .. import keys
from ..config import default_key_path
from ..output import CliError, Out


def run(args, out: Out) -> int:
    path = Path(args.key).expanduser() if args.key else default_key_path()
    try:
        priv_path, pub_path, key = keys.write_new_key_pair(path)
    except keys.KeyError_ as exc:
        raise CliError(str(exc), "key_exists") from exc
    pk = key.public_key()
    info = {
        "private_key": str(priv_path),
        "public_key": str(pub_path),
        "fingerprint": keys.fingerprint(pk),
        "public_key_base64url": keys.b64url_encode(keys.raw_public_key(pk)),
    }
    out.result(
        f"Private key: {priv_path} (keep it secret; never put it in a publication folder)\n"
        f"Public key:  {pub_path}\n"
        f"Fingerprint: {info['fingerprint']}\n"
        f"Ask a server administrator to enable this public key:\n  {info['public_key_base64url']}",
        info,
    )
    return 0



def register(sub, common, raw) -> None:
    k = sub.add_parser(
        "keygen", parents=[common], formatter_class=raw,
        help="create a signing key pair",
        description="Create an Ed25519 key pair: key.pem (PKCS#8, owner-only permissions) and key.pub.pem\n"
        "(SubjectPublicKeyInfo). Existing files are never overwritten. Prints the fingerprint and\n"
        "the base64url public key that a server administrator enables.\n\n"
        "Default location: ~/.sci/key.pem. With --key PATH, the public key is PATH with .pem\n"
        "replaced by .pub.pem (or .pub.pem appended).",
    )
    k.add_argument("--key", metavar="PATH", help="private key path")
    k.set_defaults(func=run)
