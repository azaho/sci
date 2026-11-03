"""The ``sci`` command: top-level help, argument parsing, and dispatch.

Each command lives in ``sci.commands.<name>``."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import publication as pub
from .commands import COMMANDS
from .output import CliError, Out

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


def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine-readable JSON results")

    ap = argparse.ArgumentParser(prog="sci", description=MAIN_HELP, formatter_class=raw, usage="sci <command> [options]")
    ap.add_argument("--version", action="version", version=f"sci {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="<command>")
    for command in COMMANDS:
        command.register(sub, common, raw)
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
