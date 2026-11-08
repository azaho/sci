"""Publishing destinations: local folders, ``@local``, GitHub, and Hugging Face datasets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import CliError, Exit

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_GH_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_SUFFIX = r"(?:\.git)?/?"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "github",
        re.compile(rf"https://github\.com/(?P<owner>{_GH_OWNER})/(?P<repo>{_NAME}?){_SUFFIX}"),
    ),
    ("github", re.compile(rf"git@github\.com:(?P<owner>{_GH_OWNER})/(?P<repo>{_NAME}?){_SUFFIX}")),
    (
        "github",
        re.compile(rf"ssh://git@github\.com/(?P<owner>{_GH_OWNER})/(?P<repo>{_NAME}?){_SUFFIX}"),
    ),
    (
        "huggingface",
        re.compile(
            rf"https://huggingface\.co/datasets/(?P<owner>{_NAME})/(?P<repo>{_NAME}?){_SUFFIX}"
        ),
    ),
    (
        "huggingface",
        re.compile(
            rf"git@(?:hf\.co|huggingface\.co):datasets/(?P<owner>{_NAME})/(?P<repo>{_NAME}?)"
            rf"{_SUFFIX}"
        ),
    ),
    (
        "huggingface",
        re.compile(
            rf"ssh://git@(?:hf\.co|huggingface\.co)/datasets/(?P<owner>{_NAME})/"
            rf"(?P<repo>{_NAME}?){_SUFFIX}"
        ),
    ),
]

_REMOTE_HINT = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://|^[^/\s]+@[^/\s]+:")


@dataclass(frozen=True, slots=True)
class Destination:
    kind: str
    """``folder``, ``github``, or ``huggingface``."""
    key: str
    display: str
    path: Path | None = None
    owner: str | None = None
    repo: str | None = None

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_remote(value: str) -> Destination | None:
    """Recognize a GitHub or Hugging Face repository address; None if not remote-looking.

    Raises:
        CliError: the value looks like a remote address but is unsupported.
    """
    if not _REMOTE_HINT.search(value):
        return None
    if "?" in value or "#" in value:
        raise CliError(
            "unsupported_destination",
            f"{value}: repository addresses must not contain a query or fragment",
            Exit.USAGE,
        )
    for kind, pattern in _PATTERNS:
        match = pattern.fullmatch(value)
        if match is None:
            continue
        owner, repo = match.group("owner"), match.group("repo")
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        if not repo or repo in (".", "..") or repo.endswith("."):
            break
        if kind == "github":
            return Destination(
                "github",
                f"github:{owner.lower()}/{repo.lower()}",
                f"https://github.com/{owner}/{repo}",
                owner=owner,
                repo=repo,
            )
        return Destination(
            "huggingface",
            f"huggingface:{owner.lower()}/{repo.lower()}",
            f"https://huggingface.co/datasets/{owner}/{repo}",
            owner=owner,
            repo=repo,
        )
    raise CliError(
        "unsupported_destination",
        f"{value} is not a supported destination; use a folder, @local, "
        "https://github.com/OWNER/REPO, or https://huggingface.co/datasets/OWNER/REPO "
        "(file and branch URLs are not accepted)",
        Exit.USAGE,
    )


def resolve_destinations(
    values: list[str], known_folders: list[str], cwd: Path
) -> list[Destination]:
    destinations: list[Destination] = []
    seen: set[str] = set()

    def add(destination: Destination) -> None:
        if destination.key not in seen:
            seen.add(destination.key)
            destinations.append(destination)

    for value in values:
        if value == "@local":
            for folder in known_folders:
                path = Path(folder)
                add(Destination("folder", f"folder:{path}", str(path), path=path))
            continue
        if value.startswith("@"):
            raise CliError("unsupported_destination", f"unknown destination {value!r}", Exit.USAGE)
        remote = parse_remote(value)
        if remote is not None:
            add(remote)
            continue
        path = Path(value).expanduser()
        path = path if path.is_absolute() else cwd / path
        canonical = path.resolve() if path.exists() else path.absolute()
        add(Destination("folder", f"folder:{canonical}", str(canonical), path=canonical))
    if not destinations:
        raise CliError(
            "no_destinations", "no destinations selected (no known folders?)", Exit.USAGE
        )
    return destinations
