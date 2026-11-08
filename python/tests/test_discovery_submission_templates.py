from __future__ import annotations

import json

import pytest

from pysci.accounts import Account
from pysci.discovery import (
    ArchiveDescriptor,
    IndexDescriptor,
    archive_descriptor_url,
    index_descriptor_url,
    parse_archive_descriptor,
    parse_index_descriptor,
    parse_publication_list,
    parse_publication_lookup,
    parse_timestamp_lookup,
    revocation_lookup_result,
)
from pysci.errors import Incomplete, Invalid, Unsupported
from pysci.submission import (
    build_submission_request,
    parse_submission_request,
    parse_submission_response,
)
from pysci.templates import parse_template

DIGEST = "c016f424ed267b2edc1784101e4bd15b9950957b61f83af22efdaafc450f41f9"
INDEX = {
    "schema": "sci-index/0.1",
    "publication_lookup_url": "https://index.example/p/{id}",
    "revocation_lookup_url": "https://index.example/k/{x}/{file}",
    "timestamp_lookup_url": "https://index.example/t/{id}",
}


def test_template_expansion() -> None:
    template = parse_template("https://a.example/{id2}/{+path}?q={id}")
    assert template.expand({"id2": "c0", "path": "files/a b/%41", "id": "x/y"}) == (
        "https://a.example/c0/files/a%20b/%41?q=x%2Fy"
    )
    for bad in (
        "https://a/{id",
        "https://a/{#id}",
        "https://a/{id,x}",
        "https://a/{id:3}",
        "https://a/ b",
        "https://a/}",
    ):
        with pytest.raises(ValueError):
            parse_template(bad)


def test_index_descriptor() -> None:
    descriptor = parse_index_descriptor(json.dumps({**INDEX, "custom": 1}).encode())
    assert isinstance(descriptor, IndexDescriptor)
    assert descriptor.publication_lookup("sha256:" + DIGEST) == f"https://index.example/p/{DIGEST}"
    assert descriptor.revocation_lookup("X", "revocation.json") == (
        "https://index.example/k/X/revocation.json"
    )
    assert descriptor.extra == {"custom": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"publication_lookup_url": None},
        {"publication_lookup_url": "https://index.example/all"},
        {"publication_lookup_url": "/p/{id}"},
        {"publication_lookup_url": "https://index.example/{+id}"},
        {"submit_url": "https://index.example/{id}"},
        {"policy_url": "/policy"},
        {"docs_url": 5},
    ],
)
def test_index_descriptor_rejections(change: dict[str, object]) -> None:
    document = {**INDEX, **change}
    document = {k: v for k, v in document.items() if v is not None}
    with pytest.raises(Invalid):
        parse_index_descriptor(json.dumps(document).encode())


def test_unknown_schema_is_unsupported() -> None:
    with pytest.raises(Unsupported):
        parse_index_descriptor(json.dumps({**INDEX, "schema": "sci-index/9"}).encode())


def test_archive_descriptor() -> None:
    raw = json.dumps(
        {
            "schema": "sci-archive/0.1",
            "entry_url": "https://a.example/{id}/{+path}",
            "blob_url": "https://a.example/b/{sha256}",
        }
    ).encode()
    descriptor = parse_archive_descriptor(raw)
    assert isinstance(descriptor, ArchiveDescriptor)
    assert descriptor.entry(DIGEST, "manifest.json.sig.tsr") == (
        f"https://a.example/{DIGEST}/manifest.json.sig.tsr"
    )
    assert descriptor.bundle(DIGEST) is None
    with pytest.raises(Invalid):
        parse_archive_descriptor(b'{"schema":"sci-archive/0.1","list_url":"https://a.example/l"}')
    assert archive_descriptor_url(github=("o", "r")) == (
        "https://raw.githubusercontent.com/o/r/main/sci/archive.json"
    )
    assert index_descriptor_url("https://sciscape.io/") == (
        "https://sciscape.io/.well-known/sci/index.json"
    )


def test_lookup_responses() -> None:
    assert parse_publication_lookup(404, b"") == []
    assert parse_publication_lookup(200, b'{"descriptors":["https://a.example/x.json"]}') == [
        "https://a.example/x.json"
    ]
    with pytest.raises(Invalid):
        parse_publication_lookup(200, b'{"descriptors":["/x"]}')
    with pytest.raises(Incomplete):
        parse_publication_lookup(503, b"")
    missing = parse_timestamp_lookup(404, b"")
    assert not missing.proofs_retained and missing.existence == "unknown"
    assert revocation_lookup_result(404, b"") is None
    assert parse_publication_list(f"{DIGEST}\n".encode()) == [DIGEST]
    with pytest.raises(Invalid):
        parse_publication_list(DIGEST.encode())


def test_submission_request() -> None:
    request = build_submission_request(
        "sha256:" + DIGEST, ["https://a.example/archive.json"], Account("domain", "example.org")
    )
    parsed = parse_submission_request(request.to_json_bytes())
    assert parsed == request
    for bad in (
        {"id": DIGEST, "descriptors": []},
        {"id": DIGEST, "descriptors": ["https://a", "https://a"]},
        {"id": "sha256:" + DIGEST, "descriptors": ["https://a"]},
        {"id": DIGEST, "descriptors": ["https://a"], "account": {"type": "x", "id": 1}},
        {"id": DIGEST, "descriptors": ["https://a"], "extra": True},
    ):
        with pytest.raises(Invalid):
            parse_submission_request(json.dumps(bad).encode())


def test_submission_response() -> None:
    ok = parse_submission_response(
        201, "application/json; charset=utf-8", json.dumps({"id": DIGEST, "x": 1}).encode(), DIGEST
    )
    assert ok.success and ok.body["x"] == 1
    error = parse_submission_response(
        503,
        "application/json",
        b'{"error":{"code":"unavailable","message":"later","retryable":true}}',
        DIGEST,
    )
    assert error.error is not None and error.error.retryable
    for status, content_type, body in (
        (202, "application/json", json.dumps({"id": DIGEST}).encode()),
        (200, "text/plain", json.dumps({"id": DIGEST}).encode()),
        (200, "application/json", b'{"id":"bbbb"}'),
        (422, "application/json", b'{"error":{"code":"x","message":"y","retryable":"no"}}'),
    ):
        with pytest.raises(Invalid):
            parse_submission_response(status, content_type, body, DIGEST)
