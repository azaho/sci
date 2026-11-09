"""``sci claim``: create a signed account claim (§5.2)."""

from __future__ import annotations

import argparse
import urllib.parse
from datetime import UTC, datetime

from pysci.accounts import AccountType, claim_urls, normalize_account
from pysci.errors import SciError
from pysci.keys import public_key_of
from pysci.records import DEFAULT_CLAIM_VALIDITY, create_claim, verify_claim
from pysci.rfc3339 import format_rfc3339, parse_rfc3339

from ..clients.http import Deadline, HttpClient
from ..context import Context
from ..errors import CliError, Exit
from ..output import Result
from ..spec import Command, arg
from ..storage.files import atomic_write_bytes
from ..storage.keys import require_outside
from .common import KEY_ARG_HELP, signing_key

HF_API = "https://huggingface.co/api"


def huggingface_spelling(http: HttpClient, name: str, deadline: Deadline) -> str:
    """The user or organization name as spelled by the Hub API."""
    quoted = urllib.parse.quote(name, safe="")
    for kind, field in (("users", "user"), ("organizations", "name")):
        response = http.get(f"{HF_API}/{kind}/{quoted}/overview", deadline=deadline)
        if response.status == 404:
            continue
        if response.status != 200:
            raise CliError(
                "lookup_failed",
                f"Hugging Face account lookup returned HTTP {response.status}",
                Exit.INCOMPLETE,
                retryable=True,
            )
        record = response.json_object("Hugging Face account overview")
        value = record.get(field)
        if isinstance(value, str) and value.lower() == name.lower():
            return value
        raise CliError(
            "invalid_response", "Hugging Face overview has no matching name", Exit.INVALID
        )
    raise CliError(
        "account_not_found",
        f"Hugging Face account {name!r} was not found",
        Exit.INVALID,
        observed=name,
    )


def run(args: argparse.Namespace, ctx: Context) -> Result:
    output = ctx.resolve(args.output)
    key_path, key, warnings = signing_key(ctx, args)
    require_outside(key_path, output)
    try:
        account = normalize_account(args.type, args.account)
    except ValueError as exc:
        raise CliError(
            "invalid_account",
            f"{args.type} account {args.account!r}: {exc}",
            Exit.USAGE,
            field="/account/id",
            observed=args.account,
        ) from None
    result = Result("claim")
    result.warnings.extend(warnings)
    lookup = "not_required"
    if account.type == AccountType.HUGGINGFACE:
        if args.no_lookup:
            lookup = "skipped"
            result.warn("Hugging Face spelling not checked (--no-lookup)")
        else:
            spelled = huggingface_spelling(ctx.http, account.id, ctx.deadline(args.timeout))
            account = normalize_account(account.type, spelled)
            lookup = "huggingface_api"
    now = datetime.now(UTC)
    if args.expires:
        try:
            expires = parse_rfc3339(args.expires)
        except ValueError as exc:
            raise CliError(
                "invalid_expires", f"--expires: {exc}", Exit.USAGE, observed=args.expires
            ) from None
        if expires <= now:
            raise CliError("invalid_expires", "--expires must be in the future", Exit.USAGE)
    else:
        expires = now + DEFAULT_CLAIM_VALIDITY
    if expires - now > DEFAULT_CLAIM_VALIDITY:
        result.warn("the protocol recommends claims expire within 90 days of signing")
    try:
        record = create_claim(key, account, expires=expires, note=args.note, name=args.name)
    except (ValueError, SciError) as exc:
        raise CliError("invalid_claim", str(exc), Exit.USAGE) from None
    public = public_key_of(key)
    check = verify_claim(
        record.record, record.signature, expected_public_key=public, requested_account=account
    )
    if not check.ok:  # pragma: no cover - library guarantees validity
        raise CliError("invalid_claim", str(check.issues[0]), Exit.INVALID)
    output.mkdir(parents=True, exist_ok=True)
    claim_path = output / f"{public.x}.json"
    signature_path = output / f"{public.x}.json.sig"
    renewed = claim_path.exists()
    atomic_write_bytes(signature_path, record.signature)
    atomic_write_bytes(claim_path, record.record)
    locations = [{"claim": c, "signature": s} for c, s in claim_urls(account, public.x)]
    result.data = {
        "account": account.to_json(),
        "public_key": public.to_jwk(),
        "expires": format_rfc3339(expires),
        "files": {"claim": str(claim_path), "signature": str(signature_path)},
        "status": "renewed" if renewed else "created",
        "lookup": lookup,
        "hosting_locations": locations,
    }
    result.say(f"claim:     {claim_path}")
    result.say(f"signature: {signature_path}")
    result.say(f"account:   {account.type}:{account.id} (expires {format_rfc3339(expires)})")
    result.say("Host both files at" + (" one of:" if len(locations) > 1 else ":"))
    for location in locations:
        result.say(f"  {location['claim']}")
        result.say(f"  {location['signature']}")
    result.say("These files were not uploaded. Renew before expiry; publications are unchanged.")
    return result


COMMAND = Command(
    name="claim",
    usage_hint="claim TYPE ACCOUNT --output DIR [--expires RFC3339] [--key PATH] [--json]",
    summary="Create a signed account claim.",
    description="""
Creates an SCI key claim (protocol §5.2) linking your public key to a domain,
GitHub account, or Hugging Face user or organization. The account is
normalized (domains to lowercase IDNA A-labels, GitHub logins to lowercase);
Hugging Face names take the spelling returned by the Hub API. Writes
<x>.json and <x>.json.sig and prints where to host them; nothing is
uploaded. Renewal replaces the claim files, never publication bytes.
""",
    args=(
        arg(
            "type",
            metavar="TYPE",
            choices=("domain", "github", "huggingface"),
            help="account method: domain, github, or huggingface",
        ),
        arg("account", metavar="ACCOUNT", help="domain name, GitHub login, or Hugging Face name"),
        arg(
            "--output", metavar="DIR", required=True, help="directory for <x>.json and <x>.json.sig"
        ),
        arg("--expires", metavar="RFC3339", help="claim expiry (default: 90 days from now)"),
        arg(
            "--name",
            metavar="TEXT",
            help="publisher display name, 1-80 Unicode code points",
        ),
        arg("--note", metavar="TEXT", help="optional note stored in the signed claim"),
        arg("--key", metavar="PATH", help=KEY_ARG_HELP),
        arg(
            "--no-lookup",
            action="store_true",
            help="skip the Hugging Face spelling lookup (the given spelling must be exact)",
        ),
    ),
    outputs="Account, expiry, written files, and the required hosting URLs.",
    files="Writes DIR/<x>.json and DIR/<x>.json.sig (replacing an earlier claim for the key).",
    network="Hugging Face accounts: one Hub API lookup unless --no-lookup. Otherwise none.",
    exit_codes={
        3: "the Hugging Face account does not exist",
        5: "the account lookup failed temporarily",
    },
    examples=("sci claim domain lab.example --output ./claims",),
    run=run,
    timeout=True,
)
