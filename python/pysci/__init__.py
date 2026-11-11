"""pysci: a Python implementation of the SCI protocol (draft 0.1).

The most common entry points are re-exported here; see the submodules for
the full API:

* :mod:`pysci.bundle` -- verify publications (ZIP or loose files), write ZIPs;
* :mod:`pysci.draft` -- check and sign drafts;
* :mod:`pysci.records` -- key claims and revocations;
* :mod:`pysci.timestamps` -- RFC 3161 requests and proof verification;
* :mod:`pysci.discovery`, :mod:`pysci.submission`, :mod:`pysci.templates` -- §8.
"""

from __future__ import annotations

from .baseline import BASELINE_LICENSE_SHA256, baseline_license_bytes, evaluate_baseline
from .bundle import (
    Scope,
    VerificationLimits,
    VerificationResult,
    verify_directory,
    verify_file,
    verify_loose,
    verify_path,
    verify_zip,
    write_bundle_zip,
)
from .draft import BaselineRefused, DraftReport, SignedPublication, check_draft, sign_draft
from .errors import Incomplete, Invalid, Issue, Outcome, SciError, Unsupported
from .ids import parse_publication_id, publication_id_from_manifest
from .keys import PublicKey, generate_private_key, load_private_key_pem, public_key_of
from .manifest import Manifest, build_manifest, parse_manifest, verify_manifest
from .records import create_claim, create_revocation, verify_claim, verify_revocation
from .schemas import PROTOCOL_VERSION

__version__ = "0.3.0"

__all__ = [
    "BASELINE_LICENSE_SHA256",
    "PROTOCOL_VERSION",
    "BaselineRefused",
    "DraftReport",
    "Incomplete",
    "Invalid",
    "Issue",
    "Manifest",
    "Outcome",
    "PublicKey",
    "SciError",
    "Scope",
    "SignedPublication",
    "Unsupported",
    "VerificationLimits",
    "VerificationResult",
    "__version__",
    "baseline_license_bytes",
    "build_manifest",
    "check_draft",
    "create_claim",
    "create_revocation",
    "evaluate_baseline",
    "generate_private_key",
    "load_private_key_pem",
    "parse_manifest",
    "parse_publication_id",
    "public_key_of",
    "publication_id_from_manifest",
    "sign_draft",
    "verify_claim",
    "verify_directory",
    "verify_file",
    "verify_loose",
    "verify_manifest",
    "verify_path",
    "verify_revocation",
    "verify_zip",
    "write_bundle_zip",
]
