from __future__ import annotations
from sci.clients.accounts import normalize
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from pysci import Invalid
from pysci.bundle import canonical_json, public_jwk
from pysci.validation import rfc3339
from sci.storage.files import atomic
from sci.output import CommandError
from sci.storage.keys import key, key_path


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.output:
        raise CommandError("output_required", "Supply --output DIR")
    account = normalize(args.type, args.account, args.timeout)
    if key_path(args).resolve().is_relative_to(Path(args.output).resolve()):
        raise Invalid("claim output must not contain the private key")
    k = key(args)
    jwk = public_jwk(k)
    expiry = args.expires or (datetime.now(timezone.utc) + timedelta(days=90)).isoformat().replace(
        "+00:00", "Z"
    )
    if rfc3339(expiry) <= datetime.now(timezone.utc):
        raise Invalid("claim expiry must be future")
    obj = {
        "schema": "sci-key-claim/0.1",
        "public_key": jwk,
        "account": {"type": args.type, "id": account},
        "expires": expiry,
    }
    raw = canonical_json(obj)
    root = Path(args.output)
    name = jwk["x"] + ".json"
    atomic(root / name, raw, replace=True)
    atomic(root / (name + ".sig"), k.sign(raw), replace=True)
    host = (
        f"https://{account}/.well-known/sci/keys/"
        if args.type == "domain"
        else f"https://raw.githubusercontent.com/{account}/.github/main/sci/keys/"
        if args.type == "github"
        else f"https://huggingface.co/spaces/{account}/README/resolve/main/sci/keys/"
    )
    return {
        "claim": obj,
        "host_at": [host + name, host + name + ".sig"],
        "association": "not_verified_until_hosted",
    }


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "claim",
        help="Write signed account evidence and print its hosting locations; does not upload. Hugging Face normalization performs an online lookup.",
        description="Write signed account evidence and print its hosting locations; does not upload. Hugging Face normalization performs an online lookup.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci claim domain example.org --output ./claims",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("type", choices=["domain", "github", "huggingface"])
    c.add_argument("account")
    c.add_argument("--key")
    c.add_argument("--output", required=True)
    c.add_argument("--expires", help="RFC3339 expiry; default 90 days")
    c.set_defaults(handler=run)
