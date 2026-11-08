from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any
from pysci import load_bundle, Invalid
from pysci.bundle import canonical_json
from pysci.discovery import submission
from pysci.validation import strict_json
from sci.clients.network import Network, digest, index
from sci.commands.local import candidates
from sci.output import CommandError


def submit(args: argparse.Namespace) -> dict[str, Any]:
    ids = (
        [load_bundle(p).id for p in candidates(Path(args.path))]
        if Path(args.path).exists()
        else [digest(args.path)]
    )
    results = []
    for pid in sorted(set(ids)):
        try:
            net = Network(args.timeout)
            desc = index(net, args.to)
            if "submit_url" not in desc:
                raise CommandError("unsupported_capability", "Index has no submit route", 4)
            body: dict[str, Any] = {"id": pid, "descriptors": args.archive}
            if args.account:
                body["account"] = {"type": args.account[0], "id": args.account[1]}
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
            results.append(response)
        except Exception as exc:
            results.append({"id": pid, "error": str(exc), "status": "outcome_unknown"})
    return {"results": results, "exit_code": 5 if any("error" in r for r in results) else 0}
