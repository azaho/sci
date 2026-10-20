"""Shared fixtures.

Unit tests need only the ``sci`` package. Integration tests start real
services as subprocesses and skip when a binary is unavailable:

- PostgreSQL: binaries from the ``pgserver`` wheel (or SCI_TEST_PG_BIN).
- S3-compatible storage: RustFS (SCI_TEST_RUSTFS=/path/to/rustfs).
- PostgREST: SCI_TEST_POSTGREST=/path/to/postgrest.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid as _uuid
import zlib
from pathlib import Path

import pytest

from sci import keys
from sci import publication as pub

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def make_png(width: int = 4, height: int = 3, *, color: int = 2, depth: int = 8, extra_chunks=(), interlace: int = 0) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    row_bytes = (width * channels * depth + 7) // 8
    if interlace:
        passes = []
        for x0, y0, dx, dy in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
            pw = (width - x0 + dx - 1) // dx if width > x0 else 0
            ph = (height - y0 + dy - 1) // dy if height > y0 else 0
            if pw and ph:
                passes.append((pw, ph))
        raw = b"".join(b"\x00" + b"\x7f" * ((pw * channels * depth + 7) // 8) for pw, ph in passes for _ in range(ph))
    else:
        raw = b"".join(b"\x00" + b"\x7f" * row_bytes for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace))
    if color == 3:
        out += chunk(b"PLTE", b"\x00\x00\x00\xff\xff\xff")
    for t, d in extra_chunks:
        out += chunk(t, d)
    out += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return out


SCI_EXAMPLE = {
    "title": "Spike timing predicts choice in mouse V1 across three datasets",
    "abstract": "We test whether spike timing predicts choice.\nIt does, within the tested scope.",
    "categories": ["q-bio.NC", "cs.LG"],
    "keywords": ["spike timing", "decision making"],
    "headline_figure_caption": "Decoding accuracy by time bin.",
    "citations": [
        {"target": "doi:10.48550/arXiv.1706.03762", "relation": "uses", "reason": "Architecture."},
        {"target": "sci:b2836a2b-6f5d-4c3f-9e7e-92b19656b627"},
    ],
}


def write_folder(folder: Path, *, sci=None, sci_bytes: bytes | None = None, readme: bytes = b"# Result\n\nText.\n",
                 figure: bool = True, files: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if sci_bytes is None:
        sci_bytes = (json.dumps(sci if sci is not None else SCI_EXAMPLE, indent=2, ensure_ascii=False) + "\n").encode()
    (folder / "sci.json").write_bytes(sci_bytes)
    (folder / "README.md").write_bytes(readme)
    if figure:
        (folder / "headline_figure.png").write_bytes(make_png())
    if files is None:
        files = {"code/analyze.py": b"print('hi')\n", "data/raw/a.csv": b"1,2\n", "notes.txt": b"n\n"}
    for rel, data in files.items():
        p = folder / "files" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return folder


@pytest.fixture
def key():
    return keys.generate_private_key()


@pytest.fixture
def signed(tmp_path, key):
    folder = write_folder(tmp_path / "result")
    result = pub.sign_folder(folder, key)
    return folder, result


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


def _pg_bin() -> Path | None:
    env = os.environ.get("SCI_TEST_PG_BIN")
    if env:
        return Path(env)
    try:
        import pgserver
    except ImportError:
        return None
    p = Path(pgserver.__file__).parent / "pginstall" / "bin"
    return p if (p / "postgres").exists() else None


def _binary(env: str, fallback: str) -> str | None:
    v = os.environ.get(env)
    if v and Path(v).exists():
        return v
    p = Path.home() / ".local" / "sci-dev" / "bin" / fallback
    if p.exists():
        return str(p)
    return shutil.which(fallback)


ADMIN_PASSWORD = "admin-pw"
PUBLISHER_PASSWORD = "publisher-pw"
AUTHENTICATOR_PASSWORD = "authenticator-pw"


@pytest.fixture(scope="session")
def pg_cluster(tmp_path_factory):
    bindir = _pg_bin()
    if bindir is None:
        pytest.skip("PostgreSQL binaries unavailable (install the pgserver test extra)")
    data = tmp_path_factory.mktemp("pgdata")
    pwfile = data.parent / "pgpw"
    pwfile.write_text(ADMIN_PASSWORD)
    subprocess.run(
        [str(bindir / "initdb"), "-D", str(data), "-U", "sci_admin", "--auth-local=trust",
         "--auth-host=scram-sha-256", f"--pwfile={pwfile}", "-E", "UTF8", "--locale=C"],
        check=True, capture_output=True,
    )
    port = free_port()
    sock = tmp_path_factory.mktemp("pgsock")
    proc = subprocess.Popen(
        [str(bindir / "postgres"), "-D", str(data), "-p", str(port), "-k", str(sock),
         "-c", "listen_addresses=127.0.0.1", "-c", "fsync=off", "-c", "max_connections=200"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_port(port)
        time.sleep(0.3)
        yield {"port": port, "bindir": bindir}
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def admin_dsn(cluster, db: str = "postgres") -> str:
    return f"postgresql://sci_admin:{ADMIN_PASSWORD}@127.0.0.1:{cluster['port']}/{db}"


def publisher_dsn(cluster, db: str) -> str:
    return f"postgresql://sci_publisher:{PUBLISHER_PASSWORD}@127.0.0.1:{cluster['port']}/{db}"


@pytest.fixture
def database(pg_cluster):
    import psycopg

    from server.database import migrate

    name = "t_" + _uuid.uuid4().hex[:12]
    with psycopg.connect(admin_dsn(pg_cluster), autocommit=True) as c:
        c.execute(f'create database "{name}"')
    migrate(admin_dsn(pg_cluster, name), publisher_password=PUBLISHER_PASSWORD, authenticator_password=AUTHENTICATOR_PASSWORD)
    yield {"name": name, "admin": admin_dsn(pg_cluster, name), "publisher": publisher_dsn(pg_cluster, name), "cluster": pg_cluster}


@pytest.fixture(scope="session")
def rustfs(tmp_path_factory):
    binary = _binary("SCI_TEST_RUSTFS", "rustfs")
    if not binary:
        pytest.skip("RustFS binary unavailable (set SCI_TEST_RUSTFS)")
    data = tmp_path_factory.mktemp("rustfs")
    port = free_port()
    env = dict(os.environ, RUSTFS_ACCESS_KEY="testaccess", RUSTFS_SECRET_KEY="testsecret123")
    proc = subprocess.Popen(
        [binary, "server", "--address", f"127.0.0.1:{port}", str(data)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_port(port)
        time.sleep(0.5)
        yield {"endpoint": f"http://127.0.0.1:{port}", "access": "testaccess", "secret": "testsecret123"}
    finally:
        proc.terminate()
        proc.wait(timeout=30)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def static_server(directory: Path):
    port = free_port()
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


class Stack:
    """A running local instance: publisher, storage, database, descriptor."""

    def __init__(self, settings, app_port, web_port, database, site_dir, postgrest_url=None):
        self.settings = settings
        self.api_url = f"http://127.0.0.1:{app_port}"
        self.instance_url = settings.instance_url
        self.database = database
        self.site_dir = site_dir
        self.postgrest_url = postgrest_url
        self.storage = settings.storage()
        self.app = None

    @property
    def publisher(self):
        return self.app.state.sci["publisher"]

    def admin(self, query: str, params=()):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database["admin"], autocommit=True, row_factory=dict_row) as c:
            cur = c.execute(query, params)
            return cur.fetchall() if cur.description else None

    def enable_key(self, private_key) -> None:
        x = keys.b64url_encode(keys.raw_public_key(private_key.public_key()))
        self.admin("select * from sci.enable_key(%s)", (x,))

    def disable_key(self, private_key) -> None:
        x = keys.b64url_encode(keys.raw_public_key(private_key.public_key()))
        self.admin("select * from sci.disable_key(%s)", (x,))


def start_stack(database, rustfs, tmp_path, *, postgrest_binary=None, subpath="sci/", **overrides):
    import uvicorn

    from server.app import Settings, create_app

    tag = _uuid.uuid4().hex[:10]
    site_root = tmp_path / "site"
    site_dir = site_root / subpath if subpath else site_root
    site_dir.mkdir(parents=True, exist_ok=True)
    httpd, web_port = static_server(site_root)
    app_port = free_port()
    public_bucket = f"pub-{tag}"
    overrides.setdefault("rate_limit_per_minute", 10_000)
    settings = Settings(
        instance_url=f"http://127.0.0.1:{web_port}/{subpath}",
        archive_url=f"{rustfs['endpoint']}/{public_bucket}/publications",
        database_url=database["publisher"],
        s3_endpoint=rustfs["endpoint"],
        s3_access_key_id=rustfs["access"],
        s3_secret_access_key=rustfs["secret"],
        temp_bucket=f"tmp-{tag}",
        public_bucket=public_bucket,
        s3_region="us-east-1",
        **overrides,
    )
    settings.storage().ensure_buckets()

    pgrst_proc = None
    catalog_url = "http://127.0.0.1:9/unavailable"
    if postgrest_binary:
        pg_port = free_port()
        cluster = database["cluster"]
        env = dict(
            os.environ,
            PGRST_DB_URI=f"postgresql://authenticator:{AUTHENTICATOR_PASSWORD}@127.0.0.1:{cluster['port']}/{database['name']}",
            PGRST_DB_SCHEMAS="catalog_v1",
            PGRST_DB_ANON_ROLE="anon",
            PGRST_DB_MAX_ROWS="100",
            PGRST_SERVER_PORT=str(pg_port),
            PGRST_SERVER_HOST="127.0.0.1",
            PGRST_DB_AGGREGATES_ENABLED="false",
            PGRST_LOG_LEVEL="crit",
        )
        pgrst_proc = subprocess.Popen([postgrest_binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_port(pg_port)
        catalog_url = f"http://127.0.0.1:{pg_port}"
    descriptor = {
        "schema_version": 1,
        "publishing_api_url": f"http://127.0.0.1:{app_port}",
        "catalog_url": catalog_url,
        "catalog_schema": "catalog_v1",
        "archive_url": settings.archive_url + "/",
    }
    (site_dir / "sci-server.json").write_text(json.dumps(descriptor))

    app = create_app(settings, start_worker=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=app_port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    wait_port(app_port)
    while not server.started:
        time.sleep(0.05)
    stack = Stack(settings, app_port, web_port, database, site_dir, catalog_url if postgrest_binary else None)
    stack.app = app

    def stop():
        server.should_exit = True
        t.join(timeout=10)
        httpd.shutdown()
        if pgrst_proc:
            pgrst_proc.terminate()
            pgrst_proc.wait(timeout=10)

    return stack, stop


@pytest.fixture
def stack(database, rustfs, tmp_path):
    s, stop = start_stack(database, rustfs, tmp_path)
    try:
        yield s
    finally:
        stop()


@pytest.fixture
def catalog_stack(database, rustfs, tmp_path):
    binary = _binary("SCI_TEST_POSTGREST", "postgrest")
    if not binary:
        pytest.skip("PostgREST binary unavailable (set SCI_TEST_POSTGREST)")
    s, stop = start_stack(database, rustfs, tmp_path, postgrest_binary=binary)
    try:
        yield s
    finally:
        stop()


@pytest.fixture
def sci_home(tmp_path, monkeypatch):
    home = tmp_path / "scihome"
    monkeypatch.setenv("SCI_HOME", str(home))
    return home


def run_cli(*argv) -> tuple[int, str, str]:
    """Run the CLI in-process and capture output."""
    import io
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    from sci.cli import main

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    sys.stdout.flush()
    return code, out.getvalue(), err.getvalue()
