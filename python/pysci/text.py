"""Text character rules (protocol §4.2) using RFC 9839 Unicode Assignables.

Membership is decided by the fixed code-point intervals of RFC 9839 §4.3; no
runtime Unicode category data is consulted.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterable
from typing import Final

from .errors import Invalid, Issue, IssueCollector

__all__ = [
    "ASSIGNABLE_RANGES",
    "LINE_CONTROLS",
    "TextFileValidator",
    "check_text",
    "check_text_file",
    "decode_text_file",
    "first_disallowed",
    "is_assignable",
    "is_nonblank",
]

ASSIGNABLE_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x09, 0x0A),
    (0x0D, 0x0D),
    (0x20, 0x7E),
    (0xA0, 0xD7FF),
    (0xE000, 0xFDCF),
    (0xFDF0, 0xFFFD),
    *((plane << 16, (plane << 16) | 0xFFFD) for plane in range(1, 17)),
)
"""Inclusive code-point intervals of RFC 9839 §4.3 Unicode Assignables."""

LINE_CONTROLS: Final = frozenset("\t\n\r")
"""TAB, LF, CR: permitted only in multiline fields and text files."""

_BLANK: Final = frozenset(" \t\n\r")


def _char_class(ranges: Iterable[tuple[int, int]]) -> str:
    parts = []
    for low, high in ranges:
        if low == high:
            parts.append(re.escape(chr(low)))
        else:
            parts.append(f"{re.escape(chr(low))}-{re.escape(chr(high))}")
    return "".join(parts)


_SINGLE_LINE_RANGES: Final = tuple(r for r in ASSIGNABLE_RANGES if r[0] > 0x0D)
_DISALLOWED_MULTILINE: Final = re.compile(f"[^{_char_class(ASSIGNABLE_RANGES)}]")
_DISALLOWED_SINGLE_LINE: Final = re.compile(f"[^{_char_class(_SINGLE_LINE_RANGES)}]")


def is_assignable(code_point: int) -> bool:
    """True if ``code_point`` is an RFC 9839 Unicode Assignable."""
    return any(low <= code_point <= high for low, high in ASSIGNABLE_RANGES)


def first_disallowed(text: str, *, multiline: bool) -> int | None:
    """Index of the first character not permitted by §4.2, or None."""
    pattern = _DISALLOWED_MULTILINE if multiline else _DISALLOWED_SINGLE_LINE
    match = pattern.search(text)
    return None if match is None else match.start()


def is_nonblank(text: str) -> bool:
    """True if ``text`` has a character other than SPACE, TAB, LF, or CR."""
    return any(ch not in _BLANK for ch in text)


def check_text(
    value: str,
    *,
    multiline: bool = False,
    nonblank: bool = False,
    max_code_points: int | None = None,
    file: str | None = None,
    field: str | None = None,
    collector: IssueCollector,
) -> None:
    """Check a decoded string against §4.2 and an optional length limit.

    ``multiline`` permits TAB, LF, and CR (abstract, citation reason, claim note).
    """
    index = first_disallowed(value, multiline=multiline)
    if index is not None:
        ch = value[index]
        if ch in LINE_CONTROLS:
            collector.add(
                "text_line_control",
                "TAB, LF, and CR are permitted only in abstract, citation reason, "
                "README.md, and LICENSE",
                section="4.2",
                file=file,
                field=field,
                observed=f"U+{ord(ch):04X} at code point {index}",
            )
        else:
            collector.add(
                "text_not_assignable",
                "text must contain only RFC 9839 Unicode Assignables",
                section="4.2",
                file=file,
                field=field,
                observed=f"U+{ord(ch):04X} at code point {index}",
            )
    if max_code_points is not None and len(value) > max_code_points:
        collector.add(
            "text_too_long",
            f"at most {max_code_points} Unicode code points",
            section="4",
            file=file,
            field=field,
            observed=f"{len(value)} code points",
        )
    if nonblank and not is_nonblank(value):
        collector.add(
            "text_blank",
            "must contain a character other than SPACE, TAB, LF, or CR",
            section="4.2",
            file=file,
            field=field,
        )


def decode_text_file(data: bytes, *, file: str, nonblank: bool = False) -> str:
    """Decode a README.md/LICENSE body and check §4.2. Raises :class:`Invalid`."""
    issues = check_text_file([data], file=file, nonblank=nonblank)
    if issues:
        raise Invalid(issues)
    return data.decode("utf-8")


def check_text_file(chunks: Iterable[bytes], *, file: str, nonblank: bool = False) -> list[Issue]:
    """Stream-check UTF-8 text-file chunks against §2 and §4.2; return the issues."""
    validator = TextFileValidator(file=file, nonblank=nonblank)
    for chunk in chunks:
        if not validator.feed(chunk):
            break
    return validator.finish()


class TextFileValidator:
    """Incremental checker for README.md/LICENSE bodies (§2, §4.2).

    Feed chunks in order, then call :meth:`finish`. Memory use is bounded by
    the chunk size. At most one encoding/character issue is reported, plus a
    blankness issue for README.md.
    """

    def __init__(self, *, file: str, nonblank: bool = False) -> None:
        self.file = file
        self.nonblank = nonblank
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._collector = IssueCollector()
        self._seen_nonblank = False
        self._consumed = 0
        self._position = 0
        self._failed = False
        self._finished = False

    def feed(self, chunk: bytes) -> bool:
        """Consume a chunk; return False once a violation has been found."""
        if self._failed:
            return False
        try:
            text = self._decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            self._utf8_error(exc)
            return False
        self._consumed += len(chunk)
        self._check(text)
        return not self._failed

    def finish(self) -> list[Issue]:
        """Flush the decoder and return all issues."""
        if not self._finished:
            self._finished = True
            if not self._failed:
                try:
                    self._check(self._decoder.decode(b"", final=True))
                except UnicodeDecodeError as exc:
                    self._utf8_error(exc)
            if not self._failed and self.nonblank and not self._seen_nonblank:
                self._collector.add(
                    "text_blank", f"{self.file} must be nonblank", section="2", file=self.file
                )
        return list(self._collector.issues)

    def _check(self, text: str) -> None:
        if _report_disallowed(text, self._position, self.file, self._collector):
            self._failed = True
            return
        self._position += len(text)
        if not self._seen_nonblank and is_nonblank(text):
            self._seen_nonblank = True

    def _utf8_error(self, exc: UnicodeDecodeError) -> None:
        self._failed = True
        self._collector.add(
            "text_utf8",
            "text files must be well-formed UTF-8",
            section="2",
            file=self.file,
            observed=f"ill-formed sequence near byte {self._consumed + exc.start}",
        )


def _report_disallowed(text: str, position: int, file: str, collector: IssueCollector) -> bool:
    index = first_disallowed(text, multiline=True)
    if index is None:
        return False
    collector.add(
        "text_not_assignable",
        "text must contain only RFC 9839 Unicode Assignables",
        section="4.2",
        file=file,
        observed=f"U+{ord(text[index]):04X} at code point {position + index}",
    )
    return True
