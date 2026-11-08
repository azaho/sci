from __future__ import annotations

import pytest

from pysci.errors import Invalid
from pysci.jsonstrict import JsonFloat, JsonInt, is_json_integer, parse_json, parse_json_object


def codes(data: bytes) -> str:
    with pytest.raises(Invalid) as info:
        parse_json(data)
    return info.value.issue.code


def test_parses_objects_and_keeps_number_spelling() -> None:
    value = parse_json_object(b'{"a": [1, -0, 1.0, 1e3, true, null, "x"]}')
    items = value["a"]
    assert isinstance(items, list)
    assert [type(v) for v in items[:4]] == [JsonInt, JsonInt, JsonFloat, JsonFloat]
    assert isinstance(items[1], JsonInt) and items[1].lexeme == "-0"
    assert is_json_integer(items[0]) and not is_json_integer(items[4])


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b'\xef\xbb\xbf{"a":1}', "json_bom"),
        (b'{"a":"\xff"}', "json_utf8"),
        (b'{"a":"\xed\xa0\x80"}', "json_utf8"),  # encoded surrogate
        (b'{"a":1,"a":2}', "json_duplicate_key"),
        (b'{"a":1,"\\u0061":2}', "json_duplicate_key"),
        (b'{"o":{"k":1,"k":2}}', "json_duplicate_key"),
        (b'{"a":"\\ud800"}', "json_unpaired_surrogate"),
        (b'{"\\udfff":1}', "json_unpaired_surrogate"),
        (b'{"a":NaN}', "json_syntax"),
        (b'{"a":Infinity}', "json_syntax"),
        (b'{"a":01}', "json_syntax"),
        (b'{"a":1,}', "json_syntax"),
        (b'{"a":"\x01"}', "json_syntax"),
        (b"{'a':1}", "json_syntax"),
        (b'{"a":1} x', "json_syntax"),
    ],
)
def test_rejections(data: bytes, code: str) -> None:
    assert codes(data) == code


def test_valid_surrogate_pair_decodes() -> None:
    assert parse_json(b'"\\ud83d\\ude00"') == "\U0001f600"


def test_top_level_object_required() -> None:
    with pytest.raises(Invalid) as info:
        parse_json_object(b"[]")
    assert info.value.issue.code == "json_not_object"
