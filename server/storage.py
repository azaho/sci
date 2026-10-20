"""Storage operations. There is no archive parser in this module or the server."""

import hashlib
from contextlib import contextmanager

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from sci.errors import SCIError
from sci.publication import verify_stream

CONTENT_TYPES = {
    "manifest.json": "application/json",
    "sci.json": "application/json",
    "acceptance.json": "application/json",
    "manifest.json.sig": "application/octet-stream",
    "README.md": "text/plain; charset=utf-8",
    "LICENSE": "text/plain; charset=utf-8",
    "files.zip": "application/zip",
    "headline_figure.png": "image/png",
}
UPLOAD_TYPES = {**CONTENT_TYPES, "README.md": "text/markdown; charset=utf-8"}


class MissingSource(Exception):
    pass


class ChangedSource(Exception):
    pass


def missing(exc):
    return exc.response["Error"]["Code"] in {"NoSuchKey", "NotFound", "404"}


def precondition(exc):
    return exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}


class Storage:
    def __init__(self, settings):
        self.settings = settings
        options = dict(
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=10, read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        self.client = boto3.client("s3", endpoint_url=settings.s3_endpoint, **options)
        self.signer = boto3.client("s3", endpoint_url=settings.s3_public_endpoint, **options)
        self.temporary = settings.temporary_bucket
        self.archive = settings.archive_bucket

    def head(self, bucket, key):
        try:
            return self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if missing(exc):
                return None
            raise

    @contextmanager
    def stream(self, bucket, key, etag=None):
        args = {"Bucket": bucket, "Key": key}
        if etag is not None:
            args["IfMatch"] = etag
        try:
            response = self.client.get_object(**args)
        except ClientError as exc:
            if missing(exc):
                raise MissingSource(key) from exc
            if precondition(exc):
                raise ChangedSource(key) from exc
            raise
        try:
            yield response["Body"], response["ETag"]
        finally:
            response["Body"].close()

    def read(self, bucket, key, maximum):
        with self.stream(bucket, key) as (stream, etag):
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise SCIError(f"Stored {key} exceeds its size limit")
        return data, etag

    def put(self, bucket, key, data, name, *, exclusive=False):
        args = dict(Bucket=bucket, Key=key, Body=data, ContentType=CONTENT_TYPES[name])
        if name == "files.zip":
            args["ContentDisposition"] = 'attachment; filename="files.zip"'
        if exclusive:
            args["IfNoneMatch"] = "*"
        return self.client.put_object(**args)

    def write_original(self, key, data, name):
        existing = self.head(self.temporary, key)
        if existing:
            actual, _ = self.read(self.temporary, key, len(data))
            if actual != data:
                raise SCIError(f"Stored original conflicts: {name}", "stored_conflict", 409)
            return
        self.put(self.temporary, key, data, name, exclusive=True)

    def upload_url(self, key, name, size, lifetime):
        content_type = UPLOAD_TYPES[name]
        params = {"Bucket": self.temporary, "Key": key, "ContentType": content_type}
        headers = {"Content-Type": content_type}
        if self.settings.bind_upload_size:
            params["ContentLength"] = size
            headers["Content-Length"] = str(size)
        return {
            "url": self.signer.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=max(1, lifetime), HttpMethod="PUT",
            ),
            "headers": headers,
        }

    def verify_object(self, bucket, key, entry, name, expected_etag=None):
        with self.stream(bucket, key, expected_etag) as (stream, etag):
            verify_stream(stream, entry, name)
        return etag

    def copy_verified(self, source, destination, etag, name):
        # SQL serializes destination writes; the archive bucket lock prevents overwrites.
        # CopySourceIfMatch binds the copy to the exact source verified before commit.
        args = dict(
            Bucket=self.archive, Key=destination,
            CopySource={"Bucket": self.temporary, "Key": source},
            CopySourceIfMatch=etag, MetadataDirective="REPLACE", ContentType=CONTENT_TYPES[name],
        )
        if name == "files.zip":
            args["ContentDisposition"] = 'attachment; filename="files.zip"'
        try:
            self.client.copy_object(**args)
        except ClientError as exc:
            if missing(exc):
                raise MissingSource(source) from exc
            if precondition(exc):
                raise ChangedSource(source) from exc
            raise

    def delete_temporary(self, prefix):
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.temporary, Prefix=prefix + "/"
        ):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                result = self.client.delete_objects(Bucket=self.temporary, Delete={"Objects": keys})
                if result.get("Errors"):
                    raise RuntimeError("Temporary-object cleanup was incomplete")

    def acceptance_keys(self):
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.archive, Prefix="publications/"
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.count("/") == 2 and key.endswith("/acceptance.json"):
                    yield key


def descriptor(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
