"""Provider credentials: GitHub (``gh auth token``, ``GH_TOKEN``) and Hugging Face tokens."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..errors import CliError, Exit

Runner = Callable[[Sequence[str], float], "str | None"]


def github_token(env: Mapping[str, str], runner: Runner) -> str:
    """``GH_TOKEN``, ``GITHUB_TOKEN``, or the token from ``gh auth token``."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = env.get(name, "").strip()
        if value:
            return value
    token = runner(["gh", "auth", "token"], 15.0)
    if token:
        return token
    raise CliError(
        "missing_credentials",
        "GitHub credentials not found: run `gh auth login` or set GH_TOKEN",
        Exit.USAGE,
    )


def huggingface_token(env: Mapping[str, str]) -> str:
    """``HF_TOKEN``, or the token stored by ``hf auth login``."""
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = env.get(name, "").strip()
        if value:
            return value
    candidates: list[Path] = []
    if env.get("HF_TOKEN_PATH"):
        candidates.append(Path(env["HF_TOKEN_PATH"]).expanduser())
    if env.get("HF_HOME"):
        candidates.append(Path(env["HF_HOME"]).expanduser() / "token")
    home = env.get("HOME")
    if home:
        candidates.append(Path(home) / ".cache" / "huggingface" / "token")
    for path in candidates:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    raise CliError(
        "missing_credentials",
        "Hugging Face credentials not found: run `hf auth login` or set HF_TOKEN",
        Exit.USAGE,
    )
