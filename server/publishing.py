"""Publication state machine, serialized per UUID across all service workers."""

import datetime as dt
import uuid as uuid_module

from botocore.exceptions import ClientError
from psycopg.types.json import Jsonb

from sci.client import append_url, validate_acceptance
from sci.errors import SCIError
from sci.publication import verify_documents
from sci.validation import JSON_LIMIT, compact, normalize_citation_target, strict_json
from server.storage import ChangedSource, MissingSource, precondition


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def insert_publication(conn, publication, *, accepted_at, instance_url, archive_url,
                       state, upload_id=None):
    metadata, manifest = publication.metadata, publication.manifest
    object_base = append_url(archive_url, publication.uuid)
    conn.execute("""
        INSERT INTO sci_private.publications(
          uuid, manifest_sha256, state, title, abstract, categories, keywords,
          headline_figure_caption, author_fingerprint, author_public_key, created_at,
          accepted_at, instance_url, object_base, filenames, acceptance_ref, files_tree, upload_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        publication.uuid, publication.manifest_sha256, state, metadata["title"], metadata["abstract"],
        metadata.get("categories", []), metadata.get("keywords", []),
        metadata.get("headline_figure_caption"), publication.fingerprint, manifest["public_key"]["x"],
        manifest["created_at"], accepted_at, instance_url, object_base,
        list(publication.descriptors), append_url(object_base, "acceptance.json"),
        Jsonb(manifest["files_tree"]) if "files_tree" in manifest else None, upload_id,
    ))
    for position, citation in enumerate(metadata.get("citations", [])):
        conn.execute("""
            INSERT INTO sci_private.citations(source_uuid, position, target, target_key, relation, reason)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (publication.uuid, position, citation["target"],
              normalize_citation_target(citation["target"]), citation.get("relation"), citation.get("reason")))


class Publisher:
    def __init__(self, database, storage, settings):
        self.database, self.storage, self.settings = database, storage, settings

    @staticmethod
    def publication_row(conn, uuid):
        return conn.execute("SELECT * FROM sci_private.publications WHERE uuid=%s", (uuid,)).fetchone()

    @staticmethod
    def attempt_row(conn, upload_id):
        return conn.execute("SELECT * FROM sci_private.uploads WHERE upload_id=%s", (upload_id,)).fetchone()

    @staticmethod
    def ensure_hash(row, manifest_hash):
        if row["manifest_sha256"] != manifest_hash:
            raise SCIError("This UUID is reserved for a different signed record", "uuid_conflict", 409)

    @staticmethod
    def failure(row):
        raise SCIError(
            f"Finalization failed: {row['failure_reason']}. Sign again to create a new UUID.",
            "finalization_failed", 409,
        )

    def outcome(self, row):
        return {
            "uuid": str(row["uuid"]), "manifest_sha256": row["manifest_sha256"],
            "status": "accepted", "accepted_at": timestamp(row["accepted_at"]),
            "url": append_url(self.settings.instance_url, f"publication/?id={row['uuid']}"),
        }

    def check_writes(self, conn):
        if not self.settings.writes_enabled:
            raise SCIError("Publishing is disabled", "writes_disabled", 503)
        self.database.require_writes(conn)

    def cleanup_attempt(self, conn, attempt):
        if attempt["state"] not in {"failed", "expired", "accepted"}:
            return
        # A committed failure keeps its objects for manual operator reconciliation.
        publication = self.publication_row(conn, str(attempt["uuid"]))
        if publication and publication["state"] in {"finalizing", "failed"}:
            return
        self.storage.delete_temporary(attempt["temporary_prefix"])
        # Presigned URLs can be reused until their fixed deadline. Sweep ended
        # prefixes again until then, so late PUTs cannot leave permanent orphans.
        conn.execute("""UPDATE sci_private.uploads
                     SET cleaned_at=CASE WHEN deadline <= clock_timestamp()
                                         THEN clock_timestamp() ELSE NULL END
                     WHERE upload_id=%s""",
                     (attempt["upload_id"],))

    def prepare(self, publication):
        with self.database.locked(publication.uuid) as conn:
            existing = self.publication_row(conn, publication.uuid)
            if existing:
                self.ensure_hash(existing, publication.manifest_sha256)
                if existing["state"] == "failed":
                    self.failure(existing)
                if existing["state"] == "accepted":
                    return 200, self.outcome(existing)
                return 200, self.finalize(conn, existing)
            self.check_writes(conn)
            attempts = conn.execute(
                "SELECT * FROM sci_private.uploads WHERE uuid=%s ORDER BY deadline DESC",
                (publication.uuid,),
            ).fetchall()
            active = None
            for attempt in attempts:
                if attempt["state"] in {"preparing", "pending", "verifying"}:
                    if attempt["deadline"] <= utcnow():
                        conn.execute("UPDATE sci_private.uploads SET state='expired' WHERE upload_id=%s",
                                     (attempt["upload_id"],))
                        attempt["state"] = "expired"
                    else:
                        self.ensure_hash(attempt, publication.manifest_sha256)
                        active = attempt
                if not attempt["cleaned_at"]:
                    self.cleanup_attempt(conn, attempt)
            with conn.transaction():
                self.database.whitelist(conn, publication)
                if active is None:
                    upload_id = uuid_module.uuid4()
                    active = conn.execute("""
                        INSERT INTO sci_private.uploads(upload_id,uuid,manifest_sha256,
                          author_fingerprint,temporary_prefix,deadline,state)
                        VALUES (%s,%s,%s,%s,%s,%s,'preparing') RETURNING *
                    """, (upload_id, publication.uuid, publication.manifest_sha256, publication.fingerprint,
                          str(upload_id), utcnow() + dt.timedelta(seconds=self.settings.upload_seconds))).fetchone()
            for name, data in publication.originals.items():
                self.storage.write_original(f"{active['temporary_prefix']}/{name}", data, name)
            conn.execute("UPDATE sci_private.uploads SET state='pending' WHERE upload_id=%s", (active["upload_id"],))
            lifetime = int((active["deadline"] - utcnow()).total_seconds())
            if lifetime <= 0:
                raise SCIError("Upload deadline expired; retry preparation", "upload_expired", 409)
            uploads = {}
            for name in ("README.md", "files.zip", "headline_figure.png"):
                if name in publication.manifest["files"]:
                    uploads[name] = self.storage.upload_url(
                        f"{active['temporary_prefix']}/{name}", name,
                        publication.manifest["files"][name]["bytes"], lifetime,
                    )
            return 202, {
                "uuid": publication.uuid, "status": "pending",
                "upload_id": str(active["upload_id"]), "manifest_sha256": publication.manifest_sha256,
                "deadline": timestamp(active["deadline"]), "uploads": uploads,
            }

    def verify_attempt(self, attempt):
        base = attempt["temporary_prefix"] + "/"
        manifest, _ = self.storage.read(self.storage.temporary, base + "manifest.json", JSON_LIMIT)
        sci, _ = self.storage.read(self.storage.temporary, base + "sci.json", JSON_LIMIT - len(manifest))
        signature, _ = self.storage.read(self.storage.temporary, base + "manifest.json.sig", 64)
        publication = verify_documents(manifest, sci, signature)
        self.ensure_hash(attempt, publication.manifest_sha256)
        if publication.uuid != str(attempt["uuid"]) or publication.fingerprint != attempt["author_fingerprint"]:
            raise SCIError("Stored publication identity differs from upload", "stored_conflict", 409)
        sources = {}
        for name, entry in publication.descriptors.items():
            key = base + name
            etag = self.storage.verify_object(self.storage.temporary, key, entry, name)
            sources[name] = {"key": key, "etag": etag, **entry}
        return publication, sources

    def complete(self, uuid, upload_id, manifest_hash):
        with self.database.locked(uuid) as conn:
            attempt = self.attempt_row(conn, upload_id)
            if not attempt:
                raise SCIError("Unknown upload attempt", "upload_not_found", 404)
            if str(attempt["uuid"]) != uuid or attempt["manifest_sha256"] != manifest_hash:
                raise SCIError("Completion identifiers differ from stored attempt", "upload_conflict", 409)
            existing = self.publication_row(conn, uuid)
            if existing:
                self.ensure_hash(existing, manifest_hash)
                if existing["state"] == "failed":
                    self.failure(existing)
                if existing["state"] == "accepted":
                    return 200, self.outcome(existing)
                return 201, self.finalize(conn, existing)
            self.check_writes(conn)
            if attempt["state"] not in {"pending", "verifying"}:
                raise SCIError("Upload attempt has ended or is not prepared", "upload_ended", 409)
            if attempt["deadline"] <= utcnow():
                conn.execute("UPDATE sci_private.uploads SET state='expired' WHERE upload_id=%s", (upload_id,))
                raise SCIError("Upload deadline expired; prepare a new attempt", "upload_expired", 409)
            conn.execute("UPDATE sci_private.uploads SET state='verifying' WHERE upload_id=%s", (upload_id,))
            try:
                publication, sources = self.verify_attempt(attempt)
                with conn.transaction():
                    self.check_writes(conn)
                    self.database.whitelist(conn, publication)
                    # Deadline is fixed and must still hold at the commit boundary.
                    if attempt["deadline"] <= utcnow():
                        raise SCIError("Upload deadline expired during verification", "upload_expired", 409)
                    insert_publication(
                        conn, publication, accepted_at=utcnow(), instance_url=self.settings.instance_url,
                        archive_url=self.settings.archive_url, state="finalizing", upload_id=upload_id,
                    )
                    conn.execute(
                        "UPDATE sci_private.uploads SET state='committed', sources=%s WHERE upload_id=%s",
                        (Jsonb(sources), upload_id),
                    )
            except (SCIError, MissingSource, ChangedSource) as exc:
                conn.execute(
                    "UPDATE sci_private.uploads SET state='failed', failure_reason=%s WHERE upload_id=%s",
                    (str(exc), upload_id),
                )
                if isinstance(exc, SCIError):
                    raise
                raise SCIError("Uploaded file is missing or changed; prepare and upload again",
                               "invalid_upload", 400) from exc
            return 201, self.finalize(conn, self.publication_row(conn, uuid))

    @staticmethod
    def acceptance(row):
        return {
            "schema_version": 1, "instance_url": row["instance_url"], "uuid": str(row["uuid"]),
            "manifest_sha256": row["manifest_sha256"], "accepted_at": timestamp(row["accepted_at"]),
        }

    def check_acceptance(self, row):
        key = f"publications/{row['uuid']}/acceptance.json"
        if self.storage.head(self.storage.archive, key) is None:
            return False
        data, _ = self.storage.read(self.storage.archive, key, 4096)
        actual = validate_acceptance(strict_json(data, "acceptance.json"), str(row["uuid"]),
                                     row["manifest_sha256"])
        if actual != self.acceptance(row):
            raise SCIError("Archive acceptance record conflicts with SQL; operator reconciliation required",
                           "archive_conflict", 409)
        return True

    def mark_accepted(self, conn, row):
        with conn.transaction():
            conn.execute("UPDATE sci_private.publications SET state='accepted' WHERE uuid=%s AND state='finalizing'",
                         (row["uuid"],))
            conn.execute("UPDATE sci_private.uploads SET state='accepted' WHERE upload_id=%s", (row["upload_id"],))
        return self.outcome(row)

    def verify_destination(self, key, entry, name):
        try:
            self.storage.verify_object(self.storage.archive, key, entry, name)
        except (MissingSource, ChangedSource) as exc:
            # A missing destination after an uncertain copy is not proof of source loss.
            raise SCIError("Archive copy is not yet readable; retry the same publication",
                           "archive_unavailable", 503) from exc
        except SCIError as exc:
            raise SCIError("Conflicting archive object; operator reconciliation required: " + name,
                           "archive_conflict", 409) from exc

    def finalize(self, conn, row):
        # Caller holds the session advisory lock from before state lookup until all
        # copies, acceptance writes, and terminal transitions have finished.
        if row["state"] == "failed":
            self.failure(row)
        if row["state"] == "accepted":
            return self.outcome(row)
        attempt = self.attempt_row(conn, row["upload_id"])
        sources = attempt["sources"]
        if self.check_acceptance(row):
            return self.mark_accepted(conn, row)
        try:
            for name, source in sources.items():
                destination = f"publications/{row['uuid']}/{name}"
                if self.storage.head(self.storage.archive, destination) is not None:
                    self.verify_destination(destination, source, name)
                    continue
                self.storage.copy_verified(source["key"], destination, source["etag"], name)
                self.verify_destination(destination, source, name)
        except (MissingSource, ChangedSource):
            if self.check_acceptance(row):
                return self.mark_accepted(conn, row)
            reason = "Verified source needed for a copy is missing or its ETag changed"
            with conn.transaction():
                conn.execute(
                    "UPDATE sci_private.publications SET state='failed', failure_reason=%s WHERE uuid=%s",
                    (reason, row["uuid"]),
                )
                conn.execute(
                    "UPDATE sci_private.uploads SET state='failed', failure_reason=%s WHERE upload_id=%s",
                    (reason, row["upload_id"]),
                )
            row["failure_reason"] = reason
            self.failure(row)
        acceptance = compact(self.acceptance(row))
        try:
            self.storage.put(self.storage.archive, f"publications/{row['uuid']}/acceptance.json",
                             acceptance, "acceptance.json", exclusive=True)
        except ClientError as exc:
            if not precondition(exc):
                raise
            self.check_acceptance(row)
        # A timeout after PUT remains retryable. The next worker checks durable acceptance.
        return self.mark_accepted(conn, row)

    def recover(self):
        with self.database.pool.connection() as conn:
            uuids = [str(r["uuid"]) for r in conn.execute(
                "SELECT uuid FROM sci_private.publications WHERE state='finalizing' ORDER BY accepted_at"
            )]
        errors = []
        for uuid in uuids:
            try:
                with self.database.locked(uuid) as conn:
                    self.finalize(conn, self.publication_row(conn, uuid))
            except Exception as exc:
                # Keep working on other UUIDs; never convert transient failures to terminal states.
                errors.append((uuid, type(exc).__name__))
        return errors

    def cleanup(self):
        with self.database.pool.connection() as conn:
            attempts = conn.execute("""
                SELECT upload_id, uuid FROM sci_private.uploads
                WHERE cleaned_at IS NULL AND (
                  state IN ('expired','failed','accepted') OR
                  (state IN ('preparing','pending','verifying') AND deadline <= clock_timestamp())
                )
            """).fetchall()
        for item in attempts:
            with self.database.locked(str(item["uuid"])) as conn:
                attempt = self.attempt_row(conn, item["upload_id"])
                if attempt["state"] in {"preparing", "pending", "verifying"}:
                    conn.execute("UPDATE sci_private.uploads SET state='expired' WHERE upload_id=%s",
                                 (item["upload_id"],))
                    attempt["state"] = "expired"
                self.cleanup_attempt(conn, attempt)
        with self.database.pool.connection() as conn:
            conn.execute("DELETE FROM sci_private.rate_limits WHERE window_start < clock_timestamp() - interval '10 minutes'")
