"""Unsigned drafts: checks before signing (§2-§4, §2.1) and signing (§3, §5.1).

A draft is a set of files at their bundle paths (``sci.json``, ``README.md``,
optional ``LICENSE``, ``files/...``) without ``manifest.json`` and
``manifest.json.sig``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import ed25519
from .baseline import BaselineReport, evaluate_baseline
from .bundle import VerificationResult, verify_loose
from .errors import Incomplete, Issue, IssueCollector, Outcome, SciError
from .keys import PublicKey, looks_like_private_key, public_key_of
from .manifest import (
    LICENSE_PATH,
    MANIFEST_PATH,
    METADATA_PATH,
    README_PATH,
    SIGNATURE_PATH,
    TIMESTAMP_SIDECAR_PATH,
    FileEntry,
    build_manifest,
    serialize_manifest,
)
from .metadata import Metadata, parse_metadata
from .paths import check_paths
from .source import ByteSource, PathSource, as_source, iter_chunks
from .text import TextFileValidator
from .zipformat import canonical_zip_size

__all__ = [
    "GENERATED_PATHS",
    "BaselineRefused",
    "DraftReport",
    "SignedPublication",
    "check_draft",
    "load_draft",
    "sign_draft",
]

GENERATED_PATHS: Final = frozenset({MANIFEST_PATH, SIGNATURE_PATH, TIMESTAMP_SIDECAR_PATH})
"""Files produced by signing/timestamping; never part of the draft content."""

_PLACEHOLDER_KEY: Final = PublicKey(bytes(32))
_PLACEHOLDER_HASH: Final = "0" * 64
_METADATA_READ_LIMIT: Final = 16 * 1024**2
_KEY_SCAN_OVERLAP: Final = 64


@dataclass(frozen=True, slots=True)
class DraftReport:
    """Result of :func:`check_draft`.

    ``valid`` means a signature over these files would yield a protocol-valid
    publication. Baseline eligibility is reported separately in ``baseline``.
    ``signed`` is True if generated files (manifest, signature) were present
    and ignored.
    """

    issues: tuple[Issue, ...]
    files: Mapping[str, int]
    metadata: Metadata | None
    manifest_bytes: int | None
    zip_bytes: int | None
    baseline: BaselineReport | None
    signed: bool = False

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def outcome(self) -> Outcome:
        return IssueCollector(list(self.issues)).outcome


@dataclass(frozen=True, slots=True)
class SignedPublication:
    """A signed bundle: exact manifest bytes, raw signature, and its verification."""

    manifest_bytes: bytes
    signature: bytes
    publication_id: str
    verification: VerificationResult


class BaselineRefused(Exception):
    """Signing stopped because the draft does not meet the baseline (§2.1)."""

    def __init__(self, report: BaselineReport) -> None:
        self.report = report
        super().__init__("; ".join(str(reason) for reason in report.reasons))


def load_draft(
    directory: str | os.PathLike[str], ignore: Collection[str] = ()
) -> tuple[dict[str, ByteSource], list[Issue], bool]:
    """Collect draft files from ``directory``.

    Returns ``(sources, issues, signed)``: sources keyed by relative POSIX
    path; issues for symbolic links and special files; whether generated
    files (manifest, signature, sidecar) were present (they are excluded).
    Paths in ``ignore`` (relative POSIX paths) are skipped.
    """
    root = Path(directory)
    sources: dict[str, ByteSource] = {}
    issues: list[Issue] = []
    signed = False
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as iterator:
            for item in sorted(iterator, key=lambda e: e.name):
                relative = Path(item.path).relative_to(root).as_posix()
                if relative in ignore:
                    continue
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (
                    stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                ):
                    issues.append(
                        Issue(
                            "not_regular_file",
                            "drafts may contain only regular "
                            "files and directories (no symbolic links or special "
                            "files)",
                            section="2",
                            file=relative,
                        )
                    )
                elif stat.S_ISDIR(info.st_mode):
                    stack.append(Path(item.path))
                elif relative in GENERATED_PATHS:
                    signed = True
                else:
                    sources[relative] = PathSource(item.path, info.st_size)
    return sources, issues, signed


def _contains_private_key(source: ByteSource) -> bool:
    tail = b""
    for chunk in iter_chunks(source):
        if looks_like_private_key(tail + chunk):
            return True
        tail = chunk[-_KEY_SCAN_OVERLAP:]
    return False


def check_draft(
    draft: str | os.PathLike[str] | Mapping[str, ByteSource | bytes],
    *,
    ignore: Collection[str] = (),
) -> DraftReport:
    """Check an unsigned draft without signing it (CLI ``sci check``).

    Reports all independent problems together: paths and conflicts (§3.1),
    required files (§3), ``sci.json`` (§4), README/LICENSE text (§2, §4.2),
    private-key material (§2), and the planned manifest and ZIP sizes with
    baseline eligibility (§2.1). Reads no private key and writes nothing.
    """
    if isinstance(draft, Mapping):
        sources = {path: as_source(value) for path, value in draft.items()}
        signed = any(path in GENERATED_PATHS for path in sources)
        sources = {p: s for p, s in sources.items() if p not in GENERATED_PATHS}
        load_issues: list[Issue] = []
    else:
        sources, load_issues, signed = load_draft(draft, ignore)
    collector = IssueCollector(list(load_issues))
    try:
        return _check(sources, collector, signed)
    except OSError as exc:
        collector.add(
            "input_unavailable",
            "draft files could not be read",
            section="6",
            observed=str(exc),
            outcome=Outcome.INCOMPLETE,
        )
        return DraftReport(tuple(collector.issues), {}, None, None, None, None, signed)


def _check(sources: dict[str, ByteSource], collector: IssueCollector, signed: bool) -> DraftReport:
    paths = list(sources)
    for required in (METADATA_PATH, README_PATH):
        if required not in sources:
            collector.add(
                "draft_required_file",
                f"a draft must contain {required}",
                section="2",
                file=required,
            )
    for path in paths:
        if path not in (METADATA_PATH, README_PATH, LICENSE_PATH) and not path.startswith("files/"):
            collector.add(
                "manifest_path_location",
                "files other than sci.json, README.md, and LICENSE must be under files/",
                section="3",
                file=path,
            )
    check_paths(paths, collector=collector)
    for path, source in sources.items():
        if _contains_private_key(source):
            collector.add(
                "private_key_material",
                "publishing tools must not include private key material in a bundle",
                section="2",
                file=path,
            )
    metadata = None
    if METADATA_PATH in sources:
        source = sources[METADATA_PATH]
        if source.size > _METADATA_READ_LIMIT:
            collector.add(
                "local_size_limit",
                "sci.json exceeds the local size limit",
                section="6",
                file=METADATA_PATH,
                observed=source.size,
                outcome=Outcome.INCOMPLETE,
            )
        else:
            try:
                metadata = parse_metadata(source.read_at(0, source.size), paths)
            except SciError as exc:
                collector.extend(exc.issues)
    for path, nonblank in ((README_PATH, True), (LICENSE_PATH, False)):
        if path in sources:
            validator = TextFileValidator(file=path, nonblank=nonblank)
            for chunk in iter_chunks(sources[path]):
                if not validator.feed(chunk):
                    break
            collector.extend(validator.finish())
    sizes = {path: source.size for path, source in sources.items()}
    manifest_size = zip_size = None
    baseline = None
    planned = serialize_manifest(
        _PLACEHOLDER_KEY, {p: FileEntry(_PLACEHOLDER_HASH, n) for p, n in sizes.items()}
    )
    if planned:
        manifest_size = len(planned)
        zip_size = canonical_zip_size(
            [(MANIFEST_PATH, manifest_size), (SIGNATURE_PATH, 64), *sizes.items()]
        )
        license_hash = None
        if LICENSE_PATH in sources:
            digest = hashlib.sha256()
            for chunk in iter_chunks(sources[LICENSE_PATH]):
                digest.update(chunk)
            license_hash = digest.hexdigest()
        baseline = evaluate_baseline(
            zip_bytes=zip_size,
            file_count=len(sizes),
            display_items=len(metadata.display) if metadata else 0,
            manifest_bytes=manifest_size,
            metadata_bytes=sizes.get(METADATA_PATH, 0),
            readme_bytes=sizes.get(README_PATH, 0),
            license_sha256=license_hash,
        )
    return DraftReport(
        tuple(collector.issues), sizes, metadata, manifest_size, zip_size, baseline, signed
    )


def sign_draft(
    draft: str | os.PathLike[str] | Mapping[str, ByteSource | bytes],
    private_key: Ed25519PrivateKey,
    *,
    allow_nonbaseline: bool = False,
    ignore: Collection[str] = (),
) -> SignedPublication:
    """Check, build the manifest, sign (§5.1), and verify the result (§6.4).

    Nothing is written; the caller stores ``manifest_bytes`` and
    ``signature`` (and optionally a ZIP via :func:`pysci.bundle.write_bundle_zip`).

    Raises:
        Invalid: the draft is not protocol-valid (all issues attached).
        BaselineRefused: baseline unmet and ``allow_nonbaseline`` is False.
        Incomplete: files could not be read or changed during signing.
    """
    if isinstance(draft, Mapping):
        sources = {p: as_source(v) for p, v in draft.items() if p not in GENERATED_PATHS}
    else:
        sources, _, _ = load_draft(draft, ignore)
    report = (
        check_draft(sources) if isinstance(draft, Mapping) else check_draft(draft, ignore=ignore)
    )
    if report.issues:
        IssueCollector(list(report.issues)).raise_if_any()
    assert report.baseline is not None
    if not report.baseline.eligible and not allow_nonbaseline:
        raise BaselineRefused(report.baseline)
    entries: dict[str, FileEntry] = {}
    for path, source in sources.items():
        digest = hashlib.sha256()
        for chunk in iter_chunks(source):
            digest.update(chunk)
        entries[path] = FileEntry(digest.hexdigest(), source.size)
    manifest_bytes = build_manifest(public_key_of(private_key), entries)
    signature = ed25519.sign(private_key, manifest_bytes)
    files: dict[str, ByteSource | bytes] = {
        MANIFEST_PATH: manifest_bytes,
        SIGNATURE_PATH: signature,
        **sources,
    }
    result = verify_loose(files)
    if not result.ok:
        if any(i.code in ("file_hash_mismatch", "file_size_mismatch") for i in result.issues):
            raise Incomplete(
                Issue(
                    "draft_changed",
                    "draft files changed during signing",
                    section="6",
                    outcome=Outcome.INCOMPLETE,
                )
            )
        IssueCollector(list(result.issues)).raise_if_any()
    return SignedPublication(
        manifest_bytes, signature, "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(), result
    )
