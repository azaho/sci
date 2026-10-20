"""Explicit hosted release steps. Called only by workflow_dispatch."""

import argparse
import os
import time

import httpx

from sci.client import append_url, discover


def github_headers():
    return {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json"}


def check_review():
    repository = os.environ["GITHUB_REPOSITORY"]
    response = httpx.get(
        f"https://api.github.com/repos/{repository}/environments/production-infrastructure",
        headers=github_headers(), timeout=30,
    )
    response.raise_for_status()
    rules = response.json().get("protection_rules", [])
    if not any(r.get("type") == "required_reviewers" and r.get("reviewers") for r in rules):
        raise SystemExit("Set required reviewers on the production-infrastructure environment before applying a plan.")
    print("Infrastructure apply has a required environment review.")


def migrations():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from server.migrate import migrate

    # ADMIN_DATABASE_URL is optional for operators. Hosted jobs construct the URL
    # inside Python; passwords never appear in shell arguments or logs.
    url = os.getenv("ADMIN_DATABASE_URL")
    if not url:
        url = make_conninfo(
            host=os.environ["SUPABASE_DB_HOST"],
            port=os.getenv("SUPABASE_DB_PORT", "5432"),
            user=os.getenv("SUPABASE_DB_USER", "postgres"),
            password=os.environ["SUPABASE_DB_PASSWORD"],
            dbname="postgres", sslmode="require",
        )
    migrate(url)
    runtime = os.getenv("PUBLISHER_DATABASE_URL")
    if runtime:
        params = conninfo_to_dict(runtime)
        if params.get("user", "").split(".")[0] != "sci_publisher" or not params.get("password"):
            raise SystemExit("PUBLISHER_DATABASE_URL must use the dedicated sci_publisher role.")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("ALTER ROLE sci_publisher PASSWORD {}").format(sql.Literal(params["password"])))
    print("Migrations are current. Existing publication and whitelist data remain in place.")


def publisher_release():
    app = os.environ["PUBLISHER_APP_ID"]
    expected_commit = os.environ["GITHUB_SHA"]
    repository = os.environ["GITHUB_REPOSITORY"]
    head = httpx.get(f"https://api.github.com/repos/{repository}/commits/main",
                     headers=github_headers(), timeout=30)
    head.raise_for_status()
    if head.json()["sha"] != expected_commit:
        raise SystemExit("main changed after this workflow started. Start a new publisher release.")
    headers = {"Authorization": "Bearer " + os.environ["DIGITALOCEAN_TOKEN"]}
    base = f"https://api.digitalocean.com/v2/apps/{app}"
    with httpx.Client(timeout=60, headers=headers) as client:
        response = client.post(base + "/deployments", json={"force_build": True})
        response.raise_for_status()
        deployment = response.json()["deployment"]["id"]
        for _ in range(120):
            response = client.get(base + "/deployments/" + deployment)
            response.raise_for_status()
            data = response.json()["deployment"]
            phase = data["phase"]
            if phase == "ACTIVE":
                commits = {s.get("source_commit_hash") for s in data.get("services", [])}
                if expected_commit not in commits:
                    raise SystemExit("Deployment revision differs from this workflow; inspect the app before release.")
                break
            if phase in {"ERROR", "CANCELED", "SUPERSEDED"}:
                raise SystemExit(f"Publisher deployment did not finish: {phase}")
            time.sleep(10)
        else:
            raise SystemExit("Publisher deployment did not become active within 20 minutes.")
    ready = httpx.get(append_url(os.environ["PUBLISHING_API_URL"], "health"), timeout=30)
    ready.raise_for_status()
    if ready.json().get("status") != "ready":
        raise SystemExit("Publisher readiness failed")
    print("Publisher deployment is active and ready.")


def verify_site():
    instance = os.environ["INSTANCE_URL"]
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        descriptor = discover(client, instance)
        for relative in ("", "publication/", "style.css", "app.js"):
            response = client.get(append_url(instance, relative))
            response.raise_for_status()
        headers = {"Accept-Profile": "catalog_v1"}
        if descriptor.get("supabase_publishable_key"):
            headers["apikey"] = descriptor["supabase_publishable_key"]
        response = client.get(append_url(descriptor["catalog_url"], "publications"),
                              params={"select": "uuid", "limit": 1}, headers=headers)
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("application/json"):
            raise SystemExit("Catalog is not served as JSON")
    print("Static pages, descriptor, and anonymous catalog read are available.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check-review", "migrations", "publisher", "verify-site"])
    args = parser.parse_args()
    {"check-review": check_review, "migrations": migrations, "publisher": publisher_release,
     "verify-site": verify_site}[args.operation]()


if __name__ == "__main__":
    main()
