"""Discovery and retrieval interfaces (protocol §8).

Descriptor parsing and validation, template expansion helpers, and
interpretation of lookup responses. No network access is performed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .citations import is_http_uri
from .der import DerFraming, frame_values
from .errors import Incomplete, Invalid, Issue, IssueCollector, Outcome
from .ids import is_digest, parse_publication_id
from .jsonstrict import JsonObject, JsonValue, json_type_name, parse_json_object
from .schemas import ARCHIVE_SCHEMA, INDEX_SCHEMA, check_schema
from .templates import Expression, UrlTemplate, parse_template

__all__ = [
    "ArchiveDescriptor",
    "DescriptorKind",
    "IndexDescriptor",
    "TimestampLookup",
    "archive_descriptor_url",
    "index_descriptor_url",
    "is_absolute_url",
    "parse_archive_descriptor",
    "parse_descriptor",
    "parse_publication_list",
    "parse_publication_lookup",
    "parse_timestamp_lookup",
    "revocation_lookup_result",
    "template_variables",
]

_SAMPLE: Final = {
    "id": "0" * 64,
    "id2": "00",
    "x": "A" * 43,
    "file": "revocation.json.sig",
    "sha256": "0" * 64,
    "path": "files/data/a.txt",
}

_INDEX_TEMPLATES: Final[dict[str, frozenset[Expression]]] = {
    "publication_lookup_url": frozenset({Expression("", "id")}),
    "revocation_lookup_url": frozenset({Expression("", "x"), Expression("", "file")}),
    "timestamp_lookup_url": frozenset({Expression("", "id")}),
}
_INDEX_PLAIN: Final = ("submit_url", "policy_url", "docs_url")
_ARCHIVE_TEMPLATES: Final[dict[str, frozenset[Expression]]] = {
    "bundle_url": frozenset({Expression("", "id"), Expression("", "id2")}),
    "entry_url": frozenset({Expression("", "id"), Expression("", "id2"), Expression("+", "path")}),
    "blob_url": frozenset({Expression("", "sha256")}),
}
_ARCHIVE_PLAIN: Final = ("list_url",)
REVOCATION_FILES: Final = ("revocation.json", "revocation.json.sig", "revocation.json.sig.tsr")


class DescriptorKind(StrEnum):
    INDEX = "index"
    ARCHIVE = "archive"


def is_absolute_url(value: str) -> bool:
    """True for an absolute ``http``/``https`` URL (descriptor URLs, §8)."""
    return is_http_uri(value)


def template_variables(
    publication_id: str,
    *,
    path: str | None = None,
    sha256: str | None = None,
    x: str | None = None,
    file: str | None = None,
) -> dict[str, str]:
    """Variables for §8 templates from a publication ID in any accepted spelling."""
    digest = parse_publication_id(publication_id, allow_bare=True)
    variables = {"id": digest, "id2": digest[:2]}
    if path is not None:
        variables["path"] = path
    if sha256 is not None:
        variables["sha256"] = sha256
    if x is not None:
        variables["x"] = x
    if file is not None:
        variables["file"] = file
    return variables


@dataclass(frozen=True, slots=True)
class IndexDescriptor:
    """A validated ``sci-index/0.1`` descriptor (§8.1)."""

    publication_lookup_url: UrlTemplate
    revocation_lookup_url: UrlTemplate
    timestamp_lookup_url: UrlTemplate
    submit_url: str | None = None
    policy_url: str | None = None
    docs_url: str | None = None
    extra: Mapping[str, JsonValue] = field(default_factory=dict)

    def publication_lookup(self, publication_id: str) -> str:
        return self.publication_lookup_url.expand(template_variables(publication_id))

    def timestamp_lookup(self, publication_id: str) -> str:
        return self.timestamp_lookup_url.expand(template_variables(publication_id))

    def revocation_lookup(self, x: str, file: str) -> str:
        if file not in REVOCATION_FILES:
            raise ValueError(f"file must be one of {REVOCATION_FILES}")
        return self.revocation_lookup_url.expand({"x": x, "file": file})


@dataclass(frozen=True, slots=True)
class ArchiveDescriptor:
    """A validated ``sci-archive/0.1`` descriptor (§8.2)."""

    bundle_url: UrlTemplate | None = None
    entry_url: UrlTemplate | None = None
    blob_url: UrlTemplate | None = None
    list_url: str | None = None
    extra: Mapping[str, JsonValue] = field(default_factory=dict)

    def bundle(self, publication_id: str) -> str | None:
        return (
            None
            if self.bundle_url is None
            else self.bundle_url.expand(template_variables(publication_id))
        )

    def entry(self, publication_id: str, path: str) -> str | None:
        return (
            None
            if self.entry_url is None
            else self.entry_url.expand(template_variables(publication_id, path=path))
        )

    def blob(self, sha256: str) -> str | None:
        if not is_digest(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return None if self.blob_url is None else self.blob_url.expand({"sha256": sha256})


def _check_template(
    record: JsonObject,
    name: str,
    permitted: frozenset[Expression],
    required: bool,
    issues: IssueCollector,
    file: str,
) -> UrlTemplate | None:
    value = record.get(name)
    if not isinstance(value, str):
        issues.add(
            "descriptor_field_type",
            f"{name} must be a string",
            section="8",
            file=file,
            field="/" + name,
            observed=json_type_name(value),
        )
        return None
    try:
        template = parse_template(value)
    except ValueError as exc:
        issues.add(
            "descriptor_template",
            f"{name} must be a valid URI template: {exc}",
            section="8",
            file=file,
            field="/" + name,
            observed=value,
        )
        return None
    found = template.expressions
    unknown = sorted(str(e) for e in found - permitted)
    if unknown:
        issues.add(
            "descriptor_expression",
            f"{name} permits only {', '.join(sorted(map(str, permitted))) or 'no expressions'}",
            section="8",
            file=file,
            field="/" + name,
            observed=unknown,
        )
        return None
    if required and found != permitted:
        missing = sorted(str(e) for e in permitted - found)
        issues.add(
            "descriptor_expression",
            f"{name} requires {', '.join(missing)}",
            section="8.1",
            file=file,
            field="/" + name,
            observed=value,
        )
        return None
    if not is_absolute_url(template.expand(_SAMPLE)):
        issues.add(
            "descriptor_absolute_url",
            f"{name} must expand to an absolute URL",
            section="8",
            file=file,
            field="/" + name,
            observed=value,
        )
        return None
    return template


def _check_plain(record: JsonObject, name: str, issues: IssueCollector, file: str) -> str | None:
    value = record[name]
    if not isinstance(value, str):
        issues.add(
            "descriptor_field_type",
            f"{name} must be a string",
            section="8",
            file=file,
            field="/" + name,
            observed=json_type_name(value),
        )
        return None
    if "{" in value or "}" in value:
        issues.add(
            "descriptor_expression",
            f"{name} permits no template expressions",
            section="8",
            file=file,
            field="/" + name,
            observed=value,
        )
        return None
    if not is_absolute_url(value):
        issues.add(
            "descriptor_absolute_url",
            f"{name} must be an absolute URL",
            section="8",
            file=file,
            field="/" + name,
            observed=value,
        )
        return None
    return value


def parse_index_descriptor(data: bytes, *, file: str = "index.json") -> IndexDescriptor:
    """Validate an index descriptor (§8.1). Raises Invalid/Unsupported with all issues."""
    record = parse_json_object(data, file=file)
    check_schema(record, INDEX_SCHEMA, file=file)
    issues = IssueCollector()
    templates: dict[str, UrlTemplate | None] = {}
    for name, permitted in _INDEX_TEMPLATES.items():
        if name not in record:
            issues.add(
                "descriptor_required",
                f"{name} is required",
                section="8.1",
                file=file,
                field="/" + name,
            )
            continue
        templates[name] = _check_template(record, name, permitted, True, issues, file)
    plain = {
        name: _check_plain(record, name, issues, file) for name in _INDEX_PLAIN if name in record
    }
    issues.raise_if_any()
    known = {"schema", *_INDEX_TEMPLATES, *_INDEX_PLAIN}
    publication, revocation, timestamp = (templates[n] for n in _INDEX_TEMPLATES)
    assert publication
    assert revocation
    assert timestamp
    return IndexDescriptor(
        publication,
        revocation,
        timestamp,
        plain.get("submit_url"),
        plain.get("policy_url"),
        plain.get("docs_url"),
        {k: v for k, v in record.items() if k not in known},
    )


def parse_archive_descriptor(data: bytes, *, file: str = "archive.json") -> ArchiveDescriptor:
    """Validate an archive descriptor (§8.2).

    Besides field syntax, at least one of ``bundle_url`` and ``entry_url`` is
    required, since an archive must serve the manifest and signature through
    one of them.
    """
    record = parse_json_object(data, file=file)
    check_schema(record, ARCHIVE_SCHEMA, file=file)
    issues = IssueCollector()
    templates = {
        name: _check_template(record, name, permitted, False, issues, file)
        for name, permitted in _ARCHIVE_TEMPLATES.items()
        if name in record
    }
    plain = {
        name: _check_plain(record, name, issues, file) for name in _ARCHIVE_PLAIN if name in record
    }
    if "bundle_url" not in record and "entry_url" not in record:
        issues.add(
            "descriptor_required",
            "an archive must provide bundle_url or entry_url to serve the manifest and signature",
            section="8.2",
            file=file,
        )
    issues.raise_if_any()
    known = {"schema", *_ARCHIVE_TEMPLATES, *_ARCHIVE_PLAIN}
    return ArchiveDescriptor(
        templates.get("bundle_url"),
        templates.get("entry_url"),
        templates.get("blob_url"),
        plain.get("list_url"),
        {k: v for k, v in record.items() if k not in known},
    )


def parse_descriptor(
    data: bytes, kind: DescriptorKind | str
) -> IndexDescriptor | ArchiveDescriptor:
    """Validate a descriptor of the given kind."""
    if DescriptorKind(kind) is DescriptorKind.INDEX:
        return parse_index_descriptor(data)
    return parse_archive_descriptor(data)


def index_descriptor_url(origin: str) -> str:
    """``https://<index>/.well-known/sci/index.json`` for an index host or origin."""
    host = origin.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{host}/.well-known/sci/index.json"


def archive_descriptor_url(
    *,
    domain: str | None = None,
    github: tuple[str, str] | None = None,
    huggingface_repository_url: str | None = None,
) -> str:
    """Descriptor discovery locations (§8.2.1); supply exactly one source."""
    if sum(x is not None for x in (domain, github, huggingface_repository_url)) != 1:
        raise ValueError("supply exactly one of domain, github, huggingface_repository_url")
    if domain is not None:
        return f"https://{domain}/.well-known/sci/archive.json"
    if github is not None:
        owner, repo = github
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/sci/archive.json"
    assert huggingface_repository_url is not None
    return huggingface_repository_url.rstrip("/") + "/resolve/main/sci/archive.json"


# --------------------------------------------------------------------------- responses


def _unexpected_status(status: int, what: str) -> Incomplete:
    return Incomplete(
        Issue(
            "unexpected_status",
            f"{what} returned an unexpected status",
            section="8.1",
            observed=str(status),
            outcome=Outcome.INCOMPLETE,
        )
    )


def parse_publication_lookup(status: int, body: bytes) -> list[str]:
    """Interpret a publication lookup response (§8.1.1).

    ``200`` must carry ``{"descriptors": [absolute URLs...]}``; ``404`` means
    the index has no sources (an empty list). Other statuses raise
    :class:`~pysci.errors.Incomplete`.

    Raises:
        Invalid: a ``200`` body violates §8.1.1.
    """
    if status == 404:
        return []
    if status != 200:
        raise _unexpected_status(status, "publication lookup")
    record = parse_json_object(body, file="publication lookup")
    descriptors = record.get("descriptors")
    if not isinstance(descriptors, list) or not all(
        isinstance(url, str) and is_absolute_url(url) for url in descriptors
    ):
        raise Invalid(
            Issue(
                "lookup_descriptors",
                "descriptors must be an array of absolute archive descriptor URLs",
                section="8.1.1",
                observed=json_type_name(descriptors),
            )
        )
    return [url for url in descriptors if isinstance(url, str)]


@dataclass(frozen=True, slots=True)
class TimestampLookup:
    """Interpretation of a timestamp lookup (§8.1.3).

    ``existence`` is ``"unknown"`` for 404: a missing proof does not prove that
    no timestamp exists. Retained proofs must still be verified by the reader.
    """

    proofs_retained: bool
    existence: str
    data: bytes | None = None
    framing: DerFraming | None = None

    @property
    def proof_count(self) -> int:
        return 0 if self.framing is None else len(self.framing.values)

    readers_must_verify: bool = True


def parse_timestamp_lookup(status: int, body: bytes) -> TimestampLookup:
    """Interpret a timestamp lookup response; the body is a §6.8 timestamp file."""
    if status == 404:
        return TimestampLookup(False, "unknown")
    if status != 200:
        raise _unexpected_status(status, "timestamp lookup")
    return TimestampLookup(True, "retained", body, frame_values(body))


def revocation_lookup_result(status: int, body: bytes) -> bytes | None:
    """Revocation lookup (§8.1.2): file bytes for 200, None for 404 (status unknown)."""
    if status == 404:
        return None
    if status != 200:
        raise _unexpected_status(status, "revocation lookup")
    return body


def parse_publication_list(body: bytes) -> list[str]:
    """Parse a ``list_url`` body: UTF-8, one bare publication ID per LF-terminated line."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise Invalid(
            Issue("list_encoding", "publication list must be UTF-8", section="8.2")
        ) from None
    if text and not text.endswith("\n"):
        raise Invalid(Issue("list_format", "each line must be LF-terminated", section="8.2"))
    ids = text.split("\n")[:-1] if text else []
    bad = [line for line in ids if not is_digest(line)]
    if bad:
        raise Invalid(
            Issue(
                "list_format",
                "each line must be a 64-character lowercase digest",
                section="8.2",
                observed=bad[0],
            )
        )
    return ids
