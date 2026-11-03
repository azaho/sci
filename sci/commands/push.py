"""sci push: upload a signed folder."""

from __future__ import annotations

import json
from pathlib import Path

from .. import publication as pub
from ..config import resolve_server
from ..net import (
    api_error_message,
    encode_multipart,
    fetch_descriptor,
    http,
    http_retrying,
)
from ..output import CliError, Out

HELP = """\
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



def run(args, out: Out) -> int:
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



def register(sub, common, raw) -> None:
    p = sub.add_parser("push", parents=[common], formatter_class=raw, help="upload a signed folder", description=HELP)
    p.add_argument("folder")
    p.add_argument("--server", metavar="URL", help="use this server for this command only")
    p.set_defaults(func=run)
