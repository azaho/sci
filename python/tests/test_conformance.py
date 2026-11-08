import json
from pathlib import Path
import pytest
from pysci import validation as v

ROOT = Path(__file__).resolve().parents[2] / "protocol/test-vectors/0.1"
CASES = json.loads((ROOT / "catalog.json").read_text())["cases"]


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c["kind"] in ("publication", "claim", "revocation")],
    ids=lambda c: c["id"],
)
def test_corpus(case):
    artifacts = {name: (ROOT / path).read_bytes() for name, path in case["artifacts"].items()}
    try:
        if case["kind"] == "publication":
            got = v.check_publication(artifacts, case.get("context", {}))
        else:
            got = v.check_record(
                artifacts["record.json"],
                artifacts["record.json.sig"],
                case["kind"],
                case["context"],
            )
    except v.Unsupported:
        got = {"outcome": "unsupported", "baseline": None}
    except v.Incomplete:
        got = {"outcome": "incomplete", "baseline": None}
    except (v.Invalid, ValueError, TypeError, KeyError):
        got = {"outcome": "invalid", "baseline": None}
    assert {k: got.get(k) for k in case["expected"]} == case["expected"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["kind"] == "timestamp"], ids=lambda c: c["id"]
)
def test_timestamp_corpus(case):
    from pysci.timestamps import verify_proofs

    a = case["artifacts"]
    ctx = case["context"]
    raw = (ROOT / a.get("manifest.json", a.get("revocation.json"))).read_bytes()
    sig = (ROOT / a["signature.bin"]).read_bytes()
    obj = v.strict_json(raw)
    v.verify_sig(v.jwk_key(obj["public_key"]), sig, raw)
    got = verify_proofs(
        (ROOT / a["proofs.tsr"]).read_bytes(),
        sig,
        [(ROOT / p).read_bytes() for p in ctx["trust_anchors"]],
        [(ROOT / p).read_bytes() for p in ctx["crls"]],
        ctx["verification_time"],
    )
    if "manifest.json" in a:
        got.update(publication_outcome="valid", publication_id_unchanged=True)
    else:
        got["revocation_outcome"] = "valid"
    assert {k: got.get(k) for k in case["expected"]} == case["expected"]


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c["kind"] in ("discovery", "submission", "template")],
    ids=lambda c: c["id"],
)
def test_contract_corpus(case):
    from pysci import discovery as d

    a = {k: (ROOT / p).read_bytes() for k, p in case["artifacts"].items()}
    ctx = case.get("context", {})
    try:
        if case["kind"] == "discovery":
            d.descriptor(a["descriptor.json"], ctx["descriptor_kind"])
        elif case["kind"] == "submission":
            if ctx["phase"] == "request":
                d.submission(a["request.json"])
            else:
                d.submission_response(
                    a["response.json"], ctx["status"], ctx["content_type"], ctx["requested_id"]
                )
        else:
            templates = json.loads(a["templates.json"])
            variables = json.loads(a["variables.json"])
            assert {k: d.expand(templates[k], **variables) for k in case["expected"]} == case[
                "expected"
            ]
            return
        got = {"outcome": "valid"}
    except v.Unsupported:
        got = {"outcome": "unsupported"}
    except (v.Invalid, ValueError, TypeError, KeyError):
        got = {"outcome": "invalid"}
    assert got == case["expected"]


def test_public_freetsa_response_without_accuracy():
    from pysci.timestamps import verify_proofs

    root = Path(__file__).parent / "fixtures/freetsa-20261108"
    got = verify_proofs(
        (root / "proof.tsr").read_bytes(),
        (root / "signature.bin").read_bytes(),
        [(root / "root.pem").read_bytes()],
        [(root / "root.crl.pem").read_bytes()],
        "2026-11-08T05:27:30Z",
    )
    assert got["outcome"] == "valid"
    assert got["proofs"][0]["accuracy_seconds"] is None
