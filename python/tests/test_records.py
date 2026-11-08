from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci.accounts import Account, account_problem, claim_urls, normalize_account
from pysci.errors import Outcome
from pysci.keys import public_key_of
from pysci.records import (
    create_claim,
    create_revocation,
    parse_claim,
    verify_claim,
    verify_revocation,
)
from pysci.rfc3339 import format_rfc3339, parse_rfc3339

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def test_claim_roundtrip(private_key: Ed25519PrivateKey) -> None:
    account = Account("domain", "example.org")
    signed = create_claim(private_key, account, now=NOW, note="Lab key")
    claim = parse_claim(signed.record)
    assert claim.expires == NOW + timedelta(days=90)
    x = public_key_of(private_key).x
    result = verify_claim(
        signed.record, signed.signature, expected_public_key=x, requested_account=account, now=NOW
    )
    assert result.ok and result.claim is not None


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"now": NOW + timedelta(days=90)}, "claim_expired"),
        ({"known_revocation": True}, "key_revoked"),
        ({"retrieved_from_account_location": False}, "claim_not_hosted"),
        ({"requested_account": Account("github", "example.org")}, "claim_account_mismatch"),
    ],
)
def test_claim_association_failures(
    private_key: Ed25519PrivateKey, kwargs: dict[str, object], code: str
) -> None:
    account = Account("domain", "example.org")
    signed = create_claim(private_key, account, now=NOW)
    options: dict[str, object] = {
        "expected_public_key": public_key_of(private_key),
        "requested_account": account,
        "now": NOW,
        **kwargs,
    }
    result = verify_claim(signed.record, signed.signature, **options)  # type: ignore[arg-type]
    assert result.outcome is Outcome.INVALID
    assert result.issues[0].code == code


def test_claim_unknown_account_type_is_unsupported(private_key: Ed25519PrivateKey) -> None:
    raw = (
        '{"schema":"sci-key-claim/0.1","public_key":{"kty":"OKP","crv":"Ed25519","x":"'
        + public_key_of(private_key).x
        + '"},"account":{"type":"mastodon","id":"a"},"expires":"2027-01-01T00:00:00Z"}'
    ).encode()
    signature = private_key.sign(raw)
    result = verify_claim(
        raw,
        signature,
        expected_public_key=public_key_of(private_key),
        requested_account=Account("mastodon", "a"),
        now=NOW,
    )
    assert result.outcome is Outcome.UNSUPPORTED


def test_revocation_roundtrip(private_key: Ed25519PrivateKey) -> None:
    signed = create_revocation(private_key)
    x = public_key_of(private_key).x
    assert verify_revocation(signed.record, signed.signature, expected_public_key=x).ok
    other = Ed25519PrivateKey.from_private_bytes(b"\x09" * 32)
    mismatch = verify_revocation(
        signed.record, signed.signature, expected_public_key=public_key_of(other)
    )
    assert mismatch.issues[0].code == "revocation_key_mismatch"
    forged = verify_revocation(signed.record, other.sign(signed.record))
    assert forged.outcome is Outcome.INVALID
    unknown = verify_revocation(signed.record.replace(b"/0.1", b"/0.2"), signed.signature)
    assert unknown.outcome is Outcome.UNSUPPORTED


@pytest.mark.parametrize(
    ("account_type", "account_id", "ok"),
    [
        ("domain", "example.org", True),
        ("domain", "xn--bcher-kva.example", True),
        ("domain", "Example.org", False),
        ("domain", "example.org.", False),
        ("domain", "127.0.0.1", False),
        ("domain", "xn--a.example", False),
        ("domain", "-bad.example", False),
        ("domain", "a.123", False),
        ("github", "octo-lab", True),
        ("github", "Octo", False),
        ("huggingface", "ExampleOrg", True),
        ("huggingface", "a/b", False),
    ],
)
def test_account_methods(account_type: str, account_id: str, ok: bool) -> None:
    assert (account_problem(account_type, account_id) is None) is ok


def test_account_normalization_and_urls() -> None:
    assert normalize_account("domain", "Bücher.Example.").id == "xn--bcher-kva.example"
    assert normalize_account("github", "Octo-Lab").id == "octo-lab"
    urls = claim_urls(Account("github", "octo"), "X")
    assert urls[0] == (
        "https://raw.githubusercontent.com/octo/octo/main/sci/keys/X.json",
        "https://raw.githubusercontent.com/octo/octo/main/sci/keys/X.json.sig",
    )
    assert "/.github/" in urls[1][0]
    assert claim_urls(Account("domain", "a.example"), "X")[0][0] == (
        "https://a.example/.well-known/sci/keys/X.json"
    )


@pytest.mark.parametrize(
    "value",
    [
        "2027-01-01",
        "2027-13-01T00:00:00Z",
        "2027-02-30T00:00:00Z",
        "2027-01-01T24:00:00Z",
        "2027-01-01T00:00:00",
        "2027-01-01T00:00:00+24:00",
        "2027-01-01T12:00:60Z",
    ],
)
def test_rfc3339_rejections(value: str) -> None:
    with pytest.raises(ValueError):
        parse_rfc3339(value)


def test_rfc3339_accepts_offsets_fractions_and_leap_second() -> None:
    assert parse_rfc3339("2026-12-31T19:00:00-05:00") == datetime(2027, 1, 1, tzinfo=UTC)
    assert parse_rfc3339("2026-11-07t12:00:00.5z").microsecond == 500000
    assert parse_rfc3339("2016-12-31T23:59:60Z") == datetime(2017, 1, 1, tzinfo=UTC)
    assert format_rfc3339(NOW) == "2026-11-07T12:00:00Z"
