"""Clear a failed-create taint only for a verified, active publisher app.

Run through the production-protected publisher-recover workflow. Never export
state or log API response bodies, which may contain runtime credentials.
"""

import json
import os
import subprocess
import urllib.request
from urllib.parse import urlsplit

ADDRESS = "digitalocean_app.publisher[0]"


def active_publisher(apps, name):
    matches = [app for app in apps if app.get("spec", {}).get("name") == name]
    if len(matches) != 1:
        raise ValueError("Expected exactly one app with the configured publisher name.")
    app = matches[0]
    if app.get("active_deployment", {}).get("phase") != "ACTIVE":
        raise ValueError("Publisher has no ACTIVE deployment.")
    if app.get("in_progress_deployment") or app.get("pending_deployment"):
        raise ValueError("Publisher still has an in-progress or pending deployment.")
    url = app.get("live_url", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".ondigitalocean.app")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Expected a public HTTPS App Platform URL.")
    if not app.get("id"):
        raise ValueError("Publisher app ID is missing.")
    return app["id"], url.rstrip("/")


def main():
    request = urllib.request.Request(
        "https://api.digitalocean.com/v2/apps?per_page=200",
        headers={"Authorization": f"Bearer {os.environ['DIGITALOCEAN_TOKEN']}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if data.get("links", {}).get("pages", {}).get("next"):
        raise ValueError("App list is paginated; refusing an incomplete identity check.")
    app_id, url = active_publisher(data.get("apps", []), os.environ["APP_NAME"])
    # State list returns resource addresses only, never attribute/secret values.
    addresses = subprocess.check_output(
        ["terraform", "-chdir=deploy", "state", "list", f"-id={app_id}"],
        text=True,
    ).splitlines()
    if addresses != [ADDRESS]:
        raise ValueError("The active app does not match the publisher resource in state.")
    with urllib.request.urlopen(url + "/health", timeout=30) as response:
        health = json.load(response)
    if health.get("database") != "ok" or health.get("status") not in {"ok", "degraded"}:
        raise ValueError("Publisher database health check failed.")
    buckets = health.get("storage", {})
    if not buckets or not isinstance(buckets, dict) or any(v != "ok" for v in buckets.values()):
        print("::warning::Storage is not healthy; recovery does not validate publishing.")
    print(f"Verified ACTIVE app {app_id} matches {ADDRESS}; clearing its taint flag.")
    subprocess.run(
        ["terraform", "-chdir=deploy", "untaint", "-lock-timeout=10m",
         "-allow-missing", ADDRESS],
        check=True,
    )
    print("Run infrastructure-plan next; review it before applying URL changes.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # API bodies and state contents must never be printed on failure.
        print(f"::error::Publisher recovery stopped ({type(exc).__name__}). "
              "No further recovery actions will run.")
        raise SystemExit(1) from None
