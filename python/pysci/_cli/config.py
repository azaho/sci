"""CLI state directory and ``config.json`` (index origin, key, folders, TSA, trust)."""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pysci.errors import SciError
from pysci.jsonstrict import parse_json_object

from .errors import CliError, Exit
from .storage.files import atomic_write_bytes

CONFIG_FILE = "config.json"
CATALOG_FILE = "catalog.sqlite"
DEFAULT_KEY = "key.pem"


def state_dir(env: Mapping[str, str]) -> Path:
    """``SCI_HOME`` if set, else ``~/.sci``."""
    home = env.get("SCI_HOME")
    if home:
        return Path(home).expanduser().absolute()
    return Path(env.get("HOME") or Path.home()).expanduser() / ".sci"


def ensure_state_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@dataclass(slots=True)
class TrustSettings:
    """Timestamp trust configuration.

    ``freetsa`` enables the bundled FreeTSA root; ``anchors`` and ``crls`` are
    local PEM/DER files; ``crl_urls`` are fetched only for online checks.
    """

    freetsa: bool = True
    anchors: list[str] = field(default_factory=list)
    crls: list[str] = field(default_factory=list)
    crl_urls: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "freetsa": self.freetsa,
            "anchors": list(self.anchors),
            "crls": list(self.crls),
            "crl_urls": list(self.crl_urls),
        }


@dataclass(slots=True)
class Config:
    index: str | None = None
    key: str | None = None
    folders: list[str] = field(default_factory=list)
    tsa_url: str | None = None
    timestamp_trust: TrustSettings = field(default_factory=TrustSettings)
    other: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.other)
        out.update(
            {
                "index": self.index,
                "key": self.key,
                "folders": list(self.folders),
                "tsa_url": self.tsa_url,
                "timestamp_trust": self.timestamp_trust.to_json(),
            }
        )
        return out


def _config_error(field_name: str, requirement: str, observed: object) -> CliError:
    return CliError(
        "config_invalid",
        f"{CONFIG_FILE} field {field_name}: {requirement}",
        Exit.USAGE,
        file=CONFIG_FILE,
        field="/" + field_name.replace(".", "/"),
        requirement=requirement,
        observed=repr(observed)[:120],
    )


def _optional_str(record: Mapping[str, Any], name: str) -> str | None:
    value = record.get(name)
    if value is not None and not isinstance(value, str):
        raise _config_error(name, "must be a string or null", value)
    return value


def _str_list(record: Mapping[str, Any], name: str, label: str | None = None) -> list[str]:
    value = record.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _config_error(label or name, "must be an array of strings", value)
    return [str(item) for item in value]


def load_config(home: Path) -> Config:
    path = home / CONFIG_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise CliError("config_unreadable", f"cannot read {path}: {exc}", Exit.USAGE) from None
    try:
        record = parse_json_object(data, file=CONFIG_FILE)
    except SciError as exc:
        raise CliError(
            "config_invalid", f"{path} is not a valid JSON object: {exc}", Exit.USAGE
        ) from None
    trust_record = record.get("timestamp_trust", {})
    if not isinstance(trust_record, dict):
        raise _config_error("timestamp_trust", "must be an object", trust_record)
    freetsa = trust_record.get("freetsa", True)
    if not isinstance(freetsa, bool):
        raise _config_error("timestamp_trust.freetsa", "must be a Boolean", freetsa)
    trust = TrustSettings(
        freetsa=freetsa,
        anchors=_str_list(trust_record, "anchors", "timestamp_trust.anchors"),
        crls=_str_list(trust_record, "crls", "timestamp_trust.crls"),
        crl_urls=_str_list(trust_record, "crl_urls", "timestamp_trust.crl_urls"),
    )
    config = Config(
        index=_optional_str(record, "index"),
        key=_optional_str(record, "key"),
        folders=_str_list(record, "folders"),
        tsa_url=_optional_str(record, "tsa_url"),
        timestamp_trust=trust,
        other={
            k: v
            for k, v in record.items()
            if k not in ("index", "key", "folders", "tsa_url", "timestamp_trust")
        },
    )
    if config.index is not None:
        try:
            config.index = normalize_origin(config.index)
        except CliError:
            raise _config_error("index", "must be an HTTPS origin", config.index) from None
    if config.tsa_url is not None:
        try:
            config.tsa_url = normalize_url(config.tsa_url, what="TSA URL")
        except CliError:
            raise _config_error(
                "tsa_url", "must be an absolute HTTPS URL", config.tsa_url
            ) from None
    return config


def save_config(home: Path, config: Config) -> None:
    ensure_state_dir(home)
    data = json.dumps(config.to_json(), indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    atomic_write_bytes(home / CONFIG_FILE, data)


# --------------------------------------------------------------------------- URLs


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _split(value: str, what: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port
    except ValueError:
        raise CliError("invalid_url", f"{what} is not a valid URL", observed=value) from None
    if not parts.scheme or not parts.hostname:
        raise CliError("invalid_url", f"{what} must be an absolute URL", observed=value)
    scheme = parts.scheme.lower()
    if scheme == "http" and not is_loopback(parts.hostname):
        raise CliError(
            "insecure_url",
            f"{what} must use HTTPS (plain HTTP is accepted only for loopback test servers)",
            observed=value,
        )
    if scheme not in ("http", "https"):
        raise CliError("invalid_url", f"{what} must use HTTPS", observed=value)
    if parts.username is not None or parts.password is not None:
        raise CliError("invalid_url", f"{what} must not contain credentials", observed=value)
    return parts


def normalize_origin(value: str, *, what: str = "index origin") -> str:
    """Validate an HTTPS origin and return ``scheme://host[:port]``."""
    if "://" not in value:
        value = "https://" + value
    parts = _split(value, what)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise CliError(
            "invalid_origin",
            f"{what} must be an origin without path, query, or fragment",
            observed=value,
        )
    assert parts.hostname is not None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme.lower()}://{host}{port}"


def normalize_url(value: str, *, what: str = "URL") -> str:
    """Validate an absolute HTTPS URL (fragment not allowed)."""
    parts = _split(value, what)
    if parts.fragment:
        raise CliError("invalid_url", f"{what} must not contain a fragment", observed=value)
    return value


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return Path(a).absolute() == Path(b).absolute()
