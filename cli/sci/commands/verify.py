from __future__ import annotations
from sci.storage.keys import require_signer
import argparse
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives import serialization
from pysci import load_bundle
from sci import config
from sci.storage.files import atomic
from sci.output import result


def run(args: argparse.Namespace) -> dict[str, Any]:
    b = load_bundle(Path(args.path))
    require_signer(b, args.public_key)
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


def configure(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "verify",
        help="Verify a signed directory or ZIP offline; optional online timestamp trust checks. Writes only a cached CRL when online checks are enabled.",
        description="Verify a signed directory or ZIP offline; optional online timestamp trust checks. Writes only a cached CRL when online checks are enabled.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Outputs JSON. Exit codes: 0 complete, 2 arguments/config, 3 invalid content, 4 unsupported, 5 partial/incomplete, 6 policy refusal. Example: sci verify ./campaign/ID.zip",
    )
    c.add_argument("--timeout", type=float, default=120, help="Total network deadline in seconds")
    c.add_argument("path")
    c.add_argument("--public-key", help="Require this public PEM signer")
    c.add_argument("--check-timestamp-online", action="store_true")
    c.set_defaults(handler=run)
