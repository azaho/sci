"""Apply the SCI database schema.

    SCI_ADMIN_DATABASE_URL=postgresql://... python -m db migrate

The SQL files in this folder are idempotent: each run applies all of them, in
the order below, in one transaction, and never drops data. Each file owns one
area of the schema and the privileges on its own objects.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

SQL_DIR = Path(__file__).resolve().parent
MIGRATION_FILES = ("base.sql", "publications.sql", "citations.sql", "catalog.sql", "admin.sql")


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


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m db", description="SCI database administration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply the db/*.sql files in order (idempotent)")
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
    sys.exit(main())
