"""Prepare static output and its descriptor; never modify web/ source."""

import argparse
import json
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from sci.client import validate_descriptor


def build(output, descriptor, source=None):
    descriptor = validate_descriptor(descriptor)
    source = Path(source or Path(__file__).resolve().parents[1] / "web")
    output = Path(output)
    if output.resolve() == source.resolve() or source.resolve() in output.resolve().parents:
        raise ValueError("Use a separate served-output directory, not web/")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, output, dirs_exist_ok=True)
    (output / "sci-server.json").write_text(json.dumps(descriptor, indent=2) + "\n", "utf-8")
    for name in ("categories.json", "publication_LICENSE.txt"):
        (output / name).write_bytes(files("sci").joinpath(name).read_bytes())
    origins = set()
    for name in ("catalog_url", "archive_url"):
        parsed = urlsplit(descriptor[name])
        origins.add(f"{parsed.scheme}://{parsed.netloc}")
    archive = urlsplit(descriptor["archive_url"])
    csp = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' {' '.join(sorted(origins))}; "
        f"img-src 'self' {archive.scheme}://{archive.netloc}; "
        "font-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'"
    )
    for path in (output / "index.html", output / "publication/index.html"):
        value = path.read_text("utf-8")
        value = re.sub(r'(<meta http-equiv="Content-Security-Policy" content=")[^"]*(">)',
                       lambda match: match[1] + csp + match[2], value)
        path.write_text(value, "utf-8")
    (output / ".nojekyll").touch()


def descriptor_from_env():
    result = {
        "schema_version": 1,
        "publishing_api_url": os.environ["PUBLISHING_API_URL"],
        "catalog_url": os.environ["CATALOG_URL"],
        "catalog_schema": "catalog_v1",
        "archive_url": os.environ["ARCHIVE_URL"],
    }
    if os.getenv("SUPABASE_PUBLISHABLE_KEY"):
        result["supabase_publishable_key"] = os.environ["SUPABASE_PUBLISHABLE_KEY"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.output, descriptor_from_env())


if __name__ == "__main__":
    main()
