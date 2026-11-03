"""Local CLI configuration: ~/.sci/config.json, the default server and key."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
from pathlib import Path

from .output import CliError


def sci_home() -> Path:
    return Path(os.environ.get("SCI_HOME") or (Path.home() / ".sci"))


def config_path() -> Path:
    return sci_home() / "config.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError(f"{p} must contain a JSON object")
    return data


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def validate_server_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise CliError(f"invalid server URL {url!r}: {exc}", "invalid_url") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CliError(f"invalid server URL {url!r}: use an absolute http(s) URL such as https://sci.example.org", "invalid_url")
    if parts.username or parts.password:
        raise CliError("the server URL must not contain credentials", "invalid_url")
    if parts.query or parts.fragment:
        raise CliError("the server URL must not contain a query or fragment", "invalid_url")
    if any(c.isspace() for c in url):
        raise CliError("the server URL must not contain whitespace", "invalid_url")
    return url


def resolve_server(args) -> str:
    if getattr(args, "server", None):
        return validate_server_url(args.server)
    saved = load_config().get("server")
    if not saved:
        raise CliError(
            "no server selected. Run `sci server URL` to save a default, or pass --server URL",
            "no_server",
        )
    return validate_server_url(saved)


def default_key_path() -> Path:
    cfg = load_config()
    if cfg.get("key"):
        return Path(os.path.expanduser(cfg["key"]))
    return sci_home() / "key.pem"
