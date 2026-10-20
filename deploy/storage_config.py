"""Write local storage credentials into a private Compose volume."""

import json
import os
from pathlib import Path


def main():
    path = Path("/storage-config/iam.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"identities": [{
        "name": "sci-local",
        "credentials": [{
            "accessKey": os.environ["R2_ACCESS_KEY_ID"],
            "secretKey": os.environ["R2_SECRET_ACCESS_KEY"],
        }],
        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
    }]}
    path.write_text(json.dumps(value), "utf-8")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
