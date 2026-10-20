"""Shared publication format: validation, packaging, signing, and verification.

The CLI, the publishing server, pull, imports, and archive recovery all use
these functions. Nothing here rewrites, normalizes, or repairs signed input:
validation either accepts the exact bytes or raises ``PublicationError``.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import uuid as _uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import BinaryIO, Iterator

from . import keys

# ---------------------------------------------------------------------------
# Constants and limits
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

SCI_JSON = "sci.json"
README = "README.md"
LICENSE = "LICENSE"
FILES_ZIP = "files.zip"
HEADLINE_FIGURE = "headline_figure.png"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.json.sig"
ACCEPTANCE = "acceptance.json"
FILES_DIR = "files"

REQUIRED_FILES = (SCI_JSON, README, LICENSE)
OPTIONAL_FILES = (FILES_ZIP, HEADLINE_FIGURE)
DECLARED_FILES = REQUIRED_FILES + OPTIONAL_FILES
# Every object of a complete publication, in a stable order.
PUBLICATION_OBJECTS = (SCI_JSON, MANIFEST, SIGNATURE, LICENSE, README, FILES_ZIP, HEADLINE_FIGURE)

CONTENT_TYPES = {
    SCI_JSON: "application/json",
    MANIFEST: "application/json",
    SIGNATURE: "application/octet-stream",
    LICENSE: "text/plain; charset=utf-8",
    README: "text/markdown; charset=utf-8",
    FILES_ZIP: "application/zip",
    HEADLINE_FIGURE: "image/png",
    ACCEPTANCE: "application/json",
}

MAX_JSON_BYTES = 65_536  # sci.json + manifest.json
MAX_README_BYTES = 1_000_000
MAX_FIGURE_BYTES = 2_000_000
MAX_ATTACHMENT_TOTAL = 50_000_000  # README + files.zip + headline figure
MAX_FIGURE_SIDE = 1_600
MAX_FIGURE_PIXELS = 2_560_000

MAX_TITLE = 200
MAX_ABSTRACT = 2_500
MAX_KEYWORD = 64
MAX_KEYWORDS = 20
MAX_CAPTION = 400
MAX_REASON = 1_000
MAX_CATEGORIES = 3
MAX_TARGET_CHARS = 512
MAX_TARGET_KEY_BYTES = 512

PREVIEW_MAX_PATHS = 200
PREVIEW_MAX_BYTES = 8_192

RELATIONS = ("uses", "supports", "reproduces", "challenges", "supersedes")

# Shared whitespace definition (DESIGN.md, "Text fields").
WHITESPACE = frozenset(
    [chr(c) for c in range(0x09, 0x0E)]
    + [" ", "\u0085", " ", " "]
    + [chr(c) for c in range(0x2000, 0x200B)]
    + [" ", " ", " ", " ", "　"]
)
_MULTILINE_ALLOWED = frozenset("\t\n\r")
_LINE_SEPARATORS = frozenset("  ")


class PublicationError(ValueError):
    """A validation or verification failure.

    ``field`` names the offending field or file; ``code`` is a short stable
    identifier used by the server's JSON error responses.
    """

    def __init__(self, message: str, field: str | None = None, code: str = "invalid_publication"):
        self.field = field
        self.code = code
        super().__init__(f"{field}: {message}" if field else message)


# ---------------------------------------------------------------------------
# Bundled assets
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def license_bytes() -> bytes:
    return resources.files("sci").joinpath("publication_LICENSE.txt").read_bytes()


@lru_cache(maxsize=1)
def license_entry() -> dict:
    data = license_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@lru_cache(maxsize=1)
def category_catalog() -> dict:
    data = resources.files("sci").joinpath("categories.json").read_bytes()
    return json.loads(data)


@lru_cache(maxsize=1)
def category_codes() -> frozenset[str]:
    return frozenset(c["code"] for c in category_catalog()["categories"])


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------


class _Float(float):
    """Marks numbers written with a fraction or exponent."""


def _reject_constant(name: str):
    raise PublicationError(f"nonstandard numeric value {name} is not allowed", code="invalid_json")


def _pairs_hook(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise PublicationError(f"duplicate object key {k!r}", code="invalid_json")
        obj[k] = v
    return obj


_DECODER = json.JSONDecoder(
    object_pairs_hook=_pairs_hook,
    parse_float=lambda s: _Float(s),
    parse_constant=_reject_constant,
    strict=True,
)


def parse_strict_json(data: bytes, name: str) -> dict:
    """Parse strict UTF-8 JSON with an object root.

    Rejects a BOM, malformed UTF-8, duplicate keys, trailing data, NaN and
    Infinity, and nulls anywhere in the document.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise PublicationError("expected bytes", name, "invalid_json")
    if data.startswith(b"\xef\xbb\xbf"):
        raise PublicationError("must not start with a UTF-8 byte-order mark", name, "invalid_json")
    try:
        text = bytes(data).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicationError(f"is not valid UTF-8 (byte offset {exc.start})", name, "invalid_json") from exc
    try:
        value, end = _DECODER.raw_decode(text, _skip_ws(text, 0))
    except PublicationError as exc:
        raise PublicationError(str(exc), name, "invalid_json") from exc
    except json.JSONDecodeError as exc:
        raise PublicationError(f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}", name, "invalid_json") from exc
    except RecursionError as exc:
        raise PublicationError("JSON nesting is too deep", name, "invalid_json") from exc
    except ValueError as exc:  # e.g. integers beyond Python's digit limit
        raise PublicationError(f"invalid JSON value: {exc}", name, "invalid_json") from exc
    if _skip_ws(text, end) != len(text):
        raise PublicationError("unexpected data after the JSON value", name, "invalid_json")
    if not isinstance(value, dict):
        raise PublicationError("the document root must be a JSON object", name, "invalid_json")
    _check_depth(value, name)
    _reject_nulls(value, name)
    return value


MAX_JSON_DEPTH = 8  # SCI documents nest at most four levels


def _check_depth(value, name: str) -> None:
    stack = [(value, 1)]
    while stack:
        v, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise PublicationError(f"JSON nesting deeper than {MAX_JSON_DEPTH} levels", name, "invalid_json")
        if isinstance(v, dict):
            stack.extend((c, depth + 1) for c in v.values())
        elif isinstance(v, list):
            stack.extend((c, depth + 1) for c in v)


def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\n\r":
        i += 1
    return i


def _reject_nulls(value, path: str) -> None:
    if value is None:
        raise PublicationError("JSON null is not allowed; omit absent fields", path, "invalid_json")
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_nulls(v, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# Text, identifiers, and timestamps
# ---------------------------------------------------------------------------


def _is_noncharacter(cp: int) -> bool:
    return 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def check_code_points(text: str, field: str, *, multiline: bool = False) -> None:
    """Apply the shared code point rules to one decoded string."""
    for i, ch in enumerate(text):
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise PublicationError(f"contains a surrogate code point U+{cp:04X} at index {i}", field)
        if _is_noncharacter(cp):
            raise PublicationError(f"contains the noncharacter U+{cp:04X} at index {i}", field)
        if cp < 0x20 or 0x7F <= cp <= 0x9F:
            if multiline and ch in _MULTILINE_ALLOWED:
                continue
            raise PublicationError(f"contains the control character U+{cp:04X} at index {i}", field)
        if ch in _LINE_SEPARATORS and not multiline:
            raise PublicationError(f"contains the line separator U+{cp:04X}; this field is single-line", field)


def is_blank(text: str) -> bool:
    return all(ch in WHITESPACE for ch in text)


def check_text(value, field: str, *, max_chars: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise PublicationError("must be a string", field)
    check_code_points(value, field, multiline=multiline)
    if is_blank(value):
        raise PublicationError("must contain at least one non-whitespace character", field)
    if len(value) > max_chars:
        raise PublicationError(
            f"has {len(value)} characters; the maximum is {max_chars}. Shorten it and sign again", field
        )
    return value


_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def is_uuid4(value) -> bool:
    return isinstance(value, str) and _UUID4_RE.fullmatch(value) is not None


def check_uuid4(value, field: str = "uuid") -> str:
    if not is_uuid4(value):
        raise PublicationError(
            "must be a canonical lowercase UUIDv4 (8-4-4-4-12 hex digits, version 4, RFC 4122 variant)",
            field,
            "invalid_uuid",
        )
    return value


def new_uuid4() -> str:
    return str(_uuid.UUID(bytes=os.urandom(16), version=4))


_TIMESTAMP_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,6})?Z$"
)


def parse_timestamp(value, field: str = "created_at") -> _dt.datetime:
    if not isinstance(value, str):
        raise PublicationError("must be a string", field)
    m = _TIMESTAMP_RE.fullmatch(value)
    if not m:
        raise PublicationError("must be a UTC time in the form YYYY-MM-DDTHH:MM:SS[.ffffff]Z", field)
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7)
    micro = int((frac[1:] + "000000")[:6]) if frac else 0
    if year < 1:
        raise PublicationError("year must be between 0001 and 9999", field)
    if hour > 23 or minute > 59 or second > 59:
        raise PublicationError("hours must be 00-23 and minutes/seconds 00-59 (no leap seconds)", field)
    try:
        return _dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=_dt.timezone.utc)
    except ValueError as exc:
        raise PublicationError(f"is not a real calendar date ({exc})", field) from exc


def format_timestamp(when: _dt.datetime, *, micro: bool = False) -> str:
    when = when.astimezone(_dt.timezone.utc)
    if micro:
        return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_timestamp() -> str:
    return format_timestamp(_dt.datetime.now(_dt.timezone.utc))


# ---------------------------------------------------------------------------
# Citation targets
# ---------------------------------------------------------------------------

# Characters a doi: URI may contain without percent-encoding (RFC 3986 pchar
# and "/"). Everything else in a DOI name must be percent-encoded.
_DOI_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/"
)
_HEX = frozenset("0123456789abcdefABCDEF")
_DOI_NAME_RE = re.compile(r"^10\.[0-9]+(?:\.[0-9]+)*/.+$", re.S)


def normalize_citation_target(target, field: str = "target") -> str:
    """Validate a citation target and return its ``target_key``.

    ``sci:<uuid>`` targets are their own key. ``doi:`` targets are decoded
    according to the DOI URI scheme, ASCII case-folded (DOI names are
    case-insensitive), and re-encoded deterministically: characters outside
    the safe URI set are percent-encoded with uppercase hex. The SQL function
    ``catalog_v1.normalize_citation_target`` implements the same algorithm and
    is checked against the same fixtures.
    """
    if not isinstance(target, str):
        raise PublicationError("must be a string", field, "invalid_citation")
    if any(ord(ch) > 0x7F for ch in target):
        raise PublicationError(
            "must contain only ASCII characters; percent-encode other characters in DOI names",
            field,
            "invalid_citation",
        )
    if len(target) > MAX_TARGET_CHARS:
        raise PublicationError(
            f"has {len(target)} characters; citation targets are limited to {MAX_TARGET_CHARS}"
            " ASCII characters including the scheme and percent-encoding",
            field,
            "invalid_citation",
        )
    if target.startswith("sci:"):
        if not is_uuid4(target[4:]):
            raise PublicationError(
                "a sci: target must be 'sci:' followed by a canonical lowercase UUIDv4", field, "invalid_citation"
            )
        return target
    if target[:4].lower() == "doi:":
        key = "doi:" + _normalize_doi_path(target[4:], field)
        if len(key.encode("utf-8")) > MAX_TARGET_KEY_BYTES:
            raise PublicationError(
                f"normalized key exceeds {MAX_TARGET_KEY_BYTES} UTF-8 bytes", field, "invalid_citation"
            )
        return key
    raise PublicationError(
        "must start with 'sci:' followed by a UUIDv4 or 'doi:' followed by a DOI name"
        " (for example doi:10.1000/182); bare DOIs, resolver URLs, and other schemes are not accepted",
        field,
        "invalid_citation",
    )


def _normalize_doi_path(path: str, field: str) -> str:
    raw = bytearray()
    i, n = 0, len(path)
    while i < n:
        ch = path[i]
        if ch == "%":
            h = path[i + 1 : i + 3]
            if len(h) != 2 or not set(h) <= _HEX:
                raise PublicationError("contains an invalid percent-encoding", field, "invalid_citation")
            raw.append(int(h, 16))
            i += 3
            continue
        if ch in "?#":
            raise PublicationError(
                "must not contain a query ('?') or fragment ('#'); percent-encode these characters if they are part of the DOI name",
                field,
                "invalid_citation",
            )
        if ch not in _DOI_SAFE:
            raise PublicationError(
                f"contains the character {ch!r}, which must be percent-encoded in a doi: URI",
                field,
                "invalid_citation",
            )
        raw.append(ord(ch))
        i += 1
    try:
        name = bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicationError("percent-encoded bytes are not valid UTF-8", field, "invalid_citation") from exc
    check_code_points(name, field)
    if not _DOI_NAME_RE.fullmatch(name):
        raise PublicationError(
            "is not a DOI name: expected 10.<registrant>/<suffix> with a nonempty suffix", field, "invalid_citation"
        )
    folded = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in name)
    out = []
    for b in folded.encode("utf-8"):
        c = chr(b)
        out.append(c if b < 0x80 and c in _DOI_SAFE else f"%{b:02X}")
    return "".join(out)


# ---------------------------------------------------------------------------
# sci.json
# ---------------------------------------------------------------------------

_SCI_FIELDS = {"title", "abstract", "categories", "keywords", "headline_figure_caption", "citations"}
_CITATION_FIELDS = {"target", "relation", "reason"}


def _check_fields(obj: dict, allowed: set, required: set, where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise PublicationError(f"unknown field(s): {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}", where)
    missing = sorted(required - set(obj))
    if missing:
        raise PublicationError(f"missing required field(s): {', '.join(missing)}", where)


def _check_all_strings(value, path: str) -> None:
    """Apply the global code point rules to every string, including keys."""
    if isinstance(value, str):
        check_code_points(value, path, multiline=True)
    elif isinstance(value, dict):
        for k, v in value.items():
            check_code_points(k, f"{path} key", multiline=False)
            _check_all_strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_all_strings(v, f"{path}[{i}]")


@dataclass
class Citation:
    position: int
    target: str
    target_key: str
    relation: str | None = None
    reason: str | None = None


@dataclass
class SciMetadata:
    title: str
    abstract: str
    categories: list[str]
    keywords: list[str]
    headline_figure_caption: str | None
    citations: list[Citation]


def validate_sci(doc: dict) -> SciMetadata:
    """Validate a parsed sci.json. Omitted lists are interpreted as empty."""
    _check_all_strings(doc, "sci.json")
    _check_fields(doc, _SCI_FIELDS, {"title", "abstract"}, "sci.json")
    title = check_text(doc["title"], "title", max_chars=MAX_TITLE)
    abstract = check_text(doc["abstract"], "abstract", max_chars=MAX_ABSTRACT, multiline=True)

    categories = doc.get("categories", [])
    if not isinstance(categories, list):
        raise PublicationError("must be an array of category codes", "categories")
    if len(categories) > MAX_CATEGORIES:
        raise PublicationError(f"has {len(categories)} entries; at most {MAX_CATEGORIES} are allowed", "categories")
    known = category_codes()
    for i, c in enumerate(categories):
        f = f"categories[{i}]"
        if not isinstance(c, str):
            raise PublicationError("must be a string", f)
        check_code_points(c, f)
        if c not in known:
            raise PublicationError(
                f"unknown category code {c!r}; use a code from the bundled catalog (sci/categories.json),"
                " such as cs.LG or q-bio.NC. An older installation may need a catalog update",
                f,
            )
    if len(set(categories)) != len(categories):
        raise PublicationError("must not repeat a category", "categories")

    keywords = doc.get("keywords", [])
    if not isinstance(keywords, list):
        raise PublicationError("must be an array of strings", "keywords")
    if len(keywords) > MAX_KEYWORDS:
        raise PublicationError(f"has {len(keywords)} entries; at most {MAX_KEYWORDS} are allowed", "keywords")
    for i, k in enumerate(keywords):
        check_text(k, f"keywords[{i}]", max_chars=MAX_KEYWORD)

    caption = None
    if "headline_figure_caption" in doc:
        caption = check_text(doc["headline_figure_caption"], "headline_figure_caption", max_chars=MAX_CAPTION)

    citations_in = doc.get("citations", [])
    if not isinstance(citations_in, list):
        raise PublicationError("must be an array of citation objects", "citations")
    citations: list[Citation] = []
    for i, c in enumerate(citations_in):
        where = f"citations[{i}]"
        if not isinstance(c, dict):
            raise PublicationError("must be an object with a target", where)
        _check_fields(c, _CITATION_FIELDS, {"target"}, where)
        if not isinstance(c["target"], str):
            raise PublicationError("must be a string", f"{where}.target")
        check_code_points(c["target"], f"{where}.target")
        key = normalize_citation_target(c["target"], f"{where}.target")
        relation = c.get("relation")
        if relation is not None:
            if not isinstance(relation, str) or relation not in RELATIONS:
                raise PublicationError(f"must be one of {', '.join(RELATIONS)}; omit it for a general citation", f"{where}.relation")
        reason = None
        if "reason" in c:
            reason = check_text(c["reason"], f"{where}.reason", max_chars=MAX_REASON, multiline=True)
        citations.append(Citation(i, c["target"], key, relation, reason))
    return SciMetadata(title, abstract, list(categories), list(keywords), caption, citations)


# ---------------------------------------------------------------------------
# files_tree preview
# ---------------------------------------------------------------------------


def _preview_key(path: str) -> tuple[int, str]:
    is_dir = path.endswith("/")
    depth = path.rstrip("/").count("/") + 1
    group = (0 if is_dir else 1) if depth == 1 else (2 if is_dir else 3)
    return (group, path)


def check_relative_path(path: str, field: str, *, allow_dir_slash: bool = True) -> list[str]:
    """Validate a ``/``-separated relative path; return its components."""
    if not isinstance(path, str) or not path:
        raise PublicationError("paths must be nonempty strings", field)
    check_code_points(path, field)
    if "\\" in path:
        raise PublicationError(f"path {path!r} contains a backslash", field)
    if path.startswith("/"):
        raise PublicationError(f"path {path!r} is absolute", field)
    body = path[:-1] if (allow_dir_slash and path.endswith("/")) else path
    parts = body.split("/")
    for i, p in enumerate(parts):
        if p == "":
            raise PublicationError(f"path {path!r} has an empty component", field)
        if p in (".", ".."):
            raise PublicationError(f"path {path!r} has a '.' or '..' component", field)
        if i == 0 and re.match(r"^[A-Za-z]:", p):
            raise PublicationError(f"path {path!r} looks like a drive-letter path", field)
    return parts


def compact_json_bytes(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def validate_files_tree(tree) -> None:
    where = "manifest.files_tree"
    if not isinstance(tree, dict):
        raise PublicationError("must be an object with paths and truncated", where)
    _check_fields(tree, {"paths", "truncated"}, {"paths", "truncated"}, where)
    paths, truncated = tree["paths"], tree["truncated"]
    if not isinstance(truncated, bool):
        raise PublicationError("must be a Boolean", f"{where}.truncated")
    if not isinstance(paths, list):
        raise PublicationError("must be an array of strings", f"{where}.paths")
    if len(paths) > PREVIEW_MAX_PATHS:
        raise PublicationError(f"has {len(paths)} paths; at most {PREVIEW_MAX_PATHS} are allowed", f"{where}.paths")
    if len(compact_json_bytes(tree)) > PREVIEW_MAX_BYTES:
        raise PublicationError(f"exceeds {PREVIEW_MAX_BYTES} bytes of compact UTF-8 JSON", where)
    seen = set()
    for i, p in enumerate(paths):
        f = f"{where}.paths[{i}]"
        if not isinstance(p, str):
            raise PublicationError("must be a string", f)
        parts = check_relative_path(p, f)
        if len(parts) > 2:
            raise PublicationError(f"path {p!r} is deeper than two levels", f)
        if p in seen:
            raise PublicationError(f"path {p!r} is repeated", f)
        seen.add(p)


class PreviewCollector:
    """Collect the shallow preview from a stream of entries in bounded memory.

    Keeps at most ``PREVIEW_MAX_PATHS`` candidates, ordered by the display
    group order, so a million-entry archive needs no more memory than a small
    one. ``truncated`` is set when an eligible path is dropped.
    """

    def __init__(self, max_paths: int = PREVIEW_MAX_PATHS):
        self.max_paths = max_paths
        self._keys: list[tuple[int, str]] = []
        self._present: set[str] = set()
        self.truncated = False

    def add_entry(self, rel_path: str, is_dir: bool) -> None:
        parts = [p for p in rel_path.split("/") if p != ""]
        if not parts:
            return
        first = parts[0] + ("/" if (len(parts) > 1 or is_dir) else "")
        self._add(first)
        if len(parts) >= 2:
            second = parts[0] + "/" + parts[1] + ("/" if (len(parts) > 2 or is_dir) else "")
            self._add(second)

    def _add(self, path: str) -> None:
        if path in self._present:
            return
        key = _preview_key(path)
        if len(self._keys) >= self.max_paths:
            if key >= self._keys[-1]:
                self.truncated = True
                return
            dropped = self._keys.pop()
            self._present.discard(dropped[1])
            self.truncated = True
        bisect.insort(self._keys, key)
        self._present.add(path)

    def tree(self) -> dict:
        paths = [k[1] for k in self._keys]
        truncated = self.truncated
        tree = {"paths": paths, "truncated": truncated}
        while len(compact_json_bytes(tree)) > PREVIEW_MAX_BYTES:
            paths = paths[:-1]
            truncated = True
            tree = {"paths": paths, "truncated": truncated}
        return tree


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST_FIELDS = {"schema_version", "uuid", "public_key", "created_at", "files", "files_tree"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_int(value, field: str) -> int:
    if type(value) is not int:
        raise PublicationError("must be a JSON integer written without a decimal point or exponent", field)
    return value


@dataclass
class FileEntry:
    sha256: str
    bytes: int


@dataclass
class ManifestInfo:
    uuid: str
    public_key: object  # Ed25519PublicKey
    public_key_b64: str
    fingerprint: str
    created_at: str
    created_at_dt: _dt.datetime
    files: dict[str, FileEntry]
    files_tree: dict | None


def validate_manifest(doc: dict) -> ManifestInfo:
    _check_all_strings(doc, "manifest")
    _check_fields(doc, _MANIFEST_FIELDS, _MANIFEST_FIELDS - {"files_tree"}, "manifest")
    if _check_int(doc["schema_version"], "manifest.schema_version") != SCHEMA_VERSION:
        raise PublicationError(f"unsupported schema version; this software supports {SCHEMA_VERSION}", "manifest.schema_version", "unsupported_schema")
    uid = check_uuid4(doc["uuid"], "manifest.uuid")
    try:
        pub = keys.public_key_from_jwk(doc["public_key"])
    except keys.KeyError_ as exc:
        raise PublicationError(str(exc), "manifest.public_key") from exc
    created_at = doc["created_at"]
    created_dt = parse_timestamp(created_at, "manifest.created_at")

    files_in = doc["files"]
    if not isinstance(files_in, dict):
        raise PublicationError("must be an object mapping filenames to hashes and sizes", "manifest.files")
    unknown = sorted(set(files_in) - set(DECLARED_FILES))
    if unknown:
        raise PublicationError(
            f"unexpected file name(s) {', '.join(map(repr, unknown))}; allowed: {', '.join(DECLARED_FILES)}",
            "manifest.files",
        )
    missing = [n for n in REQUIRED_FILES if n not in files_in]
    if missing:
        raise PublicationError(f"missing required file entr{'y' if len(missing) == 1 else 'ies'}: {', '.join(missing)}", "manifest.files")
    files: dict[str, FileEntry] = {}
    for name in DECLARED_FILES:
        if name not in files_in:
            continue
        where = f"manifest.files[{name!r}]"
        entry = files_in[name]
        if not isinstance(entry, dict):
            raise PublicationError("must be an object with sha256 and bytes", where)
        _check_fields(entry, {"sha256", "bytes"}, {"sha256", "bytes"}, where)
        if not isinstance(entry["sha256"], str) or not _HASH_RE.fullmatch(entry["sha256"]):
            raise PublicationError("sha256 must be 64 lowercase hexadecimal characters", where)
        size = _check_int(entry["bytes"], f"{where}.bytes")
        if size < 0:
            raise PublicationError("bytes must not be negative", where)
        files[name] = FileEntry(entry["sha256"], size)

    limits = {
        SCI_JSON: MAX_JSON_BYTES,
        README: MAX_README_BYTES,
        HEADLINE_FIGURE: MAX_FIGURE_BYTES,
        FILES_ZIP: MAX_ATTACHMENT_TOTAL,
    }
    for name, limit in limits.items():
        if name in files and files[name].bytes > limit:
            raise PublicationError(f"declares {files[name].bytes} bytes; the limit is {limit}", f"manifest.files[{name!r}]", "too_large")
    if files[README].bytes == 0:
        raise PublicationError("README.md must not be empty", "manifest.files['README.md']")
    total = sum(files[n].bytes for n in (README, FILES_ZIP, HEADLINE_FIGURE) if n in files)
    if total > MAX_ATTACHMENT_TOTAL:
        raise PublicationError(
            f"README + files.zip + headline figure total {total} bytes; the limit is {MAX_ATTACHMENT_TOTAL}",
            "manifest.files",
            "too_large",
        )

    tree = doc.get("files_tree")
    if tree is not None:
        validate_files_tree(tree)
        if FILES_ZIP not in files:
            raise PublicationError("files_tree requires a files.zip entry", "manifest.files_tree")

    raw = keys.raw_public_key(pub)
    return ManifestInfo(
        uuid=uid,
        public_key=pub,
        public_key_b64=keys.b64url_encode(raw),
        fingerprint=keys.fingerprint_raw(raw),
        created_at=created_at,
        created_at_dt=created_dt,
        files=files,
        files_tree=tree,
    )


def check_license_entry(info: ManifestInfo) -> None:
    approved = license_entry()
    entry = info.files[LICENSE]
    if entry.sha256 != approved["sha256"] or entry.bytes != approved["bytes"]:
        raise PublicationError(
            "LICENSE does not match the approved SCI publication license"
            f" (expected sha256 {approved['sha256']} and {approved['bytes']} bytes)",
            "manifest.files['LICENSE']",
            "unapproved_license",
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Verification of the signed documents
# ---------------------------------------------------------------------------


@dataclass
class VerifiedDocuments:
    manifest: ManifestInfo
    metadata: SciMetadata
    manifest_sha256: str
    sci_bytes: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    signature: bytes = field(repr=False)

    @property
    def uuid(self) -> str:
        return self.manifest.uuid


def check_json_limit(sci_bytes: bytes, manifest_bytes: bytes) -> None:
    total = len(sci_bytes) + len(manifest_bytes)
    if total > MAX_JSON_BYTES:
        raise PublicationError(
            f"sci.json and manifest.json total {total} bytes; the limit is {MAX_JSON_BYTES}."
            " Shorten metadata or citations and sign again",
            None,
            "too_large",
        )


def verify_documents(sci_bytes: bytes, manifest_bytes: bytes, signature: bytes) -> VerifiedDocuments:
    """Run the shared verification sequence on the two JSON documents.

    1. verify the manifest signature, 2. require the approved LICENSE entry,
    3. verify the exact sci.json bytes, then validate its fields. Attached
    files are verified separately with ``verify_file_bytes``/``hash_stream``.
    """
    check_json_limit(sci_bytes, manifest_bytes)
    if len(signature) != keys.SIGNATURE_BYTES:
        raise PublicationError(
            f"must be exactly {keys.SIGNATURE_BYTES} raw bytes (got {len(signature)}); do not base64-encode it or add a newline",
            SIGNATURE,
            "invalid_signature",
        )
    manifest_doc = parse_strict_json(manifest_bytes, MANIFEST)
    try:
        pub = keys.public_key_from_jwk(manifest_doc.get("public_key"))
    except keys.KeyError_ as exc:
        raise PublicationError(str(exc), "manifest.public_key") from exc
    if not keys.verify_signature(pub, signature, manifest_bytes):
        raise PublicationError(
            "the signature does not match manifest.json and its public key", SIGNATURE, "invalid_signature"
        )
    info = validate_manifest(manifest_doc)
    check_license_entry(info)
    entry = info.files[SCI_JSON]
    if len(sci_bytes) != entry.bytes or sha256_hex(sci_bytes) != entry.sha256:
        raise PublicationError(
            "sci.json differs from the signed manifest; edits after signing require signing again",
            SCI_JSON,
            "file_mismatch",
        )
    metadata = validate_sci(parse_strict_json(sci_bytes, SCI_JSON))
    return VerifiedDocuments(info, metadata, sha256_hex(manifest_bytes), bytes(sci_bytes), bytes(manifest_bytes), bytes(signature))


def check_file(name: str, entry: FileEntry, sha256: str, size: int) -> None:
    if size != entry.bytes or sha256 != entry.sha256:
        raise PublicationError(
            f"differs from the signed manifest (expected {entry.bytes} bytes with sha256 {entry.sha256},"
            f" found {size} bytes with sha256 {sha256}); edits after signing require signing again",
            name,
            "file_mismatch",
        )


def hash_stream(stream: BinaryIO, limit: int, chunk: int = 1 << 20) -> tuple[str, int]:
    """Hash a stream, reading at most ``limit + 1`` bytes."""
    h = hashlib.sha256()
    total = 0
    while True:
        want = min(chunk, limit + 1 - total)
        if want <= 0:
            break
        data = stream.read(want)
        if not data:
            break
        h.update(data)
        total += len(data)
    return h.hexdigest(), total


def check_readme(data: bytes) -> None:
    if len(data) > MAX_README_BYTES:
        raise PublicationError(f"is {len(data)} bytes; the limit is {MAX_README_BYTES}", README, "too_large")
    if not data:
        raise PublicationError("must not be empty", README)
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicationError(f"is not valid UTF-8 (byte offset {exc.start})", README) from exc


def verify_content(name: str, data: bytes) -> None:
    """Content checks for files whose bytes are already in memory."""
    if name == README:
        check_readme(data)
    elif name == HEADLINE_FIGURE:
        check_png(data)
    elif name == LICENSE:
        if data != license_bytes():
            raise PublicationError("is not the approved SCI publication license", LICENSE, "unapproved_license")


# ---------------------------------------------------------------------------
# PNG validation
# ---------------------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


@dataclass
class PngInfo:
    width: int
    height: int


def check_png(data: bytes, field: str = HEADLINE_FIGURE) -> PngInfo:
    """Validate a static PNG and its size limits.

    Reads the header and checks dimensions before decompressing anything;
    then streams the image data through zlib to confirm it decodes to exactly
    the expected scanlines. Animated PNGs (acTL/fcTL/fdAT) are rejected.
    """
    def bad(msg: str):
        return PublicationError(msg + ". Re-export the figure as a static PNG and sign again", field, "invalid_png")

    if len(data) > MAX_FIGURE_BYTES:
        raise PublicationError(
            f"is {len(data)} bytes; the limit is {MAX_FIGURE_BYTES}. Resize or re-export it and sign again",
            field,
            "too_large",
        )
    if not data.startswith(_PNG_SIG):
        raise bad("is not a PNG file")
    pos = len(_PNG_SIG)
    n = len(data)
    chunks: list[tuple[bytes, int, int]] = []
    while pos < n:
        if pos + 12 > n:
            raise bad("has a truncated chunk")
        length, ctype = struct.unpack(">I4s", data[pos : pos + 8])
        if length > 0x7FFFFFFF or pos + 12 + length > n:
            raise bad("has a chunk that extends past the end of the file")
        if not re.match(rb"^[A-Za-z]{4}$", ctype):
            raise bad("has an invalid chunk type")
        body_start = pos + 8
        crc = struct.unpack(">I", data[body_start + length : body_start + length + 4])[0]
        if zlib.crc32(data[pos + 4 : body_start + length]) & 0xFFFFFFFF != crc:
            raise bad(f"has a corrupt {ctype.decode()} chunk (CRC mismatch)")
        chunks.append((ctype, body_start, length))
        pos = body_start + length + 4
        if ctype == b"IEND":
            break
    if pos != n:
        raise bad("has data after the IEND chunk")
    if not chunks or chunks[0][0] != b"IHDR" or chunks[0][2] != 13:
        raise bad("does not start with a valid IHDR chunk")
    if chunks[-1][0] != b"IEND" or chunks[-1][2] != 0:
        raise bad("does not end with an IEND chunk")
    _, s, _ = chunks[0]
    width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", data[s : s + 13])
    if not (1 <= width <= MAX_FIGURE_SIDE and 1 <= height <= MAX_FIGURE_SIDE) or width * height > MAX_FIGURE_PIXELS:
        raise PublicationError(
            f"is {width}x{height} pixels; each side must be 1-{MAX_FIGURE_SIDE} pixels and the total at most"
            f" {MAX_FIGURE_PIXELS} pixels. Resize it and sign again",
            field,
            "invalid_png",
        )
    if color not in _PNG_DEPTHS or depth not in _PNG_DEPTHS[color]:
        raise bad("has an invalid bit depth and color type combination")
    if comp != 0 or filt != 0 or interlace not in (0, 1):
        raise bad("uses an unsupported compression, filter, or interlace method")
    types = [c[0] for c in chunks]
    for anim in (b"acTL", b"fcTL", b"fdAT"):
        if anim in types:
            raise bad("is animated; only static PNG images are allowed")
    for t in types:
        if t[0:1].isupper() and t not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
            raise bad(f"has an unknown critical chunk {t.decode()}")
    if types.count(b"IHDR") != 1 or types.count(b"IEND") != 1:
        raise bad("has repeated IHDR or IEND chunks")
    if color == 3 and b"PLTE" not in types:
        raise bad("is a palette image without a PLTE chunk")
    if color in (0, 4) and b"PLTE" in types:
        raise bad("is a grayscale image with a PLTE chunk")
    idat = [i for i, t in enumerate(types) if t == b"IDAT"]
    if not idat:
        raise bad("has no image data")
    if idat != list(range(idat[0], idat[-1] + 1)):
        raise bad("has non-consecutive IDAT chunks")

    bpp_bits = depth * _PNG_CHANNELS[color]
    if interlace == 0:
        passes = [(width, height)]
    else:
        passes = []
        for x0, y0, dx, dy in _ADAM7:
            pw = (width - x0 + dx - 1) // dx if width > x0 else 0
            ph = (height - y0 + dy - 1) // dy if height > y0 else 0
            if pw and ph:
                passes.append((pw, ph))
    rows = []
    for pw, ph in passes:
        rows.append((ph, 1 + (pw * bpp_bits + 7) // 8))
    expected = sum(ph * rb for ph, rb in rows)

    d = zlib.decompressobj()
    produced = 0
    # Track the position of each filter-type byte.
    row_iter = iter([rb for ph, rb in rows for _ in range(ph)])
    next_filter_at = 0
    current_row = next(row_iter, None)

    def consume(out: bytes):
        nonlocal produced, next_filter_at, current_row
        start = produced
        produced += len(out)
        if produced > expected:
            raise bad("decodes to more data than its dimensions allow")
        while current_row is not None and next_filter_at < produced:
            if out[next_filter_at - start] > 4:
                raise bad("has an invalid scanline filter type")
            next_filter_at += current_row
            current_row = next(row_iter, None)

    try:
        for i in idat:
            _, s, length = chunks[i]
            buf = data[s : s + length]
            while buf:
                out = d.decompress(buf, 1 << 16)
                consume(out)
                buf = d.unconsumed_tail
                if d.eof:
                    break
            if d.eof:
                break
        while not d.eof:
            out = d.decompress(b"", 1 << 16)
            if not out:
                break
            consume(out)
    except zlib.error as exc:
        raise bad(f"has corrupt compressed image data ({exc})") from exc
    if not d.eof or produced != expected:
        raise bad("has incomplete image data")
    return PngInfo(width, height)


# ---------------------------------------------------------------------------
# Packaging and signing a folder
# ---------------------------------------------------------------------------

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _iter_source_entries(root: Path) -> Iterator[tuple[str, Path, bool]]:
    """Yield (relative posix path, absolute path, is_dir) in sorted order.

    Rejects symlinks, special files, and names that are unsafe in a preview.
    """
    if root.is_symlink():
        raise PublicationError("must not be a symbolic link", FILES_DIR)
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            raise PublicationError(f"cannot read {directory}: {exc.strerror}", FILES_DIR) from exc
        subdirs = []
        for name in names:
            p = directory / name
            rel = prefix + name
            try:
                name.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise PublicationError(f"file name {rel!r} is not valid UTF-8", FILES_DIR) from exc
            check_relative_path(rel, f"files/{rel}", allow_dir_slash=False)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                raise PublicationError(f"{rel} is a symbolic link; symlinks are not packaged. Replace it with a regular file", FILES_DIR)
            if stat.S_ISDIR(st.st_mode):
                subdirs.append((p, rel + "/"))
                yield rel + "/", p, True
            elif stat.S_ISREG(st.st_mode):
                yield rel, p, False
            else:
                raise PublicationError(f"{rel} is not a regular file or folder", FILES_DIR)
        for item in reversed(subdirs):
            stack.append(item)


def build_zip(source: Path, dest: Path) -> dict:
    """Package ``files/`` into a deterministic ZIP; return its preview."""
    collector = PreviewCollector()
    entries = sorted(_iter_source_entries(source), key=lambda e: e[0])
    fd, tmp = tempfile.mkstemp(prefix=".files.zip.", dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for rel, path, is_dir in entries:
                info = zipfile.ZipInfo(rel, date_time=_ZIP_EPOCH)
                info.create_system = 3
                if is_dir:
                    info.external_attr = (0o40755 << 16) | 0x10
                    zf.writestr(info, b"")
                else:
                    info.external_attr = 0o100644 << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    size = os.path.getsize(path)
                    with open(path, "rb") as src, zf.open(info, "w", force_zip64=size > 0x7FFF0000) as out:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            out.write(chunk)
                collector.add_entry(rel.rstrip("/"), is_dir)
        os.chmod(tmp, _default_mode())
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return collector.tree()


def preview_from_zip(path: Path) -> dict:
    """Preview a prepared ZIP from its entry names only (no extraction).

    Only the CLI uses an archive reader, and only for names; the server never
    opens ZIPs.
    """
    collector = PreviewCollector()
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                is_dir = name.endswith("/")
                rel = name.rstrip("/") if is_dir else name
                check_relative_path(rel, f"files.zip entry {name!r}", allow_dir_slash=False)
                collector.add_entry(rel, is_dir)
    except zipfile.BadZipFile as exc:
        raise PublicationError(f"is not a readable ZIP archive ({exc})", FILES_ZIP) from exc
    return collector.tree()


def _hash_file(path: Path) -> FileEntry:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return FileEntry(h.hexdigest(), size)


def _regular_file(folder: Path, name: str) -> Path | None:
    p = folder / name
    if p.is_symlink():
        raise PublicationError("must be a regular file, not a symbolic link", name)
    if not p.exists():
        return None
    if not p.is_file():
        raise PublicationError("must be a regular file", name)
    return p


def serialize_manifest(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass
class SignResult:
    uuid: str
    fingerprint: str
    manifest_sha256: str
    files: dict[str, FileEntry]
    files_tree: dict | None


def ensure_license(folder: Path) -> Path:
    """Write the bundled LICENSE, reuse identical bytes, refuse a different file."""
    path = folder / LICENSE
    if path.is_symlink():
        raise PublicationError("must be a regular file, not a symbolic link", LICENSE)
    if path.exists():
        if not path.is_file() or path.read_bytes() != license_bytes():
            raise PublicationError(
                "already exists with different contents. SCI publications use one fixed license"
                " (CC BY 4.0 for text, figures, and data; MIT for code). Move the custom file away;"
                " material that cannot use these terms must be referenced externally",
                LICENSE,
                "unapproved_license",
            )
        return path
    path.write_bytes(license_bytes())
    return path


def sign_folder(
    folder: Path,
    private_key,
    *,
    created_at: str | None = None,
    publication_uuid: str | None = None,
) -> SignResult:
    folder = Path(folder)
    if not folder.is_dir():
        raise PublicationError(f"{folder} is not a folder", None, "not_found")
    sci_path = _regular_file(folder, SCI_JSON)
    if sci_path is None:
        raise PublicationError("is required; create it with a title and abstract", SCI_JSON, "not_found")
    readme_path = _regular_file(folder, README)
    if readme_path is None:
        raise PublicationError("is required; it holds the main publication text", README, "not_found")
    figure_path = _regular_file(folder, HEADLINE_FIGURE)
    for path, name, limit in ((sci_path, SCI_JSON, MAX_JSON_BYTES), (readme_path, README, MAX_README_BYTES),
                              (figure_path, HEADLINE_FIGURE, MAX_FIGURE_BYTES)):
        if path is not None and path.stat().st_size > limit:
            raise PublicationError(f"is {path.stat().st_size} bytes; the limit is {limit}", name, "too_large")
    sci_bytes = sci_path.read_bytes()
    validate_sci(parse_strict_json(sci_bytes, SCI_JSON))
    check_readme(readme_path.read_bytes())
    if figure_path is not None:
        check_png(figure_path.read_bytes())
    license_path = folder / LICENSE
    if license_path.is_symlink() or (license_path.exists() and (not license_path.is_file() or license_path.read_bytes() != license_bytes())):
        ensure_license(folder)  # raises the actionable error without changing anything

    files_dir = folder / FILES_DIR
    zip_path = folder / FILES_ZIP
    tree: dict | None = None
    new_zip: Path | None = None  # built beside the folder's files, moved in last
    if files_dir.is_symlink():
        raise PublicationError("must not be a symbolic link", FILES_DIR)
    try:
        if files_dir.exists():
            if not files_dir.is_dir():
                raise PublicationError("must be a folder", FILES_DIR)
            if zip_path.is_symlink():
                raise PublicationError("must be a regular file, not a symbolic link", FILES_ZIP)
            fd, tmp = tempfile.mkstemp(prefix=".files.zip.", dir=folder)
            os.close(fd)
            new_zip = Path(tmp)
            tree = build_zip(files_dir, new_zip)
        elif _regular_file(folder, FILES_ZIP) is not None:
            tree = preview_from_zip(zip_path)

        files: dict[str, FileEntry] = {}
        for name in DECLARED_FILES:
            if name == LICENSE:
                files[name] = FileEntry(**license_entry())
                continue
            p = new_zip if (name == FILES_ZIP and new_zip is not None) else _regular_file(folder, name)
            if p is not None:
                files[name] = _hash_file(p)
        return _finish_signing(folder, private_key, sci_bytes, files, tree, new_zip, created_at, publication_uuid)
    finally:
        if new_zip is not None:
            new_zip.unlink(missing_ok=True)


def _finish_signing(folder, private_key, sci_bytes, files, tree, new_zip, created_at, publication_uuid) -> SignResult:
    total = sum(files[n].bytes for n in (README, FILES_ZIP, HEADLINE_FIGURE) if n in files)
    if total > MAX_ATTACHMENT_TOTAL:
        raise PublicationError(
            f"README + files.zip + headline figure total {total} bytes; the limit is {MAX_ATTACHMENT_TOTAL}."
            " Reference large datasets externally (for example by DOI)",
            None,
            "too_large",
        )

    pub = private_key.public_key()
    uid = publication_uuid or new_uuid4()
    created = created_at or utc_now_timestamp()

    def manifest_for(t: dict | None) -> bytes:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "uuid": uid,
            "public_key": keys.jwk_from_public_key(pub),
            "created_at": created,
        }
        if t is not None:
            doc["files_tree"] = t
        doc["files"] = {n: {"sha256": e.sha256, "bytes": e.bytes} for n, e in files.items()}
        return serialize_manifest(doc)

    manifest_bytes = manifest_for(tree)
    while tree is not None and len(sci_bytes) + len(manifest_bytes) > MAX_JSON_BYTES and tree["paths"]:
        tree = {"paths": tree["paths"][:-1], "truncated": True}
        manifest_bytes = manifest_for(tree)
    if len(sci_bytes) + len(manifest_bytes) > MAX_JSON_BYTES:
        raise PublicationError(
            f"sci.json and the generated manifest.json total {len(sci_bytes) + len(manifest_bytes)} bytes even"
            f" without a file preview; the limit is {MAX_JSON_BYTES}. Shorten the abstract, keywords, or citations",
            SCI_JSON,
            "too_large",
        )
    signature = keys.sign(private_key, manifest_bytes)
    # Verify what we are about to write, using the same code as every verifier.
    verify_documents(sci_bytes, manifest_bytes, signature)
    # Every check passed: now change the folder.
    ensure_license(folder)
    if new_zip is not None:
        os.replace(new_zip, folder / FILES_ZIP)
    _atomic_write(folder / MANIFEST, manifest_bytes)
    _atomic_write(folder / SIGNATURE, signature)
    return SignResult(uid, keys.fingerprint(pub), sha256_hex(manifest_bytes), files, tree)


def _default_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, _default_mode())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_signed_documents(folder: Path) -> tuple[bytes, bytes, bytes]:
    folder = Path(folder)
    out = []
    for name in (SCI_JSON, MANIFEST, SIGNATURE):
        p = _regular_file(folder, name)
        if p is None:
            hint = " Run 'sci sign' first." if name != SCI_JSON else ""
            raise PublicationError(f"is missing.{hint}", name, "not_found")
        out.append(p.read_bytes())
    return out[0], out[1], out[2]


def verify_folder(folder: Path, trusted_public_key=None) -> VerifiedDocuments:
    """Verify a signed publication folder: signature, schemas, license, and
    every declared file's exact hash and size."""
    folder = Path(folder)
    sci_bytes, manifest_bytes, signature = read_signed_documents(folder)
    docs = verify_documents(sci_bytes, manifest_bytes, signature)
    if trusted_public_key is not None:
        if keys.raw_public_key(trusted_public_key) != keys.raw_public_key(docs.manifest.public_key):
            raise PublicationError(
                f"was signed by {docs.manifest.fingerprint}, not by the trusted key {keys.fingerprint(trusted_public_key)}",
                SIGNATURE,
                "untrusted_key",
            )
    for name, entry in docs.manifest.files.items():
        if name == SCI_JSON:
            continue
        p = _regular_file(folder, name)
        if p is None:
            raise PublicationError("is declared in the manifest but missing", name, "file_missing")
        if name in (README, HEADLINE_FIGURE, LICENSE):
            size = p.stat().st_size
            if size != entry.bytes:
                raise PublicationError(
                    f"is {size} bytes but the signed manifest declares {entry.bytes}; edits after signing require signing again",
                    name,
                    "file_mismatch",
                )
            data = p.read_bytes()
            check_file(name, entry, sha256_hex(data), len(data))
            verify_content(name, data)
        else:
            got = _hash_file(p)
            check_file(name, entry, got.sha256, got.bytes)
    return docs


def undeclared_files(folder: Path, docs: VerifiedDocuments) -> list[str]:
    """Publication files present in the folder but absent from the manifest."""
    return [n for n in OPTIONAL_FILES if n not in docs.manifest.files and (Path(folder) / n).exists()]


# ---------------------------------------------------------------------------
# Instance acceptance records and URLs
# ---------------------------------------------------------------------------

_ACCEPTANCE_FIELDS = {"record_version", "instance_url", "uuid", "manifest_sha256", "accepted_at"}


def serialize_acceptance(instance_url: str, publication_uuid: str, manifest_sha256: str, accepted_at: str) -> bytes:
    doc = {
        "record_version": 1,
        "instance_url": instance_url,
        "uuid": publication_uuid,
        "manifest_sha256": manifest_sha256,
        "accepted_at": accepted_at,
    }
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass
class AcceptanceRecord:
    instance_url: str
    uuid: str
    manifest_sha256: str
    accepted_at: str


def parse_acceptance(data: bytes) -> AcceptanceRecord:
    doc = parse_strict_json(data, ACCEPTANCE)
    _check_fields(doc, _ACCEPTANCE_FIELDS, _ACCEPTANCE_FIELDS, ACCEPTANCE)
    if _check_int(doc["record_version"], "acceptance.record_version") != 1:
        raise PublicationError("unsupported record version", "acceptance.record_version")
    if not isinstance(doc["instance_url"], str):
        raise PublicationError("must be a string", "acceptance.instance_url")
    check_code_points(doc["instance_url"], "acceptance.instance_url")
    check_uuid4(doc["uuid"], "acceptance.uuid")
    if not isinstance(doc["manifest_sha256"], str) or not _HASH_RE.fullmatch(doc["manifest_sha256"]):
        raise PublicationError("must be 64 lowercase hexadecimal characters", "acceptance.manifest_sha256")
    parse_timestamp(doc["accepted_at"], "acceptance.accepted_at")
    return AcceptanceRecord(doc["instance_url"], doc["uuid"], doc["manifest_sha256"], doc["accepted_at"])


def join_url(base: str, *parts: str) -> str:
    """Treat ``base`` as a directory (with or without a trailing slash)."""
    return base.rstrip("/") + "/" + "/".join(parts)


def object_url(archive_url: str, publication_uuid: str, name: str) -> str:
    check_uuid4(publication_uuid)
    if name not in PUBLICATION_OBJECTS and name != ACCEPTANCE:
        raise PublicationError(f"{name!r} is not a publication object name")
    return join_url(archive_url, publication_uuid, name)
