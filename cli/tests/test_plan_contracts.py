"""Regression checks for command contracts required by cli/DESIGN.md."""

from __future__ import annotations
import argparse
import json
import shutil
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from pysci import prepare, Invalid
from sci.main import parser, dispatch
from sci.output import CommandError


def publication(folder: Path, title: str):
    bundle = prepare(
        {
            "sci.json": json.dumps({"title": title, "abstract": "Evidence"}).encode(),
            "README.md": b"Measured observation",
        },
        Ed25519PrivateKey.generate(),
    )
    folder.mkdir(exist_ok=True)
    (folder / (bundle.id + ".zip")).write_bytes(bundle.zip())
    return bundle


def invoke(*args):
    return dispatch(parser().parse_args(list(map(str, args))))


def test_every_command_owns_its_parser_and_handler():
    root = parser()
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    assert len(sub.choices) == 15
    for name, command in sub.choices.items():
        assert command.get_default("handler").__module__ == "sci.commands." + name
        assert "Example:" in command.format_help()


def test_local_cursor_deduplication_cache_reuse_and_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    campaign = tmp_path / "campaign"
    other = tmp_path / "other"
    bundles = [publication(campaign, str(i)) for i in range(3)]
    other.mkdir()
    shutil.copy(next(campaign.glob("*.zip")), other)
    first = invoke("search", "observation", "--in", campaign, "--in", other, "--limit", "1")
    assert first["cursor"]
    ids = [first["groups"][0]["results"][0]["id"]]
    from sci.storage import catalog

    def unexpected(*a):
        raise AssertionError("unchanged ZIP should not be opened")

    monkeypatch.setattr(catalog, "load_bundle", unexpected)
    token = first["cursor"]
    while token:
        page = invoke("search", "--cursor", token)
        assert not page["errors"]
        ids.extend(r["id"] for r in page["groups"][0]["results"])
        token = page["cursor"]
    assert ids == ["sha256:" + b.id for b in sorted(bundles, key=lambda b: b.id)]
    with pytest.raises(CommandError, match="differ"):
        invoke("search", "different", "--cursor", first["cursor"])
    with pytest.raises(CommandError, match="differ"):
        invoke("search", "--in", other, "--cursor", first["cursor"])


def test_corrupt_cache_rebuild_and_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    bundle = publication(tmp_path / "campaign", "Test")
    home = tmp_path / "home"
    home.mkdir()
    (home / "catalog.sqlite").write_bytes(b"corrupt")
    result = invoke("search", "--in", tmp_path / "campaign", "--in", tmp_path / "missing")
    assert result["exit_code"] == 5
    assert result["groups"][0]["results"][0]["id"] == "sha256:" + bundle.id
    assert (home / "catalog.corrupt").exists()


def test_publish_deduplicates_destinations_and_keeps_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    source = tmp_path / "source"
    bundle = publication(source, "Proof")
    (source / (bundle.id + ".tsr")).write_bytes(b"proof sidecar transported unchanged")
    shutil.copy(source / (bundle.id + ".zip"), source / "duplicate.zip")
    destination = tmp_path / "destination"
    result = invoke("publish", source, "--to", destination, "--to", str(destination) + "/")
    assert len(result["results"]) == 1 and result["exit_code"] == 0
    assert (destination / (bundle.id + ".tsr")).read_bytes() == (
        source / (bundle.id + ".tsr")
    ).read_bytes()
    with pytest.raises(Invalid):
        invoke("publish", source, "--to", "ftp://example.org/archive")


def test_existing_pull_still_requires_expected_signer(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    bundle = publication(tmp_path / "campaign", "Signer")
    dest = tmp_path / "download"
    dest.mkdir()
    for name, data in bundle.entries.items():
        (dest / name).write_bytes(data)
    wrong = tmp_path / "wrong.pem"
    wrong.write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with pytest.raises(Invalid, match="public key mismatch"):
        invoke("pull", "sha256:" + bundle.id, "--output", dest, "--public-key", wrong)


def test_submit_reports_invalid_candidates_without_aborting_valid_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    campaign = tmp_path / "campaign"
    bundle = publication(campaign, "Valid")
    (campaign / "broken.zip").write_bytes(b"broken")
    from sci.commands import submit

    monkeypatch.setattr(submit, "index", lambda *a: {"submit_url": "https://index.example/submit"})
    calls = []

    def request(self, url, method, raw, *a, **kw):
        obj = json.loads(raw)
        calls.append(obj)
        return json.dumps({"id": obj["id"]}).encode()

    monkeypatch.setattr(submit.Network, "request", request)
    result = invoke("submit", campaign, "--archive", "https://archive.example/sci/archive.json")
    assert result["exit_code"] == 5 and len(calls) == 1 and calls[0]["id"] == bundle.id
    assert any(r.get("id") == "sha256:" + bundle.id for r in result["results"])


def test_remote_search_continuation_uses_keyset_and_labels_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    from sci.clients import catalog

    bundle = publication(tmp_path / "campaign", "One")
    rows = [
        {
            "id": c * 64,
            "title": c,
            "abstract": "A",
            "public_key": bundle.manifest["public_key"],
            "accepted_at": "2026-11-07T12:00:00Z",
        }
        for c in ("c", "b", "a")
    ]
    monkeypatch.setattr(
        catalog,
        "index",
        lambda *a: {
            "catalog_schema": "sciscape_catalog_v1",
            "catalog_url": "https://index.example/api/catalog",
        },
    )
    urls = []

    def request(self, url, **kwargs):
        urls.append(url)
        return json.dumps(rows[:2] if len(urls) == 1 else rows[1:3]).encode()

    monkeypatch.setattr(catalog.Network, "request", request)
    first = invoke("search", "--in", "https://index.example", "--limit", "1")
    second = invoke("search", "--cursor", first["cursor"])
    assert first["groups"][0]["verification"] == "catalog_metadata"
    assert second["groups"][0]["results"][0]["id"] == "sha256:" + "b" * 64
    assert "accepted_at.lt." in urls[1] and "id.lt." in urls[1]


def test_read_prefers_loose_metadata_and_labels_verification_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    bundle = publication(tmp_path / "campaign", "Read")
    from sci.clients.reading import Network
    from urllib.parse import urlsplit

    seen = []

    def request(self, url, **kwargs):
        seen.append(url)
        if url.endswith("archive.json"):
            return json.dumps(
                {
                    "schema": "sci-archive/0.1",
                    "entry_url": "https://archive.example/{id}/{+path}",
                    "bundle_url": "https://archive.example/{id}.zip",
                }
            ).encode()
        return bundle.entries[urlsplit(url).path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(Network, "request", request)
    result = invoke(
        "read", "sha256:" + bundle.id, "--archive", "https://archive.example/archive.json"
    )
    assert result["verification"] == "metadata" and result["baseline"] == "not_checked"
    assert not any(u.endswith(".zip") for u in seen)
    assert result["text"] == "Measured observation"
