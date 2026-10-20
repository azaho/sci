"""S3-compatible object storage (Cloudflare R2 in production).

Two buckets: a private temporary bucket for upload attempts and a public
bucket whose ``publications/<uuid>/`` prefix is the archive. Only the
server writes JSON, signature, license, and acceptance objects; clients
receive presigned PUT URLs for the README and declared attachments only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from sci import publication as pub

log = logging.getLogger("sci.storage")

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class StorageError(Exception):
    """A storage failure whose outcome is uncertain; callers retry."""


class SourceMissing(Exception):
    """A temporary source object is confirmed absent."""


class SourceChanged(Exception):
    """A temporary source object's ETag differs from the verified one."""


class AlreadyExists(Exception):
    """A conditional create found an existing object."""


@dataclass
class ObjectInfo:
    size: int
    etag: str
    content_type: str | None = None


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)


def _is_missing(exc: ClientError) -> bool:
    return _code(exc) in ("NoSuchKey", "404", "NotFound") or _status(exc) == 404


def _is_precondition(exc: ClientError) -> bool:
    return _code(exc) in ("PreconditionFailed", "412") or _status(exc) == 412


class Storage:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
        temp_bucket: str,
        public_bucket: str,
        region: str = "auto",
        presign_endpoint: str | None = None,
        public_prefix: str = "publications/",
    ):
        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        common = dict(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=cfg,
        )
        self.s3 = boto3.client("s3", endpoint_url=endpoint, **common)
        # Presigned URLs embed the host in the signature, so a local install
        # can sign for the host-visible address while the server talks to
        # the container-internal address.
        self.presigner = boto3.client("s3", endpoint_url=presign_endpoint or endpoint, **common)
        self.temp_bucket = temp_bucket
        self.public_bucket = public_bucket
        self.public_prefix = public_prefix.rstrip("/") + "/"

    # -- keys -------------------------------------------------------------

    def temp_key(self, temp_prefix: str, name: str) -> str:
        return f"{temp_prefix.rstrip('/')}/{name}"

    def public_key(self, publication_uuid: str, name: str) -> str:
        pub.check_uuid4(publication_uuid)
        return f"{self.public_prefix}{publication_uuid}/{name}"

    # -- generic operations ---------------------------------------------

    def _call(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except ClientError:
            raise
        except BotoCoreError as exc:
            raise StorageError(str(exc)) from exc

    def head(self, bucket: str, key: str) -> ObjectInfo | None:
        try:
            r = self._call(self.s3.head_object, Bucket=bucket, Key=key)
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise StorageError(f"HEAD {key}: {exc}") from exc
        return ObjectInfo(int(r["ContentLength"]), r["ETag"], r.get("ContentType"))

    def open(self, bucket: str, key: str):
        """Return (body stream, ObjectInfo) or None when absent."""
        try:
            r = self._call(self.s3.get_object, Bucket=bucket, Key=key)
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise StorageError(f"GET {key}: {exc}") from exc
        return r["Body"], ObjectInfo(int(r["ContentLength"]), r["ETag"], r.get("ContentType"))

    def read(self, bucket: str, key: str, limit: int) -> tuple[bytes, ObjectInfo] | None:
        opened = self.open(bucket, key)
        if opened is None:
            return None
        body, info = opened
        try:
            data = body.read(limit + 1)
        except (BotoCoreError, OSError) as exc:
            raise StorageError(f"GET {key}: {exc}") from exc
        finally:
            body.close()
        return data, info

    def hash_object(self, bucket: str, key: str, limit: int) -> tuple[str, int, ObjectInfo] | None:
        """Stream an object through SHA-256 without holding it in memory."""
        opened = self.open(bucket, key)
        if opened is None:
            return None
        body, info = opened
        try:
            sha, size = pub.hash_stream(body, limit)
        except (BotoCoreError, OSError) as exc:
            raise StorageError(f"GET {key}: {exc}") from exc
        finally:
            body.close()
        return sha, size, info

    # -- temporary bucket --------------------------------------------------

    def put_temp(self, key: str, data: bytes, content_type: str) -> str:
        try:
            r = self._call(self.s3.put_object, Bucket=self.temp_bucket, Key=key, Body=data, ContentType=content_type)
        except ClientError as exc:
            raise StorageError(f"PUT {key}: {exc}") from exc
        return r["ETag"]

    def presign_put(self, key: str, content_type: str, expires: int, content_length: int | None = None) -> dict:
        params = {"Bucket": self.temp_bucket, "Key": key, "ContentType": content_type}
        if content_length is not None:
            params["ContentLength"] = content_length
        url = self.presigner.generate_presigned_url("put_object", Params=params, ExpiresIn=max(1, int(expires)))
        headers = {"Content-Type": content_type}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        return {"url": url, "headers": headers}

    def delete_temp_prefix(self, prefix: str) -> int:
        prefix = prefix.rstrip("/") + "/"
        deleted = 0
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.temp_bucket, Prefix=prefix):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                for o in objs:
                    self.s3.delete_object(Bucket=self.temp_bucket, Key=o["Key"])
                    deleted += 1
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"cleanup {prefix}: {exc}") from exc
        return deleted

    # -- public archive -----------------------------------------------------

    def _public_headers(self, publication_uuid: str, name: str) -> dict:
        extra = {"ContentType": pub.CONTENT_TYPES[name], "CacheControl": IMMUTABLE_CACHE}
        if name == pub.FILES_ZIP:
            extra["ContentDisposition"] = f'attachment; filename="{publication_uuid}-files.zip"'
        elif name == pub.SIGNATURE:
            extra["ContentDisposition"] = 'attachment; filename="manifest.json.sig"'
        return extra

    def copy_to_public(self, temp_key: str, publication_uuid: str, name: str, source_etag: str) -> None:
        """Copy a verified temporary object into the archive.

        The copy succeeds only while the source still has the ETag observed
        during verification. Raises SourceMissing / SourceChanged for
        confirmed problems and StorageError for uncertain outcomes.
        """
        dest = self.public_key(publication_uuid, name)
        try:
            self._call(
                self.s3.copy_object,
                Bucket=self.public_bucket,
                Key=dest,
                CopySource={"Bucket": self.temp_bucket, "Key": temp_key},
                CopySourceIfMatch=source_etag,
                MetadataDirective="REPLACE",
                **self._public_headers(publication_uuid, name),
            )
        except ClientError as exc:
            if _is_precondition(exc):
                raise SourceChanged(f"{temp_key} changed after verification") from exc
            if _is_missing(exc):
                raise SourceMissing(f"{temp_key} is missing") from exc
            raise StorageError(f"COPY {temp_key} -> {dest}: {exc}") from exc

    def put_public_if_absent(self, publication_uuid: str, name: str, data: bytes) -> None:
        dest = self.public_key(publication_uuid, name)
        try:
            self._call(
                self.s3.put_object,
                Bucket=self.public_bucket,
                Key=dest,
                Body=data,
                IfNoneMatch="*",
                **self._public_headers(publication_uuid, name),
            )
        except ClientError as exc:
            if _is_precondition(exc) or _code(exc) in ("ConditionalRequestConflict",) or _status(exc) == 409:
                raise AlreadyExists(dest) from exc
            raise StorageError(f"PUT {dest}: {exc}") from exc

    def iter_acceptance_records(self) -> Iterator[tuple[str, str]]:
        """Yield (directory UUID text, key) for every acceptance record."""
        paginator = self.s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.public_bucket, Prefix=self.public_prefix):
                for o in page.get("Contents", []):
                    key = o["Key"]
                    rest = key[len(self.public_prefix):]
                    parts = rest.split("/")
                    if len(parts) == 2 and parts[1] == pub.ACCEPTANCE:
                        yield parts[0], key
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"list archive: {exc}") from exc

    HEALTH_PROBE_KEY = "sci-health-probe/absent"

    def check_buckets(self) -> None:
        """Check object access to both buckets.

        Uses an object-level HEAD on a key that does not exist, so a runtime
        token with only Object Read & Write permission passes (404 = access
        works). Bucket-level calls such as HeadBucket need more permission.
        """
        for b in (self.temp_bucket, self.public_bucket):
            try:
                self._call(self.s3.head_object, Bucket=b, Key=self.HEALTH_PROBE_KEY)
            except ClientError as exc:
                if _is_missing(exc):
                    continue
                raise StorageError(f"no object access to bucket {b}: {exc}") from exc

    def ensure_buckets(self, public_policy: bool = True) -> None:
        """Create missing buckets (local installations only)."""
        import json as _json

        for b in (self.temp_bucket, self.public_bucket):
            try:
                self.s3.head_bucket(Bucket=b)
            except ClientError:
                self.s3.create_bucket(Bucket=b)
                log.info("created bucket %s", b)
        if public_policy:
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{self.public_bucket}/{self.public_prefix}*"],
                    }
                ],
            }
            self.s3.put_bucket_policy(Bucket=self.public_bucket, Policy=_json.dumps(policy))
            try:
                self.s3.put_bucket_cors(
                    Bucket=self.public_bucket,
                    CORSConfiguration={
                        "CORSRules": [
                            {
                                "AllowedOrigins": ["*"],
                                "AllowedMethods": ["GET", "HEAD"],
                                "AllowedHeaders": ["*"],
                                "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
                                "MaxAgeSeconds": 3600,
                            }
                        ]
                    },
                )
            except ClientError as exc:  # Some local servers do not support bucket CORS.
                log.warning("could not set bucket CORS: %s", exc)
