"""The stdio entry point: one producer server, over `DSOS_DB_PATH`.

This used to be the whole of layer 2 — the FastMCP instance, nine tool
definitions, the middleware and the instruction text. That is now
`dsos/server/`, built from a `ServerConfig`; what is left here is the part
that is specific to running one server over stdio, which is knowing where
the store and the analysis interpreter are:

- it reads `DSOS_DB_PATH` (falling back to `data/store.db`) and
  `DSOS_PYTHON_PATH` (falling back to this interpreter),
- it opens the `Database`, which is the one thing in the process that
  migrates the file, and
- it runs the producer server over stdio for an MCP client that launches
  this module as a subprocess.

`mcp` is exposed through a module `__getattr__` rather than assigned, so
`from dsos.mcp_server import mcp` still works while importing this module
stays free: the store is not opened, and no environment variable is read,
until something actually asks for the server. Every smoke test sets
DSOS_DB_PATH and only then imports `mcp`, and that ordering is exactly what
depends on this being lazy.

WP-D2 replaces `__main__` with a proxy to the daemon, which is the point at
which this file stops being able to own a store at all.

Run: .venv/Scripts/python.exe -m dsos.mcp_server
Point a real MCP client (Claude Code, Claude Desktop) at this to test with
a real agent, instead of tests/smoke_test.py's direct function calls.
"""

from __future__ import annotations

import os
import sys

from dsos.db import Database

DB_PATH = os.environ.get("DSOS_DB_PATH", "data/store.db")

# The interpreter run_python targets when a call omits python_path — the
# user's own analysis Python (pandas/matplotlib/... already installed
# there), not this server's. Unset -> this server's own interpreter, so an
# existing/test install with no DSOS_PYTHON_PATH keeps working unchanged.
# It stays here, next to the other two environment lookups, because it is
# one of them: the config handed to build_producer() below.
DEFAULT_PYTHON_PATH = os.environ.get("DSOS_PYTHON_PATH", sys.executable)

_config = None
_mcp = None


def __getattr__(name: str):
    """Build the producer server on first access (PEP 562).

    The lazy half is load-bearing rather than tidy: a test that sets
    DSOS_DB_PATH and then imports `mcp` must get a server over *that* path,
    and an assignment at module scope would have read the variable — and
    opened the store — before the test could set it. Building it here means
    the env var is read at the moment the server is asked for.
    """
    if name != "mcp":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _config, _mcp
    if _mcp is None:
        from dsos.server import ServerConfig, build_producer

        _config = ServerConfig(
            db=Database(DB_PATH),
            python_path=DEFAULT_PYTHON_PATH,
            # No HTTP surface on the stdio path, which is what the
            # consumer's cite() has to cope with: it falls back to a
            # dsos:<row_id> reference rather than a link that 404s.
            base_url=None,
        )
        _mcp = build_producer(_config)
    return _mcp


if __name__ == "__main__":
    # Through __getattr__, not a bare `mcp`: a module-level __getattr__ is
    # consulted for attribute access on the module, not for a global name
    # lookup inside its own body, so a bare `mcp.run()` here is a NameError.
    __getattr__("mcp").run()
