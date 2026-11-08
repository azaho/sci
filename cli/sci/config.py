from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from pysci.bundle import canonical_json
from pysci.validation import Invalid, strict_json
from sci.storage.files import atomic


def home() -> Path:
    return Path(os.environ.get("SCI_HOME", "~/.sci")).expanduser()


def read() -> dict[str, Any]:
    p = home() / "config.json"
    if not p.exists():
        return {"folders": [], "tsa": "https://freetsa.org/tsr"}
    obj = strict_json(p.read_bytes())
    if not isinstance(obj, dict) or not isinstance(obj.get("folders", []), list):
        raise Invalid("malformed config.json")
    if not all(isinstance(x, str) for x in obj.get("folders", [])):
        raise Invalid("malformed folder settings")
    return dict(obj)


def save(config: dict[str, Any]) -> None:
    atomic(home() / "config.json", canonical_json(config), replace=True, mode=0o600)


def remember(folder: Path) -> None:
    c = read()
    c["folders"] = sorted(set(c.get("folders", [])) | {str(folder.resolve())})
    save(c)
