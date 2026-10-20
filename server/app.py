import asyncio
import contextlib
import hashlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from sci.errors import SCIError
from sci.publication import verify_documents
from sci.validation import HASH_RE, JSON_LIMIT, fields, strict_json, validate_uuid
from server.database import Database
from server.publishing import Publisher
from server.settings import Settings
from server.storage import Storage

logger = logging.getLogger("sci.publisher")


async def multipart_documents(request: Request):
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary or len(boundary) > 70:
        raise SCIError("Expected multipart/form-data with a valid boundary")
    parts = {}
    current = {}
    total_json = 0
    ended = False
    header_bytes = 0
    header_name, header_value = bytearray(), bytearray()

    def begin():
        nonlocal current
        current = {"headers": {}, "body": bytearray()}

    def header_field(data, start, end):
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > 4096:
            raise SCIError("Multipart headers are too large", "too_large", 413)
        header_name.extend(data[start:end])

    def header_data(data, start, end):
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > 4096:
            raise SCIError("Multipart headers are too large", "too_large", 413)
        header_value.extend(data[start:end])

    def header_end():
        name = bytes(header_name).lower()
        if name in current["headers"]:
            raise SCIError("Duplicate multipart header")
        current["headers"][name] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_finished():
        disposition, parameters = parse_options_header(current["headers"].get(b"content-disposition", b""))
        name = parameters.get(b"name")
        if disposition != b"form-data" or name not in {b"sci", b"manifest", b"signature"}:
            raise SCIError("Expected exactly sci, manifest, and signature multipart fields")
        name = name.decode("ascii")
        if name in parts:
            raise SCIError("Duplicate multipart field: " + name)
        if name == "signature" and current["headers"].get(b"content-type") != b"application/octet-stream":
            raise SCIError("signature must be application/octet-stream")
        current["name"] = name

    def data_part(data, start, end):
        nonlocal total_json
        count = end - start
        if current["name"] in {"sci", "manifest"}:
            total_json += count
            if total_json > JSON_LIMIT:
                raise SCIError("sci.json + manifest.json exceed 65,536 bytes", "too_large", 413)
        elif len(current["body"]) + count > 64:
            raise SCIError("signature must be exactly 64 raw bytes", "invalid_signature", 403)
        current["body"].extend(data[start:end])

    def end_part():
        if "name" not in current:
            raise SCIError("Malformed multipart field")
        parts[current["name"]] = bytes(current["body"])

    def end():
        nonlocal ended
        ended = True

    parser = MultipartParser(boundary, {
        "on_part_begin": begin, "on_header_field": header_field, "on_header_value": header_data,
        "on_header_end": header_end, "on_headers_finished": headers_finished,
        "on_part_data": data_part, "on_part_end": end_part, "on_end": end,
    })
    received = 0
    try:
        async for block in request.stream():
            received += len(block)
            if received > JSON_LIMIT + 8192:
                raise SCIError("Multipart request is too large", "too_large", 413)
            parser.write(block)
        parser.finalize()
    except (ValueError, KeyError) as exc:
        raise SCIError("Malformed multipart body") from exc
    if not ended or set(parts) != {"sci", "manifest", "signature"}:
        raise SCIError("Incomplete multipart request")
    return parts


def create_app(settings=None, database=None, storage=None, *, background=True):
    @asynccontextmanager
    async def lifespan(app):
        configured = settings or Settings.from_env()
        db = database or await run_in_threadpool(Database, configured.database_url)
        objects = storage or Storage(configured)
        app.state.publisher = Publisher(db, objects, configured)

        async def maintenance():
            while True:
                try:
                    errors = await run_in_threadpool(app.state.publisher.recover)
                    for uuid, kind in errors:
                        logger.warning("Recovery pending for %s (%s)", uuid, kind)
                    await run_in_threadpool(app.state.publisher.cleanup)
                except Exception as exc:
                    logger.warning("Maintenance will retry (%s)", type(exc).__name__)
                await asyncio.sleep(configured.recovery_seconds)

        task = asyncio.create_task(maintenance()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if database is None:
                await run_in_threadpool(db.close)

    app = FastAPI(title="SCI publisher", version="1", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Publishing has no browser credential. Signatures and the whitelist govern admission.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST", "GET"],
                       allow_headers=["Content-Type"], expose_headers=["Retry-After"])

    @app.middleware("http")
    async def headers_and_errors(request, call_next):
        try:
            if request.url.path.startswith("/publications"):
                publisher = request.app.state.publisher
                # Do not trust arbitrary X-Forwarded-For headers. Configure the ASGI
                # server's trusted proxy addresses on the actual hosting network.
                address = request.client.host if request.client else "unknown"
                bucket = hashlib.sha256(address.encode()).hexdigest()
                await run_in_threadpool(publisher.database.rate_limit, bucket,
                                        publisher.settings.requests_per_minute)
            response = await call_next(request)
        except SCIError as exc:
            response = JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)
        except Exception as exc:
            logger.error("Publishing request will need a retry (%s)", type(exc).__name__)
            response = JSONResponse(
                {"error": {"code": "temporarily_unavailable", "message": "Service unavailable; retry the same request"}},
                status_code=503,
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        if response.status_code == 429:
            response.headers["Retry-After"] = "60"
        return response

    @app.exception_handler(SCIError)
    async def sci_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": {"code": "invalid_input", "message": "Invalid request fields"}}, status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": {"code": "http_error", "message": str(exc.detail)}}, status_code=exc.status_code)

    @app.get("/health")
    def health(request: Request):
        publisher = request.app.state.publisher
        with publisher.database.pool.connection() as conn:
            conn.execute("SELECT writes_enabled FROM sci_private.instance_state WHERE singleton").fetchone()
        publisher.storage.client.head_bucket(Bucket=publisher.storage.temporary)
        publisher.storage.client.head_bucket(Bucket=publisher.storage.archive)
        return {"status": "ready", "schema_version": 1}

    @app.post("/publications")
    async def prepare(request: Request):
        parts = await multipart_documents(request)
        publication = await run_in_threadpool(verify_documents, parts["manifest"], parts["sci"], parts["signature"])
        status, outcome = await run_in_threadpool(request.app.state.publisher.prepare, publication)
        return JSONResponse(outcome, status_code=status)

    @app.post("/publications/{uuid}/complete")
    async def complete(uuid: str, request: Request):
        validate_uuid(uuid)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 512:
                raise SCIError("Completion body exceeds 512 bytes", "too_large", 413)
            body.extend(chunk)
        data = strict_json(bytes(body), "completion")
        fields(data, {"upload_id", "manifest_sha256"}, set(), "completion")
        validate_uuid(data["upload_id"], "upload_id")
        if not isinstance(data["manifest_sha256"], str) or not HASH_RE.fullmatch(data["manifest_sha256"]):
            raise SCIError("manifest_sha256 must contain 64 lowercase hex characters")
        status, outcome = await run_in_threadpool(
            request.app.state.publisher.complete, uuid, data["upload_id"], data["manifest_sha256"],
        )
        return JSONResponse(outcome, status_code=status)

    return app


app = create_app()
