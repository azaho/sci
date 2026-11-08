"""Publication identifiers (protocol §2, §4.4, §8.1.1)."""

from __future__ import annotations

import hashlib
import re
from typing import Final

__all__ = [
    "is_digest",
    "parse_publication_id",
    "publication_id",
    "publication_id_from_manifest",
    "sci_citation_target",
]

_DIGEST: Final = re.compile(r"[0-9a-f]{64}")


def is_digest(value: str) -> bool:
    """True for exactly 64 lowercase hexadecimal characters."""
    return _DIGEST.fullmatch(value) is not None


def parse_publication_id(value: str, *, allow_bare: bool = False) -> str:
    """Parse ``sha256:<digest>`` or ``sci:sha256:<digest>``; return the bare digest.

    With ``allow_bare`` a bare 64-character digest (the §8.1.1 ``id`` form) is
    also accepted. Uppercase hexadecimal is rejected, never normalized.

    Raises:
        ValueError: the value is not a publication identifier.
    """
    for prefix in ("sci:sha256:", "sha256:"):
        if value.startswith(prefix):
            digest = value[len(prefix) :]
            break
    else:
        if not allow_bare:
            raise ValueError("publication IDs use sha256:<64 lowercase hex> or sci:sha256:<...>")
        digest = value
    if not is_digest(digest):
        raise ValueError("publication digest must be 64 lowercase hexadecimal characters")
    return digest


def publication_id(digest: str) -> str:
    """Format a bare digest as ``sha256:<digest>``."""
    if not is_digest(digest):
        raise ValueError("publication digest must be 64 lowercase hexadecimal characters")
    return "sha256:" + digest


def publication_id_from_manifest(manifest_bytes: bytes) -> str:
    """``sha256:`` identifier of the exact manifest bytes (§2)."""
    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


def sci_citation_target(value: str) -> str:
    """The ``sci:sha256:<digest>`` citation form of an identifier (§4.4)."""
    return "sci:sha256:" + parse_publication_id(value, allow_bare=True)
