from __future__ import annotations
from pysci import Bundle
import json
from typing import Any


class CommandError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def result(bundle: Bundle) -> dict[str, Any]:
    return {
        "id": "sha256:" + bundle.id,
        "verification": "complete",
        "baseline": bundle.baseline,
        "signer": bundle.signer,
        "associations": "not_checked",
        "revocation": "not_checked",
        "timestamps": "not_checked",
    }
