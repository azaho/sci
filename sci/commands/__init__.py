"""One module per CLI command. Each defines run(args, out) and register(sub, common, raw)."""

from . import keygen, pull, push, server, sign, verify

# Order of `sci --help`.
COMMANDS = (server, keygen, sign, verify, push, pull)
