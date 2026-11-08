from __future__ import annotations

import pytest

from pysci.citations import TargetKind, citation_target_key, is_http_uri, parse_citation_target
from pysci.errors import IssueCollector
from pysci.ids import parse_publication_id, publication_id, sci_citation_target
from pysci.paths import check_path_conflicts, check_paths, is_valid_path

DIGEST = "c016f424ed267b2edc1784101e4bd15b9950957b61f83af22efdaafc450f41f9"


@pytest.mark.parametrize(
    "path", ["sci.json", "files/a.txt", "files/A-b_c.d/e", "files/" + "a" * 249, "files/con_x"]
)
def test_valid_paths(path: str) -> None:
    assert is_valid_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/tmp/a",
        "files//a",
        "files/a/",
        "files/./a",
        "files/../a",
        "files/a.",
        "files/a b",
        "files/a\\b",
        "files/café",
        "files/CON",
        "files/cOm1.txt",
        "files/LPT9/a",
        "files/nul.tar.gz",
        "files/" + "a" * 250,
    ],
)
def test_invalid_paths(path: str) -> None:
    assert not is_valid_path(path)


@pytest.mark.parametrize(
    ("paths", "code"),
    [
        (["files/a", "files/A"], "path_conflict"),
        (["files/a", "files/A/b.txt"], "path_file_directory_conflict"),
        (["files/Data/a.csv", "files/data/b.csv"], "path_directory_case"),
    ],
)
def test_conflicts(paths: list[str], code: str) -> None:
    collector = IssueCollector()
    check_path_conflicts(paths, collector=collector)
    assert code in {issue.code for issue in collector.issues}


def test_check_paths_reports_each_problem() -> None:
    collector = IssueCollector()
    check_paths(["files/a.", "files/CON", "files/ok"], collector=collector, field_prefix="/files")
    assert [i.field for i in collector.issues] == ["/files/files~1a.", "/files/files~1CON"]


def test_publication_ids() -> None:
    assert parse_publication_id("sha256:" + DIGEST) == DIGEST
    assert parse_publication_id("sci:sha256:" + DIGEST) == DIGEST
    assert parse_publication_id(DIGEST, allow_bare=True) == DIGEST
    for bad in (DIGEST, "sha256:" + DIGEST.upper(), "sha256:" + DIGEST[:-1], "sha1:" + DIGEST):
        with pytest.raises(ValueError):
            parse_publication_id(bad)
    assert publication_id(DIGEST) == "sha256:" + DIGEST
    assert sci_citation_target("sha256:" + DIGEST) == "sci:sha256:" + DIGEST


@pytest.mark.parametrize(
    ("target", "kind"),
    [
        ("sci:sha256:" + DIGEST, TargetKind.SCI),
        ("doi:10.1000/182", TargetKind.DOI),
        ("doi:10.48550/arXiv.1706.03762", TargetKind.DOI),
        ("https://example.org/paper?q=1#results", TargetKind.URL),
        ("http://example.org", TargetKind.URL),
        ("HTTPS://[2001:db8::1]:8443/x", TargetKind.URL),
    ],
)
def test_valid_targets(target: str, kind: TargetKind) -> None:
    parsed = parse_citation_target(target)
    assert parsed is not None and parsed.kind is kind


@pytest.mark.parametrize(
    "target",
    [
        "sci:" + DIGEST,
        "sci:sha256:" + DIGEST.upper(),
        "doi:11.1000/x",
        "doi:10.1000/",
        "doi:10.1000/a b",
        "doi:10.1000/a%zz",
        "ftp://example.org/a",
        "/relative/path",
        "https://",
        "https://user@example.org/",
        "https://example.org/a b",
        "https://exämple.org/",
    ],
)
def test_invalid_targets(target: str) -> None:
    assert parse_citation_target(target) is None


def test_target_keys_group_equivalent_spellings() -> None:
    assert citation_target_key("doi:10.1000/ABC%2fdef") == citation_target_key(
        "doi:10.1000/abc/def"
    )
    assert citation_target_key("doi:10.1000/a%41") == "doi:10.1000/aa"
    assert citation_target_key("HTTP://Example.ORG:80") == "http://example.org/"
    assert citation_target_key("https://example.org:443/%7euser") == "https://example.org/~user"
    assert citation_target_key("https://example.org/a%2fb") == "https://example.org/a%2Fb"
    assert citation_target_key("https://example.org/#Frag") == "https://example.org/#Frag"
    assert is_http_uri("https://example.org/?q=a/b?c")
    with pytest.raises(ValueError):
        citation_target_key("mailto:a@example.org")
