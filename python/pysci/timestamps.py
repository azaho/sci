"""RFC 3161 / RFC 5816 timestamp proofs (protocol §6.8).

* :func:`build_timestamp_request` -- a DER ``TimeStampReq`` with a SHA-256
  imprint of the signature bytes and ``certReq`` true.
* :func:`verify_timestamp_file` -- frame concatenated DER values and verify
  each proof independently.
* :func:`verify_timestamp_response` -- verify one complete DER value.
* :func:`check_response_matches_request` -- bind a fresh response to its request.

Per-proof results are ``verified``, ``untrusted`` (cryptographically valid but
not anchored to an accepted trust anchor), ``incomplete`` (revocation status
unavailable under the configured policy), or ``invalid``. A failed proof never
affects the publication's validity.

Trust policy: the signer certificate must carry a critical extended key usage
with only ``id-kp-timeStamping`` (RFC 3161 §2.3); every certificate in the
path must be valid at ``genTime``; CA certificates must assert
``cA``; with :attr:`RevocationPolicy.REQUIRE_CURRENT_CRL` every non-anchor
certificate needs a CRL from its issuer that is current at the verification
time. A revocation invalidates the proof unless it is dated after
``genTime`` with a benign reason (superseded, cessation of operation,
affiliation changed). MD5 and SHA-1 are not accepted for the CMS signature;
SHA-1 is accepted only for ESS ``SigningCertificate`` (v1) identifiers.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any, ClassVar, Final

from asn1crypto import cms as a_cms
from asn1crypto import core as a_core
from asn1crypto import tsp as a_tsp
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from .der import encode_length, frame_values, validate_der_structure
from .errors import Issue, Outcome

__all__ = [
    "FREETSA_CA_SHA256",
    "FREETSA_URL",
    "Accuracy",
    "FileOutcome",
    "ProofResult",
    "ProofVerification",
    "RevocationPolicy",
    "TimestampFileVerification",
    "TimestampRequest",
    "TrustConfig",
    "append_proof",
    "build_timestamp_request",
    "check_response_matches_request",
    "freetsa_ca_certificate",
    "freetsa_trust",
    "verify_timestamp_file",
    "verify_timestamp_response",
]

FREETSA_URL: Final = "https://freetsa.org/tsr"
"""Default public timestamp authority (FreeTSA)."""
FREETSA_CA_SHA256: Final = "2151b61137ffa86bf664691ba67e7da0b19f98c758e3d228d5d8ebf27e044438"
"""SHA-256 of the bundled ``freetsa-cacert.pem`` (https://freetsa.org/files/cacert.pem)."""
FREETSA_CRL_URL: Final = "http://www.freetsa.org/crl/root_ca.crl"
TIMESTAMP_CONTENT_TYPE: Final = "application/timestamp-query"
TIMESTAMP_REPLY_TYPE: Final = "application/timestamp-reply"

_SHA256_OID: Final = "2.16.840.1.101.3.4.2.1"
_MAX_PATH: Final = 8
_BENIGN_REASONS: Final = frozenset(
    {
        x509.ReasonFlags.superseded,
        x509.ReasonFlags.cessation_of_operation,
        x509.ReasonFlags.affiliation_changed,
    }
)
_HASHES: Final[dict[str, type[hashes.HashAlgorithm]]] = {
    "sha224": hashes.SHA224,
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
    "sha512_224": hashes.SHA512_224,
    "sha512_256": hashes.SHA512_256,
    "sha3_224": hashes.SHA3_224,
    "sha3_256": hashes.SHA3_256,
    "sha3_384": hashes.SHA3_384,
    "sha3_512": hashes.SHA3_512,
}
_ESS_HASHES: Final[dict[str, type[hashes.HashAlgorithm]]] = {"sha1": hashes.SHA1, **_HASHES}


class ProofResult(StrEnum):
    VERIFIED = "verified"
    UNTRUSTED = "untrusted"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class FileOutcome(StrEnum):
    """Aggregate result of a timestamp file."""

    VALID = "valid"
    """Framing is complete and every proof verified."""
    PARTIAL = "partial"
    """At least one proof verified, but another proof or the tail is unusable."""
    UNTRUSTED = "untrusted"
    """Framing is complete and every proof is valid but untrusted."""
    INCOMPLETE = "incomplete"
    """Framing is complete; no proof failed, but revocation status was unavailable."""
    INVALID = "invalid"
    """No proof verified (including empty or malformed-first files)."""


class RevocationPolicy(StrEnum):
    REQUIRE_CURRENT_CRL = "require_current_crl"
    """Every non-anchor certificate needs a current CRL from its issuer."""
    CHECK_SUPPLIED_CRLS = "check_supplied_crls"
    """Use matching supplied CRLs when present; missing CRLs are tolerated."""


@dataclass(frozen=True, slots=True)
class TrustConfig:
    """Local trust policy for timestamp verification."""

    anchors: Sequence[x509.Certificate] = ()
    intermediates: Sequence[x509.Certificate] = ()
    crls: Sequence[x509.CertificateRevocationList] = ()
    revocation_policy: RevocationPolicy = RevocationPolicy.REQUIRE_CURRENT_CRL

    @classmethod
    def from_pem(
        cls,
        anchors: Iterable[bytes] = (),
        crls: Iterable[bytes] = (),
        intermediates: Iterable[bytes] = (),
        revocation_policy: RevocationPolicy = RevocationPolicy.REQUIRE_CURRENT_CRL,
    ) -> TrustConfig:
        """Load PEM certificates (each blob may hold several) and PEM/DER CRLs."""
        return cls(
            anchors=[c for blob in anchors for c in x509.load_pem_x509_certificates(blob)],
            intermediates=[
                c for blob in intermediates for c in x509.load_pem_x509_certificates(blob)
            ],
            crls=[_load_crl(blob) for blob in crls],
            revocation_policy=revocation_policy,
        )


def _load_crl(blob: bytes) -> x509.CertificateRevocationList:
    if blob.lstrip().startswith(b"-----BEGIN"):
        return x509.load_pem_x509_crl(blob)
    return x509.load_der_x509_crl(blob)


@cache
def freetsa_ca_certificate() -> x509.Certificate:
    """The bundled FreeTSA root CA certificate (hash-checked)."""
    data = resources.files("pysci").joinpath("data/freetsa-cacert.pem").read_bytes()
    if hashlib.sha256(data).hexdigest() != FREETSA_CA_SHA256:
        raise RuntimeError("bundled FreeTSA CA certificate does not match its recorded hash")
    return x509.load_pem_x509_certificate(data)


def freetsa_trust(
    crls: Iterable[bytes] = (),
    revocation_policy: RevocationPolicy = RevocationPolicy.CHECK_SUPPLIED_CRLS,
) -> TrustConfig:
    """Trust configuration anchored at the bundled FreeTSA root.

    FreeTSA publishes its CRL at :data:`FREETSA_CRL_URL`; pass it in ``crls``
    and use ``REQUIRE_CURRENT_CRL`` for a full revocation check online.
    """
    return TrustConfig(
        anchors=[freetsa_ca_certificate()],
        crls=[_load_crl(blob) for blob in crls],
        revocation_policy=revocation_policy,
    )


@dataclass(frozen=True, slots=True)
class Accuracy:
    """TSTInfo accuracy (RFC 3161 §2.4.2)."""

    seconds: int = 0
    millis: int = 0
    micros: int = 0

    def total_seconds(self) -> Decimal:
        return Decimal(self.seconds) + Decimal(self.millis) / 1000 + Decimal(self.micros) / 10**6


@dataclass(frozen=True, slots=True)
class ProofVerification:
    """Result for one complete DER value in a timestamp file."""

    offset: int
    length: int
    result: ProofResult
    issues: tuple[Issue, ...] = ()
    gen_time: datetime | None = None
    accuracy: Accuracy | None = None
    tsa_name: str | None = None
    signer_subject: str | None = None
    policy: str | None = None
    serial_number: int | None = None
    nonce: int | None = None
    revocation_checked: bool = False


@dataclass(frozen=True, slots=True)
class TimestampFileVerification:
    """Result for a whole timestamp file."""

    outcome: FileOutcome
    framing_complete: bool
    proofs: tuple[ProofVerification, ...]
    stop_offset: int

    @property
    def framing(self) -> str:
        return "complete" if self.framing_complete else "malformed"

    @property
    def verified(self) -> tuple[ProofVerification, ...]:
        return tuple(p for p in self.proofs if p.result is ProofResult.VERIFIED)


@dataclass(frozen=True, slots=True)
class TimestampRequest:
    """A DER ``TimeStampReq`` and the values needed to check its response."""

    der: bytes
    digest: bytes
    nonce: int | None
    cert_req: bool = True
    policy: str | None = None


def build_timestamp_request(
    signature: bytes,
    *,
    nonce: int | bool | None = True,
    cert_req: bool = True,
    policy: str | None = None,
) -> TimestampRequest:
    """Build an RFC 3161 request over SHA-256 of the exact signature bytes (§6.8).

    ``nonce=True`` (default) draws a random 64-bit nonce; pass an int to fix
    it or None/False to omit it. POST ``der`` with content type
    ``application/timestamp-query`` (e.g. to :data:`FREETSA_URL`).
    """
    digest = hashlib.sha256(signature).digest()
    nonce_value: int | None
    if nonce is True:
        nonce_value = secrets.randbits(63) | (1 << 62)
    elif nonce is False or nonce is None:
        nonce_value = None
    else:
        nonce_value = int(nonce)
    request: dict[str, Any] = {
        "version": "v1",
        "message_imprint": {
            "hash_algorithm": {"algorithm": "sha256"},
            "hashed_message": digest,
        },
        "cert_req": cert_req,
    }
    if policy is not None:
        request["req_policy"] = policy
    if nonce_value is not None:
        request["nonce"] = nonce_value
    der = a_tsp.TimeStampReq(request).dump()
    return TimestampRequest(der, digest, nonce_value, cert_req, policy)


# --------------------------------------------------------------------------- verification


class _TimeStampResp(a_core.Sequence):  # type: ignore[misc]
    """TimeStampResp with the token OPTIONAL, as in RFC 3161 §2.4.2."""

    _fields: ClassVar[list[tuple[Any, ...]]] = [
        ("status", a_tsp.PKIStatusInfo),
        ("time_stamp_token", a_cms.ContentInfo, {"optional": True}),
    ]


class _ProofError(Exception):
    def __init__(
        self,
        code: str,
        requirement: str,
        observed: object = None,
        result: ProofResult = ProofResult.INVALID,
    ) -> None:
        super().__init__(requirement)
        self.result = result
        self.issue = Issue(
            code,
            requirement,
            section="6.8",
            observed=None if observed is None else str(observed),
            outcome=Outcome.INCOMPLETE if result is ProofResult.INCOMPLETE else Outcome.INVALID,
        )


@dataclass(slots=True)
class _Token:
    signed_data: Any
    tst_info: Any
    signer_info: Any
    certificates: list[x509.Certificate] = field(default_factory=list)
    signer: x509.Certificate | None = None


def verify_timestamp_response(
    der: bytes,
    signature: bytes,
    trust: TrustConfig | None,
    *,
    at: datetime | None = None,
    offset: int = 0,
) -> ProofVerification:
    """Verify one complete DER ``TimeStampResp`` against the signature bytes.

    ``at`` is the verification time used for CRL currency (default: now).
    With ``trust`` None or without anchors, a cryptographically valid proof is
    reported as ``untrusted``.
    """
    now = at or datetime.now(UTC)
    details: dict[str, Any] = {}
    try:
        token = _parse_response(der)
        details = _token_details(token)
        _check_imprint(token.tst_info, signature)
        _check_signer(token, trust)
        assert token.signer is not None
        details["signer_subject"] = token.signer.subject.rfc4514_string()
        _check_signature(token)
        gen_time: datetime = details["gen_time"]
        if gen_time > now + timedelta(minutes=5):
            raise _ProofError(
                "timestamp_future", "genTime must not be in the future", gen_time.isoformat()
            )
        assert token.signer is not None
        _check_signer_certificate(token.signer, gen_time)
        if trust is None or not trust.anchors:
            raise _ProofError(
                "timestamp_untrusted",
                "no accepted trust anchor is configured",
                result=ProofResult.UNTRUSTED,
            )
        path = _build_path(token.signer, token.certificates, trust, gen_time)
        checked = _check_revocation(path, trust, gen_time, now)
    except _ProofError as exc:
        return ProofVerification(offset, len(der), exc.result, (exc.issue,), **details)
    return ProofVerification(
        offset, len(der), ProofResult.VERIFIED, (), revocation_checked=checked, **details
    )


def _parse_response(der: bytes) -> _Token:
    if not validate_der_structure(der):
        raise _ProofError("timestamp_der", "value must be DER-encoded")
    try:
        response = _TimeStampResp.load(der, strict=True)
        status = response["status"]["status"].native
        token = response["time_stamp_token"]
    except Exception as exc:  # asn1crypto raises assorted exception types
        raise _ProofError(
            "timestamp_not_response", "value must be a TimeStampResp", type(exc).__name__
        ) from None
    if status not in ("granted", "granted_with_mods"):
        raise _ProofError("timestamp_status", "status must be granted or grantedWithMods", status)
    try:
        if token.native is None:
            raise _ProofError("timestamp_missing_token", "a TimeStampToken must be present")
        if token["content_type"].native != "signed_data":
            raise _ProofError("timestamp_cms", "token must be CMS SignedData")
        signed_data = token["content"]
        encap = signed_data["encap_content_info"]
        if encap["content_type"].native != "tst_info":
            raise _ProofError("timestamp_cms", "encapsulated content must be TSTInfo")
        if not isinstance(encap["content"], a_core.ParsableOctetString):
            raise _ProofError("timestamp_cms", "TSTInfo content must be present")
        tst_info = encap["content"].parsed
        _ = tst_info.native  # force a full parse so malformed content fails here
        signer_infos = signed_data["signer_infos"]
        if len(signer_infos) != 1:
            raise _ProofError(
                "timestamp_signers", "token must contain exactly one signature", len(signer_infos)
            )
        certificates = []
        for choice in signed_data["certificates"] or []:
            if choice.name == "certificate":
                certificates.append(x509.load_der_x509_certificate(choice.chosen.dump()))
        return _Token(signed_data, tst_info, signer_infos[0], certificates)
    except _ProofError:
        raise
    except Exception as exc:
        raise _ProofError(
            "timestamp_token_malformed", "TimeStampToken must be well formed", type(exc).__name__
        ) from None


def _token_details(token: _Token) -> dict[str, Any]:
    info = token.tst_info
    if info["version"].native != "v1":
        raise _ProofError("timestamp_version", "TSTInfo version must be 1")
    accuracy = None
    if info["accuracy"].native is not None:
        raw = info["accuracy"].native
        accuracy = Accuracy(raw["seconds"] or 0, raw["millis"] or 0, raw["micros"] or 0)
    tsa = info["tsa"]
    tsa_name = None
    if tsa.native is not None:
        chosen = tsa.chosen
        tsa_name = (
            chosen.human_friendly
            if chosen.name == "directory_name" or hasattr(chosen, "human_friendly")
            else str(tsa.native)
        )
    gen_time = info["gen_time"].native
    if not isinstance(gen_time, datetime):
        raise _ProofError("timestamp_gen_time", "genTime must be a GeneralizedTime")
    return {
        "gen_time": gen_time.astimezone(UTC),
        "accuracy": accuracy,
        "tsa_name": tsa_name,
        "policy": info["policy"].dotted,
        "serial_number": info["serial_number"].native,
        "nonce": info["nonce"].native,
    }


def _check_imprint(info: Any, signature: bytes) -> None:
    imprint = info["message_imprint"]
    algorithm = imprint["hash_algorithm"]
    if algorithm["algorithm"].dotted != _SHA256_OID:
        raise _ProofError(
            "timestamp_imprint_algorithm",
            "messageImprint must use SHA-256",
            algorithm["algorithm"].native,
        )
    if algorithm["parameters"].native is not None:
        raise _ProofError(
            "timestamp_imprint_algorithm", "SHA-256 parameters must be absent or NULL"
        )
    if imprint["hashed_message"].native != hashlib.sha256(signature).digest():
        raise _ProofError(
            "timestamp_imprint_mismatch",
            "messageImprint must equal SHA-256 of the verified signature bytes",
        )


def _find_signer(token: _Token, extra: Sequence[x509.Certificate]) -> x509.Certificate:
    sid = token.signer_info["sid"]
    candidates = [*token.certificates, *extra]
    if sid.name == "issuer_and_serial_number":
        issuer = sid.chosen["issuer"].dump()
        serial = sid.chosen["serial_number"].native
        for cert in candidates:
            if cert.serial_number == serial and cert.issuer.public_bytes() == issuer:
                return cert
    else:
        key_id = sid.chosen.native
        for cert in candidates:
            try:
                ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            except x509.ExtensionNotFound:
                continue
            if ski.value.digest == key_id:
                return cert
    raise _ProofError(
        "timestamp_signer_certificate",
        "the signing certificate must be available (request certReq)",
    )


def _check_signer(token: _Token, trust: TrustConfig | None) -> None:
    token.signer = _find_signer(token, trust.intermediates if trust else ())
    signer = token.signer
    attrs = token.signer_info["signed_attrs"]
    if not isinstance(attrs, a_cms.CMSAttributes) or len(attrs) == 0:
        raise _ProofError("timestamp_signed_attrs", "signed attributes are required")
    values: dict[str, list[Any]] = {}
    for attr in attrs:
        name = attr["type"].native
        if name in values:
            raise _ProofError(
                "timestamp_signed_attrs", "signed attribute types must be unique", name
            )
        values[name] = list(attr["values"])
    for required in ("content_type", "message_digest"):
        if len(values.get(required, [])) != 1:
            raise _ProofError(
                "timestamp_signed_attrs", f"exactly one {required} attribute value is required"
            )
    if values["content_type"][0].native != "tst_info":
        raise _ProofError("timestamp_signed_attrs", "content-type attribute must be id-ct-TSTInfo")
    digest_name = token.signer_info["digest_algorithm"]["algorithm"].native
    if digest_name not in _HASHES:
        raise _ProofError(
            "timestamp_digest_algorithm", "unsupported or weak CMS digest algorithm", digest_name
        )
    content = token.signed_data["encap_content_info"]["content"].contents
    expected = hashlib.new(_hashlib_name(digest_name), content).digest()
    if values["message_digest"][0].native != expected:
        raise _ProofError(
            "timestamp_message_digest", "message-digest attribute must match the TSTInfo"
        )
    _check_ess(values, signer)


def _hashlib_name(name: str) -> str:
    return {"sha512_224": "sha512_224", "sha512_256": "sha512_256"}.get(name, name)


def _check_ess(values: dict[str, list[Any]], signer: x509.Certificate) -> None:
    from cryptography.hazmat.primitives import serialization

    cert_der = signer.public_bytes(serialization.Encoding.DER)
    if "signing_certificate_v2" in values:
        attribute = values["signing_certificate_v2"]
        default_hash = "sha256"
    elif "signing_certificate" in values:
        attribute = values["signing_certificate"]
        default_hash = "sha1"
    else:
        raise _ProofError(
            "timestamp_ess", "ESS signing-certificate attribute (v1 or v2) is required"
        )
    if len(attribute) != 1 or len(attribute[0]["certs"]) == 0:
        raise _ProofError("timestamp_ess", "ESS attribute must identify the signing certificate")
    cert_id = attribute[0]["certs"][0]
    hash_name = default_hash
    if default_hash == "sha256" and cert_id["hash_algorithm"].native is not None:
        hash_name = cert_id["hash_algorithm"]["algorithm"].native
    if hash_name not in _ESS_HASHES:
        raise _ProofError("timestamp_ess", "unsupported ESSCertID hash algorithm", hash_name)
    actual = hashlib.new(_hashlib_name(hash_name), cert_der).digest()
    if cert_id["cert_hash"].native != actual:
        raise _ProofError(
            "timestamp_ess_mismatch", "ESSCertID must identify the actual signing certificate"
        )
    issuer_serial = cert_id["issuer_serial"]
    if issuer_serial.native is not None:
        if issuer_serial["serial_number"].native != signer.serial_number:
            raise _ProofError(
                "timestamp_ess_mismatch",
                "ESSCertID issuerSerial must match the signing certificate",
            )
        names = [n.chosen.dump() for n in issuer_serial["issuer"] if n.name == "directory_name"]
        if names and signer.issuer.public_bytes() not in names:
            raise _ProofError(
                "timestamp_ess_mismatch", "ESSCertID issuer must match the signing certificate"
            )


def _check_signature(token: _Token) -> None:
    signer = token.signer
    assert signer is not None
    info = token.signer_info
    attrs = info["signed_attrs"]
    signed = b"\x31" + encode_length(len(attrs.contents)) + attrs.contents
    signature = info["signature"].native
    digest_name = info["digest_algorithm"]["algorithm"].native
    algorithm = info["signature_algorithm"]
    key = signer.public_key()
    try:
        algo = algorithm.signature_algo
        if algo == "rsassa_pkcs1v15" and isinstance(key, rsa.RSAPublicKey):
            sig_hash = (
                algorithm.hash_algo
                if algorithm["algorithm"].native != "rsassa_pkcs1v15"
                else digest_name
            )
            key.verify(signature, signed, padding.PKCS1v15(), _hash(sig_hash))
        elif algo == "rsassa_pss" and isinstance(key, rsa.RSAPublicKey):
            params = algorithm["parameters"]
            pss_hash = params["hash_algorithm"]["algorithm"].native
            mgf_hash = params["mask_gen_algorithm"]["parameters"]["algorithm"].native
            key.verify(
                signature,
                signed,
                padding.PSS(padding.MGF1(_hash(mgf_hash)), params["salt_length"].native),
                _hash(pss_hash),
            )
        elif algo == "ecdsa" and isinstance(key, ec.EllipticCurvePublicKey):
            sig_hash = (
                algorithm.hash_algo if algorithm["algorithm"].native != "ecdsa" else digest_name
            )
            key.verify(signature, signed, ec.ECDSA(_hash(sig_hash)))
        elif (algo == "ed25519" and isinstance(key, ed25519.Ed25519PublicKey)) or (
            algo == "ed448" and isinstance(key, ed448.Ed448PublicKey)
        ):
            key.verify(signature, signed)
        else:
            raise _ProofError(
                "timestamp_signature_algorithm",
                "unsupported signature algorithm for the signer key",
                algo,
            )
    except InvalidSignature:
        raise _ProofError(
            "timestamp_signature", "CMS signature must verify with the signing certificate"
        ) from None
    except (ValueError, KeyError, TypeError) as exc:
        raise _ProofError(
            "timestamp_signature_algorithm", "unsupported signature parameters", type(exc).__name__
        ) from None


def _hash(name: str) -> hashes.HashAlgorithm:
    if name not in _HASHES:
        raise _ProofError("timestamp_digest_algorithm", "unsupported or weak hash algorithm", name)
    return _HASHES[name]()


def _check_signer_certificate(cert: x509.Certificate, gen_time: datetime) -> None:
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        eku = None
    if eku is None or not eku.critical or list(eku.value) != [ExtendedKeyUsageOID.TIME_STAMPING]:
        raise _ProofError(
            "timestamp_eku",
            "TSA certificate needs a critical extended key usage of only id-kp-timeStamping",
        )
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not (usage.digital_signature or usage.content_commitment):
            raise _ProofError("timestamp_key_usage", "TSA key usage must permit signatures")
    except x509.ExtensionNotFound:
        pass
    _check_validity(cert, gen_time)


def _check_validity(cert: x509.Certificate, when: datetime) -> None:
    if not cert.not_valid_before_utc <= when <= cert.not_valid_after_utc:
        raise _ProofError(
            "timestamp_certificate_validity",
            "certificates must be valid at genTime",
            cert.subject.rfc4514_string(),
        )


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return False
    if not constraints.ca:
        return False
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return True
    return usage.key_cert_sign


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def _build_path(
    signer: x509.Certificate,
    bundled: Sequence[x509.Certificate],
    trust: TrustConfig,
    gen_time: datetime,
) -> list[x509.Certificate]:
    """Return [signer, ..., anchor]; raise untrusted if no anchored path exists."""
    pool = [*bundled, *trust.intermediates]
    path = [signer]
    current = signer
    for _ in range(_MAX_PATH):
        for anchor in trust.anchors:
            if anchor == current:
                return path
        for anchor in trust.anchors:
            if anchor.subject == current.issuer and _issued_by(current, anchor):
                path.append(anchor)
                return path
        step = next(
            (
                c
                for c in pool
                if c.subject == current.issuer
                and c not in path
                and _is_ca(c)
                and _issued_by(current, c)
            ),
            None,
        )
        if step is None:
            break
        _check_validity(step, gen_time)
        path.append(step)
        current = step
    raise _ProofError(
        "timestamp_untrusted",
        "certificate path does not reach an accepted trust anchor",
        signer.issuer.rfc4514_string(),
        ProofResult.UNTRUSTED,
    )


def _crl_signed_by(crl: x509.CertificateRevocationList, issuer: x509.Certificate) -> bool:
    key = issuer.public_key()
    if not isinstance(
        key,
        rsa.RSAPublicKey
        | ec.EllipticCurvePublicKey
        | ed25519.Ed25519PublicKey
        | ed448.Ed448PublicKey,
    ):
        return False
    try:
        return crl.is_signature_valid(key)
    except (ValueError, TypeError):
        return False


def _check_revocation(
    path: list[x509.Certificate], trust: TrustConfig, gen_time: datetime, now: datetime
) -> bool:
    """Check every non-anchor certificate; return True if all had a current CRL."""
    all_checked = True
    for cert, issuer in itertools.pairwise(path):
        crls = [
            crl
            for crl in trust.crls
            if crl.issuer == issuer.subject
            and _crl_signed_by(crl, issuer)
            and crl.last_update_utc <= now
            and crl.next_update_utc is not None
            and now < crl.next_update_utc
        ]
        if not crls:
            all_checked = False
            if trust.revocation_policy is RevocationPolicy.REQUIRE_CURRENT_CRL:
                raise _ProofError(
                    "timestamp_revocation_unavailable",
                    "a current CRL from the issuer is required",
                    cert.subject.rfc4514_string(),
                    ProofResult.INCOMPLETE,
                )
            continue
        for crl in crls:
            revoked = crl.get_revoked_certificate_by_serial_number(cert.serial_number)
            if revoked is None:
                continue
            reason = None
            with contextlib.suppress(x509.ExtensionNotFound):
                reason = revoked.extensions.get_extension_for_class(x509.CRLReason).value.reason
            if revoked.revocation_date_utc > gen_time and reason in _BENIGN_REASONS:
                continue
            raise _ProofError(
                "timestamp_certificate_revoked",
                "a certificate in the path is revoked",
                cert.subject.rfc4514_string(),
            )
    return all_checked


# --------------------------------------------------------------------------- files


def verify_timestamp_file(
    data: bytes,
    signature: bytes,
    trust: TrustConfig | None,
    *,
    at: datetime | None = None,
    max_value_length: int | None = 1024 * 1024,
) -> TimestampFileVerification:
    """Frame and verify every complete proof in a timestamp file (§6.8).

    The caller must first complete the manifest or revocation checks for the
    signature (§6.8); this function binds proofs to ``signature`` only.
    Values longer than ``max_value_length`` stop framing (a local bound).
    """
    framing = frame_values(data, max_value_length=max_value_length)
    proofs = tuple(
        verify_timestamp_response(
            data[offset : offset + length], signature, trust, at=at, offset=offset
        )
        for offset, length in framing.values
    )
    results = [proof.result for proof in proofs]
    if framing.complete and results and all(r is ProofResult.VERIFIED for r in results):
        outcome = FileOutcome.VALID
    elif ProofResult.VERIFIED in results:
        outcome = FileOutcome.PARTIAL
    elif (
        framing.complete
        and results
        and all(r in (ProofResult.UNTRUSTED, ProofResult.INCOMPLETE) for r in results)
    ):
        outcome = (
            FileOutcome.INCOMPLETE if ProofResult.INCOMPLETE in results else FileOutcome.UNTRUSTED
        )
    else:
        outcome = FileOutcome.INVALID
    return TimestampFileVerification(outcome, framing.complete, proofs, framing.stop_offset)


def check_response_matches_request(der: bytes, request: TimestampRequest) -> list[Issue]:
    """Check a fresh TSA response against its request (status, imprint, nonce, certificate).

    Trust is not evaluated here; use :func:`verify_timestamp_response`.
    Returns an empty list when the response answers the request.
    """
    try:
        token = _parse_response(der)
        info = token.tst_info
        if info["message_imprint"]["hashed_message"].native != request.digest:
            raise _ProofError(
                "timestamp_imprint_mismatch", "response imprint must match the request"
            )
        if info["message_imprint"]["hash_algorithm"]["algorithm"].dotted != _SHA256_OID:
            raise _ProofError("timestamp_imprint_algorithm", "response must use SHA-256")
        if request.nonce is not None and info["nonce"].native != request.nonce:
            raise _ProofError("timestamp_nonce", "response nonce must match the request")
        if request.policy is not None and info["policy"].dotted != request.policy:
            raise _ProofError("timestamp_policy", "response policy must match the request")
        if request.cert_req:
            _find_signer(token, ())
    except _ProofError as exc:
        return [exc.issue]
    return []


def append_proof(existing: bytes, response: bytes) -> bytes:
    """Append a complete DER response to a timestamp file without duplicating it.

    Raises:
        ValueError: ``existing`` is not completely framed (preserve it and stop),
            or ``response`` is not exactly one complete DER value.
    """
    if existing and not frame_values(existing, max_value_length=None).complete:
        raise ValueError("existing timestamp file is malformed; refusing to append")
    framed = frame_values(response, max_value_length=None)
    if len(framed.values) != 1 or not framed.complete:
        raise ValueError("response must be exactly one complete DER value")
    current = frame_values(existing, max_value_length=None)
    for offset, length in current.values:
        if existing[offset : offset + length] == response:
            return existing
    return existing + response
