"""Service configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .storage import Storage

# About $200 per month of R2 Standard storage at $0.015 per GB-month.
DEFAULT_MAX_STORAGE_BYTES = 13_000_000_000_000



def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")



@dataclass
class Settings:
    instance_url: str
    archive_url: str
    database_url: str
    s3_endpoint: str
    s3_access_key_id: str
    s3_secret_access_key: str
    temp_bucket: str
    public_bucket: str
    s3_region: str = "auto"
    s3_presign_endpoint: str | None = None
    public_prefix: str = "publications/"
    upload_ttl_seconds: int = 3600
    rate_limit_per_minute: int = 30
    client_ip_header: str | None = None
    presign_content_length: bool = False
    writes_enabled: bool = True
    worker_interval_seconds: float = 60.0
    expiry_grace_seconds: int = 60
    max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES

    @classmethod
    def from_env(cls) -> "Settings":
        def req(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise SystemExit(f"missing required environment variable {name}")
            return v

        return cls(
            instance_url=req("SCI_INSTANCE_URL"),
            archive_url=req("SCI_ARCHIVE_URL"),
            database_url=req("SCI_DATABASE_URL"),
            s3_endpoint=req("SCI_S3_ENDPOINT"),
            s3_access_key_id=req("SCI_S3_ACCESS_KEY_ID"),
            s3_secret_access_key=req("SCI_S3_SECRET_ACCESS_KEY"),
            temp_bucket=req("SCI_TEMP_BUCKET"),
            public_bucket=req("SCI_PUBLIC_BUCKET"),
            s3_region=os.environ.get("SCI_S3_REGION") or "auto",
            s3_presign_endpoint=os.environ.get("SCI_S3_PRESIGN_ENDPOINT") or None,
            public_prefix=os.environ.get("SCI_PUBLIC_PREFIX") or "publications/",
            upload_ttl_seconds=int(os.environ.get("SCI_UPLOAD_TTL_SECONDS") or 3600),
            rate_limit_per_minute=int(os.environ.get("SCI_RATE_LIMIT_PER_MINUTE") or 30),
            client_ip_header=(os.environ.get("SCI_CLIENT_IP_HEADER") or None),
            presign_content_length=_env_bool("SCI_PRESIGN_CONTENT_LENGTH", False),
            writes_enabled=_env_bool("SCI_WRITES_ENABLED", True),
            worker_interval_seconds=float(os.environ.get("SCI_WORKER_INTERVAL_SECONDS") or 60),
            max_storage_bytes=int(os.environ.get("SCI_MAX_STORAGE_BYTES") or DEFAULT_MAX_STORAGE_BYTES),
        )

    def storage(self) -> Storage:
        return Storage(
            endpoint=self.s3_endpoint,
            presign_endpoint=self.s3_presign_endpoint,
            access_key_id=self.s3_access_key_id,
            secret_access_key=self.s3_secret_access_key,
            temp_bucket=self.temp_bucket,
            public_bucket=self.public_bucket,
            region=self.s3_region,
            public_prefix=self.public_prefix,
        )

