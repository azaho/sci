from __future__ import annotations
import json
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pysci import prepare, Invalid
from pysci.reading import read_verified, read_local


def example():
    return prepare(
        {
            "sci.json": json.dumps({"title": "T", "abstract": "A"}).encode(),
            "README.md": b"Original text",
            "files/large-data.bin": b"unrelated data",
        },
        Ed25519PrivateKey.generate(),
    )


def test_manifest_and_metadata_scopes_do_not_read_other_payloads():
    bundle = example()
    seen = []

    def get(path, limit):
        seen.append(path)
        if path == "files/large-data.bin":
            raise AssertionError("unrequested payload")
        return bundle.entries[path]

    listing = read_verified(get, expected_id=bundle.id, listing=True)
    assert listing.scope == "manifest" and seen == ["manifest.json", "manifest.json.sig"]
    seen.clear()
    reading = read_verified(get, expected_id=bundle.id)
    assert reading.scope == "metadata" and reading.metadata["title"] == "T"
    assert seen == ["manifest.json", "manifest.json.sig", "sci.json", "README.md"]


def test_selected_file_is_checked_before_return_and_metadata_not_claimed():
    bundle = example()
    reading = read_verified(lambda path, limit: bundle.entries[path], file="files/large-data.bin")
    assert reading.scope == "file" and reading.metadata is None
    with pytest.raises(Invalid, match="file check"):
        read_verified(
            lambda path, limit: b"corrupted text"
            if path == "files/large-data.bin"
            else bundle.entries[path],
            file="files/large-data.bin",
        )
    with pytest.raises(Invalid, match="ID mismatch"):
        read_verified(lambda path, limit: bundle.entries[path], expected_id="a" * 64, listing=True)


def test_local_metadata_read_allows_missing_unrequested_payload(tmp_path):
    bundle = example()
    for path, data in bundle.entries.items():
        if path != "files/large-data.bin":
            (tmp_path / path).write_bytes(data)
    assert read_local(tmp_path).scope == "metadata"
    with pytest.raises(OSError):
        read_local(tmp_path, file="files/large-data.bin")
