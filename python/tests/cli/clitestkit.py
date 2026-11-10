"""Test helpers: CLI runner, drafts, a local HTTP server, and a fake RFC 3161 TSA."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from asn1crypto import cms, tsp
from asn1crypto import x509 as ax509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pysci._cli.clients import http
from pysci._cli.main import main
from pysci.baseline import baseline_license_bytes

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
    "0000000a4944415478da6360000000020001e527defc0000000049454e44ae426082"
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_KEYS = REPO_ROOT / "protocol" / "test-vectors" / "test-keys"
TEST_PKI = REPO_ROOT / "protocol" / "test-vectors" / "0.1" / "pki"


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict[str, Any]:
        value = json.loads(self.stdout)
        assert isinstance(value, dict)
        return value

    @property
    def result(self) -> dict[str, Any]:
        value = self.json["result"]
        assert isinstance(value, dict)
        return value


@dataclass
class Cli:
    home: Path
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    transport: Any = None
    sleeps: list[float] = field(default_factory=list)
    stdin: str = ""
    tty: bool = False
    pace_interval: float = 0.0
    """Pacing interval during the call; sleeps are recorded, never performed."""

    def __call__(self, *argv: str) -> Run:
        out, err = io.StringIO(), io.StringIO()
        env = {"SCI_HOME": str(self.home), "HOME": str(self.home.parent), **self.env}
        saved = http.Pacer.interval
        http.Pacer.interval = self.pace_interval
        try:
            code = main(
                list(argv),
                env=env,
                transport=self.transport,
                stdout=out,
                stderr=err,
                stdin=_Stdin(self.stdin, self.tty),
                sleep=self.sleeps.append,
                runner=lambda argv, timeout: None,
                cwd=self.cwd,
            )
        finally:
            http.Pacer.interval = saved
        return Run(code, out.getvalue(), err.getvalue())


class _Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    """A CLI runner with an isolated state directory (SCI_HOME) and working folder."""
    work = tmp_path / "work"
    work.mkdir()
    return Cli(home=tmp_path / "state", cwd=work)


def write_draft(
    directory: Path,
    *,
    title: str = "Neural decoding of spikes",
    license: bool = True,
    readme: str = "# Result\n\nWe decode neural spikes.\n",
    extra: dict[str, bytes] | None = None,
) -> Path:
    (directory / "files").mkdir(parents=True, exist_ok=True)
    metadata = {
        "title": title,
        "abstract": "Spike timing predicts choice.",
        "keywords": ["decoding", "spikes"],
        "categories": [{"scheme": "arxiv", "code": "q-bio.NC"}],
        "display": [{"path": "files/fig.png", "caption": "Figure"}],
        "citations": [{"target": "doi:10.1000/182", "relations": ["uses"]}],
    }
    (directory / "sci.json").write_text(json.dumps(metadata))
    (directory / "README.md").write_text(readme)
    (directory / "files" / "fig.png").write_bytes(TINY_PNG)
    (directory / "files" / "data.csv").write_text("a,b\n1,2\n" * 50)
    if license:
        (directory / "LICENSE").write_bytes(baseline_license_bytes())
    for path, data in (extra or {}).items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return directory


# --------------------------------------------------------------------------- HTTP server


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


Reply = tuple[int, dict[str, str], bytes]
Handler = Callable[[Request], Reply]


class LocalServer:
    """A threaded stdlib HTTP server on 127.0.0.1 dispatching to ``handler``."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[Request] = []
        server = self

        class _H(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = Request(
                    self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
                )
                server.requests.append(request)
                status, headers, payload = server.handler(request)
                if status == 0:  # drop the connection without a response
                    self.close_connection = True
                    self.connection.close()
                    return
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_HEAD = _serve

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> LocalServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def json_reply(status: int, value: object, headers: dict[str, str] | None = None) -> Reply:
    return (
        status,
        {"Content-Type": "application/json", **(headers or {})},
        json.dumps(value).encode(),
    )


def range_reply(data: bytes, header: str | None) -> Reply:
    """Serve ``data`` honouring a single ``Range`` header (206) or in full (200)."""
    if not header:
        return 200, {"Content-Type": "application/zip"}, data
    spec = header.removeprefix("bytes=")
    start_text, end_text = spec.split("-")
    if start_text == "":
        start = max(0, len(data) - int(end_text))
        end = len(data) - 1
    else:
        start = int(start_text)
        end = min(int(end_text), len(data) - 1) if end_text else len(data) - 1
    return (
        206,
        {"Content-Type": "application/zip", "Content-Range": f"bytes {start}-{end}/{len(data)}"},
        data[start : end + 1],
    )


# --------------------------------------------------------------------------- fake TSA


class FakeTsa:
    """Mints RFC 3161 responses with the public test-only corpus PKI."""

    def __init__(self) -> None:
        key = serialization.load_pem_private_key((TEST_KEYS / "tsa-a.pem").read_bytes(), None)
        assert isinstance(key, rsa.RSAPrivateKey)
        self.key = key
        self.cert = x509.load_pem_x509_certificate((TEST_PKI / "tsa-a.pem").read_bytes())
        self.root_pem = TEST_PKI / "root.pem"
        self.serial = 1

    def respond(self, request_der: bytes) -> bytes:
        request = tsp.TimeStampReq.load(request_der)
        imprint = request["message_imprint"]
        rawcert = self.cert.public_bytes(serialization.Encoding.DER)
        certasn = ax509.Certificate.load(rawcert)
        self.serial += 1
        info_fields: dict[str, Any] = {
            "version": "v1",
            "policy": "1.3.6.1.4.1.55555.1",
            "message_imprint": {
                "hash_algorithm": {"algorithm": imprint["hash_algorithm"]["algorithm"].native},
                "hashed_message": imprint["hashed_message"].native,
            },
            "serial_number": self.serial,
            "gen_time": datetime.now(UTC).replace(microsecond=0),
            "accuracy": {"seconds": 1},
            "ordering": False,
        }
        if request["nonce"].native is not None:
            info_fields["nonce"] = request["nonce"].native
        info = tsp.TSTInfo(info_fields)
        attrs = cms.CMSAttributes(
            [
                {"type": "content_type", "values": ["tst_info"]},
                {"type": "message_digest", "values": [hashlib.sha256(info.dump()).digest()]},
                {
                    "type": "signing_certificate_v2",
                    "values": [{"certs": [{"cert_hash": hashlib.sha256(rawcert).digest()}]}],
                },
            ]
        )
        signature = self.key.sign(attrs.dump(), padding.PKCS1v15(), hashes.SHA256())
        signer = cms.SignerInfo(
            {
                "version": "v1",
                "sid": {
                    "issuer_and_serial_number": {
                        "issuer": certasn.issuer,
                        "serial_number": certasn.serial_number,
                    }
                },
                "digest_algorithm": {"algorithm": "sha256"},
                "signed_attrs": attrs,
                "signature_algorithm": {"algorithm": "rsassa_pkcs1v15"},
                "signature": signature,
            }
        )
        token = cms.ContentInfo(
            {
                "content_type": "signed_data",
                "content": {
                    "version": "v3",
                    "digest_algorithms": [{"algorithm": "sha256"}],
                    "encap_content_info": {"content_type": "tst_info", "content": info},
                    "certificates": [certasn],
                    "signer_infos": [signer],
                },
            }
        )
        return bytes(
            tsp.TimeStampResp({"status": {"status": "granted"}, "time_stamp_token": token}).dump()
        )

    def handler(self, request: Request) -> Reply:
        return 200, {"Content-Type": "application/timestamp-reply"}, self.respond(request.body)


def trust_config(home: Path, anchor: Path, *, freetsa: bool = False) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.json"
    config: dict[str, Any] = json.loads(config_path.read_text()) if config_path.exists() else {}
    config["timestamp_trust"] = {"freetsa": freetsa, "anchors": [str(anchor)]}
    config_path.write_text(json.dumps(config))
