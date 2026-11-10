"""``sci read``: read verified text or retrieve one file."""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pysci.bundle import Scope, read_zip_entry, verify_directory, verify_file, verify_zip
from pysci.errors import SciError
from pysci.manifest import LICENSE_PATH, METADATA_PATH, README_PATH, Manifest
from pysci.metadata import parse_metadata
from pysci.source import FileSource
from pysci.text import decode_text_file

from ..checks import checks, not_performed
from ..clients.archive import ArchiveSource, SourceFailure, failure_exit
from ..context import Context
from ..errors import CliError, Exit, exit_for_outcome, issue_record
from ..output import Result
from ..retrieval import resolve_sources
from ..spec import Command, arg
from ..storage.files import StoreOutcome, store_bytes
from .common import looks_like_id, parse_id

DEFAULT_LINES = 200
Getter = Callable[[str], bytes]


def _local(path: Path) -> tuple[Manifest, Getter, dict[str, Any]]:
    if path.is_dir():
        verification = verify_directory(path, scope=Scope.MANIFEST)
    else:
        verification = verify_zip(path, scope=Scope.MANIFEST)
    if not verification.ok or verification.manifest is None:
        error = CliError(
            verification.issues[0].code if verification.issues else "invalid",
            "manifest checks failed (§6.1)",
            exit_for_outcome(verification.outcome),
        )
        error.records.extend(issue_record(i) for i in verification.issues)
        raise error
    manifest = verification.manifest

    def get(member: str) -> bytes:
        try:
            if path.is_dir():
                data = (path / member).read_bytes()
                verify_file(manifest, member, data)
                return data
            with FileSource(path) as source:
                return read_zip_entry(source, member, expected_id=manifest.publication_id)[1]
        except SciError as exc:
            raise CliError.from_sci(exc, f"{member} failed file checks (§6.2)") from None
        except OSError as exc:
            raise CliError("input_unavailable", str(exc), Exit.INCOMPLETE, retryable=True) from None

    return manifest, get, {"kind": "directory" if path.is_dir() else "zip", "path": str(path)}


def _remote(
    args: argparse.Namespace, ctx: Context, digest: str, workdir: Path
) -> tuple[Manifest, Getter, dict[str, Any]]:
    deadline = ctx.deadline(args.timeout)
    sources = resolve_sources(
        ctx, digest, archives=args.archive, index_url=args.in_, deadline=deadline
    )
    failures: list[SourceFailure] = []
    archives = [ArchiveSource(ctx.http, url, digest, workdir) for url in sources.descriptors]
    usable: list[ArchiveSource] = []
    manifest: Manifest | None = None
    for archive in archives:
        try:
            manifest = archive.manifest(deadline)
            usable.append(archive)
        except SourceFailure as exc:
            exc.record.extra["descriptor"] = archive.descriptor_url
            failures.append(exc)
    if manifest is None:
        error = CliError(
            "retrieval_failed",
            f"no source provided a verified manifest for sha256:{digest}",
            failure_exit(failures),
            retryable=failure_exit(failures) == Exit.INCOMPLETE,
        )
        error.records.extend(f.record for f in failures)
        raise error

    def get(member: str) -> bytes:
        errors: list[SourceFailure] = []
        for archive in usable:
            try:
                return archive.file(member, deadline)
            except SourceFailure as exc:
                exc.record.extra["descriptor"] = archive.descriptor_url
                errors.append(exc)
        error = CliError(
            "retrieval_failed",
            f"no source provided verified bytes for {member}",
            failure_exit(errors),
            retryable=failure_exit(errors) == Exit.INCOMPLETE,
        )
        error.records.extend(e.record for e in errors)
        raise error

    info: dict[str, Any] = {
        "kind": "remote",
        "descriptors": sources.descriptors,
        "index": sources.index.origin if sources.index else None,
        "used": [a.descriptor_url for a in usable],
        "source_failures": [f.record.to_json() for f in failures],
    }
    info["_archives"] = usable
    return manifest, get, info


def _text_range(text: str, path: str, start: int, count: int) -> tuple[dict[str, Any], list[str]]:
    lines = text.splitlines()
    begin = max(start, 1) - 1
    selected = lines[begin : begin + count]
    end = begin + len(selected)
    truncated = end < len(lines)
    info = {
        "path": path,
        "start_line": begin + 1,
        "end_line": end,
        "total_lines": len(lines),
        "truncated": truncated,
        "next_line": end + 1 if truncated else None,
        "lines": selected,
    }
    return info, selected


def run(args: argparse.Namespace, ctx: Context) -> Result:
    if args.list and args.file:
        raise CliError("conflicting_options", "--list and --file cannot be combined")
    if args.output and not args.file:
        raise CliError("invalid_arguments", "--output requires --file")
    if args.start_line < 1 or args.lines < 1:
        raise CliError("invalid_arguments", "--start-line and --lines must be positive")
    target = args.id_or_path
    remote = looks_like_id(target)
    if not remote and (args.archive or args.in_):
        raise CliError("invalid_arguments", "--archive and --in apply only to publication IDs")
    with tempfile.TemporaryDirectory(prefix="sci-read-") as workdir:
        if remote:
            manifest, get, source = _remote(args, ctx, parse_id(target), Path(workdir))
        else:
            path = ctx.resolve(target)
            if not path.exists():
                raise CliError(
                    "input_missing",
                    f"{path} does not exist (publication IDs start with sha256:)",
                    Exit.USAGE,
                )
            manifest, get, source = _local(path)
        try:
            return _render(args, ctx, manifest, get, source)
        finally:
            for archive in source.pop("_archives", []):
                archive.close()


def _render(
    args: argparse.Namespace,
    ctx: Context,
    manifest: Manifest,
    get: Getter,
    source: dict[str, Any],
) -> Result:
    result = Result("read")
    verified: list[str] = []
    data: dict[str, Any] = {
        "publication_id": manifest.publication_id,
        "signer": manifest.public_key.to_jwk(),
        "source": source,
    }
    result.data = data
    result.say(f"publication: {manifest.publication_id}")
    if args.list:
        files: list[dict[str, Any]] = [
            {"path": p, "bytes": manifest.files[p].bytes, "sha256": manifest.files[p].sha256}
            for p in manifest.sorted_paths()
        ]
        data["files"] = files
        data["verified"] = "manifest checks (§6.1); listed files were not retrieved"
        for entry in files:
            result.say(f"{entry['bytes']:>12}  {entry['sha256'][:16]}  {entry['path']}")
    elif args.file:
        member = args.file
        if member not in manifest.files:
            raise CliError(
                "file_not_listed",
                f"{member} is not listed in the manifest",
                Exit.INVALID,
                file=member,
                section="6.2",
            )
        content = get(member)
        verified.append(member)
        if args.output:
            target = ctx.resolve(args.output)
            stored = store_bytes(target, content)
            if stored.outcome is StoreOutcome.CONFLICT:
                raise CliError(
                    "destination_conflict",
                    f"{target} exists with different bytes; it was left unchanged",
                    Exit.INVALID,
                    file=str(target),
                )
            data["output"] = {
                "path": str(target),
                "status": str(stored.outcome),
                "bytes": len(content),
            }
            result.say(f"saved {member} ({len(content)} bytes, verified) to {target}")
        else:
            try:
                text = content.decode("utf-8")
                if "\x00" in text:
                    raise UnicodeDecodeError("utf-8", content, 0, 1, "NUL byte")
            except UnicodeDecodeError:
                raise CliError(
                    "binary_file",
                    f"{member} is not UTF-8 text; save it with --output FILE",
                    Exit.USAGE,
                    file=member,
                ) from None
            info, lines = _text_range(text, member, args.start_line, args.lines)
            data["text"] = info
            result.lines.extend(lines)
            _truncation(result, info)
    else:
        metadata_bytes = get(METADATA_PATH)
        readme = get(README_PATH)
        verified += [METADATA_PATH, README_PATH]
        try:
            metadata = parse_metadata(metadata_bytes, list(manifest.files))
            text = decode_text_file(readme, file=README_PATH, nonblank=True)
            if LICENSE_PATH in manifest.files:
                decode_text_file(get(LICENSE_PATH), file=LICENSE_PATH)
                verified.append(LICENSE_PATH)
        except SciError as exc:
            raise CliError.from_sci(exc, "metadata checks failed (§6.3)") from None
        raw = metadata.raw
        data["metadata"] = {
            "title": metadata.title,
            "abstract": metadata.abstract,
            "categories": raw.get("categories", []),
            "keywords": list(metadata.keywords),
            "display": raw.get("display", []),
            "citations": raw.get("citations", []),
            "license": LICENSE_PATH in manifest.files,
        }
        info, lines = _text_range(text, README_PATH, args.start_line, args.lines)
        data["text"] = info
        result.say(f"title:       {metadata.title}")
        result.say(f"signer x:    {manifest.public_key.x}")
        if metadata.keywords:
            result.say(f"keywords:    {', '.join(metadata.keywords)}")
        result.say("abstract:")
        result.lines.extend("  " + line for line in metadata.abstract.splitlines())
        if metadata.citations:
            result.say(f"citations:   {len(metadata.citations)}")
            citations = raw.get("citations")
            for citation in citations if isinstance(citations, list) else []:
                if isinstance(citation, dict):
                    listed = citation.get("relations")
                    names = [str(r) for r in listed] if isinstance(listed, list) else []
                    relations = ",".join(names)
                    result.say(
                        f"  {citation.get('target')}" + (f" [{relations}]" if relations else "")
                    )
        result.say("")
        result.say(f"--- {README_PATH} ---")
        result.lines.extend(lines)
        _truncation(result, info)
    data["verified_files"] = verified
    data["checks"] = checks(
        bundle=not_performed(
            "complete bundle checks (§6.4) were not performed; only the listed files passed "
            "file checks (§6.2)"
        ),
        baseline=not_performed("baseline is evaluated only after complete bundle verification"),
        timestamps=not_performed("timestamps are checked by `sci verify` and `sci pull`"),
    )
    if verified:
        result.say(
            f"verified: {', '.join(verified)} (manifest and file checks; not a complete "
            "bundle check)"
        )
    del ctx
    return result


def _truncation(result: Result, info: dict[str, Any]) -> None:
    if info["truncated"]:
        result.say(
            f"[lines {info['start_line']}-{info['end_line']} of {info['total_lines']}; "
            f"continue with --start-line {info['next_line']}]"
        )


COMMAND = Command(
    name="read",
    usage_hint=(
        "read ID_OR_PATH [--list | --file PATH [--output FILE]] [--start-line N] [--lines N] "
        "[--archive ARCHIVE ... | --in URL] [--json]"
    ),
    summary="Read verified text or retrieve one file.",
    description="""
Accepts a signed directory, a ZIP, or a full publication ID (sha256:<digest>
or sci:sha256:<digest>). Local input is read offline. IDs are resolved like
`pull`: the configured index or --in URL, or explicit --archive descriptor
URLs or GitHub/Hugging Face repository addresses.

By default prints metadata, citations, and README text after the metadata
checks of SCI protocol §6.3. --list prints paths, sizes, and hashes after the
manifest checks (§6.1). --file PATH retrieves one manifest entry after the
file checks (§6.2). Remote files come from individual-file routes first
(entry_url, blob_url), then ZIP range requests, then a complete ZIP; the
whole file is verified before any part is shown. Only the requested files
are verified; this is not a complete bundle check.
""",
    args=(
        arg("id_or_path", metavar="ID_OR_PATH", help="signed directory, ZIP, or publication ID"),
        arg("--list", action="store_true", help="list manifest paths, sizes, and SHA-256 hashes"),
        arg("--file", metavar="PATH", help="bundle path to retrieve, e.g. files/data.csv"),
        arg(
            "--output",
            metavar="FILE",
            help="with --file, save the complete original bytes (required for binary files)",
        ),
        arg("--start-line", type=int, default=1, metavar="N", help="first text line to show"),
        arg("--lines", type=int, default=DEFAULT_LINES, metavar="N", help="number of text lines"),
        arg(
            "--archive",
            metavar="ARCHIVE",
            action="append",
            help="descriptor URL or GitHub/Hugging Face repository address (repeatable)",
        ),
        arg("--in", dest="in_", metavar="URL", help="index origin (default: configured index)"),
    ),
    outputs="""Metadata and a README line range (reporting truncation and the next line),
a file listing, or one file's text or saved path; plus which files passed
verification.""",
    files="Only --output FILE (an identical file is kept; conflicting bytes are rejected).",
    network="None for local input. For IDs: index lookup and archive GET/range requests.",
    exit_codes={
        3: "a manifest, file, or metadata check failed (or every source served bad content)",
        4: "an unsupported schema",
        5: "no source could be reached, no known sources, or a local limit was exceeded",
    },
    examples=(
        "sci read ./campaign/<digest>.zip --file files/results.csv --lines 20",
        "sci read sha256:<digest> --archive https://raw.githubusercontent.com/lab/research/main/"
        "sci/archive.json",
    ),
    run=run,
    timeout=True,
)
