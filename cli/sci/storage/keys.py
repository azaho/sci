"""Signing key files: locations, creation with owner-only access, and loading."""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci.keys import (
    PublicKey,
    generate_private_key,
    load_private_key_pem,
    load_public_key_pem,
    private_key_to_pem,
    public_key_of,
)

from ..errors import CliError, Exit
from .files import is_within


def public_key_path(private_path: Path) -> Path:
    """Replace a final ``.pem`` with ``.pub.pem``, or append ``.pub.pem``."""
    name = private_path.name
    if name.endswith(".pem"):
        return private_path.with_name(name[: -len(".pem")] + ".pub.pem")
    return private_path.with_name(name + ".pub.pem")


def resolve_key_path(explicit: str | None, configured: str | None, home: Path) -> Path:
    if explicit:
        return Path(explicit).expanduser().absolute()
    if configured:
        return Path(configured).expanduser().absolute()
    return home / "key.pem"


def create_key_pair(private_path: Path) -> tuple[PublicKey, Path]:
    """Create a PKCS#8 private key (mode 0600) and its SubjectPublicKeyInfo public key.

    Raises:
        CliError: an output already exists (exit 2).
    """
    public_path = public_key_path(private_path)
    for path in (private_path, public_path):
        if path.exists() or path.is_symlink():
            raise CliError(
                "key_exists",
                f"{path} already exists; keygen never overwrites keys",
                Exit.USAGE,
                file=str(path),
            )
    private_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = generate_private_key()
    public = public_key_of(key)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(private_key_to_pem(key))
            handle.flush()
            os.fsync(handle.fileno())
        fd = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(public.to_pem())
    except BaseException:
        with contextlib.suppress(OSError):
            private_path.unlink()
        raise
    return public, public_path


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise CliError(
            "missing_key",
            f"no private key at {path}; create one with `sci keygen` or pass --key PATH",
            Exit.USAGE,
            file=str(path),
        ) from None
    except OSError as exc:
        raise CliError("key_unreadable", f"cannot read {path}: {exc}", Exit.USAGE) from None
    try:
        return load_private_key_pem(data)
    except (ValueError, TypeError) as exc:
        raise CliError(
            "key_invalid",
            f"{path} is not an unencrypted PKCS#8 Ed25519 private key: {exc}",
            Exit.USAGE,
            file=str(path),
        ) from None


def key_permission_warning(path: Path) -> str | None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"private key {path} is accessible by other users; run chmod 600 {path}"
    return None


def require_outside(key_path: Path, *directories: Path) -> None:
    """Private keys must stay outside publication input and output directories."""
    for directory in directories:
        if is_within(key_path, directory):
            raise CliError(
                "key_inside_publication",
                f"private key {key_path} is inside {directory}; keep keys outside "
                "publication input and output directories",
                Exit.USAGE,
                file=str(key_path),
            )


def load_public_key_file(path: Path) -> PublicKey:
    try:
        return load_public_key_pem(path.read_bytes())
    except FileNotFoundError:
        raise CliError("public_key_missing", f"{path} does not exist", Exit.USAGE) from None
    except (ValueError, TypeError) as exc:
        raise CliError(
            "public_key_invalid",
            f"{path} is not a SubjectPublicKeyInfo PEM Ed25519 public key: {exc}",
            Exit.USAGE,
        ) from None
