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
    # WP-D2. Everything below the guard rather than beside it, because this WP
    # owns `__main__` and nothing else in this file: the module-level surface
    # (DB_PATH, DEFAULT_PYTHON_PATH, the lazy `mcp`) is WP-C1's and its tests'
    # and stays exactly as it is. What changes is only what running the module
    # *means*. Until WP-D1 it meant "be the server"; now the daemon is the
    # server, and this process is a stdio-to-HTTP proxy pointed at it, which is
    # the only thing an MCP client that speaks stdio can be handed.
    import argparse
    import json
    import urllib.error
    import urllib.request
    import uuid
    from pathlib import Path

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy

    from dsos.server.common import CLIENT_SESSION_HEADER

    URL_ENV = "DSOS_URL"
    TOKEN_ENV = "DSOS_TOKEN"
    PROFILES = ("producer", "consumer")

    def _healthz(base_url: str, timeout: float = 2.0) -> dict | None:
        """Is that daemon actually up? Not merely recorded in daemon.json.

        A manifest is a claim, and a claim outlives the process that made it:
        a killed daemon, a reboot, a port taken by something else. Asking
        /healthz is the same check the daemon itself makes before refusing to
        start a second one over the same store, which keeps "the shim says no"
        and "the daemon says no" from ever disagreeing. The answer comes back
        whole, because *which* store the daemon reports is half the question.
        """
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=timeout) as response:
                if response.status != 200:
                    return None
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return body if isinstance(body, dict) else {}

    def _same_store(a: str | Path, b: str | Path) -> bool:
        """The comparison dsos.daemon.same_store makes, for the same reason:
        resolved, then case-folded where the filesystem is."""
        def canonical(p: str | Path) -> str:
            return os.path.normcase(str(Path(p).resolve()))
        return canonical(a) == canonical(b)

    def _manifest_base_url(db_dir: Path) -> str | None:
        """The running daemon's base URL, from the file it wrote beside the store.

        Same reader the daemon's own "one daemon per store" check uses, and for
        the same reason: the file next to the store is the answer to "who owns
        this store", and a shim that invented a second way to ask would be a
        second answer.
        """
        path = db_dir / "daemon.json"
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        base_url = record.get("base_url")
        return base_url if isinstance(base_url, str) and base_url else None

    def _no_daemon(db_path: Path) -> int:
        """The one failure a new user hits first, said the one useful way.

        It names the store (so it is obvious which of several DSOS_DB_PATH
        values is the problem) and the exact command, built from the
        interpreter actually running this shim rather than a hardcoded
        `python` — which, in a venv, is very often the wrong one.
        """
        print(
            f"dsos daemon not running for {db_path}. "
            f"Start it: {sys.executable} -m dsos.daemon",
            file=sys.stderr,
        )
        return 1

    def _token_file_token(db_dir: Path) -> str:
        """The token the daemon wrote beside the store, or "" if it never did."""
        path = db_dir / "daemon.token"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def main(argv: list[str] | None = None) -> int:
        parser = argparse.ArgumentParser(
            prog="python -m dsos.mcp_server",
            description=(
                "stdio shim for the dsos daemon: forwards one profile to the "
                "daemon serving DSOS_DB_PATH."
            ),
        )
        parser.add_argument(
            "--profile", choices=PROFILES, default="producer",
            help="which daemon endpoint to expose (default: producer)",
        )
        args = parser.parse_args(argv)

        # The same lookup dsos.daemon does, deliberately: a client and its
        # daemon have to agree on which store is meant without the user saying
        # it twice, and this is also what the failure message names.
        db_path = Path(os.environ.get("DSOS_DB_PATH", "data/store.db")).resolve()
        db_dir = db_path.parent

        from_env = os.environ.get(URL_ENV)
        base_url = from_env or _manifest_base_url(db_dir)
        if not base_url:
            return _no_daemon(db_path)
        answer = _healthz(base_url)
        if answer is None:
            # A manifest left by a daemon that is gone, or a DSOS_URL that
            # points at nothing. Both are the same thing to the user, and
            # the same command fixes both.
            return _no_daemon(db_path)
        served = answer.get("db_path")
        # Checked whenever the user named a store. DSOS_URL with no
        # DSOS_DB_PATH at all names none — the default is a guess relative to
        # wherever the client launched this — so there the URL is taken at its
        # word, as it always was.
        stated = "DSOS_DB_PATH" in os.environ or not from_env
        if stated and isinstance(served, str) and not _same_store(served, db_path):
            # A daemon answers, but for another store: a DSOS_URL left
            # pointing at a different store's daemon, or a copied daemon.json.
            # Proxying anyway would hand the client somebody else's store
            # under this one's name — every write would land in the wrong
            # file, and nothing would say so. It is "not running" as far as
            # *this* store is concerned, so it is said the same way, with the
            # mismatch named so the fix is obvious.
            source = URL_ENV if from_env else str(db_dir / "daemon.json")
            print(
                f"dsos daemon not running for {db_path}. The daemon at {base_url} "
                f"(from {source}) is serving a different store, {served}. "
                f"Start one for this store: {sys.executable} -m dsos.daemon "
                f"with DSOS_DB_PATH={db_path}, or point DSOS_DB_PATH at {served}.",
                file=sys.stderr,
            )
            return 1

        token = os.environ.get(TOKEN_ENV) or _token_file_token(db_dir)
        if not token:
            # The daemon always runs a StaticTokenVerifier, so a proxy without
            # one gets a 401 on every call — and a proxy reports a failed
            # backend call as an empty tool list rather than as an error, which
            # reads to an agent as "this server has no tools". Refusing here,
            # with the two ways to fix it, is the difference between a five
            # second fix and an afternoon.
            print(
                f"dsos daemon at {base_url} needs a bearer token. Set {TOKEN_ENV}, "
                f"or start the daemon so it writes {db_dir / 'daemon.token'} "
                f"next to the store.",
                file=sys.stderr,
            )
            return 1

        # The trailing slash is the addressable URL. The daemon mounts the MCP
        # app with path="/", so Starlette answers /mcp/producer with a 307 to
        # /mcp/producer/ — before the auth check even runs. A client that
        # follows the redirect is fine either way; one that does not looks
        # broken. The daemon's own start-up banner prints this form, and so
        # does AGENTS.md.
        url = f"{base_url.rstrip('/')}/mcp/{args.profile}/"

        # A Client, not create_proxy(url, auth=token). The kwarg on
        # create_proxy lands on the *front* server — it would make the shim
        # demand a bearer token from the MCP client, which is backwards, and
        # leave the daemon rejecting the proxy. create_proxy also accepts a
        # disconnected Client, and a Client is where the transport's
        # credentials belong; its factory clones one per request, so each
        # proxied call gets its own backend session.
        #
        # Which is why this shim names its own session in a header: the
        # per-call backend sessions share nothing the daemon can key on, and
        # every shim reports the same clientInfo, so without it two shims
        # running at once would be logged as one consumer. One id per shim
        # process; Client.new() shares the transport, so every clone sends it.
        transport = StreamableHttpTransport(
            url, headers={CLIENT_SESSION_HEADER: uuid.uuid4().hex}
        )
        proxy = create_proxy(Client(transport, auth=token), name=f"dsos-{args.profile}")
        # `proxy` is a local, so this is an ordinary name lookup — unlike the
        # bare `mcp.run()` this replaces, which was a NameError because a
        # module-level __getattr__ is not consulted for global names inside the
        # module body. Nothing here touches `mcp`, and so nothing here opens
        # the store: the shim has no business owning one.
        proxy.run(transport="stdio")
        return 0

    sys.exit(main())
