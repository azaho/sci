"""Sharing the local search cache between concurrent processes."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from clitestkit import Cli, cli, write_draft  # noqa: F401 - cli is a fixture

from pysci._cli.storage import catalog as catalog_module
from pysci._cli.storage.catalog import Catalog
from pysci.bundle import verify_zip


def signed(cli: Cli, campaign: Path, count: int, start: int = 0) -> None:
    if not start:
        assert cli("keygen", "--json").code == 0
    for number in range(start, start + count):
        draft = write_draft(cli.cwd / f"draft{number}", title=f"Spike study {number}")
        run = cli("sign", str(draft), "--no-timestamp", "--output", str(campaign), "--json")
        assert run.code == 0, run.stderr


def rollback_mode(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=DELETE").fetchone()
    db.close()


def test_catalog_uses_wal_and_a_settled_search_takes_no_write_lock(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    signed(cli, campaign, 2)
    path = cli.home / "catalog.sqlite"
    assert cli("search", "spike", "--in", str(campaign), "--json").code == 0
    db = sqlite3.connect(path, isolation_level=None)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.execute("BEGIN IMMEDIATE")  # another process is writing
    monkeypatch.setattr(catalog_module, "BUSY_TIMEOUT_SECONDS", 0.05)
    run = cli("search", "spike", "--in", str(campaign), "--json")
    db.execute("ROLLBACK")
    db.close()
    assert run.code == 0
    group = run.result["groups"][0]
    assert group["cache"] == {"verified": 0, "reused": 2, "rebuilt": False}
    assert len(group["results"]) == 2


def test_busy_cache_is_kept_and_reported_as_retryable(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    signed(cli, campaign, 2)
    path = cli.home / "catalog.sqlite"
    assert cli("search", "spike", "--in", str(campaign), "--json").code == 0
    rollback_mode(path)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(catalog_module, "BUSY_TIMEOUT_SECONDS", 0.05)
    run = cli("search", "spike", "--in", str(campaign), "--json")
    db.execute("ROLLBACK")
    assert db.execute("SELECT count(*) FROM publications").fetchone()[0] == 2
    db.close()
    assert run.code == 5
    error = run.json["errors"][0]
    assert error["code"] == "catalog_unavailable" and error["retryable"] is True


def test_corruption_found_after_opening_rebuilds_once(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    signed(cli, campaign, 2)
    assert cli("search", "spike", "--in", str(campaign), "--json").code == 0
    original = Catalog.search
    calls = {"count": 0}

    def fail_first(self: Catalog, *args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            error = sqlite3.DatabaseError("malformed")
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Catalog, "search", fail_first)
    run = cli("search", "spike", "--in", str(campaign), "--json")
    assert run.code == 0
    group = run.result["groups"][0]
    assert group["cache"]["rebuilt"] is True and len(group["results"]) == 2


def test_scan_reuses_a_file_another_process_indexed_after_it_started(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    signed(cli, campaign, 3)
    path = tmp_path / "shared.sqlite"
    original = verify_zip
    first = {"pending": True}

    def verify(zip_path: Path) -> Any:
        if first["pending"]:
            first["pending"] = False
            other = Catalog(path)
            other.scan([campaign])  # finishes every file while this one is in flight
            other.close()
        return original(zip_path)

    monkeypatch.setattr(catalog_module, "verify_zip", verify)
    catalog = Catalog(path)
    report = catalog.scan([campaign])
    catalog.close()
    assert (report.verified, report.reused) == (1, 2)


def test_scan_keeps_a_file_added_after_its_listing(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    signed(cli, campaign, 2)
    path = tmp_path / "shared.sqlite"
    first = Catalog(path)
    first.scan([campaign])
    first.close()
    real_scandir = os.scandir
    late = sorted(campaign.glob("*.zip"))[0]

    def listing_without_late(folder: Any) -> Any:
        return [entry for entry in real_scandir(folder) if entry.path != str(late)]

    monkeypatch.setattr(os, "scandir", listing_without_late)
    catalog = Catalog(path)
    catalog.scan([campaign])
    rows = catalog.db.execute("SELECT count(*) FROM files WHERE path=?", (str(late),)).fetchone()
    catalog.close()
    assert rows[0] == 1


def test_many_processes_share_one_cache_while_bundles_arrive(cli: Cli, tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    signed(cli, campaign, 12)
    workers = 12
    env = {**os.environ, "SCI_HOME": str(cli.home)}
    command = [sys.executable, "-m", "pysci", "search", "spike", "--limit", "100"]
    command += ["--in", str(campaign), "--json"]
    processes = [
        subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(workers)
    ]
    signed(cli, campaign, 4, start=12)  # new bundles appear during the searches
    replies = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err.decode()
        replies.append(json.loads(out))
    for reply in replies:
        group = reply["result"]["groups"][0]
        assert len(group["results"]) >= 12 and not group["invalid_candidates"]
        assert group["cache"]["rebuilt"] is False

    run = cli("search", "spike", "--limit", "100", "--in", str(campaign), "--json")
    group = run.result["groups"][0]
    assert len(group["results"]) == 16
    settled = cli("search", "spike", "--limit", "100", "--in", str(campaign), "--json")
    assert settled.result["groups"][0]["cache"] == {"verified": 0, "reused": 16, "rebuilt": False}
