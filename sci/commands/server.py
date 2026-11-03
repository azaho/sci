"""sci server: show or set the default server."""

from __future__ import annotations

from ..config import load_config, save_config, validate_server_url
from ..output import Out


def run(args, out: Out) -> int:
    if args.url is None:
        saved = load_config().get("server")
        if saved:
            out.result(saved, {"server": saved})
        else:
            out.result("No default server is configured. Set one with `sci server URL`.", {"server": None})
        return 0
    url = validate_server_url(args.url)
    cfg = load_config()
    cfg["server"] = url
    save_config(cfg)
    out.result(url, {"server": url})
    return 0



def register(sub, common, raw) -> None:
    s = sub.add_parser(
        "server", parents=[common], formatter_class=raw,
        help="show or set the default server",
        description="With no URL, show the saved default server. With a URL, validate it and save it as the\n"
        "default for push and pull (in ~/.sci/config.json). No network access.\n\n"
        "The URL is the instance base; clients read <URL>/sci-server.json to find its services.\n\n"
        "Examples:\n  sci server\n  sci server https://sci.lab.example",
    )
    s.add_argument("url", nargs="?", help="instance URL, for example https://sci.example.org")
    s.set_defaults(func=run)
