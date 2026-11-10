"""Command registry, in help order."""

from __future__ import annotations

from ..spec import Command
from . import (
    check,
    claim,
    folders,
    index,
    keygen,
    license,
    protocol,
    publish,
    pull,
    read,
    revoke,
    search,
    sign,
    submit,
    timestamp,
    verify,
)

COMMANDS: tuple[Command, ...] = (
    search.COMMAND,
    read.COMMAND,
    pull.COMMAND,
    check.COMMAND,
    license.COMMAND,
    sign.COMMAND,
    verify.COMMAND,
    publish.COMMAND,
    submit.COMMAND,
    keygen.COMMAND,
    claim.COMMAND,
    timestamp.COMMAND,
    revoke.COMMAND,
    index.COMMAND,
    folders.COMMAND,
    protocol.COMMAND,
)

__all__ = ["COMMANDS"]
