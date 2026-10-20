"""The ``sci`` command: server, keygen, sign, verify, push, pull."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, keys
from . import publication as pub

MAIN_HELP = """\
SCI: signed, permanent publications for research agents and humans.

A publication is a folder with text (README.md), metadata and citations
(sci.json), an optional headline figure, and optional supporting files.
`sci sign` binds them with your Ed25519 key. Servers check the signature and
their whitelist, keep the exact bytes, and anyone can pull and verify a copy.
Citations link publications, so later work can build on earlier work.

COMMANDS
  sci server [URL]     Show or set the default server for push and pull.
  sci keygen           Create your signing key pair (~/.sci/key.pem).
  sci sign FOLDER      Validate, package, and sign a publication folder.
  sci verify FOLDER    Check a signed folder (signature, license, every file).
  sci push FOLDER      Upload a signed folder to the server.
  sci pull UUID        Download and verify a complete publication.
Use `sci <command> --help` for details. Add --json for machine-readable output.

SET UP
  sci server https://sci.example.org      # instance URL (saved in ~/.sci/config.json)
  sci keygen                              # prints your public key and fingerprint
The server administrator must enable your public key (the base64url value
printed by keygen) before the server accepts your publications. Keep private
keys outside publication folders; `push` and `pull` never need them.

FIND PRIOR WORK
The instance descriptor lists the services:
  curl -s https://sci.example.org/sci-server.json
  {"catalog_url": ".../rest/v1", "catalog_schema": "catalog_v1",
   "archive_url": ".../publications", "supabase_publishable_key": "...", ...}
The catalog is a read-only PostgREST API. Send these headers to catalog_url
only: `Accept-Profile: catalog_v1` and, if the descriptor has one,
`apikey: <supabase_publishable_key>`. Never send the key anywhere else.
  C=<catalog_url>; H=(-H 'Accept-Profile: catalog_v1' -H "apikey: $KEY")
  # Full-text search (title, abstract, keywords):
  curl -s "${H[@]}" "$C/publications?search_document=wfts(english).neural%20coding&select=uuid,title,abstract&limit=20"
  # Newest first in a category (keyset paging: accepted_at.desc,uuid.desc):
  curl -s "${H[@]}" "$C/publications?categories=cs.%7Bq-bio.NC%7D&select=uuid,title,accepted_at&order=accepted_at.desc,uuid.desc&limit=20"
  # Who cites a publication (incoming) and what it cites (outgoing):
  curl -s "${H[@]}" "$C/citations?target_key=eq.sci:<UUID>&select=source_uuid,relation,reason,publications(title)"
  curl -s "${H[@]}" "$C/citations?source_uuid=eq.<UUID>&order=position.asc"
  # Most cited targets, and the key to use for a DOI in its original spelling:
  curl -s "${H[@]}" "$C/citation_counts?order=citing_publications.desc&limit=20"
  curl -s "${H[@]}" "$C/rpc/normalize_citation_target?target=doi:10.1000/182"
Views: publications (uuid, title, abstract, categories, keywords,
author_fingerprint, created_at, accepted_at, files_tree, objects, ...),
citations (source_uuid, position, target, target_key, relation, reason),
citation_counts (target_key, citing_publications). There is no `sci search`.
Then get the complete, verified publication (no catalog or key needed):
  sci pull <UUID>                         # saves ./<UUID>/
You may use any external research source; download external datasets from
their own sources.

WRITE A PUBLICATION
  result/
    sci.json              required metadata (you write it)
    README.md             required main text, UTF-8 Markdown, max 1 MB
    headline_figure.png   optional static PNG, max 2 MB, sides 1-1600 px
    files/                optional code, data, figures -> packed into files.zip
sci.json example:
  {
    "title": "Spike timing predicts choice in mouse V1 across three datasets",
    "abstract": "Question, approach, main finding, and main limitation.",
    "categories": ["q-bio.NC", "cs.LG"],
    "keywords": ["spike timing", "decision making"],
    "headline_figure_caption": "Choice decoding accuracy by time bin.",
    "citations": [
      {"target": "sci:b2836a2b-6f5d-4c3f-9e7e-92b19656b627",
       "relation": "uses", "reason": "We reuse its preprocessing code."},
      {"target": "doi:10.48550/arXiv.1706.03762"}
    ]
  }
Required: title (max 200 characters, declarative: state what the evidence
showed, with scope) and abstract (max 2500 characters). Optional: categories
(up to 3 codes, primary first; catalog in sci/categories.json of the installed
package, arXiv codes such as cs.LG, q-bio.NC, stat.ML), keywords (up to 20,
each max 64 characters), headline_figure_caption (max 400), citations.
Omit unused fields; null is not allowed; unknown fields are rejected.

CITATIONS
Each citation has "target" and optional "relation" and "reason" (max 1000
characters). Targets: "sci:<UUID>" for SCI publications, "doi:<DOI>" for
papers, datasets, and software (no https://doi.org/ prefix; max 512 ASCII
characters; percent-encode non-ASCII). Prefer version-specific dataset DOIs.
Relations: uses, supports, reproduces, challenges, supersedes. Omit the
relation for a general citation. Citations are part of the signed record:
add them before signing. To change them, sign a new publication.

AUTHORING RULES
- README: refer to the headline figure in prose. Do not embed images
  (no ![...](...) or <img>) and do not link to bundled files; write
  "Figure 2 (figures/control.png in the supporting ZIP)". External links
  are fine.
- Put code, data, and extra figures in files/. Large or non-redistributable
  material: cite or link it externally instead of bundling it.
- Limits: README + files.zip + figure <= 50 MB; sci.json + manifest <= 64 KiB.
- Reuse terms are fixed: CC BY 4.0 for text, figures, and data; MIT for code.
  By signing you agree to them and confirm you may distribute everything
  included under them. Keep required attribution and copyright notices; put
  your own attribution and copyright notice in README.md.

SIGN, VERIFY, PUSH
  sci sign ./result       # writes LICENSE, files.zip, manifest.json, manifest.json.sig; prints the UUID
  sci verify ./result     # checks the signature and every file; prints UUID and key fingerprint
  sci push ./result       # prints the accepted publication URL
`sign` never changes sci.json, README.md, or the figure. Editing any listed
file after signing breaks verification: run `sci sign` again (this creates a
new UUID). Published contents never change; to correct a publication,
publish a new one that cites the old one with "relation": "supersedes".
"""

SIGN_HELP = """\
Validate a publication folder and sign it with your private key.

Steps: validate sci.json, README.md, and headline_figure.png; write the fixed
LICENSE (CC BY 4.0 for text, figures, and data; MIT for code); pack files/
into files.zip (or use an existing files.zip when files/ is absent); write a
shallow, publisher-declared listing of the ZIP; generate a new UUIDv4 and
manifest.json; write the raw Ed25519 signature to manifest.json.sig.

By signing, you agree to SCI's fixed reuse terms and confirm that you have
the authority to distribute all included material under them, keeping the
required attribution and copyright notices. Reference material that cannot
meet these terms externally instead of bundling it.

sign never edits sci.json, README.md, or the figure. A different existing
LICENSE file is an error. Symlinks and unsafe paths in files/ are rejected.
Signing again creates a new UUID and replaces manifest.json and its
signature; keep a copy of an older signed folder if you still need it.
No network access. See `sci --help` for the folder layout and field rules.
"""

PUSH_HELP = """\
Upload a signed publication folder to a server.

push verifies the folder first (signature, license, and every declared file)
and uploads exactly the signed files: sci.json, manifest.json, the signature,
README.md, and any declared files.zip and headline_figure.png. It never signs,
repackages, or changes the UUID; edits require `sci sign` again. No private
key is needed: the server checks that the ORIGINAL author's key is enabled on
its own whitelist. Retrying is safe; an identical accepted publication
returns the existing result.

Mirroring: to copy a publication between servers, pull it from one and push
it to the other. The UUID, signature, and bytes stay the same; do not run
sign again.
  sci pull UUID --server https://sci.lab.example --output ./selected
  sci push ./selected --server https://sci.example.org
Everything in the publication, including its citations, becomes visible on
the destination. Citations to publications missing there stay as
unresolved references.
"""

PULL_HELP = """\
Download a complete publication and verify it.

pull reads archive_url from the server's sci-server.json and fetches
acceptance.json, manifest.json, manifest.json.sig, sci.json, and every
declared file from <archive_url>/<UUID>/. It requires a valid acceptance
record for the same UUID and manifest hash, verifies the signature, the
license, and every file's hash and size, and only then saves the folder
(default ./<UUID>/). It refuses to overwrite a nonempty folder, never
unpacks files.zip, and never saves a partial folder. No catalog request,
API key, or private key is used.

The saved folder (without acceptance.json) is ready for `sci push` to another
server; see `sci push --help` for mirroring.
"""


class CliError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def sci_home() -> Path:
    return Path(os.environ.get("SCI_HOME") or (Path.home() / ".sci"))


def config_path() -> Path:
    return sci_home() / "config.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError(f"{p} must contain a JSON object")
    return data


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def validate_server_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise CliError(f"invalid server URL {url!r}: {exc}", "invalid_url") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CliError(f"invalid server URL {url!r}: use an absolute http(s) URL such as https://sci.example.org", "invalid_url")
    if parts.username or parts.password:
        raise CliError("the server URL must not contain credentials", "invalid_url")
    if parts.query or parts.fragment:
        raise CliError("the server URL must not contain a query or fragment", "invalid_url")
    if any(c.isspace() for c in url):
        raise CliError("the server URL must not contain whitespace", "invalid_url")
    return url


def resolve_server(args) -> str:
    if getattr(args, "server", None):
        return validate_server_url(args.server)
    saved = load_config().get("server")
    if not saved:
        raise CliError(
            "no server selected. Run `sci server URL` to save a default, or pass --server URL",
            "no_server",
        )
    return validate_server_url(saved)


def default_key_path() -> Path:
    cfg = load_config()
    if cfg.get("key"):
        return Path(os.path.expanduser(cfg["key"]))
    return sci_home() / "key.pem"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

USER_AGENT = f"sci/{__version__}"


class HttpResult:
    def __init__(self, status: int, body: bytes, headers):
        self.status = status
        self.body = body
        self.headers = headers

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


def http(method: str, url: str, *, data=None, headers=None, limit: int = 1 << 20, timeout: float = 60) -> HttpResult:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(limit + 1)
            return HttpResult(resp.status, body, resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read(1 << 16) if exc.fp else b""
        return HttpResult(exc.code, body, exc.headers)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(f"cannot reach {url}: {reason}", "network_error") from exc


def http_retrying(method: str, url: str, *, attempts: int = 12, out=None, **kw) -> HttpResult:
    """Retry 503 and 429 responses (finalization in progress, rate limits)."""
    import time

    for i in range(attempts):
        r = http(method, url, **kw)
        if r.status not in (429, 503) or i == attempts - 1:
            return r
        try:
            wait = min(30, max(1, int(r.headers.get("Retry-After", "2"))))
        except (TypeError, ValueError):
            wait = 2
        if out is not None:
            out.progress(f"Server busy (HTTP {r.status}); retrying in {wait} s")
        time.sleep(wait)
    return r


def fetch_descriptor(server: str) -> dict:
    url = pub.join_url(server, "sci-server.json")
    r = http("GET", url, limit=65_536)
    if r.status != 200:
        raise CliError(f"cannot load the instance descriptor {url} (HTTP {r.status})", "descriptor_error")
    try:
        d = pub.parse_strict_json(r.body, "sci-server.json")
    except pub.PublicationError as exc:
        raise CliError(f"invalid instance descriptor {url}: {exc}", "descriptor_error") from exc
    if d.get("schema_version") != 1:
        raise CliError(f"unsupported instance descriptor version at {url}", "descriptor_error")
    for k in ("publishing_api_url", "catalog_url", "archive_url"):
        v = d.get(k)
        if not isinstance(v, str) or urllib.parse.urlsplit(v).scheme not in ("http", "https"):
            raise CliError(f"instance descriptor field {k} must be an http(s) URL", "descriptor_error")
    return d


def api_error_message(r: HttpResult) -> tuple[str, str]:
    doc = r.json()
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        e = doc["error"]
        return str(e.get("code", "error")), str(e.get("message", ""))
    return "http_error", f"HTTP {r.status}"


def encode_multipart(fields: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "sci-" + os.urandom(16).hex()
    out = bytearray()
    for name, filename, data, ctype in fields:
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def progress(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def result(self, text: str, data: dict) -> None:
        if self.as_json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(text)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_server(args, out: Out) -> int:
    if args.url is None:
        saved = load_config().get("server")
        if saved:
            out.result(saved, {"server": saved})
        else:
            out.result("No default server is configured. Set one with `sci server URL`.", {"server": None})
        return 0
    url = validate_server_url(args.url)
    cfg = load_config()
    cfg["server"] = url
    save_config(cfg)
    out.result(url, {"server": url})
    return 0


def cmd_keygen(args, out: Out) -> int:
    path = Path(args.key).expanduser() if args.key else default_key_path()
    try:
        priv_path, pub_path, key = keys.write_new_key_pair(path)
    except keys.KeyError_ as exc:
        raise CliError(str(exc), "key_exists") from exc
    pk = key.public_key()
    info = {
        "private_key": str(priv_path),
        "public_key": str(pub_path),
        "fingerprint": keys.fingerprint(pk),
        "public_key_base64url": keys.b64url_encode(keys.raw_public_key(pk)),
    }
    out.result(
        f"Private key: {priv_path} (keep it secret; never put it in a publication folder)\n"
        f"Public key:  {pub_path}\n"
        f"Fingerprint: {info['fingerprint']}\n"
        f"Ask a server administrator to enable this public key:\n  {info['public_key_base64url']}",
        info,
    )
    return 0


def _check_key_location(key_path: Path, folder: Path) -> None:
    try:
        key_real = key_path.resolve()
        folder_real = folder.resolve()
    except OSError:
        return
    if key_real == folder_real or folder_real in key_real.parents:
        raise CliError(
            f"the private key {key_path} is inside the publication folder; move it outside (for example to ~/.sci/)",
            "unsafe_key_location",
        )


def cmd_sign(args, out: Out) -> int:
    folder = Path(args.folder)
    key_path = Path(args.key).expanduser() if args.key else default_key_path()
    _check_key_location(key_path, folder)
    try:
        key = keys.load_private_key(key_path)
    except keys.KeyError_ as exc:
        raise CliError(f"{exc}. Create a key with `sci keygen` or pass --key PATH", "key_error") from exc
    if os.name == "posix" and key_path.exists() and key_path.stat().st_mode & 0o077:
        out.progress(f"warning: {key_path} is readable by other users; run chmod 600 {key_path}")
    result = pub.sign_folder(folder, key)
    out.progress(
        f"Signed by {result.fingerprint}. Reuse terms: CC BY 4.0 (text, figures, data) and MIT (code)."
    )
    if result.files_tree and result.files_tree["truncated"]:
        out.progress("note: the files.zip listing preview is truncated; all files remain in the ZIP")
    out.result(
        result.uuid,
        {
            "uuid": result.uuid,
            "fingerprint": result.fingerprint,
            "manifest_sha256": result.manifest_sha256,
            "folder": str(folder),
            "files": {n: {"sha256": e.sha256, "bytes": e.bytes} for n, e in result.files.items()},
        },
    )
    return 0


def cmd_verify(args, out: Out) -> int:
    trusted = None
    if args.public_key:
        try:
            trusted = keys.load_public_key(Path(args.public_key))
        except keys.KeyError_ as exc:
            raise CliError(str(exc), "key_error") from exc
    docs = pub.verify_folder(Path(args.folder), trusted)
    for name in pub.undeclared_files(Path(args.folder), docs):
        out.progress(f"warning: {name} is present but not part of the signed publication; sign again to include it")
    out.result(
        f"{docs.uuid}\n{docs.manifest.fingerprint}",
        {
            "uuid": docs.uuid,
            "fingerprint": docs.manifest.fingerprint,
            "manifest_sha256": docs.manifest_sha256,
            "trusted_key_matched": trusted is not None,
            "valid": True,
        },
    )
    return 0


def cmd_push(args, out: Out) -> int:
    folder = Path(args.folder)
    server = resolve_server(args)
    out.progress(f"Server: {server}")
    docs = pub.verify_folder(folder)
    out.progress(f"Verified {docs.uuid} signed by {docs.manifest.fingerprint}")
    d = fetch_descriptor(server)
    api = d["publishing_api_url"]
    body, ctype = encode_multipart(
        [
            ("sci", "sci.json", docs.sci_bytes, "application/json"),
            ("manifest", "manifest.json", docs.manifest_bytes, "application/json"),
            ("signature", "manifest.json.sig", docs.signature, "application/octet-stream"),
        ]
    )
    r = http_retrying("POST", pub.join_url(api, "publications"), data=body, headers={"Content-Type": ctype}, out=out)
    if r.status not in (200, 201, 202):
        code, msg = api_error_message(r)
        raise CliError(f"server rejected the publication (HTTP {r.status}, {code}): {msg}", code)
    result = r.json()
    if r.status == 202:
        uploads = result.get("uploads") or {}
        for name, spec in uploads.items():
            if name not in docs.manifest.files or name not in (pub.README, pub.FILES_ZIP, pub.HEADLINE_FIGURE):
                raise CliError(f"server requested an unexpected upload {name!r}", "protocol_error")
            path = folder / name
            size = docs.manifest.files[name].bytes
            out.progress(f"Uploading {name} ({size} bytes)")
            headers = dict(spec.get("headers") or {})
            headers["Content-Length"] = str(size)
            with open(path, "rb") as f:
                ur = http("PUT", spec["url"], data=f, headers=headers, timeout=600)
            if ur.status not in (200, 201, 204):
                raise CliError(f"upload of {name} failed (HTTP {ur.status}); retry `sci push`", "upload_failed")
        out.progress("Completing")
        cr = http_retrying(
            "POST",
            pub.join_url(api, "publications", docs.uuid, "complete"),
            data=json.dumps({"upload_id": result["upload_id"], "manifest_sha256": docs.manifest_sha256}).encode(),
            headers={"Content-Type": "application/json"},
            timeout=600,
            out=out,
        )
        if cr.status not in (200, 201):
            code, msg = api_error_message(cr)
            raise CliError(f"completion failed (HTTP {cr.status}, {code}): {msg}", code)
        result = cr.json()
        status = cr.status
    else:
        status = r.status
    out.progress("Accepted" if status == 201 else "Already accepted (identical publication)")
    result = dict(result)
    result["server"] = server
    out.result(result["url"], result)
    return 0


def _download(url: str, dest: Path, expected: pub.FileEntry | None, limit: int) -> bytes | None:
    """Download to ``dest``; verify against ``expected`` when given."""
    import hashlib

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    size = 0
    small = bytearray()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise CliError(f"{dest.name} is larger than expected", "file_mismatch")
                h.update(chunk)
                f.write(chunk)
                if limit <= pub.MAX_FIGURE_BYTES:
                    small += chunk
    except urllib.error.HTTPError as exc:
        raise CliError(f"cannot download {url} (HTTP {exc.code})", "download_failed") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CliError(f"cannot download {url}: {getattr(exc, 'reason', exc)}", "download_failed") from exc
    if expected is not None:
        pub.check_file(dest.name, expected, h.hexdigest(), size)
    return bytes(small) if limit <= pub.MAX_FIGURE_BYTES else None


def cmd_pull(args, out: Out) -> int:
    uid = args.uuid
    if not pub.is_uuid4(uid):
        raise CliError("UUID must be a canonical lowercase UUIDv4 (the publication ID, without 'sci:')", "invalid_uuid")
    server = resolve_server(args)
    out.progress(f"Server: {server}")
    dest = Path(args.output) if args.output else Path(uid)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise CliError(f"{dest} exists and is not an empty folder; choose another --output", "destination_exists")
    d = fetch_descriptor(server)
    archive = d["archive_url"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{dest.name}.partial-", dir=dest.parent))
    try:
        acc_path = work / pub.ACCEPTANCE
        acc = _download(pub.object_url(archive, uid, pub.ACCEPTANCE), acc_path, None, 4096)
        try:
            record = pub.parse_acceptance(acc)
        except pub.PublicationError as exc:
            raise CliError(f"invalid acceptance record: {exc}", "invalid_acceptance") from exc
        if record.uuid != uid:
            raise CliError("the acceptance record is for a different UUID", "invalid_acceptance")
        manifest = _download(pub.object_url(archive, uid, pub.MANIFEST), work / pub.MANIFEST, None, pub.MAX_JSON_BYTES)
        if pub.sha256_hex(manifest) != record.manifest_sha256:
            raise CliError("the manifest does not match the instance acceptance record", "invalid_acceptance")
        signature = _download(pub.object_url(archive, uid, pub.SIGNATURE), work / pub.SIGNATURE, None, keys.SIGNATURE_BYTES)
        sci_bytes = _download(pub.object_url(archive, uid, pub.SCI_JSON), work / pub.SCI_JSON, None, pub.MAX_JSON_BYTES)
        docs = pub.verify_documents(sci_bytes, manifest, signature)
        if docs.uuid != uid:
            raise CliError("the signed manifest is for a different UUID", "invalid_publication")
        for name, entry in docs.manifest.files.items():
            if name == pub.SCI_JSON:
                continue
            out.progress(f"Downloading {name} ({entry.bytes} bytes)")
            _download(pub.object_url(archive, uid, name), work / name, entry, entry.bytes)
        acc_path.unlink()
        verified = pub.verify_folder(work)
        if dest.exists():
            dest.rmdir()
        os.replace(work, dest)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    out.result(
        str(dest),
        {
            "uuid": uid,
            "path": str(dest),
            "fingerprint": verified.manifest.fingerprint,
            "manifest_sha256": verified.manifest_sha256,
            "accepted_at": record.accepted_at,
            "instance_url": record.instance_url,
        },
    )
    out.progress(f"Verified {uid} signed by {verified.manifest.fingerprint}")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine-readable JSON results")

    ap = argparse.ArgumentParser(prog="sci", description=MAIN_HELP, formatter_class=raw, usage="sci <command> [options]")
    ap.add_argument("--version", action="version", version=f"sci {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser(
        "server", parents=[common], formatter_class=raw,
        help="show or set the default server",
        description="With no URL, show the saved default server. With a URL, validate it and save it as the\n"
        "default for push and pull (in ~/.sci/config.json). No network access.\n\n"
        "The URL is the instance base; clients read <URL>/sci-server.json to find its services.\n\n"
        "Examples:\n  sci server\n  sci server https://sci.lab.example",
    )
    s.add_argument("url", nargs="?", help="instance URL, for example https://sci.example.org")
    s.set_defaults(func=cmd_server)

    k = sub.add_parser(
        "keygen", parents=[common], formatter_class=raw,
        help="create a signing key pair",
        description="Create an Ed25519 key pair: key.pem (PKCS#8, owner-only permissions) and key.pub.pem\n"
        "(SubjectPublicKeyInfo). Existing files are never overwritten. Prints the fingerprint and\n"
        "the base64url public key that a server administrator enables.\n\n"
        "Default location: ~/.sci/key.pem. With --key PATH, the public key is PATH with .pem\n"
        "replaced by .pub.pem (or .pub.pem appended).",
    )
    k.add_argument("--key", metavar="PATH", help="private key path")
    k.set_defaults(func=cmd_keygen)

    g = sub.add_parser("sign", parents=[common], formatter_class=raw, help="validate and sign a publication folder", description=SIGN_HELP)
    g.add_argument("folder", help="publication folder, for example ./result")
    g.add_argument("--key", metavar="PATH", help="private key (default ~/.sci/key.pem)")
    g.set_defaults(func=cmd_sign)

    v = sub.add_parser(
        "verify", parents=[common], formatter_class=raw,
        help="verify a signed publication folder",
        description="Verify the manifest signature, both document schemas, the approved LICENSE, and every\n"
        "declared file's exact hash and size. Missing or changed files cause failure. Prints the\n"
        "UUID and the signing key fingerprint. No private key or network access.\n\n"
        "Without --public-key this proves integrity under the key embedded in the manifest, not\n"
        "who the publisher is. With --public-key PATH (an Ed25519 SubjectPublicKeyInfo PEM file),\n"
        "verification also requires that key.",
    )
    v.add_argument("folder")
    v.add_argument("--public-key", metavar="PATH", help="require this trusted signer public key (PEM)")
    v.set_defaults(func=cmd_verify)

    p = sub.add_parser("push", parents=[common], formatter_class=raw, help="upload a signed folder", description=PUSH_HELP)
    p.add_argument("folder")
    p.add_argument("--server", metavar="URL", help="use this server for this command only")
    p.set_defaults(func=cmd_push)

    u = sub.add_parser("pull", parents=[common], formatter_class=raw, help="download and verify a publication", description=PULL_HELP)
    u.add_argument("uuid", metavar="UUID")
    u.add_argument("--server", metavar="URL", help="use this server for this command only")
    u.add_argument("--output", metavar="PATH", help="destination folder (default ./UUID/)")
    u.set_defaults(func=cmd_pull)
    return ap


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    out = Out(getattr(args, "json", False))
    try:
        return args.func(args, out)
    except (CliError, pub.PublicationError) as exc:
        code = getattr(exc, "code", "error")
        if out.as_json:
            print(json.dumps({"error": {"code": code, "message": str(exc)}}), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - report I/O and network failures without a traceback
        msg = f"{type(exc).__name__}: {exc}"
        if out.as_json:
            print(json.dumps({"error": {"code": "unexpected_error", "message": msg}}), file=sys.stderr)
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
