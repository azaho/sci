from __future__ import annotations
import argparse
import shutil
from pathlib import Path
from typing import Any
from pysci import Invalid, load_bundle, prepare
from pysci.bundle import read_directory
from sci.output import CommandError
from sci.storage.files import atomic
from sci.storage.keys import key, key_path
from sci.storage.bundles import pack
from sci.output import result


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.path).resolve()
    selected = key_path(args).resolve()
    if (
        selected.is_relative_to(root)
        or args.output
        and selected.is_relative_to(Path(args.output).resolve())
    ):
        raise Invalid("private key must be outside publication inputs and outputs")
    data = read_directory(root, draft=True)
    b = prepare(data, key(args))
    if not b.baseline and not args.allow_nonbaseline:
        raise CommandError(
            "not_baseline", "Use --allow-nonbaseline for a valid non-baseline draft", 6
        )
    if (root / "manifest.json").exists() and not args.replace:
        old = load_bundle(root)
        if (
            old.signer == b.signer
            and all(old.entries.get(p) == raw for p, raw in data.items())
            and set(old.manifest["files"]) == set(data)
        ):
            b = old
        elif not args.replace:
            raise Invalid("signed input changed; use --replace")
    # Recoverable staging directory keeps both generated files together on interruption.
    staged = root / ".sci-sign-pending"
    if staged.exists():
        raise CommandError("pending_sign", "Recover or remove .sci-sign-pending before signing", 5)
    staged.mkdir()
    try:
        for name in ("manifest.json", "manifest.json.sig"):
            atomic(staged / name, b.entries[name])
        if read_directory(root, draft=True) != {
            **data,
            **{
                ".sci-sign-pending/manifest.json": b.entries["manifest.json"],
                ".sci-sign-pending/manifest.json.sig": b.entries["manifest.json.sig"],
            },
        }:
            raise Invalid("draft changed during signing")
        changed = (
            not (root / "manifest.json.sig").exists()
            or (root / "manifest.json.sig").read_bytes() != b.entries["manifest.json.sig"]
        )
        for name in ("manifest.json", "manifest.json.sig"):
            atomic(root / name, b.entries[name], replace=True)
        if changed:
            (root / "manifest.json.sig.tsr").unlink(missing_ok=True)
        shutil.rmtree(staged)
    except Exception:
        # Staged pair deliberately remains for explicit recovery.
        raise
    out = result(load_bundle(root))
    if args.output:
        pack(b, Path(args.output), root)
    if not args.no_timestamp:
        from sci.clients.tsa import timestamp

        try:
            out["timestamps"] = timestamp(args)
            if out["timestamps"].get("exit_code"):
                out.update(status="partial", exit_code=5)
        except Exception as exc:
            out.update(status="partial", timestamps=str(exc), exit_code=5)
    if args.output:
        pack(b, Path(args.output), root)
    return out


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "sign",
        help="Sign a draft; request a timestamp online unless --no-timestamp. Preserve signed files on TSA failure.",
        description="Sign a draft; request a timestamp online unless --no-timestamp. Preserve signed files on TSA failure.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci sign ./draft --no-timestamp --output ./campaign",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.add_argument("--key", help="Private PEM path; default SCI_HOME/key.pem")
    c.add_argument("--output", help="Campaign folder for the ZIP")
    c.add_argument("--tsa", help="TSA URL; default configured TSA or https://freetsa.org/tsr")
    c.add_argument("--replace", action="store_true")
    c.add_argument("--no-timestamp", action="store_true")
    c.add_argument("--allow-nonbaseline", action="store_true")
    c.set_defaults(handler=run)
