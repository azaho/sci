"""Idempotent local database, storage, and website initialization."""

import json
import os

import psycopg
from psycopg import sql

from deploy.site import build, descriptor_from_env
from server.migrate import migrate
from server.settings import Settings
from server.storage import Storage


def main():
    admin = os.environ["ADMIN_DATABASE_URL"]
    migrate(admin)
    with psycopg.connect(admin, autocommit=True) as conn:
        for role, variable in (
            ("sci_publisher", "LOCAL_PUBLISHER_PASSWORD"),
            ("sci_authenticator", "LOCAL_CATALOG_PASSWORD"),
        ):
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(os.environ[variable]),
            ))
    storage = Storage(Settings.from_env())
    existing = {b["Name"] for b in storage.client.list_buckets()["Buckets"]}
    for bucket in (storage.temporary, storage.archive):
        if bucket not in existing:
            storage.client.create_bucket(Bucket=bucket)
    storage.client.put_bucket_policy(Bucket=storage.archive, Policy=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{storage.archive}/publications/*"],
        }],
    }))
    # Never add a public policy or age-based lifecycle to the temporary bucket.
    build("/site", descriptor_from_env(), source="/app/web")


if __name__ == "__main__":
    main()
