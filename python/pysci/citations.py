"""Citation targets (protocol §4.4): validation and comparison keys.

Three target forms are permitted:

* ``sci:sha256:<digest>`` -- an SCI publication;
* ``doi:<name>`` -- a DOI URI (DOI names compare case-insensitively for ASCII
  after percent-decoding);
* an ``http``/``https`` URI (RFC 9110 §4.2) with an optional fragment.

:func:`citation_target_key` derives a normalized key so that equivalent
spellings of the same DOI or URL can be grouped, e.g. by an indexer.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .ids import is_digest

__all__ = [
    "CITATION_RELATIONS",
    "CitationTarget",
    "TargetKind",
    "citation_target_key",
    "is_http_uri",
    "parse_citation_target",
]

CITATION_RELATIONS: Final = frozenset({"uses", "reviews", "supports", "challenges", "supersedes"})

_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SUB_DELIMS: Final = frozenset("!$&'()*+,;=")
_PCHAR: Final = _UNRESERVED | _SUB_DELIMS | frozenset(":@")
_PATH_CHARS: Final = _PCHAR | frozenset("/")
_QUERY_CHARS: Final = _PCHAR | frozenset("/?")
_REG_NAME_CHARS: Final = _UNRESERVED | _SUB_DELIMS
_HEX: Final = frozenset("0123456789abcdefABCDEF")

_HTTP_URI: Final = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<path>[^?#]*)"
    r"(?:\?(?P<query>[^#]*))?(?:#(?P<fragment>.*))?",
    re.DOTALL,
)
_DOI: Final = re.compile(r"doi:(?P<prefix>10\.[0-9]+(?:\.[0-9]+)*)/(?P<suffix>.+)", re.DOTALL)


class TargetKind(StrEnum):
    SCI = "sci"
    DOI = "doi"
    URL = "url"


@dataclass(frozen=True, slots=True)
class CitationTarget:
    """A validated citation target and its comparison key."""

    kind: TargetKind
    target: str
    key: str


def _valid_chars(value: str, allowed: frozenset[str]) -> bool:
    """True if ``value`` uses only ``allowed`` characters and valid %HH escapes."""
    index = 0
    length = len(value)
    while index < length:
        ch = value[index]
        if ch == "%":
            if index + 2 >= length:
                return False
            if value[index + 1] not in _HEX or value[index + 2] not in _HEX:
                return False
            index += 3
            continue
        if ch not in allowed:
            return False
        index += 1
    return True


def _valid_authority(authority: str) -> bool:
    if not authority or "@" in authority:
        # RFC 9110 §4.2.1: an empty host is invalid; userinfo is deprecated
        # and treated as an error by recipients (§4.2.4).
        return False
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            return False
        literal, rest = authority[1:end], authority[end + 1 :]
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return False
        port = rest[1:] if rest.startswith(":") else None
        if rest and port is None:
            return False
    else:
        host, sep, port_text = authority.partition(":")
        port = port_text if sep else None
        if not host or not _valid_chars(host, _REG_NAME_CHARS):
            return False
    return port is None or port.isdigit() or port == ""


def is_http_uri(value: str) -> bool:
    """True for an absolute ``http``/``https`` URI with an optional fragment."""
    match = _HTTP_URI.fullmatch(value)
    if match is None or match["scheme"].lower() not in ("http", "https"):
        return False
    if not value.isascii():
        return False
    if not _valid_authority(match["authority"]):
        return False
    if not _valid_chars(match["path"], _PATH_CHARS):
        return False
    query = match["query"]
    if query is not None and not _valid_chars(query, _QUERY_CHARS):
        return False
    fragment = match["fragment"]
    return fragment is None or _valid_chars(fragment, _QUERY_CHARS)


def _percent_normalize(value: str, keep: frozenset[str], *, fold_case: bool = False) -> str:
    """Decode %HH into bytes, optionally ASCII-lowercase, and re-encode canonically.

    Characters in ``keep`` (and unreserved characters) are written literally;
    every other byte becomes an uppercase %HH escape.
    """
    raw = bytearray()
    index = 0
    while index < len(value):
        ch = value[index]
        if ch == "%":
            raw.append(int(value[index + 1 : index + 3], 16))
            index += 3
        else:
            raw.extend(ch.encode("utf-8"))
            index += 1
    if fold_case:
        raw = bytearray(raw.lower())  # bytes.lower() affects ASCII letters only
    out = []
    literal = keep | _UNRESERVED
    for byte in raw:
        ch = chr(byte)
        out.append(ch if byte < 0x80 and ch in literal else f"%{byte:02X}")
    return "".join(out)


def _normalize_escapes(value: str, *, lower: bool = False) -> str:
    """RFC 3986 §6.2.2: uppercase %HH, decode escaped unreserved characters only."""
    out: list[str] = []
    index = 0
    while index < len(value):
        ch = value[index]
        if ch == "%":
            byte = int(value[index + 1 : index + 3], 16)
            decoded = chr(byte)
            if decoded in _UNRESERVED:
                out.append(decoded.lower() if lower else decoded)
            else:
                out.append(f"%{byte:02X}")
            index += 3
        else:
            out.append(ch.lower() if lower else ch)
            index += 1
    return "".join(out)


def _url_key(value: str) -> str:
    match = _HTTP_URI.fullmatch(value)
    if match is None:  # pragma: no cover - validated before
        raise ValueError("not an HTTP URI")
    scheme = match["scheme"].lower()
    authority = match["authority"]
    if authority.startswith("["):
        end = authority.index("]")
        host = "[" + str(ipaddress.IPv6Address(authority[1:end])) + "]"
        port = authority[end + 2 :] if authority[end + 1 :].startswith(":") else ""
    else:
        host_text, _, port = authority.partition(":")
        host = _normalize_escapes(host_text, lower=True)
    if port and port.lstrip("0") == ("80" if scheme == "http" else "443"):
        port = ""
    port = port.lstrip("0") or ("0" if port else "")
    path = _normalize_escapes(match["path"]) or "/"
    key = f"{scheme}://{host}{':' + port if port else ''}{path}"
    if match["query"] is not None:
        key += "?" + _normalize_escapes(match["query"])
    if match["fragment"] is not None:
        key += "#" + _normalize_escapes(match["fragment"])
    return key


def parse_citation_target(target: str) -> CitationTarget | None:
    """Validate a §4.4 target; None if it uses none of the permitted forms."""
    if target.startswith("sci:"):
        digest = target[len("sci:sha256:") :] if target.startswith("sci:sha256:") else ""
        if not is_digest(digest):
            return None
        return CitationTarget(TargetKind.SCI, target, target)
    if target.startswith("doi:"):
        match = _DOI.fullmatch(target)
        if match is None or not target.isascii() or not _valid_chars(match["suffix"], _PATH_CHARS):
            return None
        suffix = _percent_normalize(match["suffix"], _PATH_CHARS - _UNRESERVED, fold_case=True)
        return CitationTarget(TargetKind.DOI, target, f"doi:{match['prefix']}/{suffix}")
    if is_http_uri(target):
        return CitationTarget(TargetKind.URL, target, _url_key(target))
    return None


def citation_target_key(target: str) -> str:
    """Normalized comparison key for a citation target.

    * SCI targets are returned unchanged (they are already canonical).
    * DOI targets are percent-decoded, ASCII-lowercased, and re-encoded with
      uppercase escapes for every byte outside RFC 3986 ``pchar``/``/``.
    * URLs get RFC 3986 §6.2.2-6.2.3 normalization: lowercase scheme and host,
      default port removed, empty path as ``/``, percent-encoding normalized.
      The fragment is retained.

    Raises:
        ValueError: ``target`` is not a valid citation target.
    """
    parsed = parse_citation_target(target)
    if parsed is None:
        raise ValueError("not a valid SCI, DOI, or HTTP(S) citation target")
    return parsed.key
