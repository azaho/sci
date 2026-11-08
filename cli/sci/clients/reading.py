"""Prefer loose reader files; a bounded complete ZIP is the compatibility fallback."""

from __future__ import annotations
from pysci import Incomplete, Invalid
from pysci.discovery import descriptor, expand, lookup_sources
from pysci.reading import Reading, read_verified
from pysci.validation import check_zip
from sci.clients.network import Network, digest, index
from sci.output import CommandError


def retrieve(
    value: str,
    archives: list[str],
    source: str | None,
    timeout: float,
    *,
    file: str | None = None,
    listing: bool = False,
) -> Reading:
    pid = digest(value)
    network = Network(timeout)
    if not archives:
        index_descriptor = index(network, source)
        archives = lookup_sources(
            network.request(
                expand(index_descriptor["publication_lookup_url"], id=pid), limit=65536
            ),
            200,
        )
    if not archives:
        raise CommandError("no_known_sources", "No known archive sources", 5)
    failures = []
    for url in archives:
        try:
            archive = descriptor(network.request(url, limit=65536), "archive")
            if "entry_url" in archive:
                try:

                    def entry(path: str, limit: int) -> bytes:
                        return network.request(
                            expand(archive["entry_url"], id=pid, id2=pid[:2], path=path),
                            limit=limit,
                        )

                    return read_verified(entry, expected_id=pid, file=file, listing=listing)
                except (Incomplete, Invalid, CommandError):
                    if "bundle_url" not in archive:
                        raise
            mb, sig, _, payload = check_zip(
                network.request(expand(archive["bundle_url"], id=pid, id2=pid[:2]))
            )
            entries = {"manifest.json": mb, "manifest.json.sig": sig, **payload}
            return read_verified(
                lambda path, limit: entries[path], expected_id=pid, file=file, listing=listing
            )
        except (Incomplete, Invalid, CommandError) as exc:
            failures.append(str(exc))
    raise Incomplete("All sources failed: " + "; ".join(failures))
