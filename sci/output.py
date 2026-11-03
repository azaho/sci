"""CLI errors and result output."""

from __future__ import annotations

import json
import sys


class CliError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def progress(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def result(self, text: str, data: dict) -> None:
        if self.as_json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(text)
