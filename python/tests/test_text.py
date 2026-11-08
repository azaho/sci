from __future__ import annotations

import pytest

from pysci.errors import IssueCollector
from pysci.text import (
    ASSIGNABLE_RANGES,
    check_text,
    check_text_file,
    first_disallowed,
    is_assignable,
    is_nonblank,
)


def _reference(cp: int) -> bool:
    """RFC 9839 §4.3 written independently as an exclusion predicate."""
    if cp in (0x9, 0xA, 0xD):
        return True
    if cp < 0x20 or 0x7F <= cp <= 0x9F:
        return False
    if 0xD800 <= cp <= 0xDFFF or 0xFDD0 <= cp <= 0xFDEF:
        return False
    return (cp & 0xFFFF) < 0xFFFE


def test_all_code_points_match_reference() -> None:
    text = "".join(chr(cp) for cp in range(0x110000))
    for cp in range(0x110000):
        assert is_assignable(cp) == _reference(cp), hex(cp)
    # The regex form agrees with the interval predicate everywhere.
    position = 0
    while True:
        index = first_disallowed(text[position:], multiline=True)
        if index is None:
            break
        cp = position + index
        assert not _reference(cp)
        position = cp + 1
    assert len(ASSIGNABLE_RANGES) == 22


def test_single_line_rejects_controls() -> None:
    assert first_disallowed("a\tb", multiline=False) == 1
    assert first_disallowed("a\tb", multiline=True) is None


@pytest.mark.parametrize(
    ("value", "kwargs", "code"),
    [
        ("a\u0085", {}, "text_not_assignable"),
        ("a\nb", {}, "text_line_control"),
        (" \t", {"nonblank": True, "multiline": True}, "text_blank"),
        ("\U0001f600" * 3, {"max_code_points": 2}, "text_too_long"),
    ],
)
def test_check_text(value: str, kwargs: dict[str, object], code: str) -> None:
    collector = IssueCollector()
    check_text(value, collector=collector, **kwargs)  # type: ignore[arg-type]
    assert [issue.code for issue in collector.issues] == [code]


def test_nonblank_uses_explicit_code_points() -> None:
    assert is_nonblank(chr(0xA0))
    assert is_nonblank(chr(0x200D))
    assert not is_nonblank(" \r\n\t")


def test_text_file_streaming_handles_split_utf8() -> None:
    data = "é€😀".encode()
    chunks = [data[i : i + 1] for i in range(len(data))]
    assert check_text_file(chunks, file="README.md", nonblank=True) == []


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xff", "text_utf8"),
        (b"abc\xe2\x82", "text_utf8"),  # truncated at end
        (b"\xf4\x90\x80\x80", "text_utf8"),  # above U+10FFFF
        (b"a\x00b", "text_not_assignable"),
        (b"  \n", "text_blank"),
    ],
)
def test_text_file_rejections(data: bytes, code: str) -> None:
    issues = check_text_file([data], file="README.md", nonblank=True)
    assert [issue.code for issue in issues] == [code]


def test_bom_character_is_permitted_in_text_files() -> None:
    assert (
        check_text_file([(chr(0xFEFF) + "hello").encode()], file="README.md", nonblank=True) == []
    )
