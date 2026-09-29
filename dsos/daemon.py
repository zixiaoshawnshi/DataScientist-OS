"""The daemon: one process owns one store.

Before this, the store had no owner. An MCP server process opened it with a
module-global connection and a GUI process opened a second one against the
same file; two connections, two migration attempts, and — the part that
actually bites — a `sqlite3` connection shared across a thread pool, which
is not safe. Decision D8 fixes the shape: one `Database` per process, one
connection per thread, one process-wide write lock, one daemon per store.
This module is the process that has that `Database`.

    python -m dsos.daemon [--host 127.0.0.1] [--port 8765]      # DSOS_PORT works too

It serves three things off one ASGI app, which is the point rather than a
convenience:

- the read-only GUI, mounted as a router over the *same* `Database` the MCP
  tools write through — a reader that sees a session mid-write, with no
  second process and no second connection;
- the producer MCP app at `/mcp/producer`, built by `dsos.server`;
- the consumer MCP app at `/mcp/consumer`, built the same way, which serves
  the consumer's four read-oriented tools (D6, WP-F1).

Both MCP apps carry a `StaticTokenVerifier` over a token that lives in
`<db dir>/daemon.token`, so an MCP client is a bearer-token client and the
GUI and `/healthz` stay open — a `cite` URL has to open in a browser, and
loopback is not a security boundary (D9). Open to a browser means open to a
DNS-rebinding page too, so the whole app sits behind a Host-header check that
admits loopback names only. Non-loopback binds are refused outright rather
than quietly allowed: Doc II lists remote access as an open question, and an
accidental `--host 0.0.0.0` on a machine that would accept one is a worse
outcome than a refusal.

`daemon.json`, next to the store, is how "one daemon per store" is enforced
across processes. A file naming a live pid whose `/healthz` answers *for this
store* means somebody is already serving it, and this process exits rather
than binding a second writer to the same file. A file whose pid is gone is
stale — a hard kill, a reboot — and is overwritten, and so is one whose
daemon answers for a different store. The checks are all needed, and the pid
check has to be a real one: on Windows, `os.kill(pid, 0)` reports a *reaped*
child as alive for as long as the parent holds its handle, so a naive
liveness probe would refuse to start for ever after a crash.

Run: .venv/Scripts/python.exe -m dsos.daemon
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.middleware.trustedhost import TrustedHostMiddleware

from dsos import gui
from dsos.db import Database
from dsos.server import ServerConfig, build_consumer, build_producer
from dsos.server.common import server_version

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# The binds this daemon will accept. "localhost" is here because it is what
# someone types; it resolves to a loopback address, and the check is about
# the interface the socket lands on, not about the string's spelling.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# The Host headers the app will answer. Not the same list as the binds above:
# this one is about what a *request* claims, and it is the DNS-rebinding
# guard. A page on evil.example that rebinds its name to 127.0.0.1 reaches
# this port with `Host: evil.example`, and the unauthenticated GUI and
# /healthz would otherwise answer it. Starlette compares the host part with
# the brackets of an IPv6 literal kept on ("[::1]", as of Starlette 1.x); the
# bare "::1" is kept alongside it for a parser that strips them.
ALLOWED_HOST_HEADERS = ["127.0.0.1", "localhost", "::1", "[::1]"]

TOKEN_ENV = "DSOS_TOKEN"
PORT_ENV = "DSOS_PORT"
TOKEN_FILENAME = "daemon.token"
MANIFEST_FILENAME = "daemon.json"


class DaemonConflict(RuntimeError):
    """Another daemon is already serving this store. Exits non-zero, naming
    the one that is in the way, because the alternative — two processes
    writing one SQLite file — is what this module exists to prevent."""


def db_path_from_env() -> Path:
    """Where the store is, from DSOS_DB_PATH or the usual default. Same
    lookup dsos.mcp_server does, so a client and its daemon agree without
    the user having to say it twice."""
    return Path(os.environ.get("DSOS_DB_PATH", "data/store.db")).resolve()


def resolve_port(cli_port: int | None) -> int:
    """--port wins over DSOS_PORT, which wins over the default. argparse only
    supplies the default when the flag is absent, so the env var is consulted
    here rather than in the parser — a user who exports DSOS_PORT once should
    not have to remember the flag as well."""
    if cli_port is not None:
        return cli_port
    from_env = os.environ.get(PORT_ENV)
    if from_env:
        return int(from_env)
    return DEFAULT_PORT


def _pid_alive(pid: int) -> bool:
    """Is that process still running?

    On POSIX, signal 0 is the standard existence probe. On Windows it is not
    one: `os.kill(pid, 0)` returns successfully for a process that has exited
    as long as *any* handle to it is still open, which for a daemon we
    started ourselves means for as long as the shell that launched it lives.
    A stale manifest would then read as a live daemon and the store could
    never be reopened, so Windows asks the kernel for the process's exit code
    instead, which is answerable for a handle to a dead process.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    import ctypes  # Windows-only path; no reason to pay for it elsewhere

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def resolve_token(db_dir: Path) -> str:
    """The bearer token every MCP client must present.

    DSOS_TOKEN wins, so a launcher can manage the secret itself. Otherwise
    the token is read from, or generated into, `<db dir>/daemon.token` beside
    the store: the same directory as daemon.json, which means the answer to
    "what token does this store use" is a file next to the store rather than
    a thing the user has to remember. The file is written 0600 where the OS
    has such a mode; on Windows the attribute is not the same thing and
    chmod cannot make a file private, which is one more reason the daemon
    refuses non-loopback binds.
    """
    from_env = os.environ.get(TOKEN_ENV)
    if from_env:
        return from_env
    path = db_dir / TOKEN_FILENAME
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows has no 0600; the file inherits the directory's ACL, which
        # is the best that can be done there.
        pass
    return token


def manifest_path(db_dir: Path) -> Path:
    return db_dir / MANIFEST_FILENAME


def read_manifest(db_dir: Path) -> dict | None:
    path = manifest_path(db_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A truncated or hand-edited manifest is not evidence of anything;
        # treat it as absent and let the caller overwrite it.
        return None


def check_no_other_daemon(db_dir: Path, db_path: Path) -> None:
    """Refuse to start if a live daemon already owns this store.

    Three conditions matter, and they are not the same condition. A live pid
    whose `/healthz` answers *and reports this store* is a daemon serving
    it: start nothing, say which one, exit. A live pid with a *silent*
    `/healthz` is something else — a process that crashed between writing
    the manifest and binding the port, or a long-dead pid that the OS has
    since reused — and refusing on that basis would strand the store behind
    a file nobody can explain. A live, answering daemon that reports a
    *different* store is a manifest that has stopped describing this
    directory (a copied directory, a port since taken by another store's
    daemon): nobody is serving this store, so that is stale too. A dead pid
    is stale by definition. Every stale case gets overwritten, with a note.
    """
    record = read_manifest(db_dir)
    if not record:
        return
    pid = record.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return
    base_url = record.get("base_url")
    answer = _healthz(base_url) if base_url else None
    if answer is not None:
        served = answer.get("db_path")
        # A /healthz with no db_path predates it being reported; it can only
        # be taken at its word, which is the manifest's.
        if not isinstance(served, str) or same_store(served, db_path):
            raise DaemonConflict(
                f"a dsos daemon is already serving {served or record.get('db_path', str(db_dir))}: "
                f"pid {pid} on {base_url} (started {record.get('started_at', 'unknown')}). "
                f"Stop it, or point DSOS_DB_PATH at another store."
            )
        print(
            f"dsos: overwriting a daemon.json whose daemon (pid {pid}, {base_url}) "
            f"is serving a different store, {served}, not {db_path}",
            file=sys.stderr,
        )
        return
    print(
        f"dsos: overwriting a daemon.json that no live daemon answers for "
        f"(pid {pid}, {base_url or 'no base_url recorded'})",
        file=sys.stderr,
    )


def same_store(a: str | Path, b: str | Path) -> bool:
    """Do two spellings of a store path name the same file?

    Resolved first, so a relative path, a `..` or a symlink cannot make one
    store look like two; then case-folded where the filesystem is (normcase
    is a no-op off Windows, which is the right answer there).
    """
    def canonical(p: str | Path) -> str:
        return os.path.normcase(str(Path(p).resolve()))
    return canonical(a) == canonical(b)


def _healthz(base_url: str, timeout: float = 2.0) -> dict | None:
    """That daemon's /healthz answer right now, or None if it does not give one."""
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else {}


def write_manifest(db_dir: Path, port: int, base_url: str, db_path: Path) -> None:
    manifest_path(db_dir).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "base_url": base_url,
                "db_path": str(db_path),
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def remove_manifest(db_dir: Path) -> None:
    """Only ever removes *our* manifest. A daemon that was slow to start, and
    lost the race to a second one, must not delete the winner's file on its
    way out — that would turn "refuse to start" into "no daemon at all"."""
    record = read_manifest(db_dir)
    if record and record.get("pid") != os.getpid():
        return
    try:
        manifest_path(db_dir).unlink()
    except FileNotFoundError:
        pass


def build_app(config: ServerConfig, db_path: Path, db_dir: Path) -> FastAPI:
    """One FastAPI app: the GUI's routes, both MCP servers, and /healthz.

    The lifespans are the fiddly part and the reason this is written as it
    is. A FastMCP server's HTTP app is a Starlette app whose own lifespan
    starts its session manager — the thing that holds the MCP sessions and
    drives the server's own lifespan. Mounting it without running that
    lifespan gives an app that accepts requests and serves nothing: the
    session manager is still `None`, so the first request fails. So the
    daemon's lifespan enters both mounted apps' lifespans on the way up and
    unwinds both on the way down, and the manifest is removed after that,
    so a clean shutdown is a clean shutdown in both senses.
    """
    token = resolve_token(db_dir)
    auth = StaticTokenVerifier(tokens={token: {"client_id": "dsos-daemon", "scopes": []}})

    producer = build_producer(config)
    consumer = build_consumer(config)
    # .auth is a plain attribute, and http_app() reads it — there is no auth
    # parameter on http_app() to pass it through, so this is the seam.
    producer.auth = auth
    consumer.auth = auth
    # path="/" so the mounted app's own route is "/" and the addressable URL is
    # the mount point plus a trailing slash: /mcp/producer/ .
    producer_app = producer.http_app(path="/")
    consumer_app = consumer.http_app(path="/")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with AsyncExitStack() as stack:
                for mounted in (producer_app, consumer_app):
                    await stack.enter_async_context(mounted.lifespan(mounted))
                yield
        finally:
            remove_manifest(db_dir)

    app = FastAPI(title="DS Artifact OS", lifespan=lifespan)
    # Outermost, so it covers everything below: the GUI, /healthz, and both
    # mounted MCP apps. The MCP apps have a bearer token and would survive
    # without it; the GUI and /healthz have nothing else. Every real client
    # sends a loopback Host — a browser on the printed URL, an MCP client on
    # 127.0.0.1:<port>, the shim's own /healthz probe — so this refuses only
    # a request that arrived under somebody else's name.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOST_HEADERS)
    app.include_router(gui.create_gui_router(config.db))

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness, with the store it is serving. No token: the daemon.json
        check in the next startup and the stdio shim are what read it, and a
        health endpoint that needed a secret could not tell a stale manifest
        from a live one. Both compare `db_path` against the store they were
        pointed at, so the path is part of the answer, not decoration.
        """
        return {"ok": True, "version": server_version(), "db_path": str(db_path)}

    app.mount("/mcp/producer", producer_app)
    app.mount("/mcp/consumer", consumer_app)
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m dsos.daemon",
        description="Serve the dsos store: GUI plus both MCP profiles, one process.",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"loopback address to bind ({DEFAULT_HOST}); anything else is refused",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help=f"port to bind ({DEFAULT_PORT}); overridden by {PORT_ENV}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        # Doc II open question 5: remote access is not built, and half-built
        # auth on a bindable interface is the failure mode worth refusing.
        print(
            f"dsos: refusing to bind {args.host}: remote access is not supported yet. "
            f"Use {DEFAULT_HOST} (or another loopback address).",
            file=sys.stderr,
        )
        return 2

    try:
        port = resolve_port(args.port)
    except ValueError as exc:
        print(f"dsos: {PORT_ENV} is not a port number: {exc}", file=sys.stderr)
        return 2

    db_path = db_path_from_env()
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    try:
        check_no_other_daemon(db_dir, db_path)
    except DaemonConflict as exc:
        print(f"dsos: {exc}", file=sys.stderr)
        return 3

    base_url = f"http://{args.host}:{port}"
    db = Database(db_path)
    config = ServerConfig(
        db=db,
        # run_python defaults to the daemon's own interpreter when a call
        # omits python_path; DSOS_PYTHON_PATH is the user's analysis stack.
        python_path=os.environ.get("DSOS_PYTHON_PATH", sys.executable),
        base_url=base_url,
    )
    app = build_app(config, db_path, db_dir)
    write_manifest(db_dir, port, base_url, db_path)
    print(
        f"dsos daemon on {base_url} serving {db_path}\n"
        f"  producer: {base_url}/mcp/producer/\n"
        f"  consumer: {base_url}/mcp/consumer/\n"
        f"  gui:      {base_url}/\n"
        f"  token:    {TOKEN_ENV} or {db_dir / TOKEN_FILENAME}",
        file=sys.stderr,
    )
    try:
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
    finally:
        # Belt to the lifespan's braces: uvicorn's own shutdown does not
        # always reach the finaliser above (a failed bind, for one), and a
        # manifest that outlives the process is a manifest that lies.
        remove_manifest(db_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
