"""Record schema identifiers and the schema check of §5.1/§6."""

from __future__ import annotations

from typing import Final

from .errors import Invalid, Issue, Outcome, Unsupported
from .jsonstrict import JsonObject, json_type_name

__all__ = [
    "ARCHIVE_SCHEMA",
    "CLAIM_SCHEMA",
    "INDEX_SCHEMA",
    "KNOWN_SCHEMAS",
    "PROTOCOL_VERSION",
    "PUBLICATION_SCHEMA",
    "REVOCATION_SCHEMA",
    "check_schema",
]

PROTOCOL_VERSION: Final = "0.1"
PUBLICATION_SCHEMA: Final = "sci-publication/0.1"
CLAIM_SCHEMA: Final = "sci-key-claim/0.1"
REVOCATION_SCHEMA: Final = "sci-key-revocation/0.1"
INDEX_SCHEMA: Final = "sci-index/0.1"
ARCHIVE_SCHEMA: Final = "sci-archive/0.1"
KNOWN_SCHEMAS: Final = frozenset(
    {PUBLICATION_SCHEMA, CLAIM_SCHEMA, REVOCATION_SCHEMA, INDEX_SCHEMA, ARCHIVE_SCHEMA}
)


def check_schema(record: JsonObject, expected: str, *, file: str | None = None) -> None:
    """Check that ``schema`` names the expected record type and version.

    Raises:
        Unsupported: the schema string identifies no supported schema.
        Invalid: the schema is missing, not a string, or a known schema of a
            different record type.
    """
    schema = record.get("schema")
    if not isinstance(schema, str):
        raise Invalid(
            Issue(
                "schema_missing",
                "record requires a string schema field",
                section="5.1",
                file=file,
                field="/schema",
                observed=None if "schema" not in record else json_type_name(schema),
            )
        )
    if schema == expected:
        return
    if schema in KNOWN_SCHEMAS:
        raise Invalid(
            Issue(
                "schema_wrong_type",
                f"schema must identify this record type ({expected})",
                section="5.1",
                file=file,
                field="/schema",
                observed=schema,
            )
        )
    raise Unsupported(
        Issue(
            "schema_unsupported",
            f"unsupported schema; this library supports {expected}",
            section="6",
            file=file,
            field="/schema",
            observed=schema,
            outcome=Outcome.UNSUPPORTED,
        )
    )
