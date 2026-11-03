"""Rebuild the derived catalog from the durable archive (python -m server.app rebuild-catalog)."""

from __future__ import annotations

import logging
import sys

import psycopg
from psycopg.rows import dict_row

from sci import keys
from sci import publication as pub

from . import database as dbm
from .database import Database
from .settings import Settings
from .storage import Storage

log = logging.getLogger("sci.server")


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

