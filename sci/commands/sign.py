"""sci sign: validate and sign a publication folder."""

from __future__ import annotations

import os
from pathlib import Path

from .. import keys
from .. import publication as pub
from ..config import default_key_path
from ..output import CliError, Out

HELP = """\
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


def run(args, out: Out) -> int:
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



def register(sub, common, raw) -> None:
    g = sub.add_parser("sign", parents=[common], formatter_class=raw, help="validate and sign a publication folder", description=HELP)
    g.add_argument("folder", help="publication folder, for example ./result")
    g.add_argument("--key", metavar="PATH", help="private key (default ~/.sci/key.pem)")
    g.set_defaults(func=run)
