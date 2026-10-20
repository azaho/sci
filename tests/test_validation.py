"""Field validation shared by signing, verification, admission, pull, and recovery."""

from __future__ import annotations

import json

import pytest

from sci import publication as pub
from sci.publication import PublicationError

from .conftest import FIXTURES


def sci(**fields):
    doc = {"title": "T", "abstract": "A"}
    doc.update(fields)
    return doc


def ok(doc):
    return pub.validate_sci(doc)


def bad(doc, field=None):
    with pytest.raises(PublicationError) as e:
        pub.validate_sci(doc)
    if field is not None:
        assert e.value.field == field, str(e.value)
    return e.value


# -- strict JSON --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b'\xef\xbb\xbf{"title":"T","abstract":"A"}',
        b'{"title":"T","abstract":"A"} x',
        b'{"title":"T","abstract":"A"}{}',
        b'{"title":"T","title":"U","abstract":"A"}',
        b'{"title":"T","\\u0074itle":"U","abstract":"A"}',
        b'{"title":"T","abstract":"A","keywords":[NaN]}',
        b'{"title":"T","abstract":"A","keywords":[Infinity]}',
        b'{"title":"T","abstract":null}',
        b'["not an object"]',
        b'{"title":"T","abstract":"A\xff"}',
        b'{"title":"T","abstract":"\xed\xa0\x80"}',
        b'{"title":"T","abstract":"raw\x01control"}',
        b'{"title":"T","abstract":"A",}',
        b"{'title':'T'}",
        b'{"a":{"b":{"c":1,"c":2}}}',
    ],
)
def test_strict_json_rejections(raw):
    with pytest.raises(PublicationError):
        pub.parse_strict_json(raw, "sci.json")


def test_strict_json_preserves_values():
    doc = pub.parse_strict_json('{"title":"\\ud83e\\uddea e\u0301 \u00e9","abstract":"A"}  \n'.encode(), "x")
    assert doc["title"] == "\U0001f9ea e\u0301 \u00e9"
    ok(doc)  # surrogate pair escape is one character; canonically equivalent spellings stay distinct
    assert doc["title"] != "\U0001f9ea \u00e9 \u00e9"


# -- text fields --------------------------------------------------------------


def test_multilingual_text_accepted():
    ok(sci(title="Résultat: ∀x∈ℝ, x²≥0 — 结果 🧪", abstract="Línea\tuno\r\nLine two\n∑", keywords=["e\u0301", "数据"]))


@pytest.mark.parametrize(
    "fields,field",
    [
        ({"title": ""}, "title"),
        ({"title": " \t"}, "title"),
        ({"title": "\u3000\u00a0\u2003"}, "title"),
        ({"abstract": "\u2028"}, "abstract"),
        ({"title": "a\u0000b"}, "sci.json.title"),
        ({"title": "a\u007fb"}, "sci.json.title"),
        ({"title": "a\u0085b"}, "sci.json.title"),
        ({"title": "a\ud800b"}, "sci.json.title"),
        ({"title": "a\ufdd0b"}, "sci.json.title"),
        ({"title": "a\U0001fffeb"}, "sci.json.title"),
        ({"title": "a\tb"}, "title"),
        ({"title": "a\nb"}, "title"),
        ({"title": "a\u2028b"}, "title"),
        ({"keywords": ["ok", "a\u2029b"]}, "keywords[1]"),
        ({"headline_figure_caption": "a\rb"}, "headline_figure_caption"),
        ({"title": "x" * 201}, "title"),
        ({"abstract": "x" * 2501}, "abstract"),
        ({"keywords": ["k" * 65]}, "keywords[0]"),
        ({"keywords": ["k"] * 21}, "keywords"),
        ({"keywords": [""]}, "keywords[0]"),
        ({"keywords": ["\u2003"]}, "keywords[0]"),
        ({"keywords": "k"}, "keywords"),
        ({"keywords": [1]}, "keywords[0]"),
        ({"headline_figure_caption": "c" * 401}, "headline_figure_caption"),
        ({"headline_figure_caption": " "}, "headline_figure_caption"),
        ({"citations": [{"target": "doi:10.1/x", "reason": "r" * 1001}]}, "citations[0].reason"),
        ({"citations": [{"target": "doi:10.1/x", "reason": "  "}]}, "citations[0].reason"),
        ({"citations": [{"target": "doi:10.1/x", "reason": "a\u0000"}]}, "sci.json.citations[0].reason"),
        ({"citations": [{"target": "doi:10.1/x", "relation": "cites"}]}, "citations[0].relation"),
        ({"citations": [{"target": "doi:10.1/x", "extra": "y"}]}, "citations[0]"),
        ({"citations": [{"relation": "uses"}]}, "citations[0]"),
        ({"citations": ["doi:10.1/x"]}, "citations[0]"),
        ({"citations": {"target": "doi:10.1/x"}}, "citations"),
        ({"categories": ["cs.LG", "cs.LG"]}, "categories"),
        ({"categories": ["cs.LG", "cs.AI", "stat.ML", "math.ST"]}, "categories"),
        ({"categories": ["cs.lg"]}, "categories[0]"),
        ({"unknown": 1}, "sci.json"),
        ({"title": 5}, "title"),
        ({"title": True}, "title"),
    ],
)
def test_field_rejections(fields, field):
    bad(sci(**fields), field)


def test_exact_limits_and_code_point_counting():
    ok(sci(title="x" * 200, abstract="y" * 2500, keywords=["k" * 64] * 20, headline_figure_caption="c" * 400,
           citations=[{"target": "doi:10.1/x", "reason": "r" * 1000}]))
    ok(sci(title="\U0001f9ea" * 200))  # 200 code points, 400 UTF-16 units
    bad(sci(title="\U0001f9ea" * 201), "title")


def test_optional_fields():
    m = ok(sci())
    assert m.categories == [] and m.keywords == [] and m.citations == [] and m.headline_figure_caption is None
    m = ok(sci(categories=[], keywords=[], citations=[]))
    m = ok(sci(categories=["q-bio.NC", "cs.LG", "acc-phys"]))
    assert m.categories == ["q-bio.NC", "cs.LG", "acc-phys"]  # order kept; historical codes valid
    m = ok(sci(citations=[{"target": "doi:10.1/x"}, {"target": "sci:b2836a2b-6f5d-4c3f-9e7e-92b19656b627", "relation": "supersedes", "reason": "Fix."}]))
    assert m.citations[0].relation is None and m.citations[1].relation == "supersedes"


def test_whitespace_definition():
    assert pub.WHITESPACE == frozenset(
        "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
    )
    ok(sci(title="\u200bx"))  # zero-width space is not whitespace in SCI's definition
    bad(sci(title="\u200a"), "title")


# -- timestamps and identifiers ---------------------------------------------------


@pytest.mark.parametrize("ts", ["2026-10-14T18:00:00Z", "2024-02-29T23:59:59Z", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999Z", "2026-01-01T00:00:00.5Z", "1970-01-01T00:00:00.123456Z", "2400-02-29T00:00:00Z"])
def test_valid_timestamps(ts):
    pub.parse_timestamp(ts)


@pytest.mark.parametrize("ts", ["2026-10-14", "2026-10-14T18:00:00", "2026-10-14T18:00:00+00:00", "2026-10-14T18:00:00.1234567Z",
                                "2026-02-29T00:00:00Z", "2100-02-29T00:00:00Z", "0000-01-01T00:00:00Z", "2026-13-01T00:00:00Z",
                                "2026-10-14T24:00:00Z", "2026-10-14T18:60:00Z", "2026-12-31T23:59:60Z", " 2026-10-14T18:00:00Z",
                                "2026-10-14T18:00:00Z\n", "2026-10-14t18:00:00z", "2026-10-14T18:00:00.Z", "２026-10-14T18:00:00Z", 20261014])
def test_invalid_timestamps(ts):
    with pytest.raises(PublicationError):
        pub.parse_timestamp(ts)


@pytest.mark.parametrize("u,valid", [
    ("b2836a2b-6f5d-4c3f-9e7e-92b19656b627", True),
    ("00000000-0000-4000-8000-000000000000", True),
    ("B2836A2B-6F5D-4C3F-9E7E-92B19656B627", False),
    ("b2836a2b6f5d4c3f9e7e92b19656b627", False),
    ("{b2836a2b-6f5d-4c3f-9e7e-92b19656b627}", False),
    ("b2836a2b-6f5d-5c3f-9e7e-92b19656b627", False),
    ("b2836a2b-6f5d-4c3f-ce7e-92b19656b627", False),
    ("00000-00001", False),
    ("b2836a2b-6f5d-4c3f-9e7e-92b19656b627\n", False),
])
def test_uuid_rules(u, valid):
    assert pub.is_uuid4(u) is valid


def test_generated_uuids_are_v4():
    for _ in range(100):
        assert pub.is_uuid4(pub.new_uuid4())


# -- citations ---------------------------------------------------------------------


def test_citation_fixtures():
    fx = json.loads((FIXTURES / "citation_targets.json").read_text())
    for case in fx["valid"]:
        assert pub.normalize_citation_target(case["target"]) == case["key"], case
    for case in fx["invalid"]:
        with pytest.raises(PublicationError):
            pub.normalize_citation_target(case["target"])


def test_citation_target_is_preserved():
    m = ok(sci(citations=[{"target": "doi:10.48550/arXiv.1706.03762"}]))
    assert m.citations[0].target == "doi:10.48550/arXiv.1706.03762"
    assert m.citations[0].target_key == "doi:10.48550/arxiv.1706.03762"


def test_citation_length_measured_after_json_decoding():
    t = "doi:10.1000/" + "a" * 500
    raw = json.dumps({"title": "T", "abstract": "A", "citations": [{"target": t.replace("a", "\\u0061", 3)}]}).encode()
    raw = raw.replace(b"\\\\u0061", b"\\u0061")
    doc = pub.parse_strict_json(raw, "sci.json")
    assert len(doc["citations"][0]["target"]) == 512
    ok(doc)


# -- manifest ----------------------------------------------------------------------


def _manifest(**over):
    from sci import keys

    k = keys.generate_private_key()
    doc = {
        "schema_version": 1,
        "uuid": "c8af05c1-c45b-471c-9e84-c39bedb1a462",
        "public_key": keys.jwk_from_public_key(k.public_key()),
        "created_at": "2026-10-14T18:00:00Z",
        "files": {
            "sci.json": {"sha256": "a" * 64, "bytes": 10},
            "README.md": {"sha256": "b" * 64, "bytes": 10},
            "LICENSE": dict(pub.license_entry()),
        },
    }
    doc.update(over)
    return doc


def test_manifest_valid():
    info = pub.validate_manifest(_manifest())
    assert info.fingerprint.startswith("ed25519:")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=1.0),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version="1"),
        lambda d: d.update(extra=1),
        lambda d: d.pop("created_at"),
        lambda d: d["files"].update({"other.txt": {"sha256": "c" * 64, "bytes": 1}}),
        lambda d: d["files"].update({"files/x": {"sha256": "c" * 64, "bytes": 1}}),
        lambda d: d["files"].pop("LICENSE"),
        lambda d: d["files"].pop("README.md"),
        lambda d: d["files"]["README.md"].update(bytes=-1),
        lambda d: d["files"]["README.md"].update(bytes=0),
        lambda d: d["files"]["README.md"].update(bytes=1_000_001),
        lambda d: d["files"]["README.md"].update(bytes="10"),
        lambda d: d["files"]["README.md"].update(sha256="A" * 64),
        lambda d: d["files"]["README.md"].update(sha256="a" * 63),
        lambda d: d["files"]["README.md"].update(extra=1),
        lambda d: d["files"].update({"files.zip": {"sha256": "c" * 64, "bytes": 49_999_991}}),
        lambda d: d["files"].update({"headline_figure.png": {"sha256": "c" * 64, "bytes": 2_000_001}}),
        lambda d: d["public_key"].update(d="AAAA"),
        lambda d: d["public_key"].update(kty="EC"),
        lambda d: d["public_key"].update(crv="Ed448"),
        lambda d: d["public_key"].update(x=d["public_key"]["x"] + "="),
        lambda d: d["public_key"].update(x=d["public_key"]["x"][:-2]),
        lambda d: d.update(files_tree={"paths": [], "truncated": False}),  # no files.zip
        lambda d: d.update(uuid="C8AF05C1-C45B-471C-9E84-C39BEDB1A462"),
    ],
)
def test_manifest_rejections(mutate):
    d = _manifest()
    mutate(d)
    with pytest.raises(PublicationError):
        pub.validate_manifest(d)


def test_license_entry_must_be_approved():
    d = _manifest()
    d["files"]["LICENSE"]["bytes"] += 1
    info = pub.validate_manifest(d)
    with pytest.raises(PublicationError) as e:
        pub.check_license_entry(info)
    assert e.value.code == "unapproved_license"


def test_bundled_license_is_frozen():
    data = pub.license_bytes()
    assert pub.license_entry() == {"sha256": "fed84172ce5540c20b6c51ffee014941c61e0371f34875fa1114b9ac0db99b44", "bytes": 22239}
    text = data.decode("utf-8")
    assert "Attribution 4.0 International" in text and "Permission is hereby granted" in text and "\r" not in text


@pytest.mark.parametrize(
    "tree",
    [
        {"paths": ["a/", "a/b/c"], "truncated": False},
        {"paths": ["/abs"], "truncated": False},
        {"paths": ["a\\b"], "truncated": False},
        {"paths": ["a//"], "truncated": False},
        {"paths": ["./a"], "truncated": False},
        {"paths": ["../a"], "truncated": False},
        {"paths": ["a", "a"], "truncated": False},
        {"paths": ["a\u0000"], "truncated": False},
        {"paths": ["a"], "truncated": "false"},
        {"paths": ["a"]},
        {"paths": ["a"], "truncated": False, "x": 1},
        {"paths": [f"f{i:03d}" for i in range(201)], "truncated": True},
        {"paths": ["x" * 8200], "truncated": True},
        {"paths": [1], "truncated": False},
        {"paths": [""], "truncated": False},
        {"paths": ["C:/a"], "truncated": False},
    ],
)
def test_files_tree_rejections(tree):
    with pytest.raises(PublicationError):
        pub.validate_files_tree(tree)


def test_files_tree_valid():
    pub.validate_files_tree({"paths": ["a/", "b/", "x.txt", "a/c/", "a/b.txt", "b/z"], "truncated": True})
    # Any order is accepted; the generator writes display-group order.
    pub.validate_files_tree({"paths": ["a/b.txt", "a/", "x.txt"], "truncated": False})
    pub.validate_files_tree({"paths": [], "truncated": False})


@pytest.mark.parametrize("raw", [
    b'{"title":"T","abstract":"A","keywords":[' + b"1" * 5000 + b']}',
    b'{"title":"T","abstract":"A","keywords":' + b"[" * 5000 + b"]" * 5000 + b'}',
    b'{"title":"T","abstract":"A","x":' + b"[" * 20 + b"]" * 20 + b'}',
])
def test_hostile_json_is_a_validation_error(raw):
    with pytest.raises(PublicationError):
        pub.parse_strict_json(raw, "manifest.json")
