"""External account methods for key claims (protocol §5.2.2-5.2.4)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import idna

__all__ = [
    "Account",
    "AccountType",
    "account_problem",
    "claim_urls",
    "normalize_account",
]

_LDH_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_GITHUB_LOGIN: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38})")
_HF_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")


class AccountType(StrEnum):
    DOMAIN = "domain"
    GITHUB = "github"
    HUGGINGFACE = "huggingface"


@dataclass(frozen=True, slots=True)
class Account:
    """An account object ``{"type": ..., "id": ...}`` (§5.2.1)."""

    type: str
    id: str

    def to_json(self) -> dict[str, str]:
        return {"type": self.type, "id": self.id}


def _domain_problem(host: str) -> str | None:
    if not host or not host.isascii():
        return "domain must be an ASCII DNS host name (use IDNA2008 A-labels)"
    if host != host.lower():
        return "domain must be lowercase"
    if host.endswith("."):
        return "domain must not end with a dot"
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return "IP addresses are not permitted"
    if len(host) > 253:
        return "domain exceeds 253 characters"
    labels = host.split(".")
    for label in labels:
        if not _LDH_LABEL.fullmatch(label):
            return f"invalid DNS label {label!r}"
    if labels[-1].isdigit():
        return "top-level label must not be numeric"
    try:
        if idna.encode(host, uts46=False).decode("ascii") != host:
            return "domain is not in IDNA2008 A-label form"
    except idna.IDNAError as exc:
        return f"invalid IDNA2008 label: {exc}"
    return None


def account_problem(account_type: str, account_id: str) -> str | None:
    """Why ``(type, id)`` violates its account method, or None if it is valid.

    Unknown account types are reported as unsupported by the caller; this
    function returns a message for them as well.
    """
    if account_type == AccountType.DOMAIN:
        return _domain_problem(account_id)
    if account_type == AccountType.GITHUB:
        if account_id != account_id.lower():
            return "GitHub login must be lowercase"
        if not _GITHUB_LOGIN.fullmatch(account_id):
            return "not a GitHub login"
        return None
    if account_type == AccountType.HUGGINGFACE:
        return None if _HF_NAME.fullmatch(account_id) else "not a Hugging Face user or org name"
    return f"unsupported account type {account_type!r}"


def normalize_account(account_type: str, account_id: str) -> Account:
    """Normalize user input to the protocol spelling.

    Domains are lowercased, a final dot removed, and Unicode labels converted
    to IDNA2008 A-labels; GitHub logins are lowercased. Hugging Face names are
    returned unchanged (their canonical spelling comes from the Hub API).

    Raises:
        ValueError: the result still violates the account method.
    """
    if account_type == AccountType.DOMAIN:
        host = account_id.strip().rstrip(".")
        try:
            host = idna.encode(host, uts46=True).decode("ascii")
        except idna.IDNAError as exc:
            raise ValueError(f"invalid domain: {exc}") from None
        account_id = host.lower()
    elif account_type == AccountType.GITHUB:
        account_id = account_id.strip().lower()
    problem = account_problem(account_type, account_id)
    if problem is not None:
        raise ValueError(problem)
    return Account(account_type, account_id)


def claim_urls(account: Account, x: str) -> list[tuple[str, str]]:
    """Hosting locations ``(claim URL, signature URL)`` for a key's claim.

    GitHub has two permitted repositories; both are returned in preference
    order. Raises ValueError for unsupported account types.
    """
    if account.type == AccountType.DOMAIN:
        bases = [f"https://{account.id}/.well-known/sci/keys/{x}.json"]
    elif account.type == AccountType.GITHUB:
        bases = [
            f"https://raw.githubusercontent.com/{account.id}/{account.id}/main/sci/keys/{x}.json",
            f"https://raw.githubusercontent.com/{account.id}/.github/main/sci/keys/{x}.json",
        ]
    elif account.type == AccountType.HUGGINGFACE:
        bases = [
            f"https://huggingface.co/spaces/{account.id}/README/resolve/main/sci/keys/{x}.json"
        ]
    else:
        raise ValueError(f"unsupported account type {account.type!r}")
    return [(url, url + ".sig") for url in bases]
