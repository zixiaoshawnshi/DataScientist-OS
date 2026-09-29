"""Consumer-session smoke test: one MCP client session is one consumer
session, over the daemon (D12, `ConsumerSessions` in dsos/server/common.py).

What this is the test of. D12 says every consumer call is logged against a
`sessions` row of `kind='consumer'`, one per MCP client session, so that
"how often does a PM come back to this store" is a count of visits rather
than a count of calls. The first cut keyed that row on FastMCP's
`ctx.session_id`, and over the daemon the key changed on every call: three
`find_evidence` calls in one client produced three consumer sessions.

Why. FastMCP 4's client negotiates MCP 2026-07-28 by default, and that
protocol era is sessionless by design: no `initialize` handshake, no
`mcp-session-id` header, and the SDK builds a fresh `Connection` per request.
`ctx.session_id` falls back to a uuid cached on that per-request connection,
so it is a per-*call* id. The in-process tests/consumer_smoke_test.py never
counted rows per client, so it could not see it. It is a subprocess test for
the same reason tests/daemon_smoke_test.py is one: the shim, the token file
and the mounted HTTP app only run when the daemon is started the way a user
starts it.

Covered here, each as "N calls in one client -> exactly one new row":

1. A sessionless-era (2026-07-28) HTTP client that says nothing but its
   clientInfo: its calls are one session, and a client with a different
   clientInfo is another. This is the case that failed.
2. Two sessionless clients with the *same* clientInfo, told apart by the
   `x-dsos-client-session` header: two sessions, and a reconnect carrying the
   first header lands back in the first. That header is what a proxy that
   opens a backend connection per call (the stdio shim) has to send.
3. A handshake-era client (`mode="legacy"`): keyed on its `mcp-session-id`,
   one session per connection. This worked before and must keep working.
4. Three calls through one stdio shim process (`python -m dsos.mcp_server
   --profile consumer`) -> one session.
5. In-process, no daemon: the key cache is bounded, an explicit identity
   survives any idle gap, and a sessionless one is a new visit after the
   idle window.

Run: .venv/Scripts/python.exe tests/consumer_sessions_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Absolute, because the path is handed to subprocesses that do not share the
# runner's cwd. Named by the usual convention, so a store left behind by a
# crashed run is recognisable.
DB_DIR = REPO_ROOT / "data" / "test-runs" / "consumer_sessions_smoke_test"
DB_PATH = DB_DIR / "store.db"
LOG_DIR = DB_DIR / "logs"
os.environ["DSOS_DB_PATH"] = str(DB_PATH)
shutil.rmtree(DB_DIR, ignore_errors=True)
DB_DIR.mkdir(parents=True, exist_ok=True)

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport  # noqa: E402
from mcp_types import Implementation  # noqa: E402
from mcp_types.version import MODERN_PROTOCOL_VERSIONS  # noqa: E402

from dsos.server.common import CLIENT_SESSION_HEADER  # noqa: E402

FAILURES: list[str] = []
CALLS_PER_CLIENT = 3


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def child_env(**overrides: str) -> dict[str, str]:
    """The environment a daemon or shim subprocess is launched with.

    PYTHONPATH so the child imports *this* checkout's dsos rather than
    whatever an editable install points at; DSOS_URL/DSOS_TOKEN dropped so the
    shim finds the daemon the way a user's would, through the files beside
    the store.
    """
    env = dict(os.environ)
    inherited = [p for p in [env.get("PYTHONPATH", "")] if p]
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), *inherited])
    env["PYTHONIOENCODING"] = "utf-8"
    env["DSOS_DB_PATH"] = str(DB_PATH)
    env.pop("DSOS_URL", None)
    env.pop("DSOS_TOKEN", None)
    env.update(overrides)
    return env


def launch_daemon(port: int) -> subprocess.Popen:
    # Logs to files rather than pipes: an undrained pipe fills and blocks the
    # child, which reads as a hung daemon rather than a full buffer.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / "daemon.out.log", "w", encoding="utf-8")
    err = open(LOG_DIR / "daemon.err.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "dsos.daemon", "--port", str(port)],
        cwd=str(REPO_ROOT), env=child_env(), stdout=out, stderr=err,
    )


def wait_until_serving(base_url: str, proc: subprocess.Popen, seconds: float = 120.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
                json.loads(response.read().decode("utf-8"))
                return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    return False


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def consumer_sessions() -> list[str]:
    """Every consumer session id in the store, oldest first.

    Read through a connection of the test's own rather than the daemon's:
    the store is in WAL mode, so a reader sees every committed write, and
    the daemon commits each session row as it inserts it.
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute(
            "SELECT id FROM sessions WHERE kind = 'consumer' ORDER BY started_at, rowid"
        )]
    finally:
        conn.close()


def call_sessions(session_ids: list[str]) -> dict[str, int]:
    """How many find_evidence calls each of these sessions logged."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return {
            sid: conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE session_id = ? AND tool_name = 'find_evidence'",
                (sid,),
            ).fetchone()[0]
            for sid in session_ids
        }
    finally:
        conn.close()


async def calls(client: Client, label: str) -> str | None:
    """CALLS_PER_CLIENT find_evidence calls in one open client; returns the
    protocol version it negotiated."""
    async with client:
        for i in range(CALLS_PER_CLIENT):
            await client.call_tool("find_evidence", {"claim": f"{label} probe {i}"})
        return client.protocol_version


def new_rows(before: list[str]) -> list[str]:
    return [sid for sid in consumer_sessions() if sid not in set(before)]


def http_client(url: str, token: str, *, name: str = "consumer-sessions-test",
                header: str | None = None, mode: str = "auto") -> Client:
    transport = StreamableHttpTransport(
        url, headers={CLIENT_SESSION_HEADER: header} if header else None
    )
    return Client(
        transport, auth=token, mode=mode, timeout=180,
        client_info=Implementation(name=name, version="1"),
    )


async def daemon_checks(base_url: str, token: str) -> None:
    url = f"{base_url}/mcp/consumer/"

    # 1. The failing case: a sessionless-era client with nothing but its
    #    clientInfo to go on.
    before = consumer_sessions()
    version = await calls(http_client(url, token, name="anon-a"), "anon-a")
    rows = new_rows(before)
    check("the default client negotiates the sessionless 2026-07-28 era",
          version in MODERN_PROTOCOL_VERSIONS, f"negotiated {version}")
    check("3 calls in one sessionless HTTP client -> exactly 1 consumer session",
          len(rows) == 1, f"{len(rows)} new sessions")
    check("...and all 3 calls are logged against it",
          list(call_sessions(rows).values()) == [CALLS_PER_CLIENT], f"{call_sessions(rows)}")

    before = consumer_sessions()
    await calls(http_client(url, token, name="anon-b"), "anon-b")
    rows = new_rows(before)
    check("a second sessionless client with a different clientInfo -> 1 more session",
          len(rows) == 1, f"{len(rows)} new sessions")

    # 2. Same clientInfo, told apart by the explicit header.
    before = consumer_sessions()
    await calls(http_client(url, token, name="shared", header="client-one"), "one")
    rows_one = new_rows(before)
    check("3 calls with an x-dsos-client-session header -> exactly 1 consumer session",
          len(rows_one) == 1, f"{len(rows_one)} new sessions")

    before = consumer_sessions()
    await calls(http_client(url, token, name="shared", header="client-two"), "two")
    rows_two = new_rows(before)
    check("a second client, same clientInfo, different header -> 1 more session",
          len(rows_two) == 1 and rows_two != rows_one, f"{len(rows_two)} new sessions")

    before = consumer_sessions()
    await calls(http_client(url, token, name="shared", header="client-one"), "one-again")
    rows = new_rows(before)
    counts = call_sessions(rows_one)
    check("a reconnect carrying the first header lands back in the first session",
          not rows and counts == {rows_one[0]: 2 * CALLS_PER_CLIENT},
          f"{len(rows)} new sessions; first session has {counts}")

    # 3. The handshake era: one mcp-session-id per connection.
    before = consumer_sessions()
    version = await calls(http_client(url, token, name="legacy", mode="legacy"), "legacy-1")
    rows = new_rows(before)
    check("3 calls in one handshake-era (mcp-session-id) client -> exactly 1 session",
          len(rows) == 1 and version not in MODERN_PROTOCOL_VERSIONS,
          f"{len(rows)} new sessions, negotiated {version}")

    before = consumer_sessions()
    await calls(http_client(url, token, name="legacy", mode="legacy"), "legacy-2")
    rows = new_rows(before)
    check("a second handshake-era client with the same clientInfo -> 1 more session",
          len(rows) == 1, f"{len(rows)} new sessions")

    # 4. Through the stdio shim, which clones its backend client per proxied
    #    call: every call is a fresh backend connection.
    before = consumer_sessions()
    shim = Client(
        StdioTransport(
            command=sys.executable,
            args=["-m", "dsos.mcp_server", "--profile", "consumer"],
            env=child_env(),
            cwd=str(REPO_ROOT),
        ),
        timeout=180,
    )
    await calls(shim, "shim")
    rows = new_rows(before)
    check("3 calls through one stdio shim process -> exactly 1 consumer session",
          len(rows) == 1, f"{len(rows)} new sessions")


def in_process_checks() -> None:
    """5. The cache's bound and the idle window, without a daemon or a client.

    Driven through `_session_for` directly because neither can be reached
    from the wire in a test that finishes: one needs thousands of clients,
    the other thirty idle minutes.
    """
    from dsos.db import Database
    from dsos.server.common import ConsumerSessions

    db = Database(DB_DIR / "in_process.db")
    sessions = ConsumerSessions(db, max_entries=3, idle_seconds=60.0)
    now = 1000.0

    ids = [sessions._session_for(f"k{i}", True, "c", now) for i in range(5)]
    check("the key cache never holds more than max_entries",
          len(sessions._sessions) == 3, f"{len(sessions._sessions)} entries")
    check("distinct keys are distinct sessions", len(set(ids)) == 5, f"{ids}")
    check("a recently used key is still cached (LRU, not FIFO)",
          sessions._session_for("k4", True, "c", now) == ids[4])

    stable = sessions._session_for("explicit", True, "c", now)
    check("an explicit identity keeps its session across any idle gap",
          sessions._session_for("explicit", True, "c", now + 10_000) == stable)

    anon = sessions._session_for("anon", False, "c", now)
    same_visit = sessions._session_for("anon", False, "c", now + 59)
    check("a sessionless identity inside the idle window is the same visit",
          same_visit == anon)
    later = sessions._session_for("anon", False, "c", now + 59 + 61)
    check("a sessionless identity after the idle window is a new visit",
          later != anon)


def main() -> int:
    in_process_checks()

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    daemon = launch_daemon(port)
    try:
        if not wait_until_serving(base_url, daemon):
            err = (LOG_DIR / "daemon.err.log").read_text(encoding="utf-8", errors="replace")
            check("the daemon came up", False, err[-2000:])
            return 1
        check("the daemon came up", True, base_url)
        token = (DB_DIR / "daemon.token").read_text(encoding="utf-8").strip()
        asyncio.run(daemon_checks(base_url, token))
    finally:
        stop(daemon)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("consumer_sessions_smoke_test: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
