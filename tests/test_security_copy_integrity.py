"""SCI-SEC-003: an MD5 ETag collision must not change an accepted archive.

Finalization copies temporary objects with CopySourceIfMatch on the ETag seen
during verification. For single-part uploads that ETag is the MD5 of the
bytes, so a colliding file can replace the verified source and still pass the
condition. Finalization must hash the archived copy before acceptance.
"""

from __future__ import annotations

import hashlib
import pytest

from sci import publication as pub

from .conftest import FIXTURES, write_folder
from .test_server import complete, objects, submit_folder, upload_all

PAIR = FIXTURES / "md5-collision"


def _signed_with_zip(tmp_path, key, zip_bytes):
    folder = write_folder(tmp_path / "r", files={})
    (folder / "files.zip").write_bytes(zip_bytes)
    pub.sign_folder(folder, key)
    return folder


def test_fixture_is_an_md5_collision():
    a, b = (PAIR / "md5-1.zip").read_bytes(), (PAIR / "md5-2.zip").read_bytes()
    assert a != b and hashlib.md5(a).digest() == hashlib.md5(b).digest()


def test_colliding_source_swap_is_rejected_before_acceptance(stack, tmp_path, key, monkeypatch):
    good, evil = (PAIR / "md5-1.zip").read_bytes(), (PAIR / "md5-2.zip").read_bytes()
    stack.enable_key(key)
    folder = _signed_with_zip(tmp_path, key, good)
    status, r = submit_folder(stack, folder)
    assert status == 202
    upload_all(folder, r)
    temp_key = f"{r['upload_id']}/files.zip"
    s3, bucket = stack.storage.s3, stack.settings.temp_bucket
    verified_etag = stack.storage.head(bucket, temp_key).etag

    real_verify = stack.publisher._verify_upload

    def verify_then_swap(up):
        result = real_verify(up)
        # The uploader still holds a live presigned URL and replaces the file.
        s3.put_object(Bucket=bucket, Key=temp_key, Body=evil, ContentType="application/zip")
        return result

    monkeypatch.setattr(stack.publisher, "_verify_upload", verify_then_swap)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    swapped_etag = stack.storage.head(bucket, temp_key).etag
    if swapped_etag != verified_etag:
        pytest.skip("this storage does not use MD5 ETags; the collision cannot pass CopySourceIfMatch")

    assert status == 409 and c["error"]["code"] == "finalization_failed", c
    row = stack.admin("select state, failure_reason from sci.publications where uuid = %s", (r["uuid"],))[0]
    assert row["state"] == "failed" and "files.zip" in row["failure_reason"]
    assert "acceptance.json" not in objects(stack, r["uuid"])
    # The UUID stays reserved: retries return the recorded failure.
    status, c = submit_folder(stack, folder)
    assert status == 409 and c["error"]["code"] == "finalization_failed"


def test_any_archived_mismatch_is_rejected(stack, tmp_path, key, monkeypatch):
    """Provider-independent: a copy that yields wrong bytes is never accepted."""
    stack.enable_key(key)
    folder = write_folder(tmp_path / "r")
    pub.sign_folder(folder, key)
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    real_copy = stack.storage.copy_to_public

    def corrupting_copy(temp_key, uid, name, etag):
        real_copy(temp_key, uid, name, etag)
        if name == pub.README:
            dest = stack.storage.public_key(uid, name)
            stack.storage.s3.put_object(Bucket=stack.settings.public_bucket, Key=dest, Body=b"# not the signed text\n")

    monkeypatch.setattr(stack.publisher.storage, "copy_to_public", corrupting_copy)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 409 and c["error"]["code"] == "finalization_failed"
    assert "acceptance.json" not in objects(stack, r["uuid"])
