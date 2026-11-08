from __future__ import annotations
import argparse
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pysci import Bundle, Invalid, load_bundle, prepare
from pysci.bundle import read_directory, read_regular
from sci import config
from sci.output import CommandError
from sci.storage.files import atomic


def key_path(args: argparse.Namespace) -> Path:
    return Path(args.key or config.read().get("key", config.home() / "key.pem")).expanduser()


def key(args: argparse.Namespace) -> Ed25519PrivateKey:
    value = serialization.load_pem_private_key(read_regular(key_path(args), 16384), password=None)
    if not isinstance(value, Ed25519PrivateKey):
        raise Invalid("expected Ed25519 private key")
    return value


def result(bundle: Bundle) -> dict[str, Any]:
    return {
        "id": "sha256:" + bundle.id,
        "verification": "complete",
        "baseline": bundle.baseline,
        "signer": bundle.signer,
        "associations": "not_checked",
        "revocation": "not_checked",
        "timestamps": "not_checked",
    }


def license(args: argparse.Namespace) -> dict[str, str]:
    root = Path(args.path)
    if not root.is_dir() or (root / "manifest.json").exists():
        raise Invalid("license requires an unsigned draft directory")
    atomic(root / "LICENSE", files("pysci").joinpath("sci-baseline-LICENSE.txt").read_bytes())
    return {"license": str(root / "LICENSE")}


def check(args: argparse.Namespace) -> dict[str, Any]:
    # An ephemeral public key has exactly the same serialized length as the selected key.
    # Draft checks do not read a user's private key or write outputs.
    b = prepare(read_directory(Path(args.path), draft=True), Ed25519PrivateKey.generate())
    return {"valid": True, "baseline": b.baseline, "planned_zip_bytes": len(b.zip())}


def pack(b: Bundle, destination: Path) -> None:
    atomic(destination / (b.id + ".zip"), b.zip())
    config.remember(destination)


def sign(args: argparse.Namespace) -> dict[str, Any]:
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
        pack(b, Path(args.output))
    if not args.no_timestamp:
        from sci.commands.identity import timestamp

        try:
            out["timestamps"] = timestamp(args)
            if out["timestamps"].get("exit_code"):
                out.update(status="partial", exit_code=5)
        except Exception as exc:
            out.update(status="partial", timestamps=str(exc), exit_code=5)
    return out


def verify_command(args: argparse.Namespace) -> dict[str, Any]:
    b = load_bundle(Path(args.path))
    if args.public_key:
        pub = serialization.load_pem_public_key(Path(args.public_key).read_bytes())
        if not isinstance(pub, Ed25519PublicKey) or pub.public_bytes_raw() != key_bytes(b):
            raise Invalid("public key mismatch")
    out = result(b)
    sidecar = (
        Path(args.path) / "manifest.json.sig.tsr"
        if Path(args.path).is_dir()
        else Path(args.path).with_name(b.id + ".tsr")
    )
    if sidecar.exists():
        from pysci.timestamps import verify_proofs
        from importlib.resources import files
        from datetime import datetime, timezone

        cfg = config.read()
        anchors = [Path(p).read_bytes() for p in cfg.get("trust_anchors", [])] or [
            files("pysci").joinpath("freetsa.pem").read_bytes()
        ]
        crls = [Path(p).read_bytes() for p in cfg.get("crls", [])]
        cached = config.home() / "freetsa.crl.pem"
        if args.check_timestamp_online:
            from sci.clients.network import Network
            from cryptography import x509

            raw = Network(args.timeout).request(
                "https://www.freetsa.org/crl/root_ca.crl", limit=1024 * 1024
            )
            if not raw.startswith(b"-----BEGIN"):
                raw = x509.load_der_x509_crl(raw).public_bytes(serialization.Encoding.PEM)
            atomic(cached, raw, replace=True)
        if not crls and cached.exists():
            crls = [cached.read_bytes()]
        out["timestamps"] = verify_proofs(
            sidecar.read_bytes(),
            b.entries["manifest.json.sig"],
            anchors,
            crls,
            datetime.now(timezone.utc).isoformat(),
        )
    return out


def key_bytes(b: Bundle) -> bytes:
    from pysci.validation import jwk_key

    return jwk_key(b.manifest["public_key"])


def candidates(path: Path) -> list[Path]:
    found = (
        [path]
        if path.is_file() or (path / "manifest.json").exists()
        else sorted(path.glob("*.zip"))
    )
    if not found:
        raise Invalid("no signed publications")
    return found


def publish(args: argparse.Namespace) -> dict[str, Any]:
    destinations = sorted(
        set(
            x for d in args.to for x in (config.read().get("folders", []) if d == "@local" else [d])
        )
    )
    if not destinations:
        raise CommandError("empty_destinations", "No publishing destination configured")
    inputs = candidates(Path(args.path))
    results: list[dict[str, Any]] = []
    for src in inputs:
        try:
            b = load_bundle(src)
        except Exception as exc:
            results.append({"input": str(src), "error": str(exc)})
            continue
        for dest in destinations:
            try:
                if "://" in dest or dest.startswith("git@"):
                    from sci.clients.hosting import host

                    location = host(b, dest, args.create, args.dry_run)
                elif args.dry_run:
                    location = str(Path(dest) / (b.id + ".zip"))
                else:
                    pack(b, Path(dest))
                    location = str(Path(dest) / (b.id + ".zip"))
                results.append({"id": b.id, "destination": location, "dry_run": args.dry_run})
            except Exception as exc:
                results.append({"id": b.id, "destination": dest, "error": str(exc)})
    return {"results": results, "exit_code": 5 if any("error" in x for x in results) else 0}


def read(args: argparse.Namespace) -> dict[str, Any]:
    if Path(args.path).exists():
        b = load_bundle(Path(args.path))
    else:
        from sci.clients.network import retrieve

        b = retrieve(args.path, args.archive, args.source, args.timeout)
    out = result(b)
    if args.list:
        return {**out, "files": b.manifest["files"]}
    path = args.file or "README.md"
    if path not in b.manifest["files"]:
        raise Invalid("requested path is not listed in manifest")
    raw = b.entries[path]
    if args.output:
        atomic(Path(args.output), raw)
        return {**out, "saved": args.output}
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise CommandError("binary_output", "Use --output FILE for binary data")
    lines = text.splitlines()
    if args.start_line < 1 or args.lines < 1:
        raise CommandError("line_range", "Line numbers and count must be positive")
    start, end = args.start_line - 1, args.start_line - 1 + args.lines
    return {
        **out,
        "metadata": b.metadata,
        "text": "\n".join(lines[start:end]),
        "next_line": end + 1 if end < len(lines) else None,
    }


def pull(args: argparse.Namespace) -> dict[str, Any]:
    from sci.clients.network import digest, retrieve

    pid = digest(args.path)
    dest = Path(args.output or pid)
    if not args.zip and dest.exists() and any(dest.iterdir()):
        return result(load_bundle(dest, pid))
    b = retrieve(pid, args.archive, args.source, args.timeout)
    if args.public_key:
        public = serialization.load_pem_public_key(Path(args.public_key).read_bytes())
        if not isinstance(public, Ed25519PublicKey) or public.public_bytes_raw() != key_bytes(b):
            raise Invalid("public key mismatch")
    if args.zip:
        pack(b, dest)
    else:
        if dest.is_symlink():
            raise Invalid("symlink destination")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".sci-pull-") as tmp:
            staged = Path(tmp) / "bundle"
            staged.mkdir()
            for p, raw in b.entries.items():
                atomic(staged / p, raw)
            if dest.exists():
                dest.rmdir()
            staged.rename(dest)
    return result(b)
