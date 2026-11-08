"""Timestamp tests.

``data/freetsa-response.tsr`` is a real response from https://freetsa.org/tsr
(obtained 2026-11-07) for ``build_timestamp_request(b"\\x01" * 64)``; it lets
the suite exercise a production TSA token offline.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from datetime import UTC, datetime

import pytest
from asn1crypto import tsp
from conftest import CORPUS, DATA, corpus_bytes

from pysci.der import encode_length, frame_values, validate_der_structure
from pysci.timestamps import (
    FREETSA_CA_SHA256,
    FREETSA_URL,
    FileOutcome,
    ProofResult,
    RevocationPolicy,
    TrustConfig,
    append_proof,
    build_timestamp_request,
    check_response_matches_request,
    freetsa_ca_certificate,
    freetsa_trust,
    verify_timestamp_file,
    verify_timestamp_response,
)

WHEN = datetime(2026, 11, 7, 12, tzinfo=UTC)
FREETSA_SIGNATURE = b"\x01" * 64


def corpus_trust(crl: str = "pki/good.crl.pem") -> TrustConfig:
    return TrustConfig.from_pem(anchors=[corpus_bytes("pki/root.pem")], crls=[corpus_bytes(crl)])


def test_framing_rules() -> None:
    one = b"\x02\x01\x05"
    assert frame_values(one + one).values == ((0, 3), (3, 3))
    for tail in (
        b"\x30",
        b"\x30\x82\x01",
        b"\x30\x80",
        b"\x30\x81\x00",
        b"\x30\x88" + b"\xff" * 8,
        b"\n",
        b"\x00",
        b"\x30\x84\x7f\xff\xff\xff" + one,
    ):
        framing = frame_values(one + tail)
        assert framing.values == ((0, 3),) and framing.stop_offset == 3 and not framing.complete
    assert not frame_values(b"").complete
    assert frame_values(b"\x1f\x81\x00\x00").values == ((0, 4),)  # high tag number
    assert frame_values(b"\x1f\x80\x01\x00").values == ()  # nonminimal tag number
    assert encode_length(200) == b"\x81\xc8" and encode_length(5) == b"\x05"
    assert validate_der_structure(b"\x30\x03\x02\x01\x05")
    assert not validate_der_structure(b"\x30\x03\x30\x02\x02\x01")  # inner overruns


def test_request_structure() -> None:
    request = build_timestamp_request(b"sig", nonce=42)
    parsed = tsp.TimeStampReq.load(request.der)
    assert parsed["message_imprint"]["hash_algorithm"]["algorithm"].native == "sha256"
    assert parsed["message_imprint"]["hashed_message"].native == hashlib.sha256(b"sig").digest()
    assert parsed["cert_req"].native is True and parsed["nonce"].native == 42
    assert build_timestamp_request(b"sig", nonce=None).nonce is None
    assert build_timestamp_request(b"sig").nonce is not None


def test_corpus_proof_verifies_and_reports_details() -> None:
    signature = corpus_bytes("timestamp/timestamp-one/signature.bin")
    data = corpus_bytes("timestamp/timestamp-one/proofs.tsr")
    proof = verify_timestamp_response(data, signature, corpus_trust(), at=WHEN)
    assert proof.result is ProofResult.VERIFIED, proof.issues
    assert proof.gen_time == WHEN and proof.accuracy is not None
    assert proof.accuracy.total_seconds() == 1
    assert proof.signer_subject is not None and "tsa-a" in proof.signer_subject
    assert proof.revocation_checked


def test_trust_and_revocation_outcomes() -> None:
    signature = corpus_bytes("timestamp/timestamp-one/signature.bin")
    data = corpus_bytes("timestamp/timestamp-one/proofs.tsr")
    assert verify_timestamp_response(data, signature, None, at=WHEN).result is ProofResult.UNTRUSTED
    no_crl = TrustConfig.from_pem(anchors=[corpus_bytes("pki/root.pem")])
    assert verify_timestamp_response(data, signature, no_crl, at=WHEN).result is (
        ProofResult.INCOMPLETE
    )
    lenient = TrustConfig(
        anchors=no_crl.anchors, revocation_policy=RevocationPolicy.CHECK_SUPPLIED_CRLS
    )
    proof = verify_timestamp_response(data, signature, lenient, at=WHEN)
    assert proof.result is ProofResult.VERIFIED and not proof.revocation_checked
    revoked = verify_timestamp_response(
        data, signature, corpus_trust("pki/revoked-a.crl.pem"), at=WHEN
    )
    assert revoked.issues[0].code == "timestamp_certificate_revoked"
    # A CRL that is no longer current does not count.
    late = datetime(2027, 1, 1, tzinfo=UTC)
    assert verify_timestamp_response(data, signature, corpus_trust(), at=late).result is (
        ProofResult.INCOMPLETE
    )
    other = verify_timestamp_response(data, b"x" * 64, corpus_trust(), at=WHEN)
    assert other.issues[0].code == "timestamp_imprint_mismatch"


def test_file_outcomes() -> None:
    signature = corpus_bytes("timestamp/timestamp-one/signature.bin")
    data = corpus_bytes("timestamp/timestamp-two-authorities/proofs.tsr")
    result = verify_timestamp_file(data, signature, corpus_trust(), at=WHEN)
    assert result.outcome is FileOutcome.VALID and len(result.verified) == 2
    partial = verify_timestamp_file(data + b"\n", signature, corpus_trust(), at=WHEN)
    assert partial.outcome is FileOutcome.PARTIAL and partial.framing == "malformed"
    empty = verify_timestamp_file(b"", signature, corpus_trust(), at=WHEN)
    assert empty.outcome is FileOutcome.INVALID and empty.stop_offset == 0
    untrusted = verify_timestamp_file(data, signature, TrustConfig(), at=WHEN)
    assert untrusted.outcome is FileOutcome.UNTRUSTED


def test_append_proof() -> None:
    one = corpus_bytes("timestamp/timestamp-one/proofs.tsr")
    assert append_proof(b"", one) == one
    assert append_proof(one, one) == one
    two = corpus_bytes("timestamp/timestamp-two-authorities/proofs.tsr")
    second = two[len(one) :]
    assert append_proof(one, second) == one + second
    with pytest.raises(ValueError):
        append_proof(one + b"\x30", second)
    with pytest.raises(ValueError):
        append_proof(one, second + b"\x00")


def test_freetsa_bundle_and_real_token() -> None:
    cert_pem = (DATA.parent.parent / "pysci" / "data" / "freetsa-cacert.pem").read_bytes()
    assert hashlib.sha256(cert_pem).hexdigest() == FREETSA_CA_SHA256
    assert "freetsa" in freetsa_ca_certificate().subject.rfc4514_string().lower()
    assert FREETSA_URL == "https://freetsa.org/tsr"
    der = (DATA / "freetsa-response.tsr").read_bytes()
    proof = verify_timestamp_response(der, FREETSA_SIGNATURE, freetsa_trust())
    assert proof.result is ProofResult.VERIFIED, proof.issues
    assert proof.gen_time is not None and proof.gen_time.year == 2026
    strict = verify_timestamp_response(
        der,
        FREETSA_SIGNATURE,
        freetsa_trust(revocation_policy=RevocationPolicy.REQUIRE_CURRENT_CRL),
    )
    assert strict.result is ProofResult.INCOMPLETE  # no CRL supplied
    request = build_timestamp_request(FREETSA_SIGNATURE, nonce=None)
    assert check_response_matches_request(der, request) == []
    assert check_response_matches_request(der, build_timestamp_request(b"other"))


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("SCI_NETWORK_TESTS") != "1", reason="network test")
def test_live_freetsa_roundtrip() -> None:
    signature = os.urandom(64)
    request = build_timestamp_request(signature)
    http_request = urllib.request.Request(
        FREETSA_URL, data=request.der, headers={"Content-Type": "application/timestamp-query"}
    )
    with urllib.request.urlopen(http_request, timeout=30) as response:
        der = response.read()
    assert check_response_matches_request(der, request) == []
    assert verify_timestamp_response(der, signature, freetsa_trust()).result is (
        ProofResult.VERIFIED
    )


def test_corpus_location() -> None:
    assert (CORPUS / "catalog.json").exists()
