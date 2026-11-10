"""Consistent check reports: baseline, bundle, timestamps, associations, and revocation.

Every verification-style result carries a ``checks`` object with these five
keys, so that agents can tell failed checks from checks that were not
performed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pysci.bundle import VerificationResult
from pysci.timestamps import ProofResult, ProofVerification, TimestampFileVerification

from .errors import issue_json

CHECK_NAMES = ("bundle", "baseline", "timestamps", "associations", "revocation")

OFFLINE_ASSOCIATIONS = (
    "account associations require retrieving hosted claims (§6.6); not checked by this command"
)
OFFLINE_REVOCATION = (
    "a missing revocation record does not prove a key was never revoked (§5.3); "
    "not checked by this command"
)


def not_performed(reason: str) -> dict[str, Any]:
    return {"status": "not_performed", "reason": reason}


def bundle_check(result: VerificationResult, *, scope_label: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": str(result.outcome),
        "scope": scope_label or str(result.scope),
        "publication_id": result.publication_id,
        "issues": [issue_json(issue) for issue in result.issues],
    }
    if result.zip_bytes is not None:
        out["zip_bytes"] = result.zip_bytes
    return out


def baseline_check(result: VerificationResult) -> dict[str, Any]:
    if result.baseline is None or result.baseline_report is None:
        return not_performed("baseline is evaluated only after complete bundle verification")
    return {
        "status": "eligible" if result.baseline else "not_eligible",
        "reasons": [issue_json(issue) for issue in result.baseline_report.reasons],
    }


def proof_json(proof: ProofVerification) -> dict[str, Any]:
    out: dict[str, Any] = {
        "offset": proof.offset,
        "length": proof.length,
        "result": str(proof.result),
        "issues": [issue_json(issue) for issue in proof.issues],
    }
    if proof.gen_time is not None:
        out["gen_time"] = proof.gen_time.isoformat().replace("+00:00", "Z")
    if proof.accuracy is not None:
        out["accuracy_seconds"] = str(proof.accuracy.total_seconds())
    if proof.tsa_name is not None:
        out["tsa"] = proof.tsa_name
    if proof.signer_subject is not None:
        out["signer"] = proof.signer_subject
    if proof.result is ProofResult.VERIFIED:
        out["certificate_revocation_checked"] = proof.revocation_checked
    return out


def timestamps_check(
    verification: TimestampFileVerification | None,
    *,
    source: str | None,
    online: bool,
    absent_reason: str = "no timestamp sidecar found",
) -> dict[str, Any]:
    if verification is None:
        return {"status": "absent", "reason": absent_reason, "source": source}
    out: dict[str, Any] = {
        "status": str(verification.outcome),
        "source": source,
        "framing": verification.framing,
        "proofs": [proof_json(p) for p in verification.proofs],
        "trust_checks": "online (current CRLs required)" if online else "offline",
    }
    if not online and any(p.result is ProofResult.VERIFIED for p in verification.proofs):
        out["note"] = (
            "certificate revocation status was checked only against locally configured CRLs; "
            "use --check-timestamp-online for current CRLs"
        )
    return out


def checks(
    *,
    bundle: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
    timestamps: dict[str, Any] | None = None,
    associations: dict[str, Any] | None = None,
    revocation: dict[str, Any] | None = None,
    reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the five-part check report, marking missing parts as not performed."""
    given = {
        "bundle": bundle,
        "baseline": baseline,
        "timestamps": timestamps,
        "associations": associations,
        "revocation": revocation,
    }
    defaults = {
        "bundle": "complete bundle checks were not performed",
        "baseline": "baseline eligibility was not evaluated",
        "timestamps": "timestamp proofs were not checked",
        "associations": OFFLINE_ASSOCIATIONS,
        "revocation": OFFLINE_REVOCATION,
    }
    defaults.update(reasons or {})
    out: dict[str, Any] = {}
    for name in CHECK_NAMES:
        value = given[name]
        out[name] = value if value is not None else not_performed(defaults[name])
    out["not_performed"] = [n for n in CHECK_NAMES if out[n].get("status") == "not_performed"]
    return out


def describe_checks(report: dict[str, Any]) -> list[str]:
    """Human lines for a check report."""
    lines: list[str] = []
    for name in CHECK_NAMES:
        entry = report[name]
        status = entry.get("status")
        detail = entry.get("reason") or ""
        if name == "timestamps" and entry.get("proofs"):
            verified = [p for p in entry["proofs"] if p["result"] == "verified"]
            detail = f"{len(verified)} of {len(entry['proofs'])} proof(s) verified"
            times = ", ".join(p["gen_time"] for p in verified if "gen_time" in p)
            if times:
                detail += f"; genTime {times}"
            others = [p for p in entry["proofs"] if p["result"] != "verified"]
            for proof in others:
                first = proof["issues"][0]["message"] if proof["issues"] else ""
                detail += f"; proof at {proof['offset']}: {proof['result']} {first}".rstrip()
        elif name == "baseline" and entry.get("reasons"):
            detail = "; ".join(
                f"{r.get('message')} (observed {r.get('observed')})" for r in entry["reasons"]
            )
        elif name == "bundle" and entry.get("issues"):
            detail = f"{len(entry['issues'])} issue(s)"
        lines.append(f"  {name:<13}{status}" + (f" - {detail}" if detail else ""))
    return lines


def issue_lines(issues: Sequence[dict[str, Any]], prefix: str = "  ") -> list[str]:
    lines = []
    for issue in issues:
        where = issue.get("file") or ""
        if issue.get("field"):
            where += ("#" if where else "") + str(issue["field"])
        text = f"{prefix}[{issue['code']}] "
        if where:
            text += where + ": "
        text += str(issue.get("requirement") or issue.get("message"))
        if issue.get("section"):
            text += f" (§{issue['section']})"
        if issue.get("observed") is not None:
            text += f"; observed {issue['observed']}"
        lines.append(text)
    return lines
