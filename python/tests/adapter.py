#!/usr/bin/env python3
"""JSON-lines conformance adapter for protocol/test-vectors (adapter_version 1).

Reads one request per stdin line and writes one result per line to stdout.
Context fields such as ``transport`` and ``verification_budget_bytes`` are
test-harness vocabulary; they are mapped onto library inputs here.

Run:  python3 protocol/test-vectors/tools/run_adapter.py -- python python/tests/adapter.py
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pysci.accounts import Account
from pysci.bundle import Scope, VerificationLimits, verify_loose, verify_zip
from pysci.discovery import (
    parse_descriptor,
    parse_publication_lookup,
    parse_timestamp_lookup,
)
from pysci.errors import Outcome, SciError
from pysci.manifest import verify_manifest
from pysci.records import verify_claim, verify_revocation
from pysci.rfc3339 import format_rfc3339, parse_rfc3339
from pysci.source import ByteSource, PathSource
from pysci.submission import parse_submission_request, parse_submission_response
from pysci.templates import parse_template
from pysci.timestamps import RevocationPolicy, TrustConfig, verify_timestamp_file

Result = dict[str, Any]


def _outcome_of(action: Any) -> str:
    try:
        action()
    except SciError as exc:
        return exc.outcome.value
    return Outcome.VALID.value


def publication(case: dict[str, Any], corpus: Path) -> Result:
    ctx = case.get("context", {})
    if ctx.get("transport") == "unavailable":
        return {"outcome": "incomplete", "baseline": None}
    budget = ctx.get("verification_budget_bytes")
    limits = (
        VerificationLimits(max_input_bytes=budget) if budget is not None else VerificationLimits()
    )
    scope = Scope(ctx.get("scope", "complete"))
    expected_id = ctx.get("publication_id")
    artifacts: dict[str, str] = case["artifacts"]
    if "bundle.zip" in artifacts:
        result = verify_zip(
            corpus / artifacts["bundle.zip"], expected_id=expected_id, scope=scope, limits=limits
        )
    else:
        sources: dict[str, ByteSource | bytes] = {
            name: PathSource(corpus / path) for name, path in artifacts.items()
        }
        result = verify_loose(
            sources, expected_id=expected_id, scope=scope, limits=limits, compute_zip_sha256=True
        )
    reply: Result = {"outcome": result.outcome.value, "baseline": result.baseline}
    if result.ok and result.zip_bytes is not None:
        reply["zip_bytes"] = result.zip_bytes
        if scope is Scope.MANIFEST:
            reply["within_baseline_zip_limit"] = result.within_baseline_zip_limit
    if result.zip_sha256 is not None:
        reply["zip_sha256"] = result.zip_sha256
    if result.issues:
        reply["issues"] = [str(issue) for issue in result.issues[:5]]
    return reply


def claim(case: dict[str, Any], corpus: Path) -> Result:
    ctx = case["context"]
    if ctx.get("transport") == "unavailable":
        return {"outcome": "incomplete"}
    record = (corpus / case["artifacts"]["record.json"]).read_bytes()
    signature = (corpus / case["artifacts"]["record.json.sig"]).read_bytes()
    requested = ctx["requested_account"]
    result = verify_claim(
        record,
        signature,
        expected_public_key=ctx["expected_public_key_x"],
        requested_account=Account(requested["type"], requested["id"]),
        now=parse_rfc3339(ctx["now"]),
        retrieved_from_account_location=ctx["retrieved_from_account_location"],
        known_revocation=ctx["known_revocation"],
    )
    return {"outcome": result.outcome.value, "issues": [str(i) for i in result.issues]}


def revocation(case: dict[str, Any], corpus: Path) -> Result:
    ctx = case["context"]
    record = (corpus / case["artifacts"]["record.json"]).read_bytes()
    signature = (corpus / case["artifacts"]["record.json.sig"]).read_bytes()
    result = verify_revocation(
        record, signature, expected_public_key=ctx.get("expected_public_key_x")
    )
    return {"outcome": result.outcome.value, "issues": [str(i) for i in result.issues]}


def _accuracy(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def timestamp(case: dict[str, Any], corpus: Path) -> Result:
    ctx = case["context"]
    artifacts = case["artifacts"]
    signature = (corpus / artifacts["signature.bin"]).read_bytes()
    reply: Result = {}
    if ctx.get("record_type") == "revocation":
        record = (corpus / artifacts["revocation.json"]).read_bytes()
        reply["revocation_outcome"] = verify_revocation(record, signature).outcome.value
    else:
        manifest = (corpus / artifacts["manifest.json"]).read_bytes()
        expected = ctx.get("publication_id")
        try:
            verified = verify_manifest(manifest, signature, expected_id=expected)
            reply["publication_outcome"] = "valid"
            reply["publication_id_unchanged"] = expected is None or (
                verified.publication_id == expected
            )
        except SciError as exc:
            reply["publication_outcome"] = exc.outcome.value
    trust = TrustConfig.from_pem(
        anchors=[(corpus / p).read_bytes() for p in ctx["trust_anchors"]],
        crls=[(corpus / p).read_bytes() for p in ctx["crls"]],
        revocation_policy=RevocationPolicy(ctx.get("revocation_policy", "require_current_crl")),
    )
    data = (corpus / artifacts["proofs.tsr"]).read_bytes()
    result = verify_timestamp_file(
        data, signature, trust, at=parse_rfc3339(ctx["verification_time"])
    )
    proofs = []
    for proof in result.proofs:
        item: Result = {"offset": proof.offset, "bytes": proof.length, "result": proof.result.value}
        if proof.result.value == "verified":
            assert proof.gen_time is not None
            item["gen_time"] = format_rfc3339(proof.gen_time)
            if proof.accuracy is not None:
                item["accuracy_seconds"] = _accuracy(proof.accuracy.total_seconds())
        if proof.issues:
            item["issues"] = [str(i) for i in proof.issues]
        proofs.append(item)
    reply.update(
        outcome=result.outcome.value,
        framing=result.framing,
        proofs=proofs,
        stop_offset=result.stop_offset,
    )
    return reply


def discovery(case: dict[str, Any], corpus: Path) -> Result:
    data = (corpus / case["artifacts"]["descriptor.json"]).read_bytes()
    kind = case["context"]["descriptor_kind"]
    return {"outcome": _outcome_of(lambda: parse_descriptor(data, kind))}


def submission(case: dict[str, Any], corpus: Path) -> Result:
    ctx = case["context"]
    if ctx["phase"] == "request":
        data = (corpus / case["artifacts"]["request.json"]).read_bytes()
        return {"outcome": _outcome_of(lambda: parse_submission_request(data))}
    data = (corpus / case["artifacts"]["response.json"]).read_bytes()
    return {
        "outcome": _outcome_of(
            lambda: parse_submission_response(
                ctx["status"], ctx.get("content_type"), data, ctx["requested_id"]
            )
        )
    }


def template(case: dict[str, Any], corpus: Path) -> Result:
    templates = json.loads((corpus / case["artifacts"]["templates.json"]).read_bytes())
    variables = json.loads((corpus / case["artifacts"]["variables.json"]).read_bytes())
    return {
        name: parse_template(text).expand(variables)
        for name, text in templates.items()
        if name != "schema"
    }


def http(case: dict[str, Any], corpus: Path) -> Result:
    ctx = case["context"]
    body = (corpus / case["artifacts"]["response.bin"]).read_bytes()
    # The corpus distinguishes the lookup endpoint only by case ID (both 404
    # fixtures share an identical context), so the endpoint is inferred here.
    if "timestamp" in case["id"] or ctx.get("content_type") == "application/octet-stream":
        lookup = parse_timestamp_lookup(ctx["status"], body)
        return {
            "proofs_retained": lookup.proofs_retained,
            "timestamp_existence": lookup.existence,
            "proofs": lookup.proof_count,
            "readers_must_verify": lookup.readers_must_verify,
        }
    return {"sources": parse_publication_lookup(ctx["status"], body)}


HANDLERS = {
    "publication": publication,
    "claim": claim,
    "revocation": revocation,
    "timestamp": timestamp,
    "discovery": discovery,
    "submission": submission,
    "template": template,
    "http": http,
}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        case = request["case"]
        if request.get("adapter_version") != "1":
            print(f"unsupported adapter version for {case['id']}", file=sys.stderr)
            return 2
        handler = HANDLERS.get(case["kind"])
        if handler is None:
            result: Result = {
                "outcome": "not_implemented",
                "note": f"kind {case['kind']!r} is outside the library scope",
            }
        else:
            try:
                result = handler(case, Path(request["corpus"]))
            except Exception:
                print(f"{case['id']}: adapter error\n{traceback.format_exc()}", file=sys.stderr)
                result = {"outcome": "adapter_error"}
        print(json.dumps({"id": case["id"], "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    started = datetime.now()
    status = main()
    print(f"adapter finished in {(datetime.now() - started).total_seconds():.1f}s", file=sys.stderr)
    raise SystemExit(status)
