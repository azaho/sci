import concurrent.futures
import copy
import dataclasses

import psycopg
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient

from sci.errors import SCIError
from sci.keys import load_private
from sci.publication import verify_documents
from sci.validation import compact
from server.app import create_app
from server.database import Database
from server.maintenance import rebuild_catalog
from server.storage import ChangedSource
from tests.conftest import accept, enable, upload

pytestmark = pytest.mark.integration


def row(admin, uuid):
    return admin.execute("SELECT * FROM sci_private.publications WHERE uuid=%s", (uuid,)).fetchone()


def test_complete_retry_catalog_and_opaque_zip(stack, make_publication, monkeypatch):
    publisher, admin = stack
    folder, pub = make_publication(attachments=True)
    import zipfile
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("server opened a ZIP"))
    status, outcome = accept(publisher, admin, folder, pub)
    assert status == 201 and outcome["status"] == "accepted"
    assert row(admin, pub.uuid)["state"] == "accepted"
    assert publisher.prepare(pub)[0] == 200
    assert admin.execute("SELECT count(*) AS n FROM sci_private.uploads").fetchone()["n"] == 1
    for name in pub.descriptors:
        data, _ = publisher.storage.read(publisher.storage.archive, f"publications/{pub.uuid}/{name}", 100_000)
        assert data == (folder / name).read_bytes()
    count = admin.execute("SELECT citing_publications FROM catalog_v1.citation_counts WHERE target_key='doi:10.1000/test'").fetchone()
    assert count["citing_publications"] == 1
    assert admin.execute("SELECT relation FROM catalog_v1.citations WHERE position=0").fetchone()["relation"] is None
    admin.execute("SELECT sci_private.disable_key(%s)", (pub.manifest["public_key"]["x"],))
    assert publisher.prepare(pub)[0] == 200


def test_whitelist_pending_disable_and_fresh_attempt(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    with pytest.raises(SCIError) as denied:
        publisher.prepare(pub)
    assert denied.value.status == 403
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    original = publisher.prepare(pub)[1]
    assert original["upload_id"] == attempt["upload_id"]
    assert original["deadline"] == attempt["deadline"]
    admin.execute("SELECT sci_private.disable_key(%s)", (pub.manifest["public_key"]["x"],))
    with pytest.raises(SCIError, match="not enabled"):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert row(admin, pub.uuid) is None
    enable(admin, pub)
    replacement = upload(publisher, folder, pub)
    assert replacement["upload_id"] != attempt["upload_id"]
    with pytest.raises(SCIError):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert publisher.complete(pub.uuid, replacement["upload_id"], pub.manifest_sha256)[0] == 201


def test_expiration_and_identifiers(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    for uuid, upload_id, digest in [
        (pub.uuid, "00000000-0000-4000-8000-000000000000", pub.manifest_sha256),
        ("00000000-0000-4000-8000-000000000000", attempt["upload_id"], pub.manifest_sha256),
        (pub.uuid, attempt["upload_id"], "0"*64),
    ]:
        with pytest.raises(SCIError):
            publisher.complete(uuid, upload_id, digest)
    admin.execute("UPDATE sci_private.uploads SET deadline=clock_timestamp()-interval '1 second'")
    with pytest.raises(SCIError, match="expired"):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert publisher.prepare(pub)[1]["upload_id"] != attempt["upload_id"]


def test_concurrent_prepare_complete_and_conflict(stack, make_publication, author):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        attempts = list(pool.map(lambda _: publisher.prepare(pub)[1], range(5)))
    assert len({a["upload_id"] for a in attempts}) == 1
    attempt = upload(publisher, folder, pub)
    other = copy.deepcopy(pub.manifest)
    other["created_at"] = "2001-01-01T00:00:00Z"
    data = compact(other)
    conflict = verify_documents(data, pub.sci_bytes, load_private(author).sign(data))
    with pytest.raises(SCIError) as exc:
        publisher.prepare(conflict)
    assert exc.value.code == "uuid_conflict"
    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        outcomes = list(pool.map(
            lambda _: publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256), range(5)
        ))
    assert sorted(s for s, _ in outcomes) == [200,200,200,200,201]
    assert len({v["accepted_at"] for _, v in outcomes}) == 1
    with pytest.raises(SCIError):
        publisher.prepare(conflict)


@pytest.mark.parametrize("interrupt_after", [0, 1, 3, 6])
def test_interrupted_copy_recovery_preserves_time(stack, make_publication, monkeypatch, interrupt_after):
    publisher, admin = stack
    folder, pub = make_publication(attachments=True)
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    original = publisher.storage.copy_verified
    count = 0
    def interrupted(*args):
        nonlocal count
        if count == interrupt_after:
            raise EndpointConnectionError(endpoint_url="test")
        count += 1
        return original(*args)
    monkeypatch.setattr(publisher.storage, "copy_verified", interrupted)
    with pytest.raises(EndpointConnectionError):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    before = row(admin, pub.uuid)
    assert before["state"] == "finalizing"
    assert not publisher.storage.head(publisher.storage.archive, f"publications/{pub.uuid}/acceptance.json")
    admin.execute("SELECT sci_private.disable_key(%s)", (pub.manifest["public_key"]["x"],))
    admin.execute("UPDATE sci_private.uploads SET deadline=clock_timestamp()-interval '1 day'")
    publisher.cleanup()
    assert publisher.storage.head(publisher.storage.temporary, f"{attempt['upload_id']}/README.md")
    monkeypatch.setattr(publisher.storage, "copy_verified", original)
    assert publisher.recover() == []
    after = row(admin, pub.uuid)
    assert after["state"] == "accepted" and after["accepted_at"] == before["accepted_at"]


@pytest.mark.parametrize("change", ["delete", "etag"])
def test_missing_or_changed_source_is_terminal(stack, make_publication, monkeypatch, change):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    original = publisher.storage.copy_verified
    monkeypatch.setattr(publisher.storage, "copy_verified", lambda *a: (_ for _ in ()).throw(
        EndpointConnectionError(endpoint_url="test")))
    with pytest.raises(EndpointConnectionError):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    if change == "delete":
        publisher.storage.client.delete_object(Bucket=publisher.storage.temporary,
                                               Key=f"{attempt['upload_id']}/README.md")
        monkeypatch.setattr(publisher.storage, "copy_verified", original)
    else:
        monkeypatch.setattr(publisher.storage, "copy_verified",
                            lambda *a: (_ for _ in ()).throw(ChangedSource("changed ETag")))
    with pytest.raises(SCIError) as exc:
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert exc.value.code == "finalization_failed"
    assert row(admin, pub.uuid)["state"] == "failed"
    assert publisher.recover() == []
    publisher.cleanup()
    assert publisher.storage.head(publisher.storage.temporary, f"{attempt['upload_id']}/manifest.json")
    for call in [lambda: publisher.prepare(pub),
                 lambda: publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)]:
        with pytest.raises(SCIError) as retry:
            call()
        assert retry.value.code == "finalization_failed"
    assert not publisher.storage.head(publisher.storage.archive, f"publications/{pub.uuid}/acceptance.json")
    assert admin.execute("SELECT count(*) AS n FROM catalog_v1.publications").fetchone()["n"] == 0


def test_uncertain_acceptance_write_finishes_even_without_sources(stack, make_publication, monkeypatch):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    original = publisher.storage.put
    def uncertain(bucket, key, *args, **kwargs):
        result = original(bucket, key, *args, **kwargs)
        if key.endswith("acceptance.json"):
            raise EndpointConnectionError(endpoint_url="test")
        return result
    monkeypatch.setattr(publisher.storage, "put", uncertain)
    with pytest.raises(EndpointConnectionError):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert row(admin, pub.uuid)["state"] == "finalizing"
    publisher.storage.delete_temporary(attempt["upload_id"])
    monkeypatch.setattr(publisher.storage, "put", original)
    assert publisher.recover() == []
    assert row(admin, pub.uuid)["state"] == "accepted"


def test_conflicting_destination_is_not_overwritten(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    key = f"publications/{pub.uuid}/README.md"
    publisher.storage.put(publisher.storage.archive, key, b"conflict", "README.md")
    with pytest.raises(SCIError) as exc:
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert exc.value.code == "archive_conflict"
    assert publisher.storage.read(publisher.storage.archive, key, 100)[0] == b"conflict"
    assert row(admin, pub.uuid)["state"] == "finalizing"


def test_sql_permissions_and_admin_helpers(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    accept(publisher, admin, folder, pub)
    with publisher.database.pool.connection() as conn:
        for statement in [
            "UPDATE sci_private.identities SET enabled=false",
            "SELECT * FROM sci_private.list_keys()",
            "UPDATE sci_private.instance_state SET writes_enabled=true",
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
    admin.execute("SET ROLE sci_anon")
    try:
        assert admin.execute("SELECT count(*) AS n FROM catalog_v1.publications").fetchone()["n"] == 1
        assert admin.execute("SELECT count(*) AS n FROM catalog_v1.citations").fetchone()["n"] == 3
        assert admin.execute("SELECT count(*) AS n FROM catalog_v1.citation_counts").fetchone()["n"] == 2
        for statement in [
            "SELECT * FROM sci_private.identities", "SELECT * FROM sci_private.uploads",
            "SELECT failure_reason FROM sci_private.publications",
            "DELETE FROM catalog_v1.publications", "SELECT * FROM sci_private.list_keys()",
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                admin.execute(statement)
    finally:
        admin.execute("RESET ROLE")


def test_archive_rebuild_keeps_writes_off_and_keys_disabled(stack, make_publication, settings, monkeypatch):
    publisher, admin = stack
    folder, pub = make_publication(attachments=True)
    accept(publisher, admin, folder, pub)
    old = row(admin, pub.uuid)
    admin.execute("TRUNCATE sci_private.citations,sci_private.publications,sci_private.uploads,sci_private.identities CASCADE")
    admin_db = Database(admin.info.dsn)
    import zipfile
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("recovery opened ZIP"))
    try:
        assert rebuild_catalog(admin_db, publisher.storage, settings) == 1
    finally:
        admin_db.close()
    new = row(admin, pub.uuid)
    assert new["accepted_at"] == old["accepted_at"]
    assert new["files_tree"] == old["files_tree"]
    assert new["state"] == "accepted"
    assert not admin.execute("SELECT enabled FROM sci_private.identities").fetchone()["enabled"]
    assert not admin.execute("SELECT writes_enabled FROM sci_private.instance_state").fetchone()["writes_enabled"]


def test_api_status_limits_and_preserved_parts(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    app = create_app(publisher.settings, publisher.database, publisher.storage, background=False)
    with TestClient(app) as client:
        response = client.post("/publications", files={
            "sci": ("sci.json", pub.sci_bytes, "application/json"),
            "manifest": ("manifest.json", pub.manifest_bytes, "application/json"),
            "signature": ("manifest.json.sig", pub.signature, "application/octet-stream"),
        })
        assert response.status_code == 202, response.text
        assert response.headers["x-content-type-options"] == "nosniff"
        stored, _ = publisher.storage.read(
            publisher.storage.temporary, response.json()["upload_id"] + "/sci.json", 65536)
        assert stored == pub.sci_bytes
        too_large = client.post("/publications", files={
            "sci": ("sci.json", b" " * 65537), "manifest": ("manifest.json", pub.manifest_bytes),
            "signature": ("manifest.json.sig", pub.signature, "application/octet-stream"),
        })
        assert too_large.status_code == 413
        invalid = client.post("/publications/not-a-uuid/complete", json={})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_input"
        assert client.get("/health").status_code == 200


def test_preparation_resumes_and_keeps_deadline(stack, make_publication, monkeypatch):
    publisher, admin = stack
    _, pub = make_publication()
    enable(admin, pub)
    original = publisher.storage.write_original
    def interrupted(key, data, name):
        if name == "LICENSE":
            raise EndpointConnectionError(endpoint_url="test")
        return original(key, data, name)
    monkeypatch.setattr(publisher.storage, "write_original", interrupted)
    with pytest.raises(EndpointConnectionError):
        publisher.prepare(pub)
    old = admin.execute("SELECT * FROM sci_private.uploads").fetchone()
    assert old["state"] == "preparing"
    monkeypatch.setattr(publisher.storage, "write_original", original)
    result = publisher.prepare(pub)[1]
    assert result["upload_id"] == str(old["upload_id"])
    assert admin.execute("SELECT deadline FROM sci_private.uploads").fetchone()["deadline"] == old["deadline"]


def test_changed_upload_fails_before_public_claim(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    publisher.storage.put(publisher.storage.temporary, attempt["upload_id"] + "/README.md", b"changed", "README.md")
    with pytest.raises(SCIError, match="signed size"):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    assert row(admin, pub.uuid) is None
    assert not publisher.storage.head(publisher.storage.archive, f"publications/{pub.uuid}/README.md")
    new = upload(publisher, folder, pub)
    assert new["upload_id"] != attempt["upload_id"]
    assert publisher.complete(pub.uuid, new["upload_id"], pub.manifest_sha256)[0] == 201


def test_durable_destinations_need_no_temporary_source(stack, make_publication, monkeypatch):
    publisher, admin = stack
    folder, pub = make_publication(attachments=True)
    enable(admin, pub)
    attempt = upload(publisher, folder, pub)
    original = publisher.storage.put
    def fail_before_record(bucket, key, *args, **kwargs):
        if key.endswith("/acceptance.json"):
            raise EndpointConnectionError(endpoint_url="test")
        return original(bucket, key, *args, **kwargs)
    monkeypatch.setattr(publisher.storage, "put", fail_before_record)
    with pytest.raises(EndpointConnectionError):
        publisher.complete(pub.uuid, attempt["upload_id"], pub.manifest_sha256)
    publisher.storage.delete_temporary(attempt["upload_id"])
    monkeypatch.setattr(publisher.storage, "put", original)
    assert publisher.recover() == []
    assert row(admin, pub.uuid)["state"] == "accepted"


def test_api_shared_rate_limit(stack):
    publisher, _ = stack
    settings = dataclasses.replace(publisher.settings, requests_per_minute=2)
    app = create_app(settings, publisher.database, publisher.storage, background=False)
    with TestClient(app) as client:
        assert client.post("/publications/invalid/complete", json={}).status_code == 400
        assert client.post("/publications/invalid/complete", json={}).status_code == 400
        result = client.post("/publications/invalid/complete", json={})
        assert result.status_code == 429
        assert result.json()["error"]["code"] == "rate_limited"
        assert result.headers["retry-after"] == "60"


def test_archive_rebuild_cannot_reopen_failed_uuid(stack, make_publication, settings):
    publisher, admin = stack
    folder, pub = make_publication()
    accept(publisher, admin, folder, pub)
    admin.execute("UPDATE sci_private.publications SET state='failed',failure_reason='restored terminal reservation'")
    admin_db = Database(admin.info.dsn)
    try:
        with pytest.raises(SCIError, match="terminal failed"):
            rebuild_catalog(admin_db, publisher.storage, settings)
    finally:
        admin_db.close()
    assert row(admin, pub.uuid)["state"] == "failed"
    assert not admin.execute("SELECT writes_enabled FROM sci_private.instance_state").fetchone()["writes_enabled"]


def test_late_put_is_cleaned_again_until_url_expires(stack, make_publication):
    publisher, admin = stack
    folder, pub = make_publication()
    accept(publisher, admin, folder, pub)
    attempt = admin.execute("SELECT * FROM sci_private.uploads").fetchone()
    publisher.cleanup()
    assert admin.execute("SELECT cleaned_at FROM sci_private.uploads").fetchone()["cleaned_at"] is None
    key = attempt["temporary_prefix"] + "/README.md"
    publisher.storage.put(publisher.storage.temporary, key, b"late upload", "README.md")
    admin.execute("UPDATE sci_private.uploads SET deadline=clock_timestamp()-interval '1 second'")
    publisher.cleanup()
    assert not publisher.storage.head(publisher.storage.temporary, key)
    assert admin.execute("SELECT cleaned_at FROM sci_private.uploads").fetchone()["cleaned_at"] is not None
