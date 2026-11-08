"""RFC 6570 URI templates restricted to the expressions used by SCI (§8).

Only single-variable Level 1 (``{var}``) and Level 2 reserved (``{+var}``)
expressions are supported, which covers every expression the protocol
permits: ``{id}``, ``{id2}``, ``{x}``, ``{file}``, ``{sha256}``, ``{+path}``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = ["Expression", "UrlTemplate", "parse_template"]

_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_RESERVED: Final = frozenset(":/?#[]@!$&'()*+,;=")
_VARNAME: Final = re.compile(
    r"(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+(?:\.(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})+)*"
)
_FORBIDDEN_LITERAL: Final = frozenset(" \"'<>\\^`{|}")
_HEX: Final = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class Expression:
    """One template expression; ``operator`` is ``""`` or ``"+"``."""

    operator: str
    name: str

    def __str__(self) -> str:
        return "{" + self.operator + self.name + "}"


@dataclass(frozen=True, slots=True)
class UrlTemplate:
    """A parsed template: literal strings interleaved with expressions."""

    text: str
    parts: tuple[str | Expression, ...]

    @property
    def expressions(self) -> frozenset[Expression]:
        return frozenset(p for p in self.parts if isinstance(p, Expression))

    def expand(self, variables: Mapping[str, str]) -> str:
        """Expand with string values (RFC 6570 §3.2.2 simple, §3.2.3 reserved).

        Raises:
            KeyError: a variable is missing.
        """
        out: list[str] = []
        for part in self.parts:
            if isinstance(part, str):
                out.append(_encode(part, allow_reserved=True))
            else:
                out.append(_encode(variables[part.name], allow_reserved=part.operator == "+"))
        return "".join(out)


def _encode(value: str, *, allow_reserved: bool) -> str:
    allowed = _UNRESERVED | _RESERVED if allow_reserved else _UNRESERVED
    out: list[str] = []
    index = 0
    while index < len(value):
        ch = value[index]
        if (
            allow_reserved
            and ch == "%"
            and index + 2 < len(value)
            and value[index + 1] in _HEX
            and value[index + 2] in _HEX
        ):
            out.append(value[index : index + 3])
            index += 3
            continue
        if ch in allowed:
            out.append(ch)
        else:
            out.extend(f"%{byte:02X}" for byte in ch.encode("utf-8"))
        index += 1
    return "".join(out)


def parse_template(text: str) -> UrlTemplate:
    """Parse a template restricted to ``{var}`` and ``{+var}`` expressions.

    Raises:
        ValueError: malformed template, unsupported operator or modifier, or a
            literal character RFC 6570 does not permit.
    """
    parts: list[str | Expression] = []
    literal: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == "{":
            end = text.find("}", index + 1)
            if end < 0:
                raise ValueError("unclosed template expression")
            body = text[index + 1 : end]
            if "{" in body:
                raise ValueError("nested '{' in template expression")
            operator = ""
            if body[:1] == "+":
                operator, body = "+", body[1:]
            elif body[:1] and not (body[0].isalnum() or body[0] in "_%"):
                raise ValueError(f"unsupported template operator {body[0]!r}")
            if not _VARNAME.fullmatch(body):
                raise ValueError(f"unsupported template expression {text[index : end + 1]!r}")
            if literal:
                parts.append("".join(literal))
                literal = []
            parts.append(Expression(operator, body))
            index = end + 1
            continue
        if ch == "}" or ch in _FORBIDDEN_LITERAL or ord(ch) < 0x21 or ord(ch) == 0x7F:
            raise ValueError(f"character {ch!r} is not permitted in a URI template literal")
        if ch == "%" and not (
            index + 2 < len(text) and text[index + 1] in _HEX and text[index + 2] in _HEX
        ):
            raise ValueError("'%' must begin a percent-encoded triplet")
        literal.append(ch)
        index += 1
    if literal:
        parts.append("".join(literal))
    return UrlTemplate(text, tuple(parts))
