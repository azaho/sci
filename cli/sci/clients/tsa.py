from __future__ import annotations
import argparse
import fcntl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives import serialization
from pysci import Invalid, load_bundle
from sci import config
from sci.storage.files import atomic
from sci.output import CommandError


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
