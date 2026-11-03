"""sci pull: download and verify a publication."""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .. import keys
from .. import publication as pub
from ..config import resolve_server
from ..net import USER_AGENT, fetch_descriptor
from ..output import CliError, Out

HELP = """\
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


def run(args, out: Out) -> int:
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



def register(sub, common, raw) -> None:
    u = sub.add_parser("pull", parents=[common], formatter_class=raw, help="download and verify a publication", description=HELP)
    u.add_argument("uuid", metavar="UUID")
    u.add_argument("--server", metavar="URL", help="use this server for this command only")
    u.add_argument("--output", metavar="PATH", help="destination folder (default ./UUID/)")
    u.set_defaults(func=run)
