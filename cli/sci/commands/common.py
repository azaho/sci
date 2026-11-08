"""Helpers shared by several commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pysci.errors import SciError
from pysci.ids import parse_publication_id
from pysci.timestamps import TrustConfig

from ..clients.http import Deadline
from ..clients.index import IndexClient
from ..clients.tsa import build_trust
from ..config import normalize_origin, normalize_url
from ..context import Context
from ..errors import CliError, Exit
from ..storage.keys import key_permission_warning, load_signing_key, resolve_key_path


def looks_like_id(value: str) -> bool:
    return value.startswith(("sha256:", "sci:sha256:"))


def parse_id(value: str) -> str:
    """Accept ``sha256:<digest>`` or ``sci:sha256:<digest>``; return the bare digest."""
    try:
        return parse_publication_id(value)
    except ValueError as exc:
        raise CliError(
            "invalid_id",
            f"{exc}",
            Exit.USAGE,
            observed=value,
            requirement="sha256:<64 lowercase hex> or sci:sha256:<64 lowercase hex>",
        ) from None


def index_origin(ctx: Context, explicit: str | None, *, option: str = "--to") -> str:
    if explicit:
        return normalize_origin(explicit)
    if ctx.config.index:
        return ctx.config.index
    raise CliError(
        "missing_index",
        f"no index configured; pass {option} URL or save one with `sci index URL`",
        Exit.USAGE,
    )


def index_client(ctx: Context, explicit: str | None, *, option: str = "--to") -> IndexClient:
    return IndexClient(ctx.http, index_origin(ctx, explicit, option=option))


def archive_urls(values: list[str] | None) -> list[str]:
    urls: list[str] = []
    for value in values or []:
        url = normalize_url(value, what="archive descriptor URL")
        if url not in urls:
            urls.append(url)
    return urls


def trust(ctx: Context, *, online: bool = False, deadline: Deadline | None = None) -> TrustConfig:
    config, warnings = build_trust(
        ctx.config.timestamp_trust, online=online, http=ctx.http, deadline=deadline
    )
    del warnings
    return config


def trust_with_warnings(
    ctx: Context, *, online: bool = False, deadline: Deadline | None = None
) -> tuple[TrustConfig, list[str]]:
    return build_trust(ctx.config.timestamp_trust, online=online, http=ctx.http, deadline=deadline)


def signing_key(ctx: Context, args: argparse.Namespace) -> tuple[Path, Any, list[str]]:
    path = resolve_key_path(getattr(args, "key", None), ctx.config.key, ctx.home)
    key = load_signing_key(path)
    warnings = [w for w in [key_permission_warning(path)] if w]
    return path, key, warnings


def sci_failure(exc: SciError, message: str) -> CliError:
    return CliError.from_sci(exc, message)


KEY_ARG_HELP = "private key (PKCS#8 PEM); default: configured key, else <state>/key.pem"
