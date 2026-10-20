"""Use after stopping the publisher, catalog, and database in the system test."""

import json
import shutil
from pathlib import Path

from sci.client import pull
from sci.publication import verify_publication


def main():
    index = json.loads(Path(".local/demo/index.json").read_text())
    destination = Path(".local/offline-pull")
    if destination.exists():
        shutil.rmtree(destination)
    pull(index[0]["uuid"], "http://localhost:8080", destination)
    assert verify_publication(destination) == verify_publication(index[0]["folder"])
    print("PASS: complete verified pull with publisher, catalog, and database stopped.")


if __name__ == "__main__":
    main()
