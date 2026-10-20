"""SCI publishing service.

Endpoints:
    POST /publications                  submit sci.json, manifest.json, signature
    POST /publications/{uuid}/complete  verify uploaded files and accept
    GET  /health                        readiness (database and storage)

Commands:
    python -m server.app serve
    python -m server.app rebuild-catalog [--dry-run]
    python -m server.app init-local      (local installations: buckets + descriptor)

The service verifies every publication before acceptance using the shared
``sci.publication`` code. Finalization follows DESIGN.md "Push: what happens"
and the recovery table in "SQL and object storage".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from sci import __version__, keys
from sci import publication as pub

from . import database as dbm
from .database import Database
from .storage import AlreadyExists, SourceChanged, SourceMissing, Storage, StorageError

log = logging.getLogger("sci.server")

# Multipart overhead allowance on top of the 64 KiB JSON limit and signature.
MAX_SUBMISSION_BODY = pub.MAX_JSON_BYTES + keys.SIGNATURE_BYTES + 16_384
MAX_COMPLETE_BODY = 4_096
FINALIZE_LOCK_WAIT = 0.0  # busy: answer 503 at once; clients retry
# About $200 per month of R2 Standard storage at $0.015 per GB-month.
DEFAULT_MAX_STORAGE_BYTES = 13_000_000_000_000


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    instance_url: str
    archive_url: str
    database_url: str
    s3_endpoint: str
    s3_access_key_id: str
    s3_secret_access_key: str
    temp_bucket: str
    public_bucket: str
    s3_region: str = "auto"
    s3_presign_endpoint: str | None = None
    public_prefix: str = "publications/"
    upload_ttl_seconds: int = 3600
    rate_limit_per_minute: int = 30
    client_ip_header: str | None = None
    presign_content_length: bool = False
    writes_enabled: bool = True
    worker_interval_seconds: float = 60.0
    expiry_grace_seconds: int = 60
    max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES

    @classmethod
    def from_env(cls) -> "Settings":
        def req(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise SystemExit(f"missing required environment variable {name}")
            return v

        return cls(
            instance_url=req("SCI_INSTANCE_URL"),
            archive_url=req("SCI_ARCHIVE_URL"),
            database_url=req("SCI_DATABASE_URL"),
            s3_endpoint=req("SCI_S3_ENDPOINT"),
            s3_access_key_id=req("SCI_S3_ACCESS_KEY_ID"),
            s3_secret_access_key=req("SCI_S3_SECRET_ACCESS_KEY"),
            temp_bucket=req("SCI_TEMP_BUCKET"),
            public_bucket=req("SCI_PUBLIC_BUCKET"),
            s3_region=os.environ.get("SCI_S3_REGION") or "auto",
            s3_presign_endpoint=os.environ.get("SCI_S3_PRESIGN_ENDPOINT") or None,
            public_prefix=os.environ.get("SCI_PUBLIC_PREFIX") or "publications/",
            upload_ttl_seconds=int(os.environ.get("SCI_UPLOAD_TTL_SECONDS") or 3600),
            rate_limit_per_minute=int(os.environ.get("SCI_RATE_LIMIT_PER_MINUTE") or 30),
            client_ip_header=(os.environ.get("SCI_CLIENT_IP_HEADER") or None),
            presign_content_length=_env_bool("SCI_PRESIGN_CONTENT_LENGTH", False),
            writes_enabled=_env_bool("SCI_WRITES_ENABLED", True),
            worker_interval_seconds=float(os.environ.get("SCI_WORKER_INTERVAL_SECONDS") or 60),
            max_storage_bytes=int(os.environ.get("SCI_MAX_STORAGE_BYTES") or DEFAULT_MAX_STORAGE_BYTES),
        )

    def storage(self) -> Storage:
        return Storage(
            endpoint=self.s3_endpoint,
            presign_endpoint=self.s3_presign_endpoint,
            access_key_id=self.s3_access_key_id,
            secret_access_key=self.s3_secret_access_key,
            temp_bucket=self.temp_bucket,
            public_bucket=self.public_bucket,
            region=self.s3_region,
            public_prefix=self.public_prefix,
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.headers = headers or {}

    def response(self) -> JSONResponse:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.field:
            body["error"]["field"] = self.field
        return json_response(body, self.status, self.headers)


_ERROR_STATUS = {
    "invalid_signature": 403,
    "too_large": 413,
    "unsupported_schema": 400,
}


def from_publication_error(exc: pub.PublicationError) -> ApiError:
    return ApiError(_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc), exc.field)


class FinalizationFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OperatorAttention(Exception):
    """A conflict that must never be resolved automatically."""


def finalization_failed_error(reason: str) -> ApiError:
    return ApiError(
        409,
        "finalization_failed",
        f"Finalization of this publication failed permanently ({reason}). This UUID cannot be reused."
        " Sign the publication again ('sci sign') to create a new UUID, then push it.",
    )


def json_response(body, status: int = 200, headers: dict | None = None) -> JSONResponse:
    h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    h.update(headers or {})
    return JSONResponse(body, status_code=status, headers=h)


# ---------------------------------------------------------------------------
# Publishing logic
# ---------------------------------------------------------------------------

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
                # The ETag condition is not a cryptographic check: R2-style
                # MD5 ETags allow a colliding source to be swapped in after
                # verification. Hash the copied object itself before any
                # acceptance record can refer to it.
                copied = self.storage.hash_object(self.storage.public_bucket, dest_key, exp["bytes"])
                if copied is None:
                    raise StorageError(f"{dest_key} is not readable after copy")
                sha, size, _ = copied
                if sha != exp["sha256"] or size != exp["bytes"]:
                    reason = f"archived copy of {name} does not match the signed manifest"
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
        stats = {"resumed": 0, "expired": 0, "cleaned": 0}
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
        return stats


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Per-client token bucket for publishing requests (in memory, per process)."""

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, client: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(client, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self.buckets[client] = (tokens - 1, now)
                ok, retry = True, 0
            else:
                self.buckets[client] = (tokens, now)
                ok, retry = False, int((1 - tokens) / self.rate) + 1
            if len(self.buckets) > 10_000:
                cutoff = now - 600
                self.buckets = {k: v for k, v in self.buckets.items() if v[1] > cutoff}
        return ok, retry


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


async def read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "too_large", f"The request body exceeds {limit} bytes.")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "too_large", f"The request body exceeds {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    from python_multipart.multipart import MultipartParser, parse_options_header

    ctype, params = parse_options_header(content_type)
    if ctype != b"multipart/form-data" or b"boundary" not in params:
        raise ApiError(400, "invalid_request", "Use multipart/form-data with fields sci, manifest, and signature.")
    parts: dict[str, bytes] = {}
    state = {"headers": {}, "name": None, "data": [], "hname": b"", "hvalue": b""}

    def on_part_begin():
        state["headers"] = {}
        state["data"] = []

    def on_header_field(data, start, end):
        state["hname"] += data[start:end]

    def on_header_value(data, start, end):
        state["hvalue"] += data[start:end]

    def on_header_end():
        state["headers"][state["hname"].lower()] = state["hvalue"]
        state["hname"], state["hvalue"] = b"", b""

    def on_part_data(data, start, end):
        state["data"].append(data[start:end])

    def on_part_end():
        disp = state["headers"].get(b"content-disposition", b"")
        _, dparams = parse_options_header(disp)
        name = dparams.get(b"name", b"").decode("utf-8", "replace")
        if name not in ("sci", "manifest", "signature"):
            raise ApiError(400, "invalid_request", f"Unexpected form field {name!r}; send only sci, manifest, and signature.")
        if name in parts:
            raise ApiError(400, "invalid_request", f"Form field {name!r} appears more than once.")
        parts[name] = b"".join(state["data"])

    parser = MultipartParser(
        params[b"boundary"],
        callbacks={
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
    try:
        parser.write(body)
        parser.finalize()
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(400, "invalid_request", f"Malformed multipart body: {exc}") from exc
    missing = [n for n in ("sci", "manifest", "signature") if n not in parts]
    if missing:
        raise ApiError(400, "invalid_request", f"Missing form field(s): {', '.join(missing)}.")
    return parts


def client_id(request: Request, header: str | None) -> str:
    if header:
        v = request.headers.get(header)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> Starlette:
    settings = settings or Settings.from_env()
    state: dict = {}
    limiter = RateLimiter(settings.rate_limit_per_minute)

    def publisher() -> Publisher:
        return state["publisher"]

    def guard(request: Request) -> None:
        if not settings.writes_enabled:
            raise ApiError(503, "writes_disabled", "Publishing is temporarily disabled on this server.")
        ok, retry = limiter.allow(client_id(request, settings.client_ip_header))
        if not ok:
            raise ApiError(429, "rate_limited", "Too many publishing requests; retry later.", headers={"Retry-After": str(retry)})

    async def submit(request: Request):
        try:
            guard(request)
            body = await read_capped(request, MAX_SUBMISSION_BODY)
            parts = parse_multipart(body, request.headers.get("content-type", ""))
            if len(parts["sci"]) + len(parts["manifest"]) > pub.MAX_JSON_BYTES:
                raise ApiError(413, "too_large", f"sci.json and manifest.json together exceed {pub.MAX_JSON_BYTES} bytes.")
            status, result = await run_in_threadpool(publisher().prepare, parts["sci"], parts["manifest"], parts["signature"])
            return json_response(result, status)
        except ApiError as exc:
            return exc.response()

    async def complete(request: Request):
        try:
            guard(request)
            uid = request.path_params["uuid"]
            if not pub.is_uuid4(uid):
                raise ApiError(400, "invalid_uuid", "The publication ID must be a canonical lowercase UUIDv4.")
            body = await read_capped(request, MAX_COMPLETE_BODY)
            try:
                doc = pub.parse_strict_json(body, "request")
            except pub.PublicationError as exc:
                raise ApiError(400, "invalid_request", str(exc)) from exc
            if set(doc) != {"upload_id", "manifest_sha256"}:
                raise ApiError(400, "invalid_request", "Send a JSON object with exactly upload_id and manifest_sha256.")
            upload_id, mhash = doc["upload_id"], doc["manifest_sha256"]
            if not isinstance(upload_id, str) or not pub.is_uuid4(upload_id):
                raise ApiError(400, "invalid_request", "upload_id must be the ID returned by POST /publications.")
            if not isinstance(mhash, str) or not pub._HASH_RE.fullmatch(mhash):
                raise ApiError(400, "invalid_request", "manifest_sha256 must be 64 lowercase hexadecimal characters.")
            status, result = await run_in_threadpool(publisher().complete, uid, upload_id, mhash)
            return json_response(result, status)
        except ApiError as exc:
            return exc.response()

    async def health(request: Request):
        """200 while the process and database work. Storage status is reported
        per bucket but does not fail the check, so an R2 problem cannot cause
        restart loops; publishing requests then fail with 503 storage_unavailable."""
        try:
            await run_in_threadpool(publisher().db.ping)
        except Exception as exc:  # noqa: BLE001 - readiness reports any failure
            log.warning("health check failed: database: %s", exc)
            return json_response({"status": "unavailable", "database": "error"}, 503)
        buckets = await run_in_threadpool(publisher().storage.bucket_status)
        degraded = any(v != "ok" for v in buckets.values())
        if degraded:
            log.warning("storage check failed: %s", buckets)
        return json_response({
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            "writes_enabled": settings.writes_enabled,
            "database": "ok",
            "storage": buckets,
        })

    async def root(request: Request):
        return json_response({"service": "sci-publisher", "version": __version__})

    async def not_found(request: Request, exc):
        return json_response({"error": {"code": "not_found", "message": "Not found."}}, 404)

    async def method_not_allowed(request: Request, exc):
        status = getattr(exc, "status_code", 500)
        code = {405: "method_not_allowed"}.get(status, "error")
        return json_response({"error": {"code": code, "message": getattr(exc, "detail", "Error.")}}, status)

    async def server_error(request: Request, exc):
        log.exception("unhandled error", exc_info=exc)
        return json_response({"error": {"code": "internal_error", "message": "Internal server error."}}, 500)

    async def worker_loop():
        while True:
            try:
                await run_in_threadpool(publisher().run_maintenance_once)
            except Exception:  # noqa: BLE001
                log.exception("maintenance run failed")
            await asyncio.sleep(settings.worker_interval_seconds)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.database_url)
        storage = settings.storage()
        state["publisher"] = Publisher(settings, db, storage)
        try:
            db.save_settings(settings.instance_url, settings.archive_url)
        except psycopg.Error as exc:
            log.warning("could not save instance settings: %s", exc)
        task = asyncio.create_task(worker_loop()) if start_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            db.close()

    app = Starlette(
        routes=[
            Route("/", root, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/publications", submit, methods=["POST"]),
            Route("/publications/{uuid}/complete", complete, methods=["POST"]),
        ],
        exception_handlers={404: not_found, 405: method_not_allowed, 500: server_error},
        lifespan=lifespan,
    )
    app.state.sci = state
    return app


# ---------------------------------------------------------------------------
# Archive recovery
# ---------------------------------------------------------------------------


def rebuild_catalog(settings: Settings, admin_dsn: str, *, dry_run: bool = False, out=sys.stdout) -> int:
    """Rebuild the derived catalog from durable acceptance records.

    Restores accepted publications, ordered citations, identities (disabled
    unless already present), and acceptance times. Never chooses a winner
    between conflicting records, never reopens failed UUIDs, and never
    infers whitelist membership. Returns the number of problems found.
    """
    storage = settings.storage()
    problems = 0
    restored = 0
    seen = 0
    with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as conn:
        for dir_uuid, key in storage.iter_acceptance_records():
            seen += 1
            try:
                docs, record = _verify_archived(storage, dir_uuid)
            except Exception as exc:  # noqa: BLE001 - report and continue
                problems += 1
                print(f"PROBLEM {dir_uuid}: {exc}", file=out)
                continue
            with conn.transaction():
                Database.xact_lock(conn, "claim", dir_uuid)
                row = dbm.get_publication(conn, dir_uuid, for_update=True)
                if row is not None:
                    if row["manifest_sha256"] != docs.manifest_sha256:
                        problems += 1
                        print(f"CONFLICT {dir_uuid}: SQL holds manifest {row['manifest_sha256']}, archive holds {docs.manifest_sha256}; reconcile manually", file=out)
                        continue
                    if row["state"] == "failed":
                        problems += 1
                        print(f"CONFLICT {dir_uuid}: SQL records a terminal failure but the archive has an acceptance record; reconcile manually", file=out)
                        continue
                    if row["state"] == "finalizing":
                        if not dry_run:
                            conn.execute("update sci.publications set state = 'accepted', state_changed_at = now() where uuid = %s", (dir_uuid,))
                        print(f"ACCEPTED {dir_uuid} (durable acceptance record)", file=out)
                        restored += 1
                    continue
                if dry_run:
                    print(f"WOULD RESTORE {dir_uuid}", file=out)
                    restored += 1
                    continue
                conn.execute(
                    "insert into sci.identities (fingerprint, public_key, enabled) values (%s, %s, false) on conflict do nothing",
                    (docs.manifest.fingerprint, docs.manifest.public_key_b64),
                )
                dbm.insert_publication(
                    conn,
                    docs=docs,
                    upload_id=None,
                    state="accepted",
                    accepted_at=pub.parse_timestamp(record.accepted_at),
                    object_prefix=f"{storage.public_prefix}{dir_uuid}/",
                )
                restored += 1
                print(f"RESTORED {dir_uuid}", file=out)
        conn.execute("notify pgrst, 'reload schema'")
    print(f"acceptance records: {seen}; restored or accepted: {restored}; problems: {problems}", file=out)
    return problems


def _verify_archived(storage: Storage, dir_uuid: str):
    pub.check_uuid4(dir_uuid, "archive directory")
    b = storage.public_bucket

    def read(name: str, limit: int) -> bytes:
        got = storage.read(b, storage.public_key(dir_uuid, name), limit)
        if got is None:
            raise pub.PublicationError("is missing", name)
        return got[0]

    record = pub.parse_acceptance(read(pub.ACCEPTANCE, 4096))
    if record.uuid != dir_uuid:
        raise pub.PublicationError("acceptance record UUID differs from its directory", pub.ACCEPTANCE)
    manifest_bytes = read(pub.MANIFEST, pub.MAX_JSON_BYTES)
    if pub.sha256_hex(manifest_bytes) != record.manifest_sha256:
        raise pub.PublicationError("manifest hash differs from the acceptance record", pub.MANIFEST)
    docs = pub.verify_documents(read(pub.SCI_JSON, pub.MAX_JSON_BYTES), manifest_bytes, read(pub.SIGNATURE, keys.SIGNATURE_BYTES))
    if docs.uuid != dir_uuid:
        raise pub.PublicationError("manifest UUID differs from its directory", pub.MANIFEST)
    for name, entry in docs.manifest.files.items():
        if name == pub.SCI_JSON:
            continue
        if name in (pub.README, pub.HEADLINE_FIGURE, pub.LICENSE):
            data = read(name, entry.bytes)
            pub.check_file(name, entry, pub.sha256_hex(data), len(data))
            pub.verify_content(name, data)
        else:
            got = storage.hash_object(b, storage.public_key(dir_uuid, name), entry.bytes)
            if got is None:
                raise pub.PublicationError("is missing", name)
            pub.check_file(name, entry, got[0], got[1])
    return docs, record


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def descriptor(settings: Settings, *, catalog_url: str, publishing_api_url: str, publishable_key: str | None = None) -> dict:
    d = {
        "schema_version": 1,
        "publishing_api_url": publishing_api_url,
        "catalog_url": catalog_url,
        "catalog_schema": "catalog_v1",
        "archive_url": settings.archive_url,
    }
    if publishable_key:
        d["supabase_publishable_key"] = publishable_key
    return d


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("SCI_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m server.app", description="SCI publishing service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP service")
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    r = sub.add_parser("rebuild-catalog", help="rebuild the catalog from archive acceptance records")
    r.add_argument("--database-url", default=os.environ.get("SCI_ADMIN_DATABASE_URL"))
    r.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("init-local", help="create local buckets and write sci-server.json")
    i.add_argument("--descriptor", required=True, help="output path for sci-server.json")
    i.add_argument("--catalog-url", required=True)
    i.add_argument("--publishing-api-url", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False, access_log=True)
        return 0
    settings = Settings.from_env()
    if args.cmd == "rebuild-catalog":
        if not args.database_url:
            print("error: set SCI_ADMIN_DATABASE_URL or pass --database-url", file=sys.stderr)
            return 2
        return 1 if rebuild_catalog(settings, args.database_url, dry_run=args.dry_run) else 0
    if args.cmd == "init-local":
        for attempt in range(60):  # storage may still be starting
            try:
                settings.storage().ensure_buckets()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 59:
                    raise
                log.info("waiting for storage: %s", exc)
                time.sleep(2)
        d = descriptor(settings, catalog_url=args.catalog_url, publishing_api_url=args.publishing_api_url)
        path = Path(args.descriptor)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
