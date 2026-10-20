"""Checksum-verified, transactional migrations. Never reset existing data."""

import argparse
import hashlib
import os
from importlib.resources import files

import psycopg

MIGRATIONS = [
    ("001", ("schema.sql", "normalize.sql", "admin.sql", "catalog.sql")),
]


def migrate(url):
    with psycopg.connect(url, autocommit=True) as connection:
        # A transaction lock serializes initialization on fresh databases too.
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(764218940)")
            connection.execute("CREATE SCHEMA IF NOT EXISTS sci_private")
            connection.execute("REVOKE ALL ON SCHEMA sci_private FROM PUBLIC")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sci_private.schema_migrations(
                    version text PRIMARY KEY, sha256 text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )
            """)
            for version, names in MIGRATIONS:
                sources = [files("server").joinpath(name).read_text("utf-8") for name in names]
                digest = hashlib.sha256("\n".join(sources).encode()).hexdigest()
                existing = connection.execute(
                    "SELECT sha256 FROM sci_private.schema_migrations WHERE version=%s", (version,)
                ).fetchone()
                if existing:
                    if existing[0] != digest:
                        raise RuntimeError(f"Applied migration {version} changed. Add a new migration.")
                    continue
                for source in sources:
                    connection.execute(source)
                connection.execute(
                    "INSERT INTO sci_private.schema_migrations(version,sha256) VALUES (%s,%s)",
                    (version, digest),
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    migrate(os.environ["ADMIN_DATABASE_URL"])


if __name__ == "__main__":
    main()
