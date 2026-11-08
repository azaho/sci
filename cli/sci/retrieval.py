"""Resolving publication sources by ID: index lookup (§8.1.1) or explicit archives."""

from __future__ import annotations

from dataclasses import dataclass

from .clients.http import Deadline
from .clients.index import IndexClient
from .commands.common import archive_urls, index_origin
from .context import Context
from .errors import CliError, Exit


@dataclass(frozen=True, slots=True)
class Sources:
    descriptors: list[str]
    index: IndexClient | None


def resolve_sources(
    ctx: Context,
    digest: str,
    *,
    archives: list[str] | None,
    index_url: str | None,
    deadline: Deadline,
) -> Sources:
    """Archive descriptor URLs for ``digest``.

    Explicit ``--archive`` URLs need no index. Otherwise the configured index
    or ``--in URL`` is asked; ``404`` or an empty list is ``no_known_sources``.
    """
    explicit = archive_urls(archives)
    if explicit and index_url:
        raise CliError("conflicting_options", "use either --archive or --in, not both")
    if explicit:
        return Sources(explicit, None)
    client = IndexClient(ctx.http, index_origin(ctx, index_url, option="--in"))
    descriptors = client.lookup(digest, deadline)
    unique: list[str] = []
    for url in descriptors:
        if url not in unique:
            unique.append(url)
    if not unique:
        raise CliError(
            "no_known_sources",
            f"index {client.origin} knows no sources for sha256:{digest}",
            Exit.INCOMPLETE,
            retryable=True,
        )
    return Sources(unique, client)
