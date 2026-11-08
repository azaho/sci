from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from pysci import load_bundle, Invalid
from pysci.bundle import canonical_json
from pysci.discovery import submission
from pysci.validation import strict_json
from sci.clients.network import Network, digest, index
from sci.output import CommandError
from sci.storage.bundles import candidates


def run(args: argparse.Namespace) -> dict[str, Any]:
    ids: list[str] = []
    results: list[dict[str, Any]] = []
    if Path(args.path).exists():
        for candidate in candidates(Path(args.path)):
            try:
                ids.append(load_bundle(candidate).id)
            except Exception as exc:
                results.append(
                    {"input": str(candidate), "error": str(exc), "status": "invalid_input"}
                )
    else:
        ids.append(digest(args.path))
    account = None
    if args.account:
        from sci.clients.accounts import normalize

        account = {
            "type": args.account[0],
            "id": normalize(args.account[0], args.account[1], args.timeout),
        }
    for pid in sorted(set(ids)):
        try:
            net = Network(args.timeout)
            desc = index(net, args.to)
            if "submit_url" not in desc:
                raise CommandError("unsupported_capability", "Index has no submit route", 4)
            body: dict[str, Any] = {"id": pid, "descriptors": args.archive}
            if account:
                body["account"] = account
            raw = canonical_json(body)
            submission(raw)
            response = strict_json(
                net.request(
                    desc["submit_url"],
                    "POST",
                    raw,
                    {"Content-Type": "application/json"},
                    limit=65536,
                )
            )
            if not isinstance(response, dict) or response.get("id") != pid:
                raise Invalid("submission response ID")
            results.append({**response, "id": "sha256:" + pid})
        except Exception as exc:
            confirmed = isinstance(exc, (Invalid, CommandError)) and not (
                isinstance(exc, CommandError) and exc.exit_code == 5
            )
            results.append(
                {
                    "id": "sha256:" + pid,
                    "error": str(exc),
                    "status": "refused" if confirmed else "outcome_unknown",
                    "exit_code": exc.exit_code
                    if isinstance(exc, CommandError)
                    else 3
                    if isinstance(exc, Invalid)
                    else 5,
                }
            )
    return {"results": results, "exit_code": 5 if any("error" in r for r in results) else 0}


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "submit",
        help="Register hosted publication locations online; no uploads or local folder changes.",
        description="Register hosted publication locations online; no uploads or local folder changes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci submit ./campaign --archive https://archive.example/sci/archive.json --to https://sciscape.io",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.add_argument("--archive", action="append", required=True)
    c.add_argument("--to")
    c.add_argument("--account", nargs=2, metavar=("TYPE", "ID"))
    c.set_defaults(handler=run)
