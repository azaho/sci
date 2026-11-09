"""Signed key records: claims (§5.2.1, §6.6) and revocations (§5.3, §6.7)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import ed25519
from .accounts import Account, AccountType, account_problem
from .errors import Invalid, Issue, IssueCollector, Outcome, SciError
from .jsonstrict import JsonObject, json_type_name, parse_json_object
from .keys import PublicKey, parse_public_jwk, public_key_of
from .rfc3339 import format_rfc3339, parse_rfc3339
from .schemas import CLAIM_SCHEMA, REVOCATION_SCHEMA, check_schema
from .text import check_text

__all__ = [
    "DEFAULT_CLAIM_VALIDITY",
    "REVOCATION_FILES",
    "Claim",
    "RecordVerification",
    "SignedRecord",
    "create_claim",
    "create_revocation",
    "parse_claim",
    "parse_revocation",
    "verify_claim",
    "verify_revocation",
]

DEFAULT_CLAIM_VALIDITY = timedelta(days=90)
REVOCATION_FILES = ("revocation.json", "revocation.json.sig", "revocation.json.sig.tsr")
MAX_NOTE = 2500
MAX_NAME = 80


@dataclass(frozen=True, slots=True)
class SignedRecord:
    """Exact record bytes and the raw 64-byte signature over them."""

    record: bytes
    signature: bytes


@dataclass(frozen=True, slots=True)
class Claim:
    """A parsed ``sci-key-claim/0.1`` record (§5.2.1)."""

    public_key: PublicKey
    account: Account
    expires: datetime
    note: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class RecordVerification:
    """Outcome of :func:`verify_claim` or :func:`verify_revocation`."""

    outcome: Outcome
    issues: tuple[Issue, ...] = ()
    public_key: PublicKey | None = None
    claim: Claim | None = None
    raw: JsonObject | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.VALID


def _fields(
    record: JsonObject,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    file: str,
    section: str,
    issues: IssueCollector,
) -> None:
    missing = [name for name in required if name not in record]
    extra = sorted(set(record) - set(required) - set(optional))
    if missing:
        issues.add(
            "record_fields",
            "record is missing required fields",
            section=section,
            file=file,
            observed=missing,
        )
    if extra:
        issues.add(
            "record_fields", "record fields are closed", section=section, file=file, observed=extra
        )


def parse_claim(data: bytes, *, file: str = "claim.json") -> Claim:
    """Check a claim's encoding, schema, and fields (§5.2.1, §7). Signature not checked.

    Raises:
        Invalid, Unsupported: with all independent field issues attached.
    """
    record = parse_json_object(data, file=file)
    check_schema(record, CLAIM_SCHEMA, file=file)
    issues = IssueCollector()
    _fields(
        record,
        ("schema", "public_key", "account", "expires"),
        ("note", "name"),
        file,
        "5.2.1",
        issues,
    )
    public_key = None
    if "public_key" in record:
        public_key = parse_public_jwk(record["public_key"], file=file, collector=issues)
    account = None
    if "account" in record:
        value = record["account"]
        if (
            not isinstance(value, dict)
            or set(value) != {"type", "id"}
            or not all(isinstance(v, str) for v in value.values())
        ):
            issues.add(
                "claim_account",
                "account must have exactly two string fields, type and id",
                section="5.2.1",
                file=file,
                field="/account",
                observed=json_type_name(value) if not isinstance(value, dict) else sorted(value),
            )
        else:
            account_type, account_id = str(value["type"]), str(value["id"])
            problem = account_problem(account_type, account_id)
            if problem is not None and account_type not in set(AccountType):
                issues.add(
                    "unsupported_account_type",
                    "account type has no supported method",
                    section="5.2",
                    file=file,
                    field="/account/type",
                    observed=account_type,
                    outcome=Outcome.UNSUPPORTED,
                )
            elif problem is not None:
                issues.add(
                    "claim_account_id",
                    problem,
                    section="5.2",
                    file=file,
                    field="/account/id",
                    observed=account_id,
                )
            else:
                account = Account(account_type, account_id)
    expires = None
    if "expires" in record:
        value = record["expires"]
        try:
            if not isinstance(value, str):
                raise ValueError
            expires = parse_rfc3339(value)
        except ValueError:
            issues.add(
                "claim_expires",
                "expires must be an RFC 3339 date-time",
                section="5.2.1",
                file=file,
                field="/expires",
                observed=value,
            )
    note = None
    if "note" in record:
        value = record["note"]
        if not isinstance(value, str):
            issues.add(
                "claim_note",
                "note must be a string",
                section="5.2.1",
                file=file,
                field="/note",
                observed=json_type_name(value),
            )
        else:
            before = len(issues)
            check_text(
                value,
                multiline=True,
                nonblank=True,
                max_code_points=MAX_NOTE,
                file=file,
                field="/note",
                collector=issues,
            )
            note = value if len(issues) == before else None
    name = None
    if "name" in record:
        value = record["name"]
        if not isinstance(value, str):
            issues.add(
                "claim_name", "name must be a string", section="5.2.1", file=file, field="/name"
            )
        else:
            check_text(
                value,
                nonblank=True,
                max_code_points=MAX_NAME,
                file=file,
                field="/name",
                collector=issues,
            )
            name = value
    issues.raise_if_any()
    assert public_key is not None
    assert account is not None
    assert expires is not None
    return Claim(public_key, account, expires, note, name)


def verify_claim(
    record: bytes,
    signature: bytes,
    *,
    expected_public_key: PublicKey | str,
    requested_account: Account,
    now: datetime | None = None,
    retrieved_from_account_location: bool = True,
    known_revocation: bool = False,
    file: str = "claim.json",
) -> RecordVerification:
    """Key association checks (§6.6) on retrieved claim bytes.

    ``retrieved_from_account_location`` states that the bytes were retrieved
    from the account method's hosting location (§5.2.2-5.2.4); a local
    signature alone does not establish a hosted association.
    ``known_revocation`` is True if a verified revocation for the key is known.
    """
    try:
        claim = parse_claim(record, file=file)
        ed25519.verify(claim.public_key.raw, signature, record, file=file + ".sig")
        expected = (
            expected_public_key
            if isinstance(expected_public_key, PublicKey)
            else PublicKey.from_x(expected_public_key)
        )
        issues = IssueCollector()
        if claim.public_key.raw != expected.raw:
            issues.add(
                "claim_key_mismatch",
                "claim public key must equal the expected key",
                section="6.6",
                file=file,
                field="/public_key",
                observed=claim.public_key.x,
            )
        if claim.account != requested_account:
            issues.add(
                "claim_account_mismatch",
                "account type and id must equal the requested account",
                section="6.6",
                file=file,
                field="/account",
                observed=claim.account.to_json(),
            )
        current = now or datetime.now(UTC)
        if not current < claim.expires:
            issues.add(
                "claim_expired",
                "the current time must be before expires",
                section="6.6",
                file=file,
                field="/expires",
                observed=format_rfc3339(claim.expires),
            )
        if known_revocation:
            issues.add(
                "key_revoked",
                "the key must have no known verified revocation",
                section="6.6",
                file=file,
            )
        if not retrieved_from_account_location:
            issues.add(
                "claim_not_hosted",
                "the claim must be retrieved from the account's hosting location",
                section="6.6",
                file=file,
            )
        issues.raise_if_any()
    except SciError as exc:
        return RecordVerification(exc.outcome, exc.issues)
    return RecordVerification(Outcome.VALID, (), claim.public_key, claim)


def create_claim(
    private_key: Ed25519PrivateKey,
    account: Account,
    *,
    expires: datetime | None = None,
    note: str | None = None,
    name: str | None = None,
    now: datetime | None = None,
) -> SignedRecord:
    """Create and sign a claim (default expiry 90 days from ``now``).

    The record is serialized compactly with members ``schema``,
    ``public_key``, ``account``, ``expires``, [``name``], [``note``]; it is re-parsed to
    guarantee validity.
    """
    problem = account_problem(account.type, account.id)
    if problem is not None:
        raise ValueError(problem)
    expiry = expires or (now or datetime.now(UTC)) + DEFAULT_CLAIM_VALIDITY
    document: dict[str, object] = {
        "schema": CLAIM_SCHEMA,
        "public_key": public_key_of(private_key).to_jwk(),
        "account": account.to_json(),
        "expires": format_rfc3339(expiry),
    }
    if name is not None:
        document["name"] = name
    if note is not None:
        document["note"] = note
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    parse_claim(raw)
    return SignedRecord(raw, ed25519.sign(private_key, raw))


def parse_revocation(data: bytes, *, file: str = "revocation.json") -> PublicKey:
    """Check a revocation's encoding, schema, and fields (§5.3); return its key."""
    record = parse_json_object(data, file=file)
    check_schema(record, REVOCATION_SCHEMA, file=file)
    issues = IssueCollector()
    _fields(record, ("schema", "public_key"), (), file, "5.3", issues)
    key = None
    if "public_key" in record:
        key = parse_public_jwk(record["public_key"], file=file, collector=issues)
    issues.raise_if_any()
    assert key is not None
    return key


def verify_revocation(
    record: bytes,
    signature: bytes,
    *,
    expected_public_key: PublicKey | str | None = None,
    file: str = "revocation.json",
) -> RecordVerification:
    """Revocation checks (§6.7); optionally require the revoked key (§8.1.2)."""
    try:
        key = parse_revocation(record, file=file)
        ed25519.verify(key.raw, signature, record, file=file + ".sig")
        if expected_public_key is not None:
            expected = (
                expected_public_key
                if isinstance(expected_public_key, PublicKey)
                else PublicKey.from_x(expected_public_key)
            )
            if expected.raw != key.raw:
                raise Invalid(
                    Issue(
                        "revocation_key_mismatch",
                        "record public_key.x must equal the requested key",
                        section="8.1.2",
                        file=file,
                        field="/public_key/x",
                        observed=key.x,
                    )
                )
    except SciError as exc:
        return RecordVerification(exc.outcome, exc.issues)
    return RecordVerification(Outcome.VALID, (), key)


def create_revocation(private_key: Ed25519PrivateKey) -> SignedRecord:
    """Create and sign a revocation record for ``private_key``'s public key (§5.3)."""
    raw = json.dumps(
        {"schema": REVOCATION_SCHEMA, "public_key": public_key_of(private_key).to_jwk()},
        separators=(",", ":"),
    ).encode("ascii")
    return SignedRecord(raw, ed25519.sign(private_key, raw))
