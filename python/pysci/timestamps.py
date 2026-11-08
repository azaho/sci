"""RFC 3161 framing and imprint validation; never equate an imprint with trusted time."""

from __future__ import annotations
import hashlib
from typing import Any
from asn1crypto import tsp  # type: ignore[import-untyped]
from .validation import Invalid


def frame(data: bytes, offset: int) -> int | None:
    pos = offset
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1
    if tag & 31 == 31:
        first = True
        while True:
            if pos >= len(data):
                return None
            value = data[pos]
            pos += 1
            if first and value & 127 == 0:
                return None
            first = False
            if not value & 128:
                break
    if pos >= len(data):
        return None
    value = data[pos]
    pos += 1
    if value < 128:
        length = value
    else:
        n = value & 127
        if n == 0 or n > 8 or pos + n > len(data) or data[pos] == 0:
            return None
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
        if length < 128:
            return None
    return pos + length if length <= len(data) - pos else None


def request_bytes(signature: bytes) -> bytes:
    return bytes(
        tsp.TimeStampReq(
            {
                "version": "v1",
                "message_imprint": {
                    "hash_algorithm": {"algorithm": "sha256"},
                    "hashed_message": hashlib.sha256(signature).digest(),
                },
                "cert_req": True,
            }
        ).dump()
    )


def validate_response(raw: bytes, signature: bytes) -> dict[str, Any]:
    if not raw or frame(raw, 0) != len(raw):
        raise Invalid("timestamp DER framing")
    try:
        response = tsp.TimeStampResp.load(raw, strict=True)
        if response.dump(force=True) != raw:
            raise Invalid("timestamp not DER")
        if response["status"]["status"].native not in ("granted", "granted_with_mods"):
            raise Invalid("TSA did not grant request")
        token = response["time_stamp_token"]
        if token["content_type"].native != "signed_data":
            raise Invalid("timestamp CMS")
        info = token["content"]["encap_content_info"]["content"].parsed
        imprint = info["message_imprint"]
        if (
            imprint["hash_algorithm"]["algorithm"].native != "sha256"
            or imprint["hashed_message"].native != hashlib.sha256(signature).digest()
        ):
            raise Invalid("timestamp imprint mismatch")
        return {
            "gen_time": info["gen_time"].native.isoformat(),
            "accuracy": info["accuracy"].native,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise Invalid("malformed timestamp") from exc


def verify_proofs(
    data: bytes, signature: bytes, anchors: list[bytes], crls: list[bytes], verification_time: str
) -> dict[str, Any]:
    """Use OpenSSL's RFC 3161/ESS/CMS verifier with explicit trust and revocation data.

    An offline trusted result requires supplied CRLs. No OS trust store or network
    is implicitly consulted. Each complete DER value is independent.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    from .validation import Incomplete, rfc3339

    executable = os.environ.get("SCI_OPENSSL") or shutil.which("openssl")
    if not executable:
        raise Incomplete("OpenSSL is required for timestamp trust verification")
    proofs: list[dict[str, Any]] = []
    pos = 0
    with tempfile.TemporaryDirectory(prefix="sci-timestamp-") as tmp:
        root = Path(tmp)
        (root / "signature.bin").write_bytes(signature)
        (root / "trust.pem").write_bytes(b"".join(anchors + crls))
        while pos < len(data):
            end = frame(data, pos)
            if end is None:
                break
            raw = data[pos:end]
            proof: dict[str, Any] = {"offset": pos, "bytes": end - pos, "result": "invalid"}
            try:
                validate_response(raw, signature)
                # Verify the CMS equation even without a trust anchor. An untrusted
                # timestamp must not be a malformed or incorrectly signed token.
                response = tsp.TimeStampResp.load(raw, strict=True)
                (root / "token.der").write_bytes(response["time_stamp_token"].dump())
                cryptographic = subprocess.run(
                    [
                        executable,
                        "cms",
                        "-verify",
                        "-inform",
                        "DER",
                        "-in",
                        str(root / "token.der"),
                        "-noverify",
                        "-out",
                        os.devnull,
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if cryptographic.returncode:
                    raise Invalid("timestamp CMS signature")
                if not anchors:
                    proof["result"] = "untrusted"
                elif not crls:
                    proof["result"] = "untrusted"
                else:
                    (root / "proof.tsr").write_bytes(raw)
                    command = [
                        executable,
                        "ts",
                        "-verify",
                        "-data",
                        str(root / "signature.bin"),
                        "-in",
                        str(root / "proof.tsr"),
                        "-CAfile",
                        str(root / "trust.pem"),
                        "-attime",
                        str(int(rfc3339(verification_time).timestamp())),
                        "-crl_check_all",
                    ]
                    checked = subprocess.run(command, capture_output=True, timeout=10)
                    if checked.returncode:
                        raise Invalid("timestamp chain, revocation or ESS binding")
                    info = response["time_stamp_token"]["content"]["encap_content_info"][
                        "content"
                    ].parsed
                    proof.update(
                        result="verified",
                        gen_time=info["gen_time"].native.isoformat().replace("+00:00", "Z"),
                        accuracy_seconds=info["accuracy"]["seconds"].native
                        if info["accuracy"].native is not None
                        else None,
                    )
            except (Invalid, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                pass
            proofs.append(proof)
            pos = end
    framing = "complete" if data and pos == len(data) else "malformed"
    all_good = framing == "complete" and all(p["result"] == "verified" for p in proofs)
    any_good = any(p["result"] == "verified" for p in proofs)
    untrusted = framing == "complete" and all(p["result"] == "untrusted" for p in proofs)
    return {
        "outcome": "valid"
        if all_good
        else "partial"
        if any_good
        else "untrusted"
        if untrusted
        else "invalid",
        "framing": framing,
        "proofs": proofs,
        "stop_offset": pos,
    }
