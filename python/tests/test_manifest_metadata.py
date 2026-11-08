from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci.errors import Invalid, Outcome, Unsupported
from pysci.keys import public_key_of
from pysci.manifest import FileEntry, build_manifest, parse_manifest, sign_manifest, verify_manifest
from pysci.metadata import parse_metadata


def entries() -> dict[str, FileEntry]:
    return {
        "sci.json": FileEntry(hashlib.sha256(b"{}").hexdigest(), 2),
        "README.md": FileEntry("0" * 64, 0),
        "files/b": FileEntry("1" * 64, 9007199254740991),
        "files/A": FileEntry("2" * 64, 1),
    }


def test_build_is_stable_sorted_and_signable(private_key: Ed25519PrivateKey) -> None:
    public = public_key_of(private_key)
    raw = build_manifest(public, entries())
    assert raw.startswith(b'{"schema":"sci-publication/0.1","public_key":{"kty":"OKP"')
    assert list(json.loads(raw)["files"]) == ["README.md", "files/A", "files/b", "sci.json"]
    signature = sign_manifest(raw, private_key)
    manifest = verify_manifest(raw, signature, expected_id="sci:sha256:" + manifest_digest(raw))
    assert manifest.public_key == public
    assert manifest.files["files/b"].bytes == 9007199254740991
    with pytest.raises(Invalid) as info:
        verify_manifest(raw, signature, expected_id="0" * 64)
    assert info.value.issue.code == "publication_id_mismatch"


def manifest_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_build_rejects_invalid_paths(private_key: Ed25519PrivateKey) -> None:
    bad = {**entries(), "notes.txt": FileEntry("0" * 64, 1)}
    with pytest.raises(Invalid):
        build_manifest(public_key_of(private_key), bad)


def _manifest(files: object, **extra: object) -> bytes:
    document = {
        "schema": "sci-publication/0.1",
        "public_key": {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": "B0FOUbkEkRCSvkhN3_l43qNcIhewT-QlZq-BnxuvDPg",
        },
        "files": files,
        **extra,
    }
    return json.dumps(document).encode()


GOOD = {"sci.json": {"sha256": "0" * 64, "bytes": 1}, "README.md": {"sha256": "0" * 64, "bytes": 1}}


def test_parse_collects_independent_issues() -> None:
    files = {**GOOD, "files/x": {"sha256": "A" * 64, "bytes": -1}, "other": GOOD["sci.json"]}
    with pytest.raises(Invalid) as info:
        parse_manifest(_manifest(files, extra=1))
    found = {issue.code for issue in info.value.issues}
    assert {
        "manifest_fields",
        "manifest_sha256",
        "manifest_bytes",
        "manifest_path_location",
    } <= found


@pytest.mark.parametrize("spelling", ["1.0", "1e0", "-0", "01", "true", '"1"', "9007199254740992"])
def test_byte_count_spelling(spelling: str) -> None:
    raw = _manifest({}).replace(
        b'"files": {}',
        b'"files": {"sci.json": {"sha256": "'
        + b"0" * 64
        + b'", "bytes": '
        + spelling.encode()
        + b'}, "README.md": {"sha256": "'
        + b"0" * 64
        + b'", "bytes": 0}}',
    )
    with pytest.raises(Invalid):
        parse_manifest(raw)


def test_schema_outcomes() -> None:
    with pytest.raises(Unsupported) as unsupported:
        parse_manifest(_manifest(GOOD).replace(b"publication/0.1", b"publication/0.2"))
    assert unsupported.value.outcome is Outcome.UNSUPPORTED
    with pytest.raises(Invalid):
        parse_manifest(_manifest(GOOD).replace(b"sci-publication/0.1", b"sci-key-claim/0.1"))


def metadata(**fields: object) -> bytes:
    return json.dumps({"title": "T", "abstract": "A", **fields}, ensure_ascii=False).encode()


def test_metadata_valid_full() -> None:
    doc = metadata(
        categories=[{"scheme": "arxiv", "code": "cs.LG"}],
        keywords=["k" * 64],
        display=[{"path": "files/a.png", "caption": "c"}],
        citations=[{"target": "doi:10.1000/182", "relations": ["uses"], "reason": "a\tb"}],
    )
    parsed = parse_metadata(doc, ["files/a.png"])
    assert parsed.categories[0].code == "cs.LG"
    assert parsed.citations[0].target.key == "doi:10.1000/182"
    assert parsed.display[0].caption == "c"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"title": "x" * 201}, "text_too_long"),
        ({"title": "a\tb"}, "text_line_control"),
        ({"abstract": " \n"}, "text_blank"),
        ({"keywords": ["a"] * 21}, "metadata_array_length"),
        ({"categories": [{"scheme": "A", "code": "x"}]}, "category_scheme"),
        ({"categories": [{"scheme": "a", "code": "x"}] * 2}, "category_duplicate"),
        ({"display": [{"path": "files/missing"}]}, "display_path"),
        ({"display": [{"path": "files/a.png"}] * 2}, "display_duplicate"),
        ({"citations": [{"target": "ftp://x"}]}, "citation_target"),
        (
            {"citations": [{"target": "doi:10.1/x", "relations": ["uses", "uses"]}]},
            "citation_relations",
        ),
        ({"citations": [{"target": "doi:10.1/x", "note": 1}]}, "metadata_fields"),
        ({"extra": 1}, "metadata_fields"),
        ({"title": 3}, "metadata_type"),
        ({"\u0085key": 1}, "text_not_assignable"),
    ],
)
def test_metadata_rejections(fields: dict[str, object], code: str) -> None:
    with pytest.raises(Invalid) as info:
        parse_metadata(metadata(**fields), ["files/a.png"])
    assert code in {issue.code for issue in info.value.issues}


def test_metadata_reports_all_issues_together() -> None:
    with pytest.raises(Invalid) as info:
        parse_metadata(metadata(title="", keywords=[1], categories="x"), [])
    assert len(info.value.issues) >= 3
