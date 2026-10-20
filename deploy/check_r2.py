"""Probe real storage before enabling hosted admission. No credentials enter the report.

Uses unique _sci-probe/ prefixes outside the protected publications/ prefix.
Requires SCI_ALLOW_STORAGE_PROBE=true. Do not point this at unrelated buckets.
"""

import dataclasses
import json
import os
import uuid

import httpx

from server.settings import Settings
from server.storage import Storage


def probe(storage):
    prefix = "_sci-probe/" + str(uuid.uuid4())
    source = prefix + "/source"
    destination = prefix + "/copy"
    results = {}
    try:
        signed = storage.upload_url(source, "README.md", 5, 90)
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            exact = client.put(signed["url"], content=b"12345", headers=signed["headers"])
            results["exact_length_status"] = exact.status_code
            repeated = client.put(signed["url"], content=b"12345", headers=signed["headers"])
            results["repeated_put_status"] = repeated.status_code
            changed = client.put(signed["url"], content=b"123456",
                                 headers={**signed["headers"], "Content-Length": "6"})
            results["changed_length_status"] = changed.status_code
            omitted = dict(signed["headers"])
            omitted.pop("Content-Length", None)
            without = client.put(signed["url"], content=iter([b"123456"]), headers=omitted)
            results["omitted_length_status"] = without.status_code
            changed_type = client.put(signed["url"], content=b"12345",
                                      headers={**signed["headers"], "Content-Type": "text/html"})
            results["changed_type_status"] = changed_type.status_code
        original = storage.put(storage.temporary, source, b"12345", "README.md")
        try:
            storage.copy_verified(source, destination, '"definitely-wrong"', "README.md")
            results["conditional_copy_rejects_changed_source"] = False
        except Exception as exc:
            from server.storage import ChangedSource
            if isinstance(exc, ChangedSource):
                results["conditional_copy_rejects_changed_source"] = True
            else:
                raise
        storage.copy_verified(source, destination, original["ETag"], "README.md")
        data, _ = storage.read(storage.archive, destination, 5)
        results["conditional_copy_exact_bytes"] = data == b"12345"
        results["length_enforced"] = (
            exact.is_success and repeated.is_success and changed.status_code >= 400
            and without.status_code >= 400 and changed_type.status_code >= 400
        )
        return results
    finally:
        storage.client.delete_object(Bucket=storage.temporary, Key=source)
        storage.client.delete_object(Bucket=storage.archive, Key=destination)


def main():
    if os.getenv("SCI_ALLOW_STORAGE_PROBE") != "true":
        raise SystemExit("Set SCI_ALLOW_STORAGE_PROBE=true to authorize probes in the configured SCI buckets.")
    settings = dataclasses.replace(Settings.from_env(), bind_upload_size=True)
    result = probe(Storage(settings))
    print(json.dumps(result, indent=2))
    if not result["conditional_copy_rejects_changed_source"] or not result["conditional_copy_exact_bytes"]:
        raise SystemExit("Conditional copy requirements failed; do not enable publishing.")
    if not result["length_enforced"]:
        raise SystemExit("A hard upload cap was not established. Resolve the upload-handler or limited-pilot choice.")


if __name__ == "__main__":
    main()
