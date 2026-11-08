"""``sci license``: install the approved baseline license in a draft."""

from __future__ import annotations

import argparse
import hashlib

from pysci.baseline import BASELINE_LICENSE_SHA256, baseline_license_bytes
from pysci.manifest import LICENSE_PATH, MANIFEST_PATH, SIGNATURE_PATH

from ..context import Context
from ..errors import CliError, Exit
from ..output import Result
from ..spec import Command, arg
from ..storage.files import store_bytes


def run(args: argparse.Namespace, ctx: Context) -> Result:
    draft = ctx.resolve(args.dir)
    if not draft.is_dir():
        raise CliError("draft_missing", f"{draft} is not a directory", Exit.USAGE, file=str(draft))
    for generated in (MANIFEST_PATH, SIGNATURE_PATH):
        if (draft / generated).exists():
            raise CliError(
                "signed_directory",
                f"{draft} is signed ({generated} exists); signed publication bytes cannot change",
                Exit.USAGE,
                file=str(draft / generated),
            )
    target = draft / LICENSE_PATH
    data = baseline_license_bytes()
    if target.is_symlink():
        raise CliError("license_conflict", f"{target} is a symbolic link", Exit.INVALID)
    stored = store_bytes(target, data)
    if stored.outcome == "conflict":
        raise CliError(
            "license_conflict",
            f"{target} exists with different terms; it was left unchanged",
            Exit.INVALID,
            file=str(target),
            requirement=f"sha256 {BASELINE_LICENSE_SHA256} (SCI baseline license 0.1)",
            observed=f"sha256 {stored.existing_sha256}",
        )
    result = Result("license")
    result.data = {
        "path": str(target),
        "status": str(stored.outcome),
        "license": "SCI baseline license 0.1",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    verb = "installed" if stored.outcome == "written" else "already present"
    result.say(f"SCI baseline license 0.1 {verb}: {target}")
    result.say("Terms: CC BY 4.0 for text, figures, and data; MIT for code.")
    return result


COMMAND = Command(
    name="license",
    usage_hint="license DIR [--json]",
    summary="Install the SCI baseline license in a draft.",
    description="""
Copies the exact approved baseline license (SCI protocol §2.1: CC BY 4.0 for
text, figures, and data; MIT for code) to DIR/LICENSE. Running this command
selects those terms explicitly; signing never inserts a license. An identical
LICENSE is kept; a different one is rejected and left unchanged. Signed
directories are rejected.
""",
    args=(arg("dir", metavar="DIR", help="unsigned draft directory"),),
    outputs="The license path, whether it was written or already present, and its SHA-256.",
    files="Creates DIR/LICENSE if absent.",
    network="None.",
    exit_codes={3: "DIR/LICENSE exists with different bytes"},
    examples=("sci license ./draft",),
    run=run,
)
