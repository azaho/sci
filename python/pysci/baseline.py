"""Baseline publications (protocol §2.1)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Final

from .errors import Issue

__all__ = [
    "APPROVED_LICENSE_SHA256",
    "BASELINE_LICENSE_SHA256",
    "BaselineLimits",
    "BaselineReport",
    "baseline_license_bytes",
    "evaluate_baseline",
]

BASELINE_LICENSE_SHA256: Final = "8ccfd5fdaa93e5e71dc8812bf11e0a7198bd0a534fa64b8970fd2de58e6c2478"
"""SHA-256 of the SCI baseline license 0.1."""

APPROVED_LICENSE_SHA256: Final = frozenset({BASELINE_LICENSE_SHA256})
"""Hashes of every approved baseline license."""


@dataclass(frozen=True, slots=True)
class BaselineLimits:
    """The §2.1 maxima."""

    zip_bytes: int = 64 * 1024 * 1024
    files: int = 1000
    display_items: int = 3
    manifest_bytes: int = 256 * 1024
    metadata_bytes: int = 64 * 1024
    readme_bytes: int = 1024 * 1024


BASELINE_LIMITS: Final = BaselineLimits()


@cache
def baseline_license_bytes() -> bytes:
    """Exact bytes of the approved SCI baseline license 0.1 (bundled package data).

    Raises:
        RuntimeError: the packaged file is damaged (hash mismatch).
    """
    data = resources.files("pysci").joinpath("data/sci-baseline-LICENSE.txt").read_bytes()
    if hashlib.sha256(data).hexdigest() != BASELINE_LICENSE_SHA256:
        raise RuntimeError("bundled baseline license does not match its approved hash")
    return data


@dataclass(frozen=True, slots=True)
class BaselineReport:
    """Result of evaluating the baseline requirements.

    ``eligible`` is True only when every limit holds and the license is an
    approved one. ``reasons`` lists each unmet requirement as an issue.
    """

    eligible: bool
    zip_bytes: int
    reasons: tuple[Issue, ...] = field(default_factory=tuple)


def evaluate_baseline(
    *,
    zip_bytes: int,
    file_count: int,
    display_items: int,
    manifest_bytes: int,
    metadata_bytes: int,
    readme_bytes: int,
    license_sha256: str | None,
    display_problems: Mapping[str, str],
    limits: BaselineLimits = BASELINE_LIMITS,
) -> BaselineReport:
    """Evaluate §2.1 for an otherwise valid publication (or a draft plan).

    ``file_count`` counts the manifest ``files`` entries (metadata, README, and
    LICENSE included; manifest and signature excluded). ``license_sha256`` is
    the SHA-256 of the ``LICENSE`` bytes, or None if there is no LICENSE.
    ``display_problems`` maps each display file outside the §2.1 display
    requirements to the reason (see :func:`pysci.png.display_problem`).
    """
    reasons: list[Issue] = []

    def check(value: int, maximum: int, code: str, what: str) -> None:
        if value > maximum:
            reasons.append(
                Issue(
                    code,
                    f"baseline allows at most {maximum:,} {what}",
                    section="2.1",
                    observed=f"{value:,}",
                )
            )

    check(zip_bytes, limits.zip_bytes, "baseline_zip_bytes", "bytes in the complete ZIP")
    check(file_count, limits.files, "baseline_file_count", "entries in the manifest files object")
    check(display_items, limits.display_items, "baseline_display_items", "display items")
    check(
        manifest_bytes, limits.manifest_bytes, "baseline_manifest_bytes", "bytes of manifest.json"
    )
    check(metadata_bytes, limits.metadata_bytes, "baseline_metadata_bytes", "bytes of sci.json")
    check(readme_bytes, limits.readme_bytes, "baseline_readme_bytes", "bytes of README.md")
    for path, problem in display_problems.items():
        reasons.append(
            Issue(
                "baseline_display_file",
                "display files must be static PNGs named .png: at most 2 MiB, 8,000,000 pixels, "
                "and 8,192 pixels per side; convert the file or remove it from display",
                section="2.1",
                file=path,
                observed=problem,
            )
        )
    if license_sha256 is None:
        reasons.append(
            Issue(
                "baseline_license_missing",
                "baseline requires LICENSE with the exact bytes of an approved license",
                section="2.1",
                file="LICENSE",
            )
        )
    elif license_sha256 not in APPROVED_LICENSE_SHA256:
        reasons.append(
            Issue(
                "baseline_license_not_approved",
                "LICENSE must be byte-identical to an approved baseline license",
                section="2.1",
                file="LICENSE",
                observed=license_sha256,
            )
        )
    return BaselineReport(eligible=not reasons, zip_bytes=zip_bytes, reasons=tuple(reasons))
