"""SCI-SEC-001: a replayed upload URL must not leave orphaned temporary objects.

Presigned PUT URLs are bearer capabilities. Cleanup that runs while one is
still usable can be undone by replaying it, and the attempt is never revisited.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from pathlib import Path

from sci import publication as pub

from .conftest import write_folder
from .test_server import complete, http, submit_folder, upload_all


def _publish(stack, tmp_path, key):
    stack.enable_key(key)
    folder = write_folder(tmp_path / "r")
    pub.sign_folder(folder, key)
    status, r = submit_folder(stack, folder)
    assert status == 202
    upload_all(folder, r)
    assert complete(stack, r["uuid"], r["upload_id"], r["manifest_sha256"])[0] == 201
    return folder, r


def _temp_objects(stack, upload_id):
    page = stack.storage.s3.list_objects_v2(Bucket=stack.settings.temp_bucket, Prefix=f"{upload_id}/")
    return sorted(o["Key"] for o in page.get("Contents", []))


def test_presigned_urls_never_outlive_the_deadline(stack, tmp_path, key):
    stack.enable_key(key)
    folder = write_folder(tmp_path / "r")
    pub.sign_folder(folder, key)
    status, r = submit_folder(stack, folder)
    deadline = pub.parse_timestamp(r["deadline"])
    for name, spec in r["uploads"].items():
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(spec["url"]).query))
        signed = dt.datetime.strptime(q["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        assert signed + dt.timedelta(seconds=int(q["X-Amz-Expires"])) <= deadline + dt.timedelta(seconds=1), name


def test_cleanup_waits_until_upload_urls_are_quiescent(stack, tmp_path, key):
    folder, r = _publish(stack, tmp_path, key)
    upload_id = r["upload_id"]
    readme = r["uploads"]["README.md"]

    # Accepted, but upload URLs are still live: cleanup must not run yet.
    stats = stack.publisher.run_maintenance_once()
    assert stats["cleaned"] == 0
    assert _temp_objects(stack, upload_id)

    # A bearer replays the URL while it is still valid.
    status, _, _ = http("PUT", readme["url"], (Path(folder) / "README.md").read_bytes(), dict(readme["headers"]))
    assert status == 200

    # After the deadline plus the quiet period, one cleanup removes everything,
    # including replayed objects, and the attempt is marked clean.
    quiet = stack.settings.upload_quiescence_seconds
    stack.admin("update sci.uploads set deadline = now() - make_interval(secs => %s) where upload_id = %s", (quiet + 1, upload_id))
    stats = stack.publisher.run_maintenance_once()
    assert stats["cleaned"] == 1
    assert _temp_objects(stack, upload_id) == []
    assert stack.admin("select temp_cleaned_at is not null as done from sci.uploads where upload_id = %s", (upload_id,))[0]["done"]
