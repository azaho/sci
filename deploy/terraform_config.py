"""Map non-secret GitHub variables to Terraform inputs without shell interpolation."""

import json
import os
from pathlib import Path

REQUIRED = {
    "CLOUDFLARE_ACCOUNT_ID": "cloudflare_account_id",
    "ARCHIVE_ZONE_ID": "archive_zone_id",
    "ARCHIVE_HOSTNAME": "archive_hostname",
    "TEMPORARY_BUCKET": "temporary_bucket_name",
    "ARCHIVE_BUCKET": "archive_bucket_name",
    "SUPABASE_ORGANIZATION_ID": "supabase_organization_id",
    "SUPABASE_PROJECT_REF": "supabase_project_ref",
    "SUPABASE_PROJECT_NAME": "supabase_project_name",
    "SUPABASE_REGION": "supabase_region",
    "INSTANCE_URL": "instance_url",
}
OPTIONAL = {
    "PUBLISHER_REGION": "publisher_region", "PUBLISHER_SIZE": "publisher_size",
    "R2_SIZE_ENFORCEMENT": "r2_size_enforcement",
}
BOOLEANS = {
    "DEPLOY_PUBLISHER": "deploy_publisher", "SCI_WRITES_ENABLED": "writes_enabled",
    "MANAGE_ARCHIVE_HEADERS": "manage_archive_headers",
}


def main():
    config = json.loads(os.environ["DEPLOY_CONFIG"])
    result = {}
    for source, target in REQUIRED.items():
        if not config.get(source):
            raise SystemExit(f"Missing repository variable: {source}")
        result[target] = config[source]
    for source, target in OPTIONAL.items():
        if config.get(source):
            result[target] = config[source]
    for source, target in BOOLEANS.items():
        if config.get(source):
            if config[source] not in {"true", "false"}:
                raise SystemExit(f"{source} must be true or false")
            result[target] = config[source] == "true"
    path = Path(".local/infra.tfvars.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
