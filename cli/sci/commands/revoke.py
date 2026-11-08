from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from pysci import Invalid
from pysci.bundle import canonical_json, public_jwk
from sci.storage.files import atomic
from sci.output import CommandError
from sci.storage.keys import key, key_path


def run(args: argparse.Namespace) -> dict[str, Any]:
    from pysci.validation import check_record, strict_json

    if bool(args.output) == bool(args.submit):
        raise CommandError("action_required", "Choose exactly one of --output or --submit")
    if args.output:
        import sys

        if sys.stdin.isatty():
            print(
                "This creates a permanent key revocation. Anyone holding the record and signature can submit it. Creation does not submit it.",
                file=sys.stderr,
            )
        k = key(args)
        raw = canonical_json({"schema": "sci-key-revocation/0.1", "public_key": public_jwk(k)})
        root = Path(args.output)
        if (root / "manifest.json").exists() or key_path(args).resolve().is_relative_to(
            root.resolve()
        ):
            raise Invalid("revocation output must be outside publication/key directories")
        atomic(root / "revocation.json", raw)
        atomic(root / "revocation.json.sig", k.sign(raw))
        return {
            "revocation": str(root),
            "effect": "Permanent once submitted; creation alone does not distribute this record.",
        }
    root = Path(args.submit)
    raw = (root / "revocation.json").read_bytes()
    sig = (root / "revocation.json.sig").read_bytes()
    obj = strict_json(raw)
    check_record(raw, sig, "revocation", {"expected_public_key_x": obj["public_key"]["x"]})
    from sci.clients.network import Network, index

    net = Network(args.timeout)
    d = index(net, args.to)
    if "revocation_url" not in d:
        raise CommandError("unsupported_capability", "No revocation submission route", 4)
    import httpx

    # Construct multipart bytes locally; Network owns deadlines and retry behavior.
    request = httpx.Request(
        "POST",
        d["revocation_url"],
        files={
            **(
                {
                    "revocation.json.sig.tsr": (
                        "revocation.json.sig.tsr",
                        (root / "revocation.json.sig.tsr").read_bytes(),
                        "application/octet-stream",
                    )
                }
                if (root / "revocation.json.sig.tsr").exists()
                else {}
            ),
            "revocation.json": ("revocation.json", raw, "application/json"),
            "revocation.json.sig": ("revocation.json.sig", sig, "application/octet-stream"),
        },
    )
    return dict(
        strict_json(net.request(d["revocation_url"], "POST", request.read(), dict(request.headers)))
    )


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "revoke",
        help="Create a permanent key revocation offline, or submit an existing record online. Creation alone does not distribute it.",
        description="Create a permanent key revocation offline, or submit an existing record online. Creation alone does not distribute it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci revoke --output ./revocation",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    action = c.add_mutually_exclusive_group(required=True)
    action.add_argument("--output")
    action.add_argument("--submit")
    c.add_argument("--key")
    c.add_argument("--to")
    c.set_defaults(handler=run)
