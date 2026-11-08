from __future__ import annotations
import json
from typing import Any


class CommandError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
