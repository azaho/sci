"""Run a real HTTP publication round trip against a fresh local installation."""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import psycopg

from examples.create_demo import create
from sci.client import discover, pull, push
from sci.publication import verify_publication


def enable_demo(key):
    url = os.getenv("SCI_DEMO_DATABASE_URL")
    if url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT sci_private.enable_key(%s)", (key,))
    else:
        # Public key is generated locally and has the canonical base64url alphabet.
        subprocess.run(
            ["docker", "compose", "-f", "deploy/compose.yaml", "exec", "-T",
             "database", "psql", "-U", "postgres", "-d", "sci", "-v", "ON_ERROR_STOP=1"],
            input=f"SELECT sci_private.enable_key('{key}');\n", text=True, check=True,
            capture_output=True,
        )


def run(instance, *, existing=False):
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        for _ in range(60):
            try:
                descriptor = discover(client, instance)
                ready = client.get(descriptor["publishing_api_url"].rstrip("/") + "/health")
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Local services did not become ready")
        if existing:
            index = json.loads(Path(".local/demo/index.json").read_text())
        else:
            index, key = create(Path(".local/demo"))
            enable_demo(key)
            for item in index:
                outcome = push(item["folder"], instance)
                assert outcome["status"] == "accepted"
        destination = Path(".local/smoke-pull")
        if destination.exists():
            shutil.rmtree(destination)
        pull(index[0]["uuid"], instance, destination)
        original = verify_publication(index[0]["folder"])
        assert verify_publication(destination) == original
        for name in original.descriptors:
            assert (destination / name).read_bytes() == (Path(index[0]["folder"]) / name).read_bytes()
        headers = {"Accept-Profile": "catalog_v1"}
        catalog = descriptor["catalog_url"].rstrip("/")
        response = client.get(catalog + "/publications", params={"order": "accepted_at.desc,uuid.desc", "limit": 100}, headers=headers)
        response.raise_for_status()
        assert len(response.json()) == 3
        uuid = index[0]["uuid"]
        embedded = client.get(catalog + "/citations", params={
            "target_key": "eq.sci:" + uuid, "select": "source_uuid,publications(uuid,title)", "limit": 100,
        }, headers=headers)
        embedded.raise_for_status()
        assert len(embedded.json()) == 2 and all(r["publications"]["title"] for r in embedded.json())
        for params, expected in [
            ({"search_document": "wfts(english).median"}, 1),
            ({"categories": "cs.{cs.LG}"}, 2),
            ({"author_fingerprint": "eq." + original.fingerprint}, 3),
        ]:
            response = client.get(catalog + "/publications", params=params, headers=headers)
            response.raise_for_status()
            assert len(response.json()) == expected
        for resource in ["identities", "uploads", "rpc/enable_key", "rpc/list_keys"]:
            assert client.get(catalog + "/" + resource, headers=headers).status_code >= 400
        assert client.get(catalog + "/publications", headers={"Accept-Profile": "sci_private"}).status_code >= 400
        for method in ["POST", "PATCH", "DELETE"]:
            assert client.request(
                method, catalog + "/publications",
                headers={**headers, "Content-Profile": "catalog_v1"},
                json={"title": "Unauthorized write"} if method != "DELETE" else None,
            ).status_code >= 400
        assert client.get(catalog + "/publications", params={"select": "count()"}, headers=headers).status_code >= 400
        archive = descriptor["archive_url"].rstrip("/") + "/" + uuid
        for filename, content_type in [
            ("README.md", "text/plain"), ("LICENSE", "text/plain"),
            ("manifest.json", "application/json"), ("manifest.json.sig", "application/octet-stream"),
            ("headline_figure.png", "image/png"), ("files.zip", "application/zip"),
        ]:
            response = client.get(archive + "/" + filename, headers={"Origin": "https://reader.example"})
            response.raise_for_status()
            assert response.headers["content-type"].startswith(content_type)
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["access-control-allow-origin"] == "*"
            if filename == "files.zip":
                assert response.headers["content-disposition"].startswith("attachment")
        print("PASS: real HTTP sign/push/browse/pull, exact bytes, catalog filters, relationships, grants, and delivery.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="http://localhost:8080")
    parser.add_argument("--existing", action="store_true")
    args = parser.parse_args()
    run(args.instance, existing=args.existing)


if __name__ == "__main__":
    main()
