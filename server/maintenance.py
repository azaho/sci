"""Recovery commands. Catalog rebuild requires an administrator database URL."""

import argparse
import dataclasses
import os
from contextlib import contextmanager

from sci.client import validate_acceptance
from sci.errors import SCIError
from sci.publication import verify_documents, verify_files
from sci.validation import JSON_LIMIT, strict_json, validate_uuid
from server.database import Database
from server.publishing import Publisher, insert_publication
from server.settings import Settings
from server.storage import Storage


def rebuild_catalog(database, storage, settings):
    """Rebuild accepted records only; keep writes off until SQL recovery is reconciled."""
    with database.pool.connection() as conn:
        conn.execute("UPDATE sci_private.instance_state SET writes_enabled=false WHERE singleton")
    restored = 0
    for key in storage.acceptance_keys():
        uuid = key.split("/")[1]
        validate_uuid(uuid)
        base = f"publications/{uuid}/"
        record, _ = storage.read(storage.archive, key, 4096)
        acceptance = validate_acceptance(strict_json(record, "acceptance.json"), uuid)
        manifest, _ = storage.read(storage.archive, base + "manifest.json", JSON_LIMIT)
        metadata, _ = storage.read(storage.archive, base + "sci.json", JSON_LIMIT - len(manifest))
        signature, _ = storage.read(storage.archive, base + "manifest.json.sig", 64)
        publication = verify_documents(manifest, metadata, signature)
        validate_acceptance(acceptance, publication.uuid, publication.manifest_sha256)
        if publication.uuid != uuid:
            raise SCIError(f"Archive directory UUID differs from manifest: {uuid}")

        @contextmanager
        def opener(name):
            with storage.stream(storage.archive, base + name) as (stream, _):
                yield stream
        verify_files(publication, opener)
        with database.locked(uuid) as conn, conn.transaction():
            current = Publisher.publication_row(conn, uuid)
            if current:
                Publisher.ensure_hash(current, publication.manifest_sha256)
                if current["state"] == "failed":
                    raise SCIError(f"Archive conflicts with terminal failed reservation {uuid}; reconcile SQL backups")
                expected = Publisher.acceptance(current)
                if expected != acceptance:
                    raise SCIError(f"Acceptance metadata differs from SQL for {uuid}; reconcile backups")
                conn.execute("UPDATE sci_private.publications SET state='accepted' WHERE uuid=%s", (uuid,))
                continue
            # An archived author key is not an enabled whitelist entry.
            conn.execute("""
                INSERT INTO sci_private.identities(fingerprint,public_key,enabled) VALUES (%s,%s,false)
                ON CONFLICT (fingerprint) DO NOTHING
            """, (publication.fingerprint, publication.manifest["public_key"]["x"]))
            insert_publication(conn, publication, state="accepted",
                               accepted_at=acceptance["accepted_at"], instance_url=acceptance["instance_url"],
                               archive_url=settings.archive_url)
            restored += 1
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["recover", "cleanup", "rebuild-catalog"])
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.operation == "rebuild-catalog":
        settings = dataclasses.replace(settings, database_url=os.environ["ADMIN_DATABASE_URL"])
    database = Database(settings.database_url)
    try:
        storage = Storage(settings)
        publisher = Publisher(database, storage, settings)
        if args.operation == "rebuild-catalog":
            count = rebuild_catalog(database, storage, settings)
            print(f"Rebuilt {count} catalog records. Writes remain disabled. Restore and reconcile operational SQL state.")
        elif args.operation == "recover":
            errors = publisher.recover()
            if errors:
                raise SystemExit(f"Recovery remains pending for {len(errors)} publication(s)")
        else:
            publisher.cleanup()
    finally:
        database.close()


if __name__ == "__main__":
    main()
