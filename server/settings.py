import os
from dataclasses import dataclass

from psycopg.conninfo import conninfo_to_dict

from sci.client import validate_url


@dataclass(frozen=True)
class Settings:
    database_url: str
    s3_endpoint: str
    s3_public_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    temporary_bucket: str
    archive_bucket: str
    instance_url: str
    archive_url: str
    region: str = "auto"
    upload_seconds: int = 900
    requests_per_minute: int = 120
    recovery_seconds: int = 30
    writes_enabled: bool = True
    bind_upload_size: bool = False

    def __post_init__(self):
        validate_url(self.instance_url)
        validate_url(self.archive_url)
        if self.upload_seconds < 30 or self.upload_seconds > 3600:
            raise ValueError("SCI_UPLOAD_SECONDS must be 30–3600")
        if self.temporary_bucket == self.archive_bucket:
            raise ValueError("Temporary and archive buckets must be different")
        database = conninfo_to_dict(self.database_url)
        if database.get("host", "").endswith(".pooler.supabase.com") and database.get("port") == "6543":
            raise ValueError("Use a direct PostgreSQL connection or the Supabase session pooler; port 6543 is transaction pooling")

    @classmethod
    def from_env(cls):
        endpoint = os.environ["S3_ENDPOINT_URL"]
        return cls(
            database_url=os.environ["PUBLISHER_DATABASE_URL"],
            s3_endpoint=endpoint,
            s3_public_endpoint=os.getenv("S3_PUBLIC_ENDPOINT_URL", endpoint),
            s3_access_key=os.environ["R2_ACCESS_KEY_ID"],
            s3_secret_key=os.environ["R2_SECRET_ACCESS_KEY"],
            temporary_bucket=os.environ["TEMPORARY_BUCKET"],
            archive_bucket=os.environ["ARCHIVE_BUCKET"],
            instance_url=os.environ["INSTANCE_URL"],
            archive_url=os.environ["ARCHIVE_URL"],
            region=os.getenv("S3_REGION", "auto"),
            upload_seconds=int(os.getenv("SCI_UPLOAD_SECONDS", "900")),
            requests_per_minute=int(os.getenv("SCI_REQUESTS_PER_MINUTE", "120")),
            recovery_seconds=int(os.getenv("SCI_RECOVERY_SECONDS", "30")),
            writes_enabled=os.getenv("SCI_WRITES_ENABLED", "true").lower() == "true",
            bind_upload_size=os.getenv("SCI_BIND_UPLOAD_SIZE", "false").lower() == "true",
        )
