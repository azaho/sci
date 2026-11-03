"""Publication admission and finalization (DESIGN.md "Push: what happens"),
plus the maintenance pass that recovers interrupted uploads.
"""

from __future__ import annotations

import json
import logging
import time
import uuid as _uuid

from sci import keys
from sci import publication as pub

from . import database as dbm
from .database import Database
from .errors import (
    ApiError,
    FinalizationFailed,
    OperatorAttention,
    finalization_failed_error,
    from_publication_error,
)
from .settings import Settings
from .storage import AlreadyExists, SourceChanged, SourceMissing, Storage, StorageError

log = logging.getLogger("sci.server")


FINALIZE_LOCK_WAIT = 0.0  # busy: answer 503 at once; clients retry


UPLOAD_NAMES = (pub.README, pub.FILES_ZIP, pub.HEADLINE_FIGURE)



SERVER_WRITTEN = (pub.SCI_JSON, pub.MANIFEST, pub.SIGNATURE, pub.LICENSE)



def expected_objects(docs: pub.VerifiedDocuments) -> dict:
    """Expected sha256/bytes for every object of the publication."""
    out = {n: {"sha256": e.sha256, "bytes": e.bytes} for n, e in docs.manifest.files.items()}
    out[pub.MANIFEST] = {"sha256": docs.manifest_sha256, "bytes": len(docs.manifest_bytes)}
    out[pub.SIGNATURE] = {"sha256": pub.sha256_hex(docs.signature), "bytes": len(docs.signature)}
    return out



def _object_order(names) -> list[str]:
    return [n for n in pub.PUBLICATION_OBJECTS if n in names]



class Publisher:
    def __init__(self, settings: Settings, db: Database, storage: Storage):
        self.settings = settings
        self.db = db
        self.storage = storage

    # -- helpers -------------------------------------------------------------

    def publication_url(self, publication_uuid: str) -> str:
        return pub.join_url(self.settings.instance_url, f"publication/?id={publication_uuid}")

    def accepted_result(self, row: dict) -> dict:
        return {
            "uuid": str(row["uuid"]),
            "status": "accepted",
            "manifest_sha256": row["manifest_sha256"],
            "accepted_at": pub.format_timestamp(row["accepted_at"], micro=True),
            "url": self.publication_url(str(row["uuid"])),
            "archive_url": pub.join_url(self.settings.archive_url, str(row["uuid"])) + "/",
        }

    def _check_whitelist(self, conn, docs: pub.VerifiedDocuments, *, lock: bool) -> None:
        fp = docs.manifest.fingerprint
        if lock:
            Database.xact_lock(conn, "identity", fp, shared=True)
        ident = dbm.get_identity(conn, fp)
        if ident is None or not ident["enabled"]:
            raise ApiError(
                403,
                "key_not_enabled",
                f"The signing key {fp} is not enabled on this server's whitelist. Ask the server administrator"
                f" to enable public key {docs.manifest.public_key_b64}.",
            )
        if ident["public_key"] != docs.manifest.public_key_b64:
            raise ApiError(403, "key_mismatch", "The whitelisted key does not match the manifest's public key.")

    def _check_storage_quota(self, conn, files: dict) -> None:
        """Reject new uploads once stored plus reserved bytes would exceed the limit."""
        Database.xact_lock(conn, "storage", "quota")
        used = conn.execute(
            """
            select coalesce(sum((f.value ->> 'bytes')::bigint), 0) as used
            from (select files from sci.publications
                  union all
                  select files from sci.uploads where state = 'pending' and deadline > now()) t,
                 jsonb_each(t.files) f
            """
        ).fetchone()["used"]
        if used + sum(e["bytes"] for e in files.values()) > self.settings.max_storage_bytes:
            raise ApiError(507, "storage_full", "This server has reached its storage limit and is not accepting new publications.")

    def _uploads_for(self, upload_row: dict, docs_files: dict) -> dict:
        remaining = max(1, int((upload_row["deadline"].timestamp() - time.time())))
        expires = min(remaining, 3600)
        uploads = {}
        for name in UPLOAD_NAMES:
            if name not in docs_files:
                continue
            key = self.storage.temp_key(upload_row["temp_prefix"], name)
            length = docs_files[name]["bytes"] if self.settings.presign_content_length else None
            uploads[name] = self.storage.presign_put(key, pub.CONTENT_TYPES[name], expires, length)
        return uploads

    # -- POST /publications ---------------------------------------------------

    def prepare(self, sci_bytes: bytes, manifest_bytes: bytes, signature: bytes) -> tuple[int, dict]:
        try:
            docs = pub.verify_documents(sci_bytes, manifest_bytes, signature)
        except pub.PublicationError as exc:
            raise from_publication_error(exc) from exc
        uid = docs.uuid
        resume = False
        with self.db.transaction() as conn:
            Database.xact_lock(conn, "claim", uid)
            row = dbm.get_publication(conn, uid)
            if row is not None:
                if row["manifest_sha256"] != docs.manifest_sha256:
                    raise ApiError(409, "uuid_conflict", f"Publication {uid} already exists on this server with different contents.")
                if row["state"] == "accepted":
                    return 200, self.accepted_result(row)
                if row["state"] == "failed":
                    raise finalization_failed_error(row["failure_reason"])
                resume = True
            else:
                up = dbm.get_active_upload(conn, uid)
                if up is not None and up["expired"]:
                    conn.execute(
                        "update sci.uploads set state = 'expired', updated_at = now() where upload_id = %s",
                        (up["upload_id"],),
                    )
                    up = None
                if up is not None and up["manifest_sha256"] != docs.manifest_sha256:
                    raise ApiError(
                        409,
                        "uuid_conflict",
                        f"Another upload with a different manifest is in progress for publication {uid}.",
                    )
                self._check_whitelist(conn, docs, lock=False)
                if up is None:
                    files = expected_objects(docs)
                    self._check_storage_quota(conn, files)
                    upload_id = str(_uuid.uuid4())
                    up = conn.execute(
                        """
                        insert into sci.uploads (upload_id, publication_uuid, manifest_sha256, author_fingerprint,
                          temp_prefix, files, state, deadline)
                        values (%s, %s, %s, %s, %s, %s, 'pending', now() + make_interval(secs => %s))
                        returning *
                        """,
                        (
                            upload_id,
                            uid,
                            docs.manifest_sha256,
                            docs.manifest.fingerprint,
                            upload_id,
                            json.dumps(files),
                            self.settings.upload_ttl_seconds,
                        ),
                    ).fetchone()
        if resume:
            return self._finalize_response(uid, newly=True)

        # Server-written temporary objects. A reused attempt writes only missing
        # objects, so a retry never replaces a verified source.
        prefix = up["temp_prefix"]
        try:
            for name, data in ((pub.SCI_JSON, docs.sci_bytes), (pub.MANIFEST, docs.manifest_bytes),
                               (pub.SIGNATURE, docs.signature), (pub.LICENSE, pub.license_bytes())):
                key = self.storage.temp_key(prefix, name)
                if self.storage.head(self.storage.temp_bucket, key) is None:
                    self.storage.put_temp(key, data, pub.CONTENT_TYPES[name])
        except StorageError as exc:
            log.warning("temporary write failed for %s: %s", uid, exc)
            raise ApiError(503, "storage_unavailable", "Storage is temporarily unavailable; retry the push.", headers={"Retry-After": "5"})
        files = up["files"]
        return 202, {
            "uuid": uid,
            "status": "pending",
            "upload_id": str(up["upload_id"]),
            "manifest_sha256": docs.manifest_sha256,
            "deadline": pub.format_timestamp(up["deadline"]),
            "uploads": self._uploads_for(up, files),
        }

    # -- POST /publications/{uuid}/complete -------------------------------------

    def complete(self, uid: str, upload_id: str, manifest_sha256: str) -> tuple[int, dict]:
        with self.db.transaction() as conn:
            row = dbm.get_publication(conn, uid)
            up = dbm.get_upload(conn, upload_id)
        if row is not None:
            if row["manifest_sha256"] != manifest_sha256:
                raise ApiError(409, "uuid_conflict", f"Publication {uid} exists on this server with a different manifest.")
            if row["state"] == "failed":
                raise finalization_failed_error(row["failure_reason"])
            if str(row["upload_id"]) != upload_id:
                raise ApiError(409, "upload_mismatch", "This upload ID does not belong to the committed publication. Run 'sci push' again.")
            if row["state"] == "accepted":
                return 200, self.accepted_result(row)
            return self._finalize_response(uid, newly=True)
        if up is None:
            raise ApiError(404, "upload_not_found", "No upload attempt has this ID. Run 'sci push' again.")
        if str(up["publication_uuid"]) != uid or up["manifest_sha256"] != manifest_sha256:
            raise ApiError(409, "upload_mismatch", "The UUID or manifest hash does not match this upload attempt.")
        if up["state"] != "pending":
            raise ApiError(409, f"upload_{up['state']}", f"This upload attempt is {up['state']}. Run 'sci push' again to start a new attempt.")
        if up["expired"]:
            raise ApiError(409, "upload_expired", "This upload attempt expired. Run 'sci push' again to start a new attempt.")

        docs, etags = self._verify_upload(up)

        with self.db.transaction() as conn:
            Database.xact_lock(conn, "claim", uid)
            row = dbm.get_publication(conn, uid, for_update=True)
            if row is not None:
                # Committed meanwhile: finish it regardless of the current whitelist.
                if row["manifest_sha256"] != manifest_sha256:
                    raise ApiError(409, "uuid_conflict", f"Publication {uid} exists on this server with a different manifest.")
                if row["state"] == "failed":
                    raise finalization_failed_error(row["failure_reason"])
                if str(row["upload_id"]) != upload_id:
                    raise ApiError(409, "upload_mismatch", "This upload ID does not belong to the committed publication.")
            else:
                up2 = dbm.get_upload(conn, upload_id, for_update=True)
                if up2 is None or up2["state"] != "pending":
                    raise ApiError(409, "upload_not_pending", "This upload attempt is no longer pending. Run 'sci push' again.")
                if up2["expired"]:
                    raise ApiError(409, "upload_expired", "This upload attempt expired. Run 'sci push' again to start a new attempt.")
                self._check_whitelist(conn, docs, lock=True)
                dbm.insert_publication(
                    conn,
                    docs=docs,
                    upload_id=upload_id,
                    state="finalizing",
                    object_prefix=f"{self.storage.public_prefix}{uid}/",
                )
                conn.execute(
                    """
                    update sci.uploads set state = 'committed', source_etags = %s, outcome = %s, updated_at = now()
                    where upload_id = %s
                    """,
                    (json.dumps(etags), json.dumps({"instance_url": self.settings.instance_url}), upload_id),
                )
        return self._finalize_response(uid, newly=True)

    def _verify_upload(self, up: dict) -> tuple[pub.VerifiedDocuments, dict]:
        """Reload server-stored documents and verify every uploaded file."""
        prefix = up["temp_prefix"]
        etags: dict[str, str] = {}

        def read_small(name: str, limit: int) -> bytes:
            got = self.storage.read(self.storage.temp_bucket, self.storage.temp_key(prefix, name), limit)
            if got is None:
                raise ApiError(409, "upload_incomplete", f"The server copy of {name} is missing; run 'sci push' again.")
            data, info = got
            etags[name] = info.etag
            return data

        try:
            sci_bytes = read_small(pub.SCI_JSON, pub.MAX_JSON_BYTES)
            manifest_bytes = read_small(pub.MANIFEST, pub.MAX_JSON_BYTES)
            signature = read_small(pub.SIGNATURE, keys.SIGNATURE_BYTES)
            if pub.sha256_hex(manifest_bytes) != up["manifest_sha256"]:
                raise ApiError(409, "upload_mismatch", "The stored manifest does not match this upload attempt.")
            try:
                docs = pub.verify_documents(sci_bytes, manifest_bytes, signature)
            except pub.PublicationError as exc:
                raise from_publication_error(exc) from exc

            for name, entry in docs.manifest.files.items():
                if name == pub.SCI_JSON:
                    continue
                key = self.storage.temp_key(prefix, name)
                info = self.storage.head(self.storage.temp_bucket, key)
                if info is None:
                    raise ApiError(400, "file_missing", f"{name} has not been uploaded. Run 'sci push' again.", name)
                if info.size != entry.bytes:
                    raise ApiError(
                        400, "file_mismatch",
                        f"{name} has {info.size} bytes; the signed manifest declares {entry.bytes}.", name,
                    )
                if name in (pub.README, pub.HEADLINE_FIGURE, pub.LICENSE):
                    got = self.storage.read(self.storage.temp_bucket, key, entry.bytes)
                    if got is None:
                        raise ApiError(400, "file_missing", f"{name} has not been uploaded.", name)
                    data, info = got
                    try:
                        pub.check_file(name, entry, pub.sha256_hex(data), len(data))
                        pub.verify_content(name, data)
                    except pub.PublicationError as exc:
                        raise from_publication_error(exc) from exc
                else:
                    got = self.storage.hash_object(self.storage.temp_bucket, key, entry.bytes)
                    if got is None:
                        raise ApiError(400, "file_missing", f"{name} has not been uploaded.", name)
                    sha, size, info = got
                    try:
                        pub.check_file(name, entry, sha, size)
                    except pub.PublicationError as exc:
                        raise from_publication_error(exc) from exc
                etags[name] = info.etag
        except StorageError as exc:
            log.warning("verification storage error: %s", exc)
            raise ApiError(503, "storage_unavailable", "Storage is temporarily unavailable; retry.", headers={"Retry-After": "5"})
        return docs, etags

    # -- finalization -----------------------------------------------------------

    def _finalize_response(self, uid: str, *, newly: bool) -> tuple[int, dict]:
        try:
            row, changed = self.finalize(uid, wait=FINALIZE_LOCK_WAIT)
        except FinalizationFailed as exc:
            raise finalization_failed_error(exc.reason)
        except OperatorAttention as exc:
            log.error("operator attention required for %s: %s", uid, exc)
            raise ApiError(500, "operator_attention", "Finalization found conflicting stored objects; an administrator must reconcile them.")
        except StorageError as exc:
            log.warning("finalization storage error for %s: %s", uid, exc)
            raise ApiError(503, "finalization_pending", "Finalization was interrupted by a temporary storage error; retry.", headers={"Retry-After": "5"})
        if row is None:
            raise ApiError(503, "finalization_in_progress", "Another request is finalizing this publication; retry shortly.", headers={"Retry-After": "2"})
        return (201 if changed and newly else 200), self.accepted_result(row)

    def finalize(self, uid: str, *, wait: float) -> tuple[dict | None, bool]:
        """Finish a committed publication. Returns (row, changed) or (None, False)
        when another worker holds the finalization lock."""
        with self.db.session_lock("finalize", uid, wait) as (acquired, conn):
            if not acquired:
                return None, False
            row = dbm.get_publication(conn, uid)
            if row is None:
                raise OperatorAttention(f"no committed publication {uid}")
            if row["state"] == "accepted":
                return row, False
            if row["state"] == "failed":
                raise FinalizationFailed(row["failure_reason"])
            up = dbm.get_upload(conn, str(row["upload_id"])) if row["upload_id"] else None
            if up is None:
                raise OperatorAttention(f"publication {uid} is finalizing without its upload record")
            accepted_at = pub.format_timestamp(row["accepted_at"], micro=True)
            instance_url = (up["outcome"] or {}).get("instance_url") or self.settings.instance_url
            record = pub.serialize_acceptance(instance_url, uid, row["manifest_sha256"], accepted_at)

            if self._matching_acceptance(uid, row, accepted_at):
                return self._mark_accepted(conn, uid), True

            expected = up["files"]
            etags = up["source_etags"] or {}
            for name in _object_order(expected):
                exp = expected[name]
                dest_key = self.storage.public_key(uid, name)
                existing = self.storage.hash_object(self.storage.public_bucket, dest_key, exp["bytes"])
                if existing is not None:
                    sha, size, _ = existing
                    if sha != exp["sha256"] or size != exp["bytes"]:
                        raise OperatorAttention(f"{dest_key} exists with different contents")
                    continue
                try:
                    self.storage.copy_to_public(self.storage.temp_key(up["temp_prefix"], name), uid, name, etags.get(name, ""))
                except (SourceMissing, SourceChanged) as exc:
                    if self._matching_acceptance(uid, row, accepted_at):
                        return self._mark_accepted(conn, uid), True
                    reason = f"temporary source for {name} {'is missing' if isinstance(exc, SourceMissing) else 'changed after verification'}"
                    self._mark_failed(conn, uid, reason)
                    raise FinalizationFailed(reason)
            try:
                self.storage.put_public_if_absent(uid, pub.ACCEPTANCE, record)
            except AlreadyExists:
                if not self._matching_acceptance(uid, row, accepted_at):
                    raise OperatorAttention(f"conflicting acceptance record for {uid}")
            return self._mark_accepted(conn, uid), True

    def _matching_acceptance(self, uid: str, row: dict, accepted_at: str) -> bool:
        got = self.storage.read(self.storage.public_bucket, self.storage.public_key(uid, pub.ACCEPTANCE), 4096)
        if got is None:
            return False
        try:
            rec = pub.parse_acceptance(got[0])
        except pub.PublicationError as exc:
            raise OperatorAttention(f"invalid acceptance record for {uid}: {exc}")
        if rec.uuid != uid or rec.manifest_sha256 != row["manifest_sha256"] or rec.accepted_at != accepted_at:
            raise OperatorAttention(f"acceptance record for {uid} does not match the committed publication")
        return True

    def _mark_accepted(self, conn, uid: str) -> dict:
        with conn.transaction():
            conn.execute(
                "update sci.publications set state = 'accepted', state_changed_at = now() where uuid = %s and state = 'finalizing'",
                (uid,),
            )
            row = dbm.get_publication(conn, uid)
            if row["upload_id"]:
                conn.execute(
                    "update sci.uploads set outcome = coalesce(outcome, '{}'::jsonb) || %s, updated_at = now() where upload_id = %s",
                    (json.dumps({"accepted": True}), row["upload_id"]),
                )
        log.info("accepted publication %s", uid)
        return row

    def _mark_failed(self, conn, uid: str, reason: str) -> None:
        with conn.transaction():
            conn.execute(
                """
                update sci.publications set state = 'failed', failure_reason = %s, state_changed_at = now()
                where uuid = %s and state = 'finalizing'
                """,
                (reason, uid),
            )
        log.error("publication %s failed permanently: %s", uid, reason)

    # -- background maintenance ---------------------------------------------------

    def run_maintenance_once(self) -> dict:
        stats = {"resumed": 0, "expired": 0, "cleaned": 0, "recounted": 0}
        with self.db.session_lock("worker", "maintenance", 0) as (acquired, _conn):
            if not acquired:
                return stats
            if self.settings.writes_enabled:
                with self.db.connection() as conn:
                    pending = [str(r["uuid"]) for r in conn.execute(
                        "select uuid from sci.publications where state = 'finalizing' order by accepted_at limit 100"
                    ).fetchall()]
                for uid in pending:
                    try:
                        row, changed = self.finalize(uid, wait=0)
                        stats["resumed"] += int(bool(changed))
                    except FinalizationFailed:
                        pass
                    except (OperatorAttention, StorageError) as exc:
                        log.warning("recovery of %s deferred: %s", uid, exc)
            with self.db.transaction() as conn:
                expired = conn.execute(
                    """
                    update sci.uploads set state = 'expired', updated_at = now()
                    where state = 'pending' and deadline < now() - make_interval(secs => %s)
                    returning upload_id
                    """,
                    (self.settings.expiry_grace_seconds,),
                ).fetchall()
                stats["expired"] = len(expired)
            with self.db.connection() as conn:
                # Never clean sources of committed attempts unless the publication is accepted.
                cleanable = conn.execute(
                    """
                    select u.upload_id, u.temp_prefix from sci.uploads u
                    left join sci.publications p on p.upload_id = u.upload_id
                    where u.temp_cleaned_at is null
                      and (u.state in ('expired', 'failed')
                           or (u.state = 'committed' and p.state = 'accepted'))
                    limit 200
                    """
                ).fetchall()
            for r in cleanable:
                try:
                    self.storage.delete_temp_prefix(r["temp_prefix"])
                except StorageError as exc:
                    log.warning("cleanup of %s deferred: %s", r["temp_prefix"], exc)
                    continue
                with self.db.transaction() as conn:
                    conn.execute("update sci.uploads set temp_cleaned_at = now() where upload_id = %s", (r["upload_id"],))
                stats["cleaned"] += 1
            with self.db.transaction() as conn:
                # Share the refresh deadline across processes/restarts; recovery still runs each minute.
                refreshed = conn.execute("""
                    select sci.refresh_citation_counts() as n
                    where not exists (
                        select 1 from sci.catalog_totals where updated_at > now() - interval '5 minutes'
                    )
                """).fetchone()
                if refreshed:
                    stats["recounted"] = refreshed["n"]
        return stats

