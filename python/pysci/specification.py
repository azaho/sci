"""The SCI protocol specification text this library implements (bundled package data)."""

from __future__ import annotations

import hashlib
import re
from functools import cache
from importlib import resources

#: SHA-256 of ``protocol/README.md`` for protocol draft 0.1, as pinned by the
#: conformance corpus (``protocol/test-vectors/0.1/catalog.json``).
PROTOCOL_SPEC_SHA256 = "311e40720392570938fee3ff67d53e6cef933916b228653d1279a9c3f52afc0d"

_HEADING = re.compile(r"^(#{2,6}) (\d+(?:\.\d+)*)\.? ")
_ANY_HEADING = re.compile(r"^(#{1,6}) ")


@cache
def protocol_specification() -> str:
    """Return the complete protocol specification as Markdown.

    Raises:
        RuntimeError: the packaged file is damaged (hash mismatch).
    """
    data = resources.files("pysci").joinpath("data/sci-protocol.md").read_bytes()
    if hashlib.sha256(data).hexdigest() != PROTOCOL_SPEC_SHA256:
        raise RuntimeError("bundled protocol specification does not match its pinned hash")
    return data.decode("utf-8")


def specification_section(number: str) -> str | None:
    """Return one numbered section (e.g. ``"4.4"``) with its subsections, or None."""
    lines = protocol_specification().splitlines(keepends=True)
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        heading = _ANY_HEADING.match(line)
        if heading is None:
            continue
        if start is None:
            match = _HEADING.match(line)
            if match is not None and match.group(2) == number:
                start, level = index, len(match.group(1))
        elif len(heading.group(1)) <= level:
            return "".join(lines[start:index]).rstrip() + "\n"
    if start is None:
        return None
    return "".join(lines[start:]).rstrip() + "\n"


def specification_sections() -> list[tuple[str, str]]:
    """List ``(number, title)`` for every numbered heading, in document order."""
    sections = []
    for line in protocol_specification().splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((match.group(2), line[match.end() :].strip()))
    return sections
