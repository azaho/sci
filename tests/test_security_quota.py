"""SCI-SEC-002: an attempt past its deadline must stay charged until expired.

A completion can check its pending attempt before the deadline and commit
after it. If preparation stopped counting that attempt at the deadline, a
second publication could be admitted into the same budget.
"""

from __future__ import annotations

import json
from pathlib import Path

from sci import publication as pub

from .conftest import start_stack, write_folder
from .test_server import submit_folder


def _charged_bytes(folder: Path) -> int:
    manifest = (folder / "manifest.json").read_bytes()
    files = json.loads(manifest)["files"]
    return sum(f["bytes"] for f in files.values()) + len(manifest) + 64


def test_pending_reservation_stays_charged_until_expired(database, rustfs, tmp_path, key):
    a = write_folder(tmp_path / "a", sci={"title": "A", "abstract": "A."})
    b = write_folder(tmp_path / "b", sci={"title": "B", "abstract": "B."})
    pub.sign_folder(a, key)
    pub.sign_folder(b, key)
    budget = _charged_bytes(a) + _charged_bytes(b) - 1  # room for one, not both

    s, stop = start_stack(database, rustfs, tmp_path / "stack", max_storage_bytes=budget)
    try:
        s.enable_key(key)
        status, ra = submit_folder(s, a)
        assert status == 202

        # A's deadline has passed, but A is not expired yet: a completion that
        # checked A in time could still commit it. B must not take its place.
        s.admin("update sci.uploads set deadline = now() - interval '1 second' where upload_id = %s", (ra["upload_id"],))
        status, rb = submit_folder(s, b)
        assert status == 507 and rb["error"]["code"] == "storage_full", rb

        # Once maintenance marks A expired, A can never commit and the budget frees up.
        s.admin("update sci.uploads set deadline = now() - interval '10 minutes' where upload_id = %s", (ra["upload_id"],))
        assert s.publisher.run_maintenance_once()["expired"] == 1
        status, rb = submit_folder(s, b)
        assert status == 202, rb
    finally:
        stop()
