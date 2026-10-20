import argparse
import json
import sys
from pathlib import Path

import httpx

from sci import __version__
from sci.client import default_key, load_config, pull, push, resolve_server, set_server
from sci.errors import SCIError
from sci.keys import keygen
from sci.publication import sign_publication, verify_publication

GUIDE = """
SCI preserves signed research publications and citations. A public key is
an identity. The destination must enable the author's key before upload.
Keep the private key outside publication folders. Corrections are new
publications; use a supersedes citation to refer to the earlier work.

Start
  sci server https://example.org/sci/   # Set destination; no network request
  sci keygen                          # ~/.sci/key.pem and key.pub.pem
  sci sign ./result
  sci verify ./result
  sci push ./result
  sci pull UUID                       # Full verified copy in ./UUID/

Use --key PATH for keygen/sign, --public-key PATH for a trusted verify,
--server URL for one push/pull, --output PATH for pull, and --json for
machine-readable results. Progress and errors go to stderr. No server is
assumed. Configuration is in ~/.sci/config.json; "key" selects a key path.

Find prior work
  Read INSTANCE/sci-server.json for catalog_url, catalog_schema,
  archive_url, and publishing_api_url. Treat INSTANCE as a directory.
  Send Accept-Profile: catalog_v1 on catalog GET requests.
  If the descriptor has supabase_publishable_key, send it as apikey only
  to catalog_url. Do not send it to the archive or publisher.

  Examples relative to catalog_url (URL-encode query values):
    /publications?select=uuid,title,abstract&search_document=wfts(english).memory&order=accepted_at.desc,uuid.desc&limit=20
    /publications?categories=cs.{cs.LG}&limit=20
    /citations?source_uuid=eq.UUID&order=position.asc&limit=100
    /citations?target_key=eq.sci:UUID&order=source_uuid.asc,position.asc&limit=100
    /rpc/normalize_citation_target?target=doi%3A10.1000%2F182
  Use the normalized key for incoming DOI queries. Retain original target
  spelling for display. Page outgoing citations by position. For feeds,
  retain the last accepted_at/uuid pair and use a keyset filter. See README.
  Read a complete publication with sci pull UUID. Search external sources
  freely; retrieve external datasets from their sources. No sci search
  command is required or provided.

Prepare a contribution
  result/
    sci.json          Author metadata; exact bytes are preserved
    README.md         Main text, methods, findings, limits, attribution
    headline_figure.png   Optional static PNG
    files/            Optional code, data, and supporting figures

  Minimal sci.json:
    {
      "title": "The measured result and its tested scope",
      "abstract": "Question, method, result, and limitation.",
      "categories": ["cs.LG"],
      "citations": [
        {"target": "sci:b2836a2b-6f5d-4c3f-9e7e-92b19656b627",
         "relation": "reproduces", "reason": "We repeat the stated test."},
        {"target": "doi:10.1000/182"}
      ]
    }

  title and abstract are required. Optional: categories, keywords,
  headline_figure_caption, citations. Omit absent fields; do not use null.
  A citation needs only target. Optional relations: uses, supports,
  reproduces, challenges, supersedes. No relation means a general citation.
  reason is optional, with or without a relation. Use sci:<UUIDv4> or
  doi:<DOI>; no resolver URLs. Encode suffix slashes in DOI URIs as %2F.
  Prefer version-specific dataset DOIs. Add citations before signing.
  Edits require a new signature and UUID.

  Limits: title 200 Unicode code points; abstract 2,500; 20 keywords of
  64 each; caption 400; citation reason 1,000; three distinct categories.
  Find codes in the installed sci/categories.json (arXiv taxonomy).
  Citation target: 512 ASCII characters; key: 512 UTF-8 bytes.
  Both JSON files together: 65,536 bytes. README: 1,000,000 bytes.
  README + ZIP + headline figure: 50,000,000 bytes.
  Headline figure: static PNG, 2,000,000 bytes, each dimension 1–1,600.
  The caption belongs in sci.json. Refer to the headline figure in prose.
  Do not embed README images or use relative links to bundled files.
  Refer to supporting figures by number and filename, for example:
  "Figure 2 (figures/control.png in the supporting ZIP)".

Publishing terms
  Text, figures, and data: CC BY 4.0. Code: MIT. Signing confirms agreement
  and authority to distribute all included material under these terms.
  Retain attribution, copyright, and third-party notices in the README
  and supporting files. Link incompatible material externally.
  sign writes the fixed LICENSE, files.zip, manifest.json, and a raw
  64-byte manifest.json.sig. A different existing LICENSE causes failure.
  The fixed text is installed as sci/publication_LICENSE.txt and shown at
  INSTANCE/#publishing-terms. Hosting rules are separate from reuse terms.

Verify and share
  sci sign ./result   # Offline; author JSON and README are unchanged
  sci verify ./result --public-key ~/.sci/key.pub.pem
  sci push ./result   # Upload existing signature; no private key needed

  Verification checks integrity under the embedded key; use --public-key
  to require a trusted signer. It does not prove rights ownership.
  push never signs or rebuilds files.zip. Changes in files/ need sign.
  Signing again creates a new UUID. Keep older signed folders if needed.
  pull verifies all files, never unpacks ZIPs, and refuses a nonempty output.
  To mirror: pull from one instance, then push that folder to another.
  The destination must enable the original author's key. Do not sign again.
"""


def parser():
    p = argparse.ArgumentParser(
        prog="sci", description="Signed, portable scientific publications.",
        epilog=GUIDE, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="machine-readable result")
    commands = p.add_subparsers(dest="command", required=True)
    descriptions = {
        "server": "Show or set the default instance URL offline; preserve other settings.",
        "keygen": "Create an Ed25519 key pair. Never overwrite either file. Private key mode: 0600.",
        "sign": "Sign a folder offline. Agree to CC BY 4.0 for text/figures/data and MIT for code; "
                "confirm distribution rights and retain notices. Generate LICENSE, ZIP, manifest, "
                "and signature. A new sign creates a new UUID. See sci --help for authoring limits.",
        "verify": "Verify every declared file, signature, and fixed license offline. "
                  "An embedded key proves integrity, not trust; use --public-key to pin the signer.",
        "push": "Verify and upload a signed folder. No private key is needed. Retry with the same "
                "folder. For mirroring, pull then push without signing again. "
                "The destination must enable the original author's key.",
        "pull": "Fetch and verify all original files through the static descriptor and archive. "
                "No publisher, catalog, or private key is needed. Never unpack ZIPs. "
                "Refuse nonempty output. To mirror, push the resulting folder without re-signing.",
    }
    for name, description in descriptions.items():
        sub = commands.add_parser(name, help=description.split(".")[0], description=description)
        sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "server":
            sub.add_argument("url", nargs="?", help="instance base, e.g. https://example.org/sci/")
        if name in {"sign", "verify", "push"}:
            sub.add_argument("path", type=Path, help="publication folder, e.g. ./result")
        if name in {"keygen", "sign"}:
            sub.add_argument("--key", type=Path, help="private PEM path; default config key or ~/.sci/key.pem")
        if name == "verify":
            sub.add_argument("--public-key", type=Path, help="trusted Ed25519 public PEM")
        if name in {"push", "pull"}:
            sub.add_argument("--server", help="instance URL for this command only")
        if name == "pull":
            sub.add_argument("uuid", help="canonical lowercase UUIDv4")
            sub.add_argument("--output", type=Path, help="empty destination; default ./UUID/")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "server":
            result = {"server": set_server(args.url) if args.url else load_config().get("server")}
            display = result["server"] or "No server configured. Run sci server URL."
        elif args.command == "keygen":
            path = (args.key or default_key()).expanduser()
            public, identity = keygen(path)
            result = {"private_key": str(path), "public_key": str(public), "fingerprint": identity}
            display = f"Created {path} and {public}\n{identity}"
        elif args.command in {"sign", "verify"}:
            pub = (sign_publication(args.path, (args.key or default_key()).expanduser())
                   if args.command == "sign" else verify_publication(args.path, args.public_key))
            result = {"uuid": pub.uuid, "fingerprint": pub.fingerprint, "manifest_sha256": pub.manifest_sha256}
            display = pub.uuid if args.command == "sign" else f"{pub.uuid}\n{pub.fingerprint}"
        elif args.command == "push":
            result = push(args.path, resolve_server(args.server))
            display = result["url"]
        else:
            result = pull(args.uuid, resolve_server(args.server), args.output)
            display = result["path"]
        print(json.dumps(result) if args.json else display)
        return 0
    except (SCIError, OSError, httpx.HTTPError, ValueError) as exc:
        error = {"error": {"code": getattr(exc, "code", "client_error"), "message": str(exc)}}
        print(json.dumps(error) if args.json else f"sci: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
