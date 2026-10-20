from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from sci.errors import SCIError


class Database:
    def __init__(self, url):
        self.pool = ConnectionPool(
            url, min_size=1, max_size=8, timeout=15, open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        self.pool.wait(timeout=20)

    def close(self):
        self.pool.close()

    @contextmanager
    def locked(self, uuid):
        # Use a direct or SESSION-pooled PostgreSQL connection. Transaction poolers
        # cannot safely support this lock across storage operations/SQL commits.
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", ("publication:" + uuid,))
            try:
                yield conn
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", ("publication:" + uuid,))

    @staticmethod
    def whitelist(conn, publication):
        # Admin helpers use this same transaction lock when they disable/enable.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("key:" + publication.fingerprint,))
        row = conn.execute(
            "SELECT public_key, enabled FROM sci_private.identities WHERE fingerprint=%s",
            (publication.fingerprint,),
        ).fetchone()
        if not row or not row["enabled"] or row["public_key"] != publication.manifest["public_key"]["x"]:
            raise SCIError("Author key is not enabled on this instance", "key_denied", 403)

    @staticmethod
    def require_writes(conn):
        row = conn.execute("SELECT writes_enabled FROM sci_private.instance_state WHERE singleton").fetchone()
        if not row or not row["writes_enabled"]:
            raise SCIError("Publishing is disabled pending operational recovery", "writes_disabled", 503)

    def rate_limit(self, key, maximum):
        with self.pool.connection() as conn:
            row = conn.execute("""
                INSERT INTO sci_private.rate_limits AS r(bucket, window_start, requests)
                VALUES (%s, date_trunc('minute', clock_timestamp()), 1)
                ON CONFLICT (bucket) DO UPDATE SET
                  requests = CASE WHEN r.window_start = date_trunc('minute', clock_timestamp())
                                  THEN r.requests + 1 ELSE 1 END,
                  window_start = date_trunc('minute', clock_timestamp())
                RETURNING requests
            """, (key,)).fetchone()
            if row["requests"] > maximum:
                raise SCIError("Request limit reached; retry after one minute", "rate_limited", 429)
