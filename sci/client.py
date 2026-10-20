"""Instance discovery and byte-preserving push/pull clients."""

import hashlib
import ipaddress
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from sci.errors import SCIError, invalid
from sci.publication import (
    _atomic_write,
    verify_documents,
    verify_publication,
)
from sci.validation import (
    HASH_RE,
    JSON_LIMIT,
    compact,
    fields,
    strict_json,
    validate_timestamp,
    validate_uuid,
)


def validate_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise invalid("URL", "use an absolute HTTP(S) URL without whitespace")
    try:
        parts = urlsplit(value)
        port = parts.port
        del port
    except ValueError as exc:
        raise invalid("URL", str(exc)) from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or "\\" in value):
        raise invalid("URL", "requires an absolute HTTP(S) base URL without credentials, query, or fragment")
    if parts.scheme == "http":
        local = parts.hostname in {"localhost"} or parts.hostname.endswith((".localhost", ".local"))
        try:
            local |= ipaddress.ip_address(parts.hostname).is_private
        except ValueError:
            pass
        if not local:
            raise invalid("URL", "public deployments require HTTPS; HTTP is only for private/local addresses")
    return value


def append_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path


def config_path() -> Path:
    return Path.home() / ".sci" / "config.json"


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("expected an object")
        return result
    except (ValueError, OSError) as exc:
        raise SCIError(f"Cannot read {path}: {exc}") from exc


def set_server(url):
    validate_url(url)
    config = load_config()
    config["server"] = url
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, compact(config))
    return url


def resolve_server(override=None):
    value = override or load_config().get("server")
    if not value:
        raise SCIError("No server configured. Run sci server URL or use --server URL.")
    validate_url(value)
    print(f"Server: {value}", file=sys.stderr)
    return value


def default_key():
    return Path(load_config().get("key", "~/.sci/key.pem")).expanduser()


def http_client():
    # Never forward descriptor keys or upload headers through redirects.
    return httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False)


def response_error(response):
    if 200 <= response.status_code < 300:
        return
    try:
        error = response.json().get("error", {})
        message = error.get("message", response.reason_phrase)
        code = error.get("code", "http_error")
    except (ValueError, AttributeError):
        message, code = response.reason_phrase, "http_error"
    raise SCIError(f"HTTP {response.status_code}: {message}", code, response.status_code)


def request_retry(client, method, url, **kwargs):
    for attempt in range(3):
        try:
            result = client.request(method, url, **kwargs)
            if result.status_code not in {429, 502, 503, 504} or attempt == 2:
                response_error(result)
                return result
        except httpx.TransportError:
            if attempt == 2:
                raise
        time.sleep(0.25 * 2**attempt)


def download(client, url, destination=None, maximum=JSON_LIMIT):
    received = 0
    data = bytearray()
    with client.stream("GET", url) as response:
        response_error(response)
        for block in response.iter_bytes(64 * 1024):
            received += len(block)
            if received > maximum:
                raise SCIError(f"Archive object exceeds {maximum} bytes", "too_large", 413)
            if destination is None:
                data.extend(block)
            else:
                destination.write(block)
    return bytes(data)


def validate_descriptor(data):
    fields(data, {"schema_version", "publishing_api_url", "catalog_url", "catalog_schema", "archive_url"},
           {"supabase_publishable_key"}, "sci-server.json")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise invalid("sci-server.json.schema_version", "expected integer 1")
    if data["catalog_schema"] != "catalog_v1":
        raise invalid("catalog_schema", "this client requires catalog_v1")
    for key in ("publishing_api_url", "catalog_url", "archive_url"):
        validate_url(data[key])
    if "supabase_publishable_key" in data:
        key = data["supabase_publishable_key"]
        if not isinstance(key, str) or not key.startswith("sb_publishable_") or len(key) > 512:
            raise invalid("supabase_publishable_key", "expected a public sb_publishable_ key")
    return data


def discover(client, server):
    return validate_descriptor(strict_json(
        download(client, append_url(validate_url(server), "sci-server.json"), maximum=16_384),
        "sci-server.json",
    ))


def validate_acceptance(data, uuid, manifest_hash=None):
    fields(data, {"schema_version", "instance_url", "uuid", "manifest_sha256", "accepted_at"},
           set(), "acceptance.json")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise invalid("acceptance.json.schema_version", "expected integer 1")
    validate_uuid(data["uuid"])
    if data["uuid"] != uuid:
        raise invalid("acceptance.json.uuid", "does not match requested UUID")
    if not isinstance(data["manifest_sha256"], str) or not HASH_RE.fullmatch(data["manifest_sha256"]):
        raise invalid("acceptance.json.manifest_sha256", "invalid manifest hash")
    if manifest_hash is not None and data["manifest_sha256"] != manifest_hash:
        raise invalid("acceptance.json.manifest_sha256", "does not match original manifest")
    validate_url(data["instance_url"])
    validate_timestamp(data["accepted_at"], "acceptance.json.accepted_at")
    return data


def push(folder, server, client=None):
    publication = verify_publication(folder)
    folder = Path(folder)
    owns_client = client is None
    client = client or http_client()
    try:
        descriptor = discover(client, server)
        response = request_retry(client, "POST", append_url(descriptor["publishing_api_url"], "publications"),
                                 files={
                                     "sci": ("sci.json", publication.sci_bytes, "application/json"),
                                     "manifest": ("manifest.json", publication.manifest_bytes, "application/json"),
                                     "signature": ("manifest.json.sig", publication.signature, "application/octet-stream"),
                                 }).json()
        if response.get("uuid") != publication.uuid or response.get("manifest_sha256") != publication.manifest_sha256:
            raise SCIError("Publisher returned a different publication identity")
        if response.get("status") == "accepted":
            return response
        uploads = response.get("uploads", {})
        allowed = set(publication.manifest["files"]) & {"README.md", "files.zip", "headline_figure.png"}
        if response.get("status") == "pending" and set(uploads) != allowed:
            raise SCIError("Publisher returned incorrect upload destinations")
        for name, upload in uploads.items():
            if name not in allowed:
                raise SCIError("Publisher requested an undeclared upload")
            # Presigned URLs have queries, so validate the origin/base separately.
            parts = urlsplit(upload["url"])
            validate_url(parts._replace(query="", fragment="").geturl())
            if parts.fragment:
                raise SCIError("Invalid upload URL")
            print(f"Uploading {name}", file=sys.stderr)
            with (folder / name).open("rb") as stream:
                result = client.put(upload["url"], content=stream, headers=upload.get("headers", {}))
                response_error(result)
        result = request_retry(
            client, "POST", append_url(descriptor["publishing_api_url"], f"publications/{publication.uuid}/complete"),
            json={"upload_id": response["upload_id"], "manifest_sha256": publication.manifest_sha256},
        ).json()
        if (result.get("status") != "accepted" or result.get("uuid") != publication.uuid
                or result.get("manifest_sha256") != publication.manifest_sha256):
            raise SCIError("Publication is not yet accepted; retry sci push with the same folder")
        return result
    finally:
        if owns_client:
            client.close()


def pull(uuid, server, output=None, client=None):
    validate_uuid(uuid)
    destination = Path(output or uuid)
    if destination.is_symlink() or (destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    )):
        raise SCIError(f"Refusing to overwrite a nonempty or unsafe destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".sci-pull-", dir=destination.parent))
    owns_client = client is None
    client = client or http_client()
    try:
        descriptor = discover(client, server)
        base = append_url(descriptor["archive_url"], uuid)
        acceptance = strict_json(download(client, append_url(base, "acceptance.json"), maximum=4096),
                                 "acceptance.json")
        validate_acceptance(acceptance, uuid)
        manifest = download(client, append_url(base, "manifest.json"))
        signature = download(client, append_url(base, "manifest.json.sig"), maximum=64)
        validate_acceptance(acceptance, uuid, hashlib.sha256(manifest).hexdigest())
        metadata = download(client, append_url(base, "sci.json"), maximum=JSON_LIMIT - len(manifest))
        publication = verify_documents(manifest, metadata, signature)
        if publication.uuid != uuid:
            raise SCIError("Signed manifest UUID differs from requested UUID")
        for name, data in {"manifest.json": manifest, "manifest.json.sig": signature, "sci.json": metadata}.items():
            (stage / name).write_bytes(data)
        for name, entry in publication.manifest["files"].items():
            if name == "sci.json":
                continue
            with (stage / name).open("xb") as out:
                download(client, append_url(base, name), out, entry["bytes"])
        verify_publication(stage)
        if destination.exists():
            destination.rmdir()  # Fails if another process filled it during the download.
        stage.rename(destination)
        return {"uuid": uuid, "fingerprint": publication.fingerprint, "path": str(destination)}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if owns_client:
            client.close()
