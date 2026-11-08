from __future__ import annotations
import re
from pysci import Invalid
from pysci.validation import check_domain


def normalize(kind: str, account: str, timeout: float) -> str:
    if kind == "domain":
        import idna

        account = idna.encode(account.lower().rstrip(".")).decode()
        check_domain(account)
    elif kind == "github":
        account = account.lower()
        if not re.fullmatch("[a-z0-9]+(?:-[a-z0-9]+)*", account) or len(account) > 39:
            raise Invalid("GitHub login")
    elif kind == "huggingface":
        from sci.clients.network import Network
        from pysci.validation import strict_json
        from urllib.parse import quote

        obj = strict_json(
            Network(timeout).request(
                "https://huggingface.co/api/users/" + quote(account, safe="") + "/overview",
                limit=65536,
            )
        )
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise Invalid("Hub account lookup")
        account = obj["name"]
    else:
        raise Invalid("unsupported account type")
    return account
