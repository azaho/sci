"""Run only synthetic benchmarks; create and remove uniquely named resources.

Secrets stay in process memory. No production database or existing functions are changed.
"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import time
import uuid

import requests


class Bench:
    def __init__(self, output):
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.ref = os.environ["SUPABASE_PROJECT_REF"]
        assert re.fullmatch(r"[a-z]{20}", self.ref)
        self.token = os.environ["SUPABASE_ACCESS_TOKEN"]
        self.management = f"https://api.supabase.com/v1/projects/{self.ref}"
        self.base = f"https://{self.ref}.supabase.co"
        self.name = "sci-bench-" + uuid.uuid4().hex[:16]
        self.key = None
        self.bucket_created = False
        self.function_created = False
        self.objects = []
        self.started = datetime.now(timezone.utc)
        self.results = []

    def call(self, method, url, *, management=False, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token if management else self.key}"
        if not management:
            headers["apikey"] = self.key
        r = requests.request(method, url, headers=headers, timeout=150, **kwargs)
        if not r.ok:
            # Never include arbitrary API response bodies or credentials in logs.
            raise RuntimeError(f"{method} {url.split('?')[0]} returned HTTP {r.status_code}")
        return r

    def prepare(self, fixtures):
        keys = self.call("GET", self.management + "/api-keys?reveal=true", management=True).json()
        self.key = next((k["api_key"] for k in keys if k.get("name") == "service_role"), None)
        if not self.key:
            raise RuntimeError("This benchmark requires an existing enabled legacy service_role key; no keys were changed.")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::add-mask::{self.key}", flush=True)
        print(f"Temporary resource name: {self.name}", flush=True)
        self.call("POST", self.base + "/storage/v1/bucket", json={
            "id": self.name, "name": self.name, "public": False, "file_size_limit": 8_388_608,
        })
        self.bucket_created = True
        cases = json.loads((fixtures / "cases.json").read_text())
        for case in cases:
            case["parts"] = []
            with (fixtures / f"{case['name']}.zip").open("rb") as stream:
                i = 0
                while block := stream.read(8_000_000):
                    part = f"{case['name']}/{i}.bin"
                    self.call("POST", self.base + f"/storage/v1/object/{self.name}/{part}",
                              headers={"Content-Type": "application/octet-stream"}, data=block)
                    self.objects.append(part)
                    case["parts"].append(part)
                    i += 1
        source = Path(__file__).with_name("verify.ts").read_text()
        handler = Path(__file__).with_name("index.ts").read_text()
        handler = handler.replace('import { LIMITS, verifyZip } from "./verify.ts";\n', '')
        handler = handler.replace('/* FIXTURE_CONFIG */ { bucket: "", cases: [] as any[] }',
                                  json.dumps({"bucket": self.name, "cases": cases}))
        # The Management API bundles a single entrypoint and its pinned npm dependency.
        self.function_created = True  # Also attempt cleanup if deployment partially fails.
        deployment = self.call("POST", self.management + f"/functions/deploy?slug={self.name}",
            management=True, files={
                "metadata": (None, json.dumps({"name": self.name, "entrypoint_path": "index.ts", "verify_jwt": True}), "application/json"),
                "file": ("index.ts", source + "\n" + handler, "application/typescript"),
            }).json()
        self.function_id = deployment.get("id")
        self.output.joinpath("deployment.json").write_text(json.dumps({
            "name": self.name, "id": self.function_id, "version": deployment.get("version"),
            "started": self.started.isoformat(), "git_sha": os.environ.get("GITHUB_SHA"),
        }, indent=2) + "\n")
        return cases

    def run(self, cases, repeats):
        for case in cases:
            for repeat in range(repeats if case["expected_error"] is None else 1):
                start = time.monotonic()
                try:
                    response = requests.post(self.base + f"/functions/v1/{self.name}",
                        headers={"Authorization": f"Bearer {self.key}", "apikey": self.key},
                        json={"case": case["name"], "run": repeat}, timeout=145)
                    try:
                        result = response.json()
                    except ValueError:
                        result = {"error": "non_json_response"}
                    if not response.ok:
                        # Platform errors have only an allowlisted subset retained.
                        result = {"error": result.get("code", result.get("error", "platform_error"))}
                    result.update(http_status=response.status_code)
                except requests.RequestException as error:
                    result = {"http_status": None, "error": type(error).__name__}
                result.update(case=case["name"], repeat=repeat, client_wall_ms=(time.monotonic() - start) * 1000,
                              expected_error=case["expected_error"], payload_bytes=case["payload_bytes"],
                              file_count=case["files"], fixture_manifest_bytes=case["manifest_bytes"])
                result["passed"] = (result.get("http_status") == 200 and
                    ((case["expected_error"] is None and result.get("accepted") is True) or
                     (case["expected_error"] is not None and result.get("accepted") is False and
                      result.get("error") == case["expected_error"])))
                self.results.append(result)
                self.output.joinpath("results.json").write_text(json.dumps(self.results, indent=2) + "\n")
                print(json.dumps(result), flush=True)

    def logs(self):
        if not getattr(self, "function_id", None):
            return
        # Limit retrieval to this temporary function. Log access is optional.
        query = ("select timestamp, event_message, metadata from logs "
                 "where source = 'function_edge_logs' "
                 f"and position(toString(metadata), '{self.function_id}') > 0 "
                 "order by timestamp asc limit 1000")
        try:
            response = self.call("GET", self.management + "/analytics/endpoints/logs", management=True,
                params={"sql": query, "iso_timestamp_start": (self.started - timedelta(minutes=1)).isoformat(),
                        "iso_timestamp_end": datetime.now(timezone.utc).isoformat()}).json()
            self.output.joinpath("runtime-logs.json").write_text(json.dumps(response, indent=2) + "\n")
            print("Saved function-scoped runtime telemetry.", flush=True)
        except Exception as error:
            print(f"Runtime telemetry unavailable: {error}", flush=True)
            self.output.joinpath("runtime-logs-status.txt").write_text(str(error) + "\n")

    def cleanup(self):
        failures = []
        if self.function_created:
            try: self.call("DELETE", self.management + f"/functions/{self.name}", management=True)
            except Exception as e: failures.append(str(e))
        if self.bucket_created:
            try:
                if self.objects:
                    self.call("DELETE", self.base + f"/storage/v1/object/{self.name}", json={"prefixes": self.objects})
                self.call("DELETE", self.base + f"/storage/v1/bucket/{self.name}")
            except Exception as e: failures.append(str(e))
        self.output.joinpath("cleanup.json").write_text(json.dumps({"resource": self.name, "errors": failures}, indent=2) + "\n")
        print("Cleanup: " + ("complete" if not failures else "; ".join(failures)), flush=True)
        if failures:
            raise RuntimeError("Temporary benchmark resources need cleanup; see cleanup.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    assert 1 <= args.repeats <= 10
    bench = Bench(args.output)
    try:
        cases = bench.prepare(args.fixtures)
        bench.run(cases, args.repeats)
        time.sleep(15)  # Allow runtime events to reach log storage.
        bench.logs()
    finally:
        bench.cleanup()
    if any(not r["passed"] for r in bench.results):
        raise SystemExit("Some benchmark cases failed; see results.json (resource failures are results too).")
