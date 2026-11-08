from __future__ import annotations
import argparse
import sys
from pathlib import Path
from typing import Any
from pysci import Invalid, Incomplete, Unsupported
from sci import config
from sci.output import CommandError, emit
from sci.commands import local


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sci",
        description="SCI signed research publications (protocol 0.1 draft).",
        epilog="Local: sci keygen; sci license draft; sci sign draft --no-timestamp --output campaign; sci verify campaign/ID.zip. "
        "Online: sci publish campaign --to https://github.com/OWNER/REPO; sci submit ID --archive URL --to https://sciscape.io. "
        "Exit codes: 0 completed, 2 arguments/config, 3 invalid, 4 unsupported, 5 partial/incomplete, 6 policy refusal.",
    )
    p.add_argument("--version", action="version", version="sci-cli 0.1.0a1")
    sub = p.add_subparsers(dest="command", required=True)
    descriptions = {
        "check": "Validate a draft offline; writes nothing and reads no private key.",
        "license": "Install exact baseline license in an unsigned draft offline.",
        "sign": "Sign a draft; timestamp online unless --no-timestamp. Preserve signed files on TSA failure.",
        "verify": "Verify a directory or ZIP offline; report baseline separately.",
        "read": "Read verified text from a local bundle offline or retrieve an ID online.",
        "pull": "Retrieve and verify a complete publication online before saving it.",
        "publish": "Store verified publications in folders or hosted archives; does not submit to an index.",
        "submit": "Register hosted publications with an index online.",
        "keygen": "Create owner-only private PEM and public PEM files offline; refuses existing files.",
        "claim": "Create signed account evidence; requires hosting separately.",
        "revoke": "Create a permanent signed revocation offline or submit it online.",
        "timestamp": "Obtain timestamp proof online; preserves original publication bytes.",
        "index": "Show or save default HTTPS index origin offline.",
        "folders": "List, add or forget local campaign folders; never deletes publications.",
        "search": "Search verified local ZIPs or remote catalog metadata. Default: current folder offline.",
    }
    for name, description in descriptions.items():
        c = sub.add_parser(
            name,
            help=description,
            description=description,
            epilog="Outputs JSON. Exit codes: 0 complete, 2 config, 3 invalid, 4 unsupported, 5 partial, 6 policy. Example: sci "
            + {
                "keygen": "keygen",
                "index": "index https://sciscape.io",
                "folders": "folders add ./campaign",
                "search": "search decoding --in ./campaign",
                "claim": "claim domain example.org --output ./claims",
                "revoke": "revoke --output ./revocation",
            }.get(name, name + " ./draft"),
        )
        c.add_argument(
            "--timeout",
            type=float,
            default=120,
            help="Total network deadline in seconds (default: 120)",
        )
        if name in (
            "check",
            "license",
            "sign",
            "verify",
            "read",
            "pull",
            "publish",
            "submit",
            "timestamp",
        ):
            c.add_argument("path")
        if name in ("keygen", "sign", "claim", "revoke"):
            c.add_argument("--key", help="Private PEM path; default SCI_HOME/key.pem")
        if name in ("sign", "read", "pull", "claim", "revoke"):
            c.add_argument("--output")
        if name in ("sign", "timestamp"):
            c.add_argument("--tsa", help="TSA URL; default https://freetsa.org/tsr")
        if name == "sign":
            c.add_argument("--replace", action="store_true")
            c.add_argument("--no-timestamp", action="store_true")
            c.add_argument("--allow-nonbaseline", action="store_true")
        if name in ("verify", "pull"):
            c.add_argument("--public-key")
            c.add_argument("--check-timestamp-online", action="store_true")
        if name == "read":
            group = c.add_mutually_exclusive_group()
            group.add_argument("--file")
            group.add_argument("--list", action="store_true")
            c.add_argument("--start-line", type=int, default=1)
            c.add_argument("--lines", type=int, default=200)
        if name in ("read", "pull"):
            c.add_argument("--archive", action="append", default=[])
            c.add_argument("--in", dest="source")
        if name == "pull":
            c.add_argument("--zip", action="store_true")
        if name == "publish":
            c.add_argument("--to", action="append", required=True)
            c.add_argument("--dry-run", action="store_true")
            c.add_argument("--create", action="store_true")
        if name == "submit":
            c.add_argument("--archive", action="append", required=True)
            c.add_argument("--to")
            c.add_argument("--account", nargs=2)
        if name == "claim":
            c.add_argument("type", choices=["domain", "github", "huggingface"])
            c.add_argument("account")
            c.add_argument("--expires")
        if name == "timestamp":
            c.add_argument("--submit", action="store_true")
            c.add_argument("--to")
        if name == "revoke":
            c.add_argument("--submit")
            c.add_argument("--to")
        if name == "index":
            c.add_argument("url", nargs="?")
        if name == "folders":
            c.add_argument("action", nargs="?", choices=["add", "remove"])
            c.add_argument("folder", nargs="?")
        if name == "search":
            c.add_argument("query", nargs="?", default="")
            c.add_argument("--in", dest="sources", action="append")
            c.add_argument("--limit", type=int, default=20)
            c.add_argument("--category", nargs=2)
            c.add_argument("--key-x")
            c.add_argument("--domain")
    return p


def dispatch(a: argparse.Namespace) -> Any:
    commands = {
        "check": local.check,
        "license": local.license,
        "sign": local.sign,
        "verify": local.verify_command,
        "read": local.read,
        "pull": local.pull,
        "publish": local.publish,
    }
    if a.command in commands:
        return commands[a.command](a)
    if a.command in ("keygen", "claim", "revoke", "timestamp"):
        from sci.commands import identity

        return getattr(identity, a.command)(a)
    if a.command == "submit":
        from sci.commands.remote import submit

        return submit(a)
    if a.command == "search":
        from sci.commands.search import search

        return search(a)
    if a.command == "index":
        c = config.read()
        if a.url:
            from sci.clients.network import origin

            c["index"] = origin(a.url)
            config.save(c)
        return {"index": c.get("index")}
    c = config.read()
    if a.action:
        if not a.folder:
            raise CommandError("folder_required", "Supply a folder")
        path = Path(a.folder).expanduser().resolve()
        if a.action == "add":
            if not path.is_dir():
                raise Invalid("folder does not exist")
            config.remember(path)
        else:
            c["folders"] = [f for f in c.get("folders", []) if f != str(path)]
            config.save(c)
    return {
        "folders": [
            {"path": f, "available": Path(f).is_dir()} for f in config.read().get("folders", [])
        ]
    }


def main() -> None:
    try:
        result = dispatch(parser().parse_args())
        emit(result)
        sys.exit(result.get("exit_code", 0) if isinstance(result, dict) else 0)
    except (Invalid, Unsupported, Incomplete, CommandError, OSError, ValueError) as exc:
        code = (
            exc.exit_code
            if isinstance(exc, CommandError)
            else 4
            if isinstance(exc, Unsupported)
            else 5
            if isinstance(exc, (Incomplete, OSError))
            else 3
        )
        emit(
            {
                "error": {
                    "code": getattr(exc, "code", type(exc).__name__.lower()),
                    "message": str(exc),
                    "retryable": code == 5,
                }
            }
        )
        sys.exit(code)


if __name__ == "__main__":
    main()
