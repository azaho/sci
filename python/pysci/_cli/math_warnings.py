"""Advisory checks for common math authoring mistakes, without executing TeX."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from pysci.baseline import BASELINE_LIMITS
from pysci.draft import DraftReport
from pysci.errors import SciError
from pysci.jsonstrict import parse_json_object
from pysci.source import PathSource

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

# A JSON escape can silently turn the first two characters of a TeX command into
# one control character. Limit the hint to a plausible control word inside math.
_CONTROL = {"\t": "t", "\n": "n", "\r": "r", "\b": "b", "\f": "f"}
_DAMAGED = re.compile(r"([\t\n\r\x08\x0c])([A-Za-z]{1,48})(?![A-Za-z])")


def _brace_warning(formula: str) -> str | None:
    if r"\verb" in formula:
        return None  # Verbatim arguments have their own delimiter rules.
    depth = 0
    for token in re.finditer(r"\\[^\n]|%[^\n]*|[{}]", formula):
        if token[0] == "{":
            depth += 1
        elif token[0] == "}":
            if depth == 0:
                return "extra closing } in math; remove it or escape a literal brace as \\}."
            depth -= 1
    return (
        "missing closing } in math; close the group before the math delimiter." if depth else None
    )


def _inline(text: str, *, metadata: bool = False) -> Iterator[str]:
    opening: int | None = None
    for match in re.finditer(r"\\[^\n]|\$+|`", text):
        mark = match[0]
        if mark.startswith("\\"):
            continue
        if mark != "$":
            opening = None
            continue
        pos = match.start()
        if opening is not None and not text[pos + 1 : pos + 2].isdigit():
            formula = text[opening + 1 : pos]
            if metadata and formula == formula.strip(" ") and (broken := _DAMAGED.search(formula)):
                command = _CONTROL[broken[1]] + broken[2]
                yield rf"possible JSON escape in math; write \\{command} in the JSON source."
                opening = None
                continue
            if (
                formula
                and not formula[0].isspace()
                and not formula[-1].isspace()
                and "\n" not in formula
                and "\r" not in formula
            ):
                if message := _brace_warning(formula):
                    yield message
                opening = None
                continue
        opening = pos


def _prose(md: MarkdownIt, text: str) -> str:
    """Mask inline code, retaining newlines for diagnostics and a boundary for math."""
    if "`" not in text:
        return text
    from markdown_it.rules_inline import StateInline, backtick

    state = StateInline(text, md, {}, [])
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"\\[\\`]|\x60+", text):
        if match.start() < last or match[0].startswith("\\"):
            continue
        state.pos = match.start()
        backtick(state, silent=True)
        if state.pos > match.end():
            parts.extend(
                (
                    text[last : match.start()],
                    "`" + "\n" * text.count("\n", match.start(), state.pos) + "`",
                )
            )
            last = state.pos
    return "".join((*parts, text[last:]))


def _manuscript(text: str) -> Iterator[str]:
    if "$" not in text and r"\[" not in text:
        return
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False}).disable("inline")
    for token in md.parse(text):
        if token.type != "inline" or not token.map:
            continue  # CommonMark already separated fenced and indented code, including in lists.
        lines = _prose(md, token.content).split("\n")
        display: int | None = None
        for i, line in enumerate(lines):
            marker = line.strip()
            location = f"README.md:{token.map[0] + i + 1}"
            if marker == "$$":
                if display is None:
                    display = i
                else:
                    if message := _brace_warning("\n".join(lines[display + 1 : i])):
                        yield f"README.md:{token.map[0] + display + 1}: {message}"
                    display = None
            elif display is None:
                if marker == r"\[":
                    yield rf"{location}: use $$ on its own line in place of \[ and \]."
                else:
                    for message in _inline(line):
                        yield f"{location}: {message}"


def math_warnings(draft: Path, report: DraftReport) -> Iterator[str]:
    """Only read files already inventoried as regular and within baseline text sizes."""
    for name, limit in (
        ("sci.json", BASELINE_LIMITS.metadata_bytes),
        ("README.md", BASELINE_LIMITS.readme_bytes),
    ):
        size = report.files.get(name)
        if size is None or size > limit:
            continue
        try:
            if name == "README.md":
                data = PathSource(draft / name, size).read_at(0, size)
                yield from _manuscript(data.decode("utf-8"))
            else:
                fields = (
                    report.metadata.raw
                    if report.metadata
                    else parse_json_object(PathSource(draft / name, size).read_at(0, size))
                )
                for field in ("title", "abstract"):
                    value = fields.get(field)
                    if isinstance(value, str):
                        for message in _inline(value, metadata=True):
                            yield f"sci.json /{field}: {message}"
        except (OSError, UnicodeDecodeError, SciError):
            continue  # Missing, changed or malformed input is handled by the draft checks.
