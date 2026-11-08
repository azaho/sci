"""Strict JSON decoding under protocol §7.

Rules enforced on top of RFC 8259:

* UTF-8 without a byte-order mark (ill-formed UTF-8 is rejected, never replaced);
* no duplicate object keys after escape decoding, at any depth;
* no unpaired UTF-16 surrogate escapes;
* no ``NaN``/``Infinity`` or other non-RFC 8259 tokens.

Numbers keep their original spelling so that schema checks can insist on, for
example, digits-only byte counts (``-0``, ``1.0`` and ``1e3`` are rejected
where an integer is required).
"""

from __future__ import annotations

import json
from typing import Final, TypeAlias

from .errors import Incomplete, Invalid, Issue, Outcome

__all__ = [
    "MAX_SAFE_INTEGER",
    "JsonFloat",
    "JsonInt",
    "JsonObject",
    "JsonValue",
    "is_json_integer",
    "json_pointer",
    "json_type_name",
    "parse_json",
    "parse_json_object",
]

MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
"""2^53 - 1, the largest byte count permitted by §3."""

_BOM: Final = b"\xef\xbb\xbf"


class JsonInt(int):
    """An integer literal; ``lexeme`` is its exact source spelling."""

    lexeme: str

    def __new__(cls, lexeme: str) -> JsonInt:
        obj = super().__new__(cls, lexeme)
        obj.lexeme = lexeme
        return obj


class JsonFloat(float):
    """A number literal with a fraction or exponent; ``lexeme`` is its spelling."""

    lexeme: str

    def __new__(cls, lexeme: str) -> JsonFloat:
        obj = super().__new__(cls, lexeme)
        obj.lexeme = lexeme
        return obj


JsonValue: TypeAlias = (
    "bool | JsonInt | JsonFloat | str | list[JsonValue] | dict[str, JsonValue] | None"
)
JsonObject: TypeAlias = "dict[str, JsonValue]"


class _DuplicateKeyError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key


class _ConstantError(Exception):
    def __init__(self, token: str) -> None:
        self.token = token


def _pairs_hook(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(token: str) -> JsonValue:
    raise _ConstantError(token)


def _has_surrogate(text: str) -> bool:
    # Code points U+D800..U+DFFF only arise from unpaired escapes because the
    # UTF-8 decoder rejects encoded surrogates.
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def _find_surrogate(value: JsonValue, pointer: str) -> str | None:
    stack: list[tuple[JsonValue, str]] = [(value, pointer)]
    while stack:
        item, where = stack.pop()
        if isinstance(item, str):
            if _has_surrogate(item):
                return where
        elif isinstance(item, dict):
            for key, child in item.items():
                child_where = where + "/" + _escape_pointer(key)
                if _has_surrogate(key):
                    return child_where
                stack.append((child, child_where))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                stack.append((child, f"{where}/{index}"))
    return None


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(*tokens: str | int) -> str:
    """Build an RFC 6901 JSON pointer from path tokens."""
    return "".join("/" + _escape_pointer(str(token)) for token in tokens)


def parse_json(data: bytes, *, file: str | None = None, section: str = "7") -> JsonValue:
    """Decode ``data`` under the §7 rules.

    Raises:
        Invalid: the bytes are not strict JSON.
        Incomplete: nesting exceeded the interpreter's recursion limit (a local
            resource limit, not proof of invalidity).
    """

    def fail(code: str, requirement: str, observed: str | None = None) -> Invalid:
        return Invalid(Issue(code, requirement, section=section, file=file, observed=observed))

    if data.startswith(_BOM):
        raise fail("json_bom", "JSON must be UTF-8 without a byte-order mark")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise fail(
            "json_utf8", "JSON must be well-formed UTF-8", f"byte offset {exc.start}"
        ) from exc
    try:
        value: JsonValue = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_int=JsonInt,
            parse_float=JsonFloat,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyError as exc:
        raise fail(
            "json_duplicate_key",
            "object keys must be unique after escape decoding",
            repr(exc.key),
        ) from None
    except _ConstantError as exc:
        raise fail("json_syntax", "RFC 8259 does not permit this token", exc.token) from None
    except json.JSONDecodeError as exc:
        raise fail(
            "json_syntax", "document must be RFC 8259 JSON", f"{exc.msg} at char {exc.pos}"
        ) from None
    except RecursionError:
        raise Incomplete(
            Issue(
                "json_depth_limit",
                "JSON nesting exceeds the local parser limit",
                section="6",
                file=file,
                outcome=Outcome.INCOMPLETE,
            )
        ) from None
    where = _find_surrogate(value, "")
    if where is not None:
        raise Invalid(
            Issue(
                "json_unpaired_surrogate",
                "unpaired UTF-16 surrogate escapes are prohibited",
                section=section,
                file=file,
                field=where or None,
            )
        )
    return value


def parse_json_object(data: bytes, *, file: str | None = None) -> JsonObject:
    """Decode strict JSON and require a top-level object."""
    value = parse_json(data, file=file)
    if not isinstance(value, dict):
        raise Invalid(
            Issue(
                "json_not_object",
                "document must be a JSON object",
                section="7",
                file=file,
                observed=json_type_name(value),
            )
        )
    return value


def is_json_integer(value: JsonValue) -> bool:
    """True for an integer literal (not a boolean, fraction, or exponent)."""
    return type(value) is JsonInt


def json_type_name(value: object) -> str:
    """Return the JSON type name of a decoded value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, JsonInt | JsonFloat | int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
