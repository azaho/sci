"""PostgreSQL access for the publishing service and administrative commands.

The service connects as ``sci_publisher``. Migrations run as the database
administrator:

    SCI_ADMIN_DATABASE_URL=postgresql://... python -m server.database migrate

Migrations apply schema.sql, catalog.sql, and admin.sql in one transaction.
The files are idempotent and never drop data.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("sci.database")

SQL_DIR = Path(__file__).resolve().parent
MIGRATION_FILES = ("schema.sql", "catalog.sql", "admin.sql")


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10):
        self.pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    @contextlib.contextmanager
    def connection(self):
        with self.pool.connection() as conn:
            yield conn

    # -- locks ---------------------------------------------------------------

    @staticmethod
    def xact_lock(conn, kind: str, key: str, shared: bool = False) -> None:
        fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        conn.execute(sql.SQL("select {}(sci.lock_key(%s, %s))").format(sql.Identifier(fn)), (kind, key))

    @contextlib.contextmanager
    def session_lock(self, kind: str, key: str, wait_seconds: float):
        """Hold a session advisory lock on a dedicated connection.

        Yields (acquired, connection); ``acquired`` is False after
        ``wait_seconds``. A broken connection releases the lock automatically.
        """
        with self.pool.connection() as conn:
            deadline = time.monotonic() + wait_seconds
            acquired = False
            while True:
                row = conn.execute("select pg_try_advisory_lock(sci.lock_key(%s, %s)) as ok", (kind, key)).fetchone()
                if row["ok"]:
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            try:
                yield acquired, conn
            finally:
                if acquired:
                    try:
                        conn.execute("select pg_advisory_unlock(sci.lock_key(%s, %s))", (kind, key))
                    except psycopg.Error:
                        log.exception("failed to release advisory lock %s:%s", kind, key)

    # -- settings -------------------------------------------------------------

    def save_settings(self, instance_url: str, archive_url: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                insert into sci.settings (id, instance_url, archive_url) values (true, %s, %s)
                on conflict (id) do update set instance_url = excluded.instance_url,
                  archive_url = excluded.archive_url, updated_at = now()
                where (sci.settings.instance_url, sci.settings.archive_url)
                      is distinct from (excluded.instance_url, excluded.archive_url)
                """,
                (instance_url, archive_url),
            )

    def ping(self) -> None:
        with self.connection() as conn:
            conn.execute("select 1")


# -- row helpers -----------------------------------------------------------------


def get_publication(conn, publication_uuid: str, *, for_update: bool = False) -> dict | None:
    q = "select * from sci.publications where uuid = %s" + (" for update" if for_update else "")
    return conn.execute(q, (publication_uuid,)).fetchone()


def get_identity(conn, fingerprint: str) -> dict | None:
    return conn.execute("select * from sci.identities where fingerprint = %s", (fingerprint,)).fetchone()


def get_active_upload(conn, publication_uuid: str) -> dict | None:
    return conn.execute(
        "select *, deadline <= now() as expired from sci.uploads where publication_uuid = %s and state = 'pending' for update",
        (publication_uuid,),
    ).fetchone()


def get_upload(conn, upload_id: str, *, for_update: bool = False) -> dict | None:
    q = "select *, deadline <= now() as expired from sci.uploads where upload_id = %s" + (" for update" if for_update else "")
    return conn.execute(q, (upload_id,)).fetchone()


def insert_publication(conn, *, docs, upload_id: str | None, state: str, accepted_at=None, object_prefix: str) -> dict:
    """Insert extracted catalog fields and ordered citations for verified documents."""
    m, meta = docs.manifest, docs.metadata
    row = conn.execute(
        """
        insert into sci.publications (
          uuid, manifest_sha256, state, author_fingerprint, author_public_key, title, abstract,
          categories, keywords, headline_figure_caption, created_at, files, files_tree,
          object_prefix, accepted_at, upload_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), %s)
        returning *
        """,
        (
            m.uuid,
            docs.manifest_sha256,
            state,
            m.fingerprint,
            m.public_key_b64,
            meta.title,
            meta.abstract,
            meta.categories,
            meta.keywords,
            meta.headline_figure_caption,
            m.created_at_dt,
            json.dumps({n: {"sha256": e.sha256, "bytes": e.bytes} for n, e in m.files.items()}),
            json.dumps(m.files_tree) if m.files_tree is not None else None,
            object_prefix,
            accepted_at,
            upload_id,
        ),
    ).fetchone()
    if meta.citations:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into sci.citations (source_uuid, position, target, target_key, relation, reason)
                values (%s, %s, %s, %s, %s, %s)
                """,
                [(m.uuid, c.position, c.target, c.target_key, c.relation, c.reason) for c in meta.citations],
            )
    return row


# -- migrations ------------------------------------------------------------------


def migrate(
    admin_dsn: str,
    *,
    publisher_password: str | None = None,
    authenticator_password: str | None = None,
    instance_url: str | None = None,
    archive_url: str | None = None,
) -> list[str]:
    """Apply the idempotent SQL files; return the names that changed."""
    changed = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.transaction():
            # Serialize concurrent migration runs.
            conn.execute("select pg_advisory_xact_lock(7271390001)")
            for name in MIGRATION_FILES:
                text = (SQL_DIR / name).read_text(encoding="utf-8")
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                conn.execute(text)
                prev = conn.execute("select sha256 from sci.schema_migrations where name = %s", (name,)).fetchone()
                if prev is None or prev[0] != digest:
                    changed.append(name)
                    conn.execute(
                        """
                        insert into sci.schema_migrations (name, sha256) values (%s, %s)
                        on conflict (name) do update set sha256 = excluded.sha256, applied_at = now()
                        """,
                        (name, digest),
                    )
            if publisher_password:
                conn.execute(
                    sql.SQL("alter role sci_publisher login password {}").format(sql.Literal(publisher_password))
                )
            if authenticator_password:
                # Local PostgREST only; Supabase provides its own authenticator.
                exists = conn.execute("select 1 from pg_roles where rolname = 'authenticator'").fetchone()
                if not exists:
                    conn.execute("create role authenticator noinherit login")
                conn.execute(
                    sql.SQL("alter role authenticator login password {}").format(sql.Literal(authenticator_password))
                )
                conn.execute("grant anon to authenticator")
                conn.execute("alter role anon set statement_timeout = '5s'")
            if instance_url and archive_url:
                conn.execute(
                    """
                    insert into sci.settings (id, instance_url, archive_url) values (true, %s, %s)
                    on conflict (id) do update set instance_url = excluded.instance_url,
                      archive_url = excluded.archive_url, updated_at = now()
                    """,
                    (instance_url, archive_url),
                )
            conn.execute("notify pgrst, 'reload schema'")
    return changed


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m server.database", description="SCI database administration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply schema.sql, catalog.sql, and admin.sql (idempotent)")
    m.add_argument("--database-url", default=os.environ.get("SCI_ADMIN_DATABASE_URL"))
    args = ap.parse_args(argv)
    if args.cmd == "migrate":
        if not args.database_url:
            print("error: set SCI_ADMIN_DATABASE_URL or pass --database-url", file=sys.stderr)
            return 2
        changed = migrate(
            args.database_url,
            publisher_password=os.environ.get("SCI_PUBLISHER_DB_PASSWORD") or None,
            authenticator_password=os.environ.get("SCI_AUTHENTICATOR_PASSWORD") or None,
            instance_url=os.environ.get("SCI_INSTANCE_URL") or None,
            archive_url=os.environ.get("SCI_ARCHIVE_URL") or None,
        )
        print("migrations applied" + (f"; changed: {', '.join(changed)}" if changed else "; no changes"))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
