"""Version 1 schemas and semantic validation, shared by every verifier."""

import bisect
import datetime as dt
import hashlib
import io
import json
import re
import unicodedata
from importlib.resources import files
from urllib.parse import unquote_to_bytes

from PIL import Image, UnidentifiedImageError

from sci.errors import SCIError, invalid
from sci.keys import jwk_key

JSON_LIMIT = 65_536
README_LIMIT = 1_000_000
FIGURE_LIMIT = 2_000_000
PAYLOAD_LIMIT = 50_000_000
PREVIEW_LIMIT = 8_192
PREVIEW_COUNT = 200
REQUIRED_FILES = {"sci.json", "README.md", "LICENSE"}
ATTACHMENTS = {"README.md", "files.zip", "headline_figure.png"}
ALLOWED_FILES = REQUIRED_FILES | ATTACHMENTS
RELATIONS = {"uses", "supports", "reproduces", "challenges", "supersedes"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
WHITESPACE = frozenset(
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
LICENSE = files("sci").joinpath("publication_LICENSE.txt").read_bytes()
LICENSE_DESCRIPTOR = {"sha256": hashlib.sha256(LICENSE).hexdigest(), "bytes": len(LICENSE)}
CATEGORY_CATALOG = json.loads(files("sci").joinpath("categories.json").read_text("utf-8"))
CATEGORIES = {c["code"] for c in CATEGORY_CATALOG["categories"]}
# Frozen Unicode Graphic ranges make SQL and Python agree across runtime Unicode versions.
GRAPHIC_RANGES = json.loads(files("sci").joinpath("doi_graphic_ranges.json").read_text("utf-8"))["ranges"]
GRAPHIC_STARTS = [r[0] for r in GRAPHIC_RANGES]


def compact(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_uuid(value: str, field: str = "uuid") -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise invalid(field, "expected a canonical lowercase UUIDv4")
    return value


def validate_timestamp(value: str, field: str = "created_at") -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value, re.ASCII
    ):
        raise invalid(field, "expected UTC YYYY-MM-DDTHH:MM:SS[.ffffff]Z")
    try:
        dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise invalid(field, "expected a real Gregorian date and time, without leap seconds") from exc
    return value


def validate_chars(value: str, field: str, multiline: bool = False) -> None:
    for char in value:
        n = ord(char)
        if (0xD800 <= n <= 0xDFFF or 0xFDD0 <= n <= 0xFDEF or n & 0xFFFF >= 0xFFFE
                or (unicodedata.category(char) == "Cc" and not (multiline and char in "\t\n\r"))
                or (not multiline and char in "\u2028\u2029")):
            raise invalid(field, f"forbidden character U+{n:04X}")


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise invalid(key, "duplicate JSON key")
        obj[key] = value
    return obj


def _walk(value, path):
    if value is None:
        raise invalid(path, "JSON null is not allowed; omit absent fields")
    if isinstance(value, str):
        multiline = path == "sci.json.abstract" or bool(
            re.fullmatch(r"sci\.json\.citations\[\d+\]\.reason", path)
        )
        validate_chars(value, path, multiline)
    elif isinstance(value, dict):
        for key, child in value.items():
            validate_chars(key, path + " key")
            _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _walk(child, f"{path}[{i}]")
    elif type(value) not in (int, bool):
        raise invalid(path, "only integer numbers without a decimal point or exponent are allowed")


def strict_json(data: bytes, field: str) -> dict:
    if data.startswith(b"\xef\xbb\xbf"):
        raise invalid(field, "UTF-8 byte-order marks are not allowed")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                         parse_constant=lambda v: (_ for _ in ()).throw(
                             invalid(field, f"nonstandard JSON number {v}")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise invalid(field, f"expected strict UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise invalid(field, "expected a JSON object")
    try:
        _walk(obj, field)
    except RecursionError as exc:
        raise invalid(field, "excessive nesting") from exc
    return obj


def fields(obj, required, optional, field):
    if not isinstance(obj, dict):
        raise invalid(field, "expected an object")
    missing, extra = required - obj.keys(), obj.keys() - required - optional
    if missing:
        raise invalid(field, "missing fields: " + ", ".join(sorted(missing)))
    if extra:
        raise invalid(field, "unknown fields: " + ", ".join(sorted(extra)))


def text(value, field, maximum, *, multiline=False):
    if not isinstance(value, str):
        raise invalid(field, "expected a string")
    validate_chars(value, field, multiline)
    if not value or all(c in WHITESPACE for c in value):
        raise invalid(field, "must contain a non-whitespace character; omit unused optional fields")
    if len(value) > maximum:
        raise invalid(field, f"maximum {maximum} Unicode code points; shorten the author input")


def is_graphic(char):
    n = ord(char)
    i = bisect.bisect_right(GRAPHIC_STARTS, n) - 1
    return i >= 0 and n <= GRAPHIC_RANGES[i][1]


def normalize_citation_target(target: str) -> str:
    """DOI URI Scheme (2024-11-13), §§2–3; ASCII case folding only.

    The suffix is one URI segment: suffix slashes must be percent-encoded.
    Keys decode UTF-8 once, preserve Unicode, and lowercase only Basic Latin.
    """
    if not isinstance(target, str) or not target.isascii() or len(target) > 512:
        raise invalid("citation.target", "maximum 512 ASCII characters, including scheme and escapes")
    if target.startswith("sci:"):
        validate_uuid(target[4:], "citation.target")
        return target
    if target[:4].lower() != "doi:":
        raise invalid("citation.target", "use sci:<UUIDv4> or doi:<DOI>; resolver URLs are not targets")
    body = target[4:]
    if body.count("/") != 1:
        raise invalid("citation.target", "DOI URI needs one prefix/suffix slash; encode suffix slashes as %2F")
    segment = r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+"
    if not re.fullmatch(segment + "/" + segment, body):
        raise invalid("citation.target", "invalid DOI URI encoding; no query, fragment, or bare percent")
    try:
        prefix, suffix = (
            unquote_to_bytes(part).decode("utf-8") for part in body.split("/")
        )
    except UnicodeDecodeError as exc:
        raise invalid("citation.target", "percent escapes must encode valid UTF-8") from exc
    if not re.fullmatch(r"10\.[0-9]+(?:\.[0-9]+)*", prefix):
        raise invalid("citation.target", "expected a DOI prefix beginning 10. followed by digits")
    if not suffix or not all(is_graphic(c) for c in suffix):
        raise invalid("citation.target", "DOI names require Unicode Graphic characters")
    key = "doi:" + (prefix + "/" + suffix).translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
    ))
    validate_chars(key, "citation.target_key")
    if len(key.encode("utf-8")) > 512:
        raise invalid("citation.target_key", "maximum 512 UTF-8 bytes")
    return key


def validate_metadata(obj: dict) -> None:
    fields(obj, {"title", "abstract"},
           {"categories", "keywords", "headline_figure_caption", "citations"}, "sci.json")
    text(obj["title"], "title", 200)
    text(obj["abstract"], "abstract", 2500, multiline=True)
    categories = obj.get("categories", [])
    if not isinstance(categories, list) or len(categories) > 3:
        raise invalid("categories", "expected an ordered list of at most three distinct codes")
    if any(not isinstance(c, str) or c not in CATEGORIES for c in categories):
        raise invalid("categories", "unknown code; consult or update the bundled sci/categories.json")
    if len(set(categories)) != len(categories):
        raise invalid("categories", "duplicate category codes")
    keywords = obj.get("keywords", [])
    if not isinstance(keywords, list) or len(keywords) > 20:
        raise invalid("keywords", "expected at most 20 strings")
    for i, keyword in enumerate(keywords):
        text(keyword, f"keywords[{i}]", 64)
    if "headline_figure_caption" in obj:
        text(obj["headline_figure_caption"], "headline_figure_caption", 400)
    citations = obj.get("citations", [])
    if not isinstance(citations, list):
        raise invalid("citations", "expected an array")
    for i, citation in enumerate(citations):
        field = f"citations[{i}]"
        fields(citation, {"target"}, {"relation", "reason"}, field)
        try:
            normalize_citation_target(citation["target"])
        except SCIError as exc:
            raise invalid(field + ".target", str(exc)) from exc
        if "relation" in citation and (
            not isinstance(citation["relation"], str) or citation["relation"] not in RELATIONS
        ):
            raise invalid(field + ".relation", "expected uses, supports, reproduces, challenges, or supersedes")
        if "reason" in citation:
            text(citation["reason"], field + ".reason", 1000, multiline=True)


def safe_path(path: str) -> list[str]:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise invalid("files_tree.paths", "expected safe relative paths with / separators")
    validate_chars(path, "files_tree.paths")
    parts = path.rstrip("/").split("/")
    if path.endswith("//") or any(p in {"", ".", ".."} for p in parts) or re.match(r"^[A-Za-z]:", path):
        raise invalid("files_tree.paths", "absolute, empty, dot, and parent path components are forbidden")
    return parts


def path_order(path: str):
    depth = len(path.rstrip("/").split("/"))
    return ((0 if path.endswith("/") else 1) + 2 * (depth - 1), path)


def validate_preview(preview):
    fields(preview, {"paths", "truncated"}, set(), "files_tree")
    if type(preview["truncated"]) is not bool or not isinstance(preview["paths"], list):
        raise invalid("files_tree", "requires string paths and a Boolean truncated")
    paths = preview["paths"]
    if len(paths) > PREVIEW_COUNT:
        raise invalid("files_tree.paths", "maximum 200 paths")
    for path in paths:
        if len(safe_path(path)) > 2:
            raise invalid("files_tree.paths", "maximum two levels")
    if paths != sorted(set(paths), key=path_order):
        raise invalid("files_tree.paths", "paths must be unique and sorted in folder/file group order")
    if len(compact(preview)) > PREVIEW_LIMIT:
        raise invalid("files_tree", "maximum 8,192 compact UTF-8 JSON bytes")


def validate_manifest(obj):
    fields(obj, {"schema_version", "uuid", "public_key", "created_at", "files"},
           {"files_tree"}, "manifest.json")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise invalid("schema_version", "expected integer 1")
    validate_uuid(obj["uuid"])
    jwk_key(obj["public_key"])
    validate_timestamp(obj["created_at"])
    entries = obj["files"]
    fields(entries, REQUIRED_FILES, ALLOWED_FILES - REQUIRED_FILES, "files")
    for name, entry in entries.items():
        fields(entry, {"sha256", "bytes"}, set(), f"files.{name}")
        if not isinstance(entry["sha256"], str) or not HASH_RE.fullmatch(entry["sha256"]):
            raise invalid(f"files.{name}.sha256", "expected 64 lowercase hex characters")
        if type(entry["bytes"]) is not int or entry["bytes"] < 0:
            raise invalid(f"files.{name}.bytes", "expected a nonnegative JSON integer")
        maximum = {"sci.json": JSON_LIMIT, "README.md": README_LIMIT,
                   "headline_figure.png": FIGURE_LIMIT, "LICENSE": len(LICENSE)}.get(name, PAYLOAD_LIMIT)
        if entry["bytes"] > maximum:
            raise SCIError(f"files.{name}.bytes: maximum {maximum} bytes", "too_large", 413)
    if entries["LICENSE"] != LICENSE_DESCRIPTOR:
        raise invalid("files.LICENSE", "must match the bundled version 1 publication license")
    if sum(entries.get(n, {"bytes": 0})["bytes"] for n in ATTACHMENTS) > PAYLOAD_LIMIT:
        raise SCIError("README + ZIP + headline figure exceed 50,000,000 bytes", "too_large", 413)
    if "files_tree" in obj:
        if "files.zip" not in entries:
            raise invalid("files_tree", "requires files.zip")
        validate_preview(obj["files_tree"])


def validate_readme(data: bytes):
    if len(data) > README_LIMIT:
        raise SCIError("README.md exceeds 1,000,000 bytes", "too_large", 413)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise invalid("README.md", "must be UTF-8 Markdown") from exc


def validate_png(data: bytes):
    if len(data) > FIGURE_LIMIT:
        raise SCIError("headline_figure.png exceeds 2,000,000 bytes; resize or re-export", "too_large", 413)
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        if data[offset + 4:offset + 8] == b"acTL":
            raise invalid("headline_figure.png", "animation is not supported; export a static PNG")
        offset += length + 12
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or not (1 <= image.width <= 1600 and 1 <= image.height <= 1600):
                raise invalid("headline_figure.png", "requires PNG, dimensions 1–1,600; resize or re-export")
            if getattr(image, "is_animated", False):
                raise invalid("headline_figure.png", "animation is not supported; export a static PNG")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise invalid("headline_figure.png", f"invalid static PNG: {exc}") from exc
