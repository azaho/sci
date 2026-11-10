"""``sci keygen``: create a signing key pair."""

from __future__ import annotations

import argparse

from ..context import Context
from ..output import Result
from ..spec import Command, arg
from ..storage.keys import create_key_pair, resolve_key_path


def run(args: argparse.Namespace, ctx: Context) -> Result:
    path = resolve_key_path(args.key, None, ctx.home)
    if args.key is None:
        ctx.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    public, public_path = create_key_pair(path)
    result = Result("keygen")
    result.data = {
        "private_key": str(path),
        "public_key_file": str(public_path),
        "public_key": public.to_jwk(),
        "x": public.x,
    }
    result.say(f"private key: {path} (owner-only access)")
    result.say(f"public key:  {public_path}")
    result.say(f"x:           {public.x}")
    result.say("Keep the private key outside publication folders and back it up securely.")
    return result


COMMAND = Command(
    name="keygen",
    usage_hint="keygen [--key PATH] [--json]",
    summary="Create an Ed25519 signing key pair.",
    description="""
Creates a PKCS#8 private key and a SubjectPublicKeyInfo public key (SCI
protocol §5.1). The private key file is readable only by its owner. Existing
files are never overwritten. The public key file name replaces a final .pem
with .pub.pem, or appends .pub.pem. --key also selects the key for `sign`,
`claim`, and `revoke`; to make a custom path the default, set "key" in
config.json.
""",
    args=(
        arg(
            "--key",
            metavar="PATH",
            help="private key path to create (default: <state>/key.pem, state is ~/.sci or "
            "$SCI_HOME)",
        ),
    ),
    outputs="The public JWK and its x value, and both file paths.",
    files="Creates PATH (mode 0600) and its .pub.pem companion; rejects existing outputs.",
    network="None.",
    exit_codes={2: "an output file already exists or the location is not writable"},
    examples=("sci keygen", "sci keygen --key ~/keys/lab.pem --json"),
    run=run,
)
