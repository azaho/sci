from __future__ import annotations
import argparse
import fcntl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pysci import Invalid, load_bundle
from pysci.bundle import canonical_json, public_jwk
from pysci.validation import check_domain, rfc3339
from sci import config
from sci.commands.local import key, key_path
from sci.storage.files import atomic
from sci.output import CommandError


def keygen(args: argparse.Namespace) -> dict[str, Any]:
    private = key_path(args)
    public = (
        private.with_name(private.stem + ".pub.pem")
        if private.suffix == ".pem"
        else Path(str(private) + ".pub.pem")
    )
    if private.exists() or public.exists():
        raise Invalid("key outputs already exist")
    k = Ed25519PrivateKey.generate()
    atomic(
        private,
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    atomic(
        public,
        k.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    return {
        "public_key": public_jwk(k),
        "private_key_path": str(private),
        "public_key_path": str(public),
    }


def claim(args: argparse.Namespace) -> dict[str, Any]:
    import re

    if not args.output:
        raise CommandError("output_required", "Supply --output DIR")
    account = args.account
    if args.type == "domain":
        import idna

        account = idna.encode(account.lower().rstrip(".")).decode()
        check_domain(account)
    elif args.type == "github":
        account = account.lower()
        if not re.fullmatch("[a-z0-9]+(?:-[a-z0-9]+)*", account) or len(account) > 39:
            raise Invalid("GitHub login")
    else:
        from sci.clients.network import Network
        from pysci.validation import strict_json
        from urllib.parse import quote

        obj = strict_json(
            Network(args.timeout).request(
                "https://huggingface.co/api/users/" + quote(account, safe="") + "/overview",
                limit=65536,
            )
        )
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise Invalid("Hub account lookup")
        account = obj["name"]
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


def revoke(args: argparse.Namespace) -> dict[str, Any]:
    from pysci.validation import check_record, strict_json

    if bool(args.output) == bool(args.submit):
        raise CommandError("action_required", "Choose exactly one of --output or --submit")
    if args.output:
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


def timestamp(args: argparse.Namespace) -> dict[str, Any]:
    from pysci.timestamps import frame, validate_response, request_bytes
    from sci.clients.network import Network

    path = Path(args.path)
    if path.is_dir() and (path / "revocation.json").exists():
        from pysci.validation import strict_json, check_record

        raw_record = (path / "revocation.json").read_bytes()
        sig = (path / "revocation.json.sig").read_bytes()
        obj = strict_json(raw_record)
        check_record(
            raw_record, sig, "revocation", {"expected_public_key_x": obj["public_key"]["x"]}
        )
        sidecar = path / "revocation.json.sig.tsr"
        publication_id = None
    else:
        b = load_bundle(path)
        sig = b.entries["manifest.json.sig"]
        sidecar = path / "manifest.json.sig.tsr" if path.is_dir() else path.with_name(b.id + ".tsr")
        publication_id = b.id
    url = args.tsa or config.read().get("tsa", "https://freetsa.org/tsr")
    if getattr(args, "submit", False):
        if args.tsa:
            raise CommandError("exclusive_options", "--submit cannot combine with --tsa")
        if publication_id is None:
            raise CommandError(
                "revocation_submission", "Use sci revoke --submit DIR for revocation proofs"
            )
        from sci.clients.network import index
        from pysci.validation import strict_json
        import httpx

        net = Network(args.timeout)
        d = index(net, args.to)
        if "timestamp_submit_url" not in d:
            raise CommandError(
                "unsupported_capability", "Index has no timestamp submission route", 4
            )
        req = httpx.Request(
            "POST",
            d["timestamp_submit_url"],
            data={"id": publication_id},
            files={
                "manifest.json.sig.tsr": (
                    "manifest.json.sig.tsr",
                    sidecar.read_bytes(),
                    "application/octet-stream",
                )
            },
        )
        return dict(
            strict_json(
                net.request(
                    d["timestamp_submit_url"], "POST", req.read(), dict(req.headers), limit=65536
                )
            )
        )
    old = sidecar.read_bytes() if sidecar.exists() else b""
    values = []
    pos = 0
    while pos < len(old):
        end = frame(old, pos)
        if end is None:
            raise Invalid("malformed existing timestamp sidecar; preserved")
        values.append(old[pos:end])
        pos = end
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    # Cross-process limiter shared by CLI invocations on this machine.
    with (home / "tsa.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        last = lock.read().decode()
        delay = max(0, 1 - (time.time() - float(last or 0)))
        if delay:
            time.sleep(delay)
        lock.seek(0)
        lock.truncate()
        lock.write(str(time.time()).encode())
        lock.flush()
        raw = Network(args.timeout).request(
            url,
            "POST",
            request_bytes(sig),
            {"Content-Type": "application/timestamp-query"},
            limit=1024 * 1024,
        )
    validate_response(raw, sig)
    if raw not in values:
        atomic(sidecar, old + raw, replace=True)
    from importlib.resources import files
    from cryptography import x509
    from pysci.timestamps import verify_proofs

    trust = config.read()
    anchors = [Path(p).read_bytes() for p in trust.get("trust_anchors", [])] or [
        files("pysci").joinpath("freetsa.pem").read_bytes()
    ]
    crls = [Path(p).read_bytes() for p in trust.get("crls", [])]
    if not crls and url == "https://freetsa.org/tsr":
        crl = Network(args.timeout).request(
            "https://www.freetsa.org/crl/root_ca.crl", limit=1024 * 1024
        )
        if not crl.startswith(b"-----BEGIN"):
            crl = x509.load_der_x509_crl(crl).public_bytes(serialization.Encoding.PEM)
        atomic(config.home() / "freetsa.crl.pem", crl, replace=True)
        crls = [crl]
    checked = verify_proofs(raw, sig, anchors, crls, datetime.now(timezone.utc).isoformat())
    return {
        "sidecar": str(sidecar),
        "imprint": "verified",
        "trust": checked,
        "exit_code": 0 if checked["outcome"] == "valid" else 5,
        "tsa": url,
    }
