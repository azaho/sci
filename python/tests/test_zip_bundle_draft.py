from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path

import pytest
from conftest import CORPUS, make_draft
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci.baseline import BASELINE_LICENSE_SHA256, baseline_license_bytes, evaluate_baseline
from pysci.bundle import (
    Scope,
    VerificationLimits,
    extract_verified,
    read_zip_entry,
    verify_directory,
    verify_file,
    verify_loose,
    verify_path,
    verify_zip,
    write_bundle_zip,
)
from pysci.draft import BaselineRefused, check_draft, sign_draft
from pysci.errors import Invalid, Outcome
from pysci.source import MemorySource
from pysci.zipformat import canonical_zip_size, parse_zip_layout, write_canonical_zip


def signed_zip(private_key: Ed25519PrivateKey, draft: dict[str, bytes]) -> tuple[bytes, str]:
    signed = sign_draft(draft, private_key)
    sink = io.BytesIO()
    size = write_bundle_zip(sink, signed.manifest_bytes, signed.signature, draft)
    data = sink.getvalue()
    assert size == len(data)
    return data, signed.publication_id


def test_baseline_license_is_packaged_exactly() -> None:
    data = baseline_license_bytes()
    assert hashlib.sha256(data).hexdigest() == BASELINE_LICENSE_SHA256
    assert data == (CORPUS.parents[1] / "sci-baseline-LICENSE.txt").read_bytes()


def test_sign_write_verify_roundtrip(
    private_key: Ed25519PrivateKey, draft: dict[str, bytes]
) -> None:
    data, publication_id = signed_zip(private_key, draft)
    result = verify_zip(data, expected_id=publication_id)
    assert result.outcome is Outcome.VALID and result.baseline is True
    assert result.zip_bytes == len(data)
    assert result.zip_sha256 == hashlib.sha256(data).hexdigest()
    assert result.metadata is not None and result.metadata.title == "A test publication"
    # Python's zipfile agrees with the canonical writer.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist()[:2] == ["manifest.json", "manifest.json.sig"]
        assert archive.namelist()[2:] == sorted(draft, key=lambda p: p.encode())
        assert archive.testzip() is None
    # Loose verification gives the same canonical ZIP hash.
    signed = sign_draft(draft, private_key)
    loose = verify_loose(
        {
            "manifest.json": signed.manifest_bytes,
            "manifest.json.sig": signed.signature,
            "manifest.json.sig.tsr": b"ignored",
            **draft,
        },
        compute_zip_sha256=True,
    )
    assert loose.ok and loose.zip_sha256 == result.zip_sha256


def test_canonical_zip_size_formula(
    private_key: Ed25519PrivateKey, draft: dict[str, bytes]
) -> None:
    data, _ = signed_zip(private_key, draft)
    signed = sign_draft(draft, private_key)
    sizes = [("manifest.json", len(signed.manifest_bytes)), ("manifest.json.sig", 64)]
    sizes += [(p, len(b)) for p, b in draft.items()]
    assert canonical_zip_size(sizes) == len(data)


def test_zip_layout_rejections(private_key: Ed25519PrivateKey, draft: dict[str, bytes]) -> None:
    data, _ = signed_zip(private_key, draft)
    assert len(parse_zip_layout(MemorySource(data))) == len(draft) + 2

    def outcome(mutated: bytes) -> tuple[Outcome, str]:
        result = verify_zip(mutated)
        return result.outcome, result.issues[0].code

    assert outcome(b"x" + data)[0] is Outcome.INVALID
    assert outcome(data + b"\0") == (Outcome.INVALID, "zip_end_record")
    assert outcome(data[:-1])[0] is Outcome.INVALID
    assert outcome(b"")[1] == "zip_truncated"
    # Method field of the first local header (offset 8).
    assert outcome(data[:8] + b"\x08\x00" + data[10:]) == (Outcome.INVALID, "zip_method")
    # Corrupt one README byte without fixing the CRC.
    index = data.index(b"# Hello")
    assert outcome(data[:index] + b"$" + data[index + 1 :]) == (Outcome.INVALID, "zip_crc")
    # ZIP64 sentinel in the end record's entry count.
    eocd = len(data) - 22
    mutated = data[: eocd + 8] + struct.pack("<HH", 0xFFFF, 0xFFFF) + data[eocd + 12 :]
    assert outcome(mutated) == (Outcome.INVALID, "zip64")


def test_limits_report_incomplete(private_key: Ed25519PrivateKey, draft: dict[str, bytes]) -> None:
    data, _ = signed_zip(private_key, draft)
    result = verify_zip(data, limits=VerificationLimits(max_input_bytes=10))
    assert result.outcome is Outcome.INCOMPLETE and result.baseline is None
    result = verify_zip(data, limits=VerificationLimits(max_manifest_bytes=10))
    assert result.outcome is Outcome.INCOMPLETE


def test_manifest_scope(private_key: Ed25519PrivateKey, draft: dict[str, bytes]) -> None:
    signed = sign_draft(draft, private_key)
    result = verify_loose(
        {"manifest.json": signed.manifest_bytes, "manifest.json.sig": signed.signature},
        scope=Scope.MANIFEST,
    )
    assert result.ok and result.baseline is None and result.within_baseline_zip_limit
    complete = verify_loose(
        {"manifest.json": signed.manifest_bytes, "manifest.json.sig": signed.signature}
    )
    assert complete.outcome is Outcome.INVALID
    assert complete.issues[0].code == "bundle_missing_file"


def test_verify_file(private_key: Ed25519PrivateKey, draft: dict[str, bytes]) -> None:
    signed = sign_draft(draft, private_key)
    manifest = signed.verification.manifest
    assert manifest is not None
    verify_file(manifest, "README.md", draft["README.md"])
    with pytest.raises(Invalid):
        verify_file(manifest, "README.md", draft["README.md"] + b"x")
    with pytest.raises(Invalid):
        verify_file(manifest, "files/none", b"")


def test_directory_roundtrip_and_extraction(
    tmp_path: Path, private_key: Ed25519PrivateKey, draft: dict[str, bytes]
) -> None:
    source_dir = tmp_path / "draft"
    for path, data in draft.items():
        (source_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (source_dir / path).write_bytes(data)
    report = check_draft(source_dir)
    assert report.valid and report.baseline is not None and report.baseline.eligible
    signed = sign_draft(source_dir, private_key)
    (source_dir / "manifest.json").write_bytes(signed.manifest_bytes)
    (source_dir / "manifest.json.sig").write_bytes(signed.signature)
    assert verify_directory(source_dir).ok
    assert check_draft(source_dir).signed
    zip_path = tmp_path / "renamed.bin"
    with open(zip_path, "wb") as handle:
        write_bundle_zip(handle, signed.manifest_bytes, signed.signature, draft)
    assert verify_path(zip_path, expected_id=signed.publication_id).ok
    out = tmp_path / "out"
    assert extract_verified(zip_path, out).ok
    assert (out / "files/data/table.csv").read_bytes() == draft["files/data/table.csv"]
    (source_dir / "files" / "extra.txt").write_bytes(b"x")
    assert verify_directory(source_dir).issues[0].code == "bundle_extra_file"
    (source_dir / "files" / "link").symlink_to(source_dir / "README.md")
    assert verify_directory(source_dir).issues[0].code == "not_regular_file"


def test_draft_reports_all_issues() -> None:
    bad = make_draft(
        **{
            "sci.json": b'{"title": "", "abstract": 1}',
            "README.md": b" ",
            "notes.txt": b"x",
            "files__key.pem": b"-----BEGIN PRIVATE KEY-----\nabc\n",
        }
    )
    del bad["LICENSE"]
    report = check_draft(bad)
    codes = {issue.code for issue in report.issues}
    assert {
        "manifest_path_location",
        "private_key_material",
        "text_blank",
        "metadata_type",
    } <= codes
    assert report.baseline is not None and not report.baseline.eligible


def test_sign_refuses_nonbaseline_unless_allowed(private_key: Ed25519PrivateKey) -> None:
    draft = make_draft()
    del draft["LICENSE"]
    with pytest.raises(BaselineRefused) as info:
        sign_draft(draft, private_key)
    assert info.value.report.reasons[0].code == "baseline_license_missing"
    signed = sign_draft(draft, private_key, allow_nonbaseline=True)
    assert signed.verification.ok and signed.verification.baseline is False


def test_sign_rejects_invalid_draft(private_key: Ed25519PrivateKey) -> None:
    with pytest.raises(Invalid):
        sign_draft(make_draft(**{"README.md": b"\n"}), private_key)


def test_evaluate_baseline_limits() -> None:
    report = evaluate_baseline(
        zip_bytes=64 * 2**20 + 1,
        file_count=1001,
        display_items=4,
        manifest_bytes=262145,
        metadata_bytes=65537,
        readme_bytes=2**20 + 1,
        license_sha256="0" * 64,
    )
    assert not report.eligible and len(report.reasons) == 7


def test_writer_matches_independent_reader() -> None:
    entries = [("manifest.json", MemorySource(b"{}")), ("manifest.json.sig", MemorySource(b"s"))]
    sink = io.BytesIO()
    write_canonical_zip(sink, entries)
    with zipfile.ZipFile(sink) as archive:
        assert archive.read("manifest.json") == b"{}"
        info = archive.getinfo("manifest.json")
        assert info.date_time == (1980, 1, 1, 0, 0, 0) and info.create_version == 10


def test_read_zip_entry(private_key: Ed25519PrivateKey, draft: dict[str, bytes]) -> None:
    data, publication_id = signed_zip(private_key, draft)
    manifest, body = read_zip_entry(data, "README.md", expected_id=publication_id)
    assert body == draft["README.md"] and manifest.publication_id == publication_id
    with pytest.raises(Invalid):
        read_zip_entry(data, "files/absent")
