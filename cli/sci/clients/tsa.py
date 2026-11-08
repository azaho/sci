"""RFC 3161 timestamp authority client, trust configuration, and sidecar files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography import x509

from pysci.der import frame_values
from pysci.timestamps import (
    FREETSA_CRL_URL,
    FREETSA_URL,
    TIMESTAMP_CONTENT_TYPE,
    ProofVerification,
    RevocationPolicy,
    TimestampFileVerification,
    TrustConfig,
    append_proof,
    build_timestamp_request,
    check_response_matches_request,
    freetsa_ca_certificate,
    verify_timestamp_file,
    verify_timestamp_response,
)

from ..config import TrustSettings
from ..errors import CliError, ErrorRecord, Exit, issue_record
from ..storage.files import atomic_write_bytes
from .http import Deadline, HttpClient, status_error

MAX_RESPONSE = 1 << 20


def load_crl(blob: bytes) -> x509.CertificateRevocationList:
    """Load a PEM or DER CRL through the library's trust loader."""
    return TrustConfig.from_pem(crls=[blob]).crls[0]


MAX_SIDECAR = 16 << 20

__all__ = [
    "FREETSA_URL",
    "TsaResult",
    "append_sidecar",
    "build_trust",
    "read_sidecar",
    "request_proof",
    "verify_sidecar_bytes",
]


def build_trust(
    settings: TrustSettings,
    *,
    online: bool = False,
    http: HttpClient | None = None,
    deadline: Deadline | None = None,
) -> tuple[TrustConfig, list[str]]:
    """Trust anchors from configuration; with ``online`` fetch CRLs and require them."""
    warnings: list[str] = []
    anchors: list[x509.Certificate] = []
    if settings.freetsa:
        anchors.append(freetsa_ca_certificate())
    for path in settings.anchors:
        try:
            anchors.extend(x509.load_pem_x509_certificates(Path(path).expanduser().read_bytes()))
        except (OSError, ValueError) as exc:
            raise CliError(
                "trust_anchor_unreadable",
                f"timestamp trust anchor {path} could not be loaded: {exc}",
                Exit.USAGE,
                file="config.json",
                field="/timestamp_trust/anchors",
            ) from None
    crls = []
    for path in settings.crls:
        try:
            crls.append(load_crl(Path(path).expanduser().read_bytes()))
        except (OSError, ValueError) as exc:
            warnings.append(f"CRL {path} could not be loaded: {exc}")
    policy = RevocationPolicy.CHECK_SUPPLIED_CRLS
    if online:
        assert http is not None
        assert deadline is not None
        policy = RevocationPolicy.REQUIRE_CURRENT_CRL
        urls = list(settings.crl_urls)
        if settings.freetsa:
            urls.append(FREETSA_CRL_URL)
        for url in urls:
            try:
                response = http.get(url, deadline=deadline, max_bytes=8 << 20)
                if response.status != 200:
                    raise status_error(response, f"CRL {url}")
                crls.append(load_crl(response.body))
            except (CliError, ValueError) as exc:
                warnings.append(f"CRL {url} unavailable: {exc}")
    return TrustConfig(anchors=anchors, crls=crls, revocation_policy=policy), warnings


@dataclass(frozen=True, slots=True)
class TsaResult:
    response: bytes
    verification: ProofVerification
    url: str


def request_proof(
    http: HttpClient,
    url: str,
    signature: bytes,
    trust: TrustConfig,
    deadline: Deadline,
) -> TsaResult:
    """Request a timestamp for SHA-256(signature) and check the reply.

    The response structure, status, imprint, and nonce are required to match
    the request; trust is reported separately in ``verification``.

    Raises:
        CliError: (exit 5) the TSA was unavailable or returned an unusable reply.
    """
    request = build_timestamp_request(signature)
    response = http.request(
        "POST",
        url,
        deadline=deadline,
        headers={"Content-Type": TIMESTAMP_CONTENT_TYPE},
        body=request.der,
        max_bytes=MAX_RESPONSE,
        paced=True,
    )
    if response.status != 200:
        error = status_error(response, f"timestamp authority {url}")
        error.exit_code = Exit.INCOMPLETE
        raise error
    der = response.body
    framing = frame_values(der, max_value_length=MAX_RESPONSE)
    if len(framing.values) != 1 or not framing.complete:
        raise CliError(
            "timestamp_response_malformed",
            "timestamp authority reply is not exactly one DER TimeStampResp",
            Exit.INCOMPLETE,
            retryable=True,
            url=url,
        )
    issues = check_response_matches_request(der, request)
    if issues:
        error = CliError(
            "timestamp_response_rejected",
            f"timestamp authority reply does not answer the request: {issues[0].requirement}",
            Exit.INCOMPLETE,
            retryable=True,
            url=url,
        )
        error.records.extend(issue_record(i) for i in issues)
        raise error
    verification = verify_timestamp_response(der, signature, trust)
    return TsaResult(der, verification, url)


def read_sidecar(path: Path) -> bytes | None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    if size > MAX_SIDECAR:
        raise CliError(
            "sidecar_too_large",
            f"{path.name} exceeds the local limit of {MAX_SIDECAR} bytes",
            Exit.INCOMPLETE,
            file=str(path),
        )
    return path.read_bytes()


def append_sidecar(path: Path, response: bytes) -> bool:
    """Append a DER response to the sidecar atomically; False if it was already present.

    Raises:
        CliError: (exit 3) the existing sidecar is malformed; it is preserved.
    """
    existing = read_sidecar(path) or b""
    try:
        combined = append_proof(existing, response)
    except ValueError as exc:
        raise CliError(
            "sidecar_malformed",
            f"existing timestamp sidecar {path.name} is malformed and was preserved: {exc}",
            Exit.INVALID,
            file=str(path),
        ) from None
    if combined == existing:
        return False
    atomic_write_bytes(path, combined)
    return True


def merge_proofs(existing: bytes, incoming: bytes) -> tuple[bytes, list[ErrorRecord]]:
    """Union of complete DER values from ``incoming`` appended to ``existing``."""
    problems: list[ErrorRecord] = []
    framing = frame_values(incoming, max_value_length=MAX_RESPONSE)
    combined = existing
    for offset, length in framing.values:
        try:
            combined = append_proof(combined, incoming[offset : offset + length])
        except ValueError as exc:
            problems.append(ErrorRecord("sidecar_malformed", str(exc)))
            return existing, problems
    if not framing.complete and incoming:
        problems.append(
            ErrorRecord(
                "proof_framing",
                "retrieved timestamp file has trailing bytes that are not a complete DER value",
            )
        )
    return combined, problems


def verify_sidecar_bytes(
    data: bytes, signature: bytes, trust: TrustConfig
) -> TimestampFileVerification:
    return verify_timestamp_file(data, signature, trust)
