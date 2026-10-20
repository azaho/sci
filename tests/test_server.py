"""Admission, uploads, finalization recovery, and archive rebuild."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sci import keys
from sci import publication as pub
from sci.cli import encode_multipart
from server import app as appmod
from server.storage import StorageError

from .conftest import make_png, write_folder

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def submit(stack, sci_bytes, manifest_bytes, signature):
    body, ctype = encode_multipart([
        ("sci", "sci.json", sci_bytes, "application/json"),
        ("manifest", "manifest.json", manifest_bytes, "application/json"),
        ("signature", "manifest.json.sig", signature, "application/octet-stream"),
    ])
    status, data, headers = http("POST", f"{stack.api_url}/publications", body, {"Content-Type": ctype})
    assert headers["Content-Type"].startswith("application/json")
    assert headers["X-Content-Type-Options"] == "nosniff"
    return status, json.loads(data)


def submit_folder(stack, folder):
    return submit(stack, *(Path(folder, n).read_bytes() for n in ("sci.json", "manifest.json", "manifest.json.sig")))


def upload_all(folder, result, override=None):
    for name, spec in result["uploads"].items():
        data = (override or {}).get(name, Path(folder, name).read_bytes())
        status, _, _ = http("PUT", spec["url"], data, dict(spec["headers"]))
        assert status == 200, name


def complete(stack, uid, upload_id, mhash):
    status, data, _ = http(
        "POST", f"{stack.api_url}/publications/{uid}/complete",
        json.dumps({"upload_id": upload_id, "manifest_sha256": mhash}).encode(), {"Content-Type": "application/json"},
    )
    return status, json.loads(data)


def publish(stack, folder):
    status, r = submit_folder(stack, folder)
    assert status == 202, r
    upload_all(folder, r)
    return complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])


def signed_folder(tmp_path, key, name="r", **kw):
    folder = write_folder(tmp_path / name, **kw)
    res = pub.sign_folder(folder, key)
    return folder, res


def craft(key, sci_bytes: bytes, *, readme=b"# R\n", license_entry=None, **manifest_overrides):
    """Build and sign a manifest by hand (for correctly signed invalid documents)."""
    files = {
        "sci.json": {"sha256": pub.sha256_hex(sci_bytes), "bytes": len(sci_bytes)},
        "README.md": {"sha256": pub.sha256_hex(readme), "bytes": len(readme)},
        "LICENSE": license_entry or pub.license_entry(),
    }
    doc = {
        "schema_version": 1,
        "uuid": pub.new_uuid4(),
        "public_key": keys.jwk_from_public_key(key.public_key()),
        "created_at": "2026-10-14T18:00:00Z",
        "files": files,
    }
    doc.update(manifest_overrides)
    m = pub.serialize_manifest(doc)
    return sci_bytes, m, key.sign(m)


def objects(stack, uid):
    keys_ = []
    paginator = stack.storage.s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=stack.settings.public_bucket, Prefix=f"publications/{uid}/"):
        keys_ += [o["Key"].rsplit("/", 1)[1] for o in page.get("Contents", [])]
    return sorted(keys_)


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------


def test_whitelist_and_third_party_submission(stack, tmp_path, key):
    folder, res = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    assert status == 403 and r["error"]["code"] == "key_not_enabled"
    stack.enable_key(key)
    stack.disable_key(key)
    status, r = submit_folder(stack, folder)
    assert status == 403
    stack.enable_key(key)
    # "Bob" submits Alice's signed files; Bob needs no identity.
    status, r = publish(stack, folder)
    assert status == 201 and r["status"] == "accepted"
    status, r2 = submit_folder(stack, folder)
    assert status == 200 and r2["accepted_at"] == r["accepted_at"]


def test_rejects_invalid_signature_and_changed_documents(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    sci_b, man_b, sig = (Path(folder, n).read_bytes() for n in ("sci.json", "manifest.json", "manifest.json.sig"))
    bad_sig = bytes([sig[0] ^ 1]) + sig[1:]
    assert submit(stack, sci_b, man_b, bad_sig)[0] == 403
    assert submit(stack, sci_b, man_b, sig + b"\n")[0] == 403
    import base64

    assert submit(stack, sci_b, man_b, base64.b64encode(sig))[0] == 403
    status, r = submit(stack, sci_b.replace(b"Spike", b"Spoke"), man_b, sig)
    assert status == 400 and r["error"]["code"] == "file_mismatch"
    status, r = submit(stack, sci_b, man_b.replace(b'"schema_version": 1', b'"schema_version": 2'), sig)
    assert status == 403  # any manifest change breaks the signature
    # Mixed publications: sci.json from another signed folder.
    other, _ = signed_folder(tmp_path, key, name="o", sci={"title": "Other", "abstract": "B."})
    status, r = submit(stack, (other / "sci.json").read_bytes(), man_b, sig)
    assert status == 400 and r["error"]["code"] == "file_mismatch"


@pytest.mark.parametrize(
    "sci_doc,field",
    [
        ({"title": "x" * 201, "abstract": "A"}, "title"),
        ({"title": "T", "abstract": " 　 "}, "abstract"),
        ({"title": "T", "abstract": "A", "keywords": [""]}, "keywords[0]"),
        ({"title": "T", "abstract": "A", "extra": 1}, "sci.json"),
        ({"title": "T\nX", "abstract": "A"}, "title"),
        ({"title": "T", "abstract": "A", "categories": ["xx.YY"]}, "categories[0]"),
        ({"title": "T", "abstract": "A", "citations": [{"target": "10.1000/182"}]}, "citations[0].target"),
    ],
)
def test_signed_but_schema_invalid_documents_rejected_before_upload(stack, key, sci_doc, field):
    stack.enable_key(key)
    status, r = submit(stack, *craft(key, json.dumps(sci_doc).encode()))
    assert status == 400, r
    assert r["error"]["field"] == field
    assert stack.admin("select count(*) as n from sci.uploads")[0]["n"] == 0


def test_rejects_unapproved_license_and_bad_manifests(stack, key):
    stack.enable_key(key)
    sci_b = b'{"title": "T", "abstract": "A"}'
    status, r = submit(stack, *craft(key, sci_b, license_entry={"sha256": "0" * 64, "bytes": 10}))
    assert status == 400 and r["error"]["code"] == "unapproved_license"
    status, r = submit(stack, *craft(key, sci_b, uuid="b2836a2b-6f5d-1c3f-9e7e-92b19656b627"))
    assert status == 400 and r["error"]["code"] == "invalid_uuid"
    status, r = submit(stack, *craft(key, sci_b, created_at="2026-02-30T00:00:00Z"))
    assert status == 400 and r["error"]["field"] == "manifest.created_at"
    status, r = submit(stack, *craft(key, sci_b, uuid="00000000-0000-4000-8000-000000000000"))
    assert status == 202  # structurally valid UUIDv4 with many zeros
    status, r = submit(stack, *craft(key, sci_b, files_tree={"paths": ["a/b/c"], "truncated": False}))
    assert status == 400 and "deeper" in r["error"]["message"]


def test_size_limits_and_malformed_requests(stack, key):
    stack.enable_key(key)
    big = json.dumps({"title": "T", "abstract": "A", "keywords": ["k"] * 1}).encode() + b" " * 70_000
    status, r = submit(stack, big, b"{}", b"x" * 64)
    assert status == 413
    status, data, _ = http("POST", f"{stack.api_url}/publications", b"x" * 200_000, {"Content-Type": "multipart/form-data; boundary=x"})
    assert status == 413
    status, data, _ = http("POST", f"{stack.api_url}/publications/00000-00001/complete", b"{}", {"Content-Type": "application/json"})
    assert status == 400 and json.loads(data)["error"]["code"] == "invalid_uuid"
    status, data, _ = http("POST", f"{stack.api_url}/publications", b"nope", {"Content-Type": "text/plain"})
    assert status == 400


def test_completion_verifies_uploaded_files(stack, tmp_path, key):
    stack.enable_key(key)
    folder, res = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    assert status == 202
    assert set(r["uploads"]) == {"README.md", "files.zip", "headline_figure.png"}  # no LICENSE URL
    # Missing uploads.
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 400 and c["error"]["code"] == "file_missing"
    # Wrong bytes, same size.
    readme = (folder / "README.md").read_bytes()
    upload_all(folder, r, {"README.md": readme.upper()})
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 400 and c["error"]["code"] == "file_mismatch"
    # Fix the upload; the attempt stays pending and completes.
    upload_all(folder, r)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 201
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 200


def test_completion_identifier_checks(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    other = pub.new_uuid4()
    assert complete(stack, other, r["upload_id"], r["manifest_sha256"])[0] == 409
    assert complete(stack, r["uuid"], pub.new_uuid4(), r["manifest_sha256"])[0] == 404
    assert complete(stack, r["uuid"], r["upload_id"], "0" * 64)[0] == 409


def test_conflicting_uuid(stack, tmp_path, key):
    stack.enable_key(key)
    folder, res = signed_folder(tmp_path, key)
    assert publish(stack, folder)[0] == 201
    other = write_folder(tmp_path / "o", sci={"title": "Different", "abstract": "A"})
    pub.sign_folder(other, key, publication_uuid=res.uuid)
    status, r = submit_folder(stack, other)
    assert status == 409 and r["error"]["code"] == "uuid_conflict"


def test_pending_attempt_reuse_deadline_and_expiry(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    s1, r1 = submit_folder(stack, folder)
    s2, r2 = submit_folder(stack, folder)
    assert s1 == s2 == 202
    assert r1["upload_id"] == r2["upload_id"] and r1["deadline"] == r2["deadline"]
    # A different manifest for the same UUID while pending -> conflict.
    other = write_folder(tmp_path / "o", sci={"title": "Different", "abstract": "A"})
    pub.sign_folder(other, key, publication_uuid=r1["uuid"])
    assert submit_folder(stack, other)[0] == 409
    # Expire the attempt and clean up.
    stack.admin("update sci.uploads set deadline = now() - interval '1 hour' where upload_id = %s", (r1["upload_id"],))
    upload_all(folder, r1)
    status, c = complete(stack, r1["uuid"], r1["upload_id"], r1["manifest_sha256"])
    assert status == 409 and c["error"]["code"] == "upload_expired"
    stats = stack.publisher.run_maintenance_once()
    assert stats["expired"] == 1 and stats["cleaned"] == 1
    assert stack.storage.head(stack.settings.temp_bucket, f"{r1['upload_id']}/README.md") is None
    # Fresh attempt: new ID and prefix; the old completion cannot affect it.
    s3, r3 = submit_folder(stack, folder)
    assert s3 == 202 and r3["upload_id"] != r1["upload_id"]
    assert complete(stack, r1["uuid"], r1["upload_id"], r1["manifest_sha256"])[0] == 409
    upload_all(folder, r3)
    assert complete(stack, r3["uuid"], r3["upload_id"], r3["manifest_sha256"])[0] == 201


def test_disable_key_during_pending_upload(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    stack.disable_key(key)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 403
    assert stack.admin("select count(*) as n from sci.publications")[0]["n"] == 0


def test_concurrent_preparation_and_completion(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    results = []

    def prep():
        results.append(submit_folder(stack, folder))

    threads = [threading.Thread(target=prep) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = {r["upload_id"] for s, r in results}
    assert len(ids) == 1 and all(s == 202 for s, _ in results)
    r = results[0][1]
    upload_all(folder, r)
    outcomes = []

    def comp():
        outcomes.append(complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"]))

    threads = [threading.Thread(target=comp) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    statuses = sorted(s for s, _ in outcomes)
    assert statuses.count(201) == 1, outcomes
    assert all(s in (200, 201, 503) for s in statuses), outcomes
    assert stack.admin("select count(*) as n from sci.publications")[0]["n"] == 1
    assert objects(stack, r["uuid"]).count("acceptance.json") == 1


def test_concurrent_conflicting_manifests(stack, tmp_path, key):
    stack.enable_key(key)
    uid = pub.new_uuid4()
    folders = []
    for i in range(4):
        f = write_folder(tmp_path / f"c{i}", sci={"title": f"Variant {i}", "abstract": "A"})
        pub.sign_folder(f, key, publication_uuid=uid)
        folders.append(f)
    results = []

    def go(f):
        s, r = submit_folder(stack, f)
        if s == 202:
            upload_all(f, r)
            s, r = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
        results.append(s)

    threads = [threading.Thread(target=go, args=(f,)) for f in folders]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(201) == 1 and results.count(409) == 3, results
    assert objects(stack, uid).count("acceptance.json") == 1


def test_rate_limit(database, rustfs, tmp_path, key):
    from .conftest import start_stack

    s, stop = start_stack(database, rustfs, tmp_path, rate_limit_per_minute=2)
    try:
        folder, _ = signed_folder(tmp_path, key)
        codes = [submit_folder(s, folder)[0] for _ in range(4)]
        assert codes[-1] == 429
    finally:
        stop()


def test_writes_disabled(database, rustfs, tmp_path, key):
    from .conftest import start_stack

    s, stop = start_stack(database, rustfs, tmp_path, writes_enabled=False)
    try:
        folder, _ = signed_folder(tmp_path, key)
        status, r = submit_folder(s, folder)
        assert status == 503 and r["error"]["code"] == "writes_disabled"
    finally:
        stop()


def test_storage_limit(database, rustfs, tmp_path, key):
    from .conftest import start_stack

    s, stop = start_stack(database, rustfs, tmp_path, max_storage_bytes=1)
    try:
        folder, _ = signed_folder(tmp_path, key)
        s.enable_key(key)
        status, r = submit_folder(s, folder)
        assert status == 507 and r["error"]["code"] == "storage_full"
    finally:
        stop()


# ---------------------------------------------------------------------------
# finalization recovery
# ---------------------------------------------------------------------------


def _commit_then_crash(stack, folder, monkeypatch, fail_after: int):
    """Run completion but make the copy step fail after N copies."""
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    real = stack.storage.copy_to_public
    calls = {"n": 0}

    def flaky(*a, **kw):
        if calls["n"] >= fail_after:
            raise StorageError("simulated interruption")
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(stack.publisher.storage, "copy_to_public", flaky)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 503 and c["error"]["code"] == "finalization_pending"
    monkeypatch.setattr(stack.publisher.storage, "copy_to_public", real)
    return r


def test_interrupted_finalization_resumes(stack, tmp_path, key, monkeypatch, sci_home):
    from .conftest import run_cli

    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    r = _commit_then_crash(stack, folder, monkeypatch, fail_after=3)
    uid = r["uuid"]
    row = stack.admin("select state, accepted_at from sci.publications where uuid = %s", (uid,))[0]
    assert row["state"] == "finalizing"
    assert "acceptance.json" not in objects(stack, uid) and len(objects(stack, uid)) == 3
    # Partially copied directories fail pull.
    assert run_cli("pull", uid, "--server", stack.instance_url, "--output", str(tmp_path / "p"))[0] == 1
    assert not (tmp_path / "p").exists()
    # Cleanup must keep the temporary sources while copies are unfinished.
    real = stack.storage.copy_to_public

    def broken(*a, **kw):
        raise StorageError("still unavailable")

    monkeypatch.setattr(stack.publisher.storage, "copy_to_public", broken)
    stack.admin("update sci.uploads set deadline = now() - interval '1 day' where upload_id = %s", (r["upload_id"],))
    stats = stack.publisher.run_maintenance_once()
    assert stats["resumed"] == 0 and stats["cleaned"] == 0
    assert stack.storage.head(stack.settings.temp_bucket, f"{r['upload_id']}/README.md") is not None
    assert stack.admin("select state from sci.publications where uuid = %s", (uid,))[0]["state"] == "finalizing"
    monkeypatch.setattr(stack.publisher.storage, "copy_to_public", real)
    # Disabling the key does not stop a committed finalization.
    stack.disable_key(key)
    stats = stack.publisher.run_maintenance_once()
    assert stats["resumed"] == 1
    row2 = stack.admin("select state, accepted_at from sci.publications where uuid = %s", (uid,))[0]
    assert row2["state"] == "accepted" and row2["accepted_at"] == row["accepted_at"]
    assert "acceptance.json" in objects(stack, uid)
    assert run_cli("pull", uid, "--server", stack.instance_url, "--output", str(tmp_path / "p"))[0] == 0
    # Temp sources are cleaned only after acceptance.
    stack.publisher.run_maintenance_once()
    assert stack.storage.head(stack.settings.temp_bucket, f"{r['upload_id']}/README.md") is None


def test_missing_source_fails_terminally(stack, tmp_path, key, monkeypatch):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    r = _commit_then_crash(stack, folder, monkeypatch, fail_after=2)
    # The README source (copied later) disappears.
    stack.storage.s3.delete_object(Bucket=stack.settings.temp_bucket, Key=f"{r['upload_id']}/README.md")
    stack.publisher.run_maintenance_once()
    row = stack.admin("select state, failure_reason from sci.publications where uuid = %s", (r["uuid"],))[0]
    assert row["state"] == "failed" and "README.md" in row["failure_reason"]
    assert "acceptance.json" not in objects(stack, r["uuid"])
    status, c = submit_folder(stack, folder)
    assert status == 409 and c["error"]["code"] == "finalization_failed" and "uploads" not in c
    assert complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])[1]["error"]["code"] == "finalization_failed"
    # A newly signed publication proceeds normally.
    pub.sign_folder(folder, key)
    assert publish(stack, folder)[0] == 201


def test_changed_source_etag_fails_terminally(stack, tmp_path, key, monkeypatch):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    r = _commit_then_crash(stack, folder, monkeypatch, fail_after=1)
    # Replace a not-yet-copied source with different bytes (different ETag).
    stack.storage.s3.put_object(Bucket=stack.settings.temp_bucket, Key=f"{r['upload_id']}/README.md", Body=b"tampered")
    stack.publisher.run_maintenance_once()
    row = stack.admin("select state, failure_reason from sci.publications where uuid = %s", (r["uuid"],))[0]
    assert row["state"] == "failed" and "changed" in row["failure_reason"]


def test_missing_source_after_destination_verified_is_fine(stack, tmp_path, key, monkeypatch):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    r = _commit_then_crash(stack, folder, monkeypatch, fail_after=3)
    # sci.json, manifest.json, and the signature are already copied.
    for n in ("sci.json", "manifest.json", "manifest.json.sig"):
        stack.storage.s3.delete_object(Bucket=stack.settings.temp_bucket, Key=f"{r['upload_id']}/{n}")
    stack.publisher.run_maintenance_once()
    assert stack.admin("select state from sci.publications where uuid = %s", (r["uuid"],))[0]["state"] == "accepted"


def test_uncertain_acceptance_write_and_durable_record(stack, tmp_path, key, monkeypatch, sci_home):
    from .conftest import run_cli

    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    real_put = stack.storage.put_public_if_absent
    real_mark = appmod.Publisher._mark_accepted

    def put_then_fail(*a, **kw):
        real_put(*a, **kw)
        raise StorageError("timeout after write")

    monkeypatch.setattr(stack.publisher.storage, "put_public_if_absent", put_then_fail)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 503
    monkeypatch.setattr(stack.publisher.storage, "put_public_if_absent", real_put)
    # The record is durable while SQL still says finalizing: direct pull works,
    # the catalog does not show it yet.
    assert stack.admin("select state from sci.publications where uuid = %s", (r["uuid"],))[0]["state"] == "finalizing"
    assert run_cli("pull", r["uuid"], "--server", stack.instance_url, "--output", str(tmp_path / "p"))[0] == 0
    # Retry: the matching record leads to acceptance, never failure.
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 201
    assert objects(stack, r["uuid"]).count("acceptance.json") == 1


def test_conflicting_destination_is_never_overwritten(stack, tmp_path, key, monkeypatch):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    r = _commit_then_crash(stack, folder, monkeypatch, fail_after=0)
    dest = f"publications/{r['uuid']}/README.md"
    stack.storage.s3.put_object(Bucket=stack.settings.public_bucket, Key=dest, Body=b"conflict")
    stats = stack.publisher.run_maintenance_once()
    assert stats["resumed"] == 0
    got = stack.storage.s3.get_object(Bucket=stack.settings.public_bucket, Key=dest)["Body"].read()
    assert got == b"conflict"
    assert stack.admin("select state from sci.publications where uuid = %s", (r["uuid"],))[0]["state"] == "finalizing"


# ---------------------------------------------------------------------------
# archive recovery
# ---------------------------------------------------------------------------


def test_rebuild_catalog_after_sql_loss(stack, tmp_path, key, pg_cluster):
    import psycopg

    from server.database import migrate

    from .conftest import PUBLISHER_PASSWORD, admin_dsn

    stack.enable_key(key)
    uids = []
    for i in range(3):
        f, _ = signed_folder(tmp_path, key, name=f"p{i}", sci={**json.loads(json.dumps({"title": f"P{i}", "abstract": "A", "citations": [{"target": "doi:10.1000/ABC", "relation": "supports", "reason": "r"}]}))})
        assert publish(stack, f)[0] == 201
        uids.append(json.loads((f / "manifest.json").read_bytes())["uuid"])
    # An orphan prefix without an acceptance record is ignored.
    orphan = pub.new_uuid4()
    stack.storage.s3.put_object(Bucket=stack.settings.public_bucket, Key=f"publications/{orphan}/manifest.json", Body=b"{}")
    before = stack.admin("select uuid, manifest_sha256, title, accepted_at, created_at, files_tree, author_fingerprint from sci.publications order by uuid")
    cites_before = stack.admin("select source_uuid, position, target, target_key, relation, reason from sci.citations order by 1, 2")

    with psycopg.connect(admin_dsn(pg_cluster), autocommit=True) as c:
        c.execute('create database "rebuilt"')
    dsn = admin_dsn(pg_cluster, "rebuilt")
    migrate(dsn, publisher_password=PUBLISHER_PASSWORD)
    out = io.StringIO()
    problems = appmod.rebuild_catalog(stack.settings, dsn, out=out)
    assert problems == 0, out.getvalue()
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        after = c.execute("select uuid, manifest_sha256, title, accepted_at, created_at, files_tree, author_fingerprint from sci.publications order by uuid").fetchall()
        cites_after = c.execute("select source_uuid, position, target, target_key, relation, reason from sci.citations order by 1, 2").fetchall()
        idents = c.execute("select enabled from sci.identities").fetchall()
        states = {r["state"] for r in c.execute("select state from sci.publications").fetchall()}
    assert after == before and cites_after == cites_before
    assert idents == [{"enabled": False}]  # whitelist membership is never inferred
    assert states == {"accepted"}
    assert orphan not in out.getvalue()
    # A second run changes nothing.
    assert appmod.rebuild_catalog(stack.settings, dsn, out=io.StringIO()) == 0


def test_rebuild_never_reopens_failed_uuid(stack, tmp_path, key, monkeypatch):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    assert publish(stack, folder)[0] == 201
    uid = json.loads((folder / "manifest.json").read_bytes())["uuid"]
    stack.admin("update sci.publications set state = 'failed', failure_reason = 'test' where uuid = %s", (uid,))
    out = io.StringIO()
    assert appmod.rebuild_catalog(stack.settings, stack.database["admin"], out=out) == 1
    assert "CONFLICT" in out.getvalue()
    assert stack.admin("select state from sci.publications where uuid = %s", (uid,))[0]["state"] == "failed"


def test_server_never_parses_zip_archives():
    src = "\n".join(p.read_text() for p in Path(appmod.__file__).parent.glob("*.py"))
    assert "zipfile" not in src and "ZipFile" not in src and "infolist" not in src


def test_hostile_unsigned_manifest_is_rejected_not_500(stack):
    for manifest in (b'{"x":' + b"1" * 5000 + b"}", b'{"x":' + b"[" * 3000 + b"]" * 3000 + b"}"):
        status, r = submit(stack, b'{"title":"T","abstract":"A"}', manifest, b"x" * 64)
        assert status == 400, r


def test_wrong_upload_id_for_committed_publication(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    assert complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])[0] == 201
    status, c = complete(stack, r["uuid"], pub.new_uuid4(), r["manifest_sha256"])
    assert status == 409 and c["error"]["code"] == "upload_mismatch"


def test_attempt_expiring_during_verification_is_not_committed(stack, tmp_path, key, monkeypatch):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    status, r = submit_folder(stack, folder)
    upload_all(folder, r)
    real = stack.publisher._verify_upload

    def slow(up):
        out = real(up)
        stack.admin("update sci.uploads set deadline = now() - interval '1 second' where upload_id = %s", (r["upload_id"],))
        return out

    monkeypatch.setattr(stack.publisher, "_verify_upload", slow)
    status, c = complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])
    assert status == 409 and c["error"]["code"] == "upload_expired"
    assert stack.admin("select count(*) as n from sci.publications")[0]["n"] == 0


def test_reused_attempt_does_not_rewrite_server_objects(stack, tmp_path, key):
    stack.enable_key(key)
    folder, _ = signed_folder(tmp_path, key)
    s1, r1 = submit_folder(stack, folder)
    etag = stack.storage.head(stack.settings.temp_bucket, f"{r1['upload_id']}/manifest.json").etag
    stack.storage.s3.delete_object(Bucket=stack.settings.temp_bucket, Key=f"{r1['upload_id']}/sci.json")
    s2, r2 = submit_folder(stack, folder)
    assert r2["upload_id"] == r1["upload_id"]
    assert stack.storage.head(stack.settings.temp_bucket, f"{r1['upload_id']}/sci.json") is not None
    assert stack.storage.head(stack.settings.temp_bucket, f"{r1['upload_id']}/manifest.json").etag == etag
