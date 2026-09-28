"""Lane-D smoke test: one daemon owns the store (dsos/daemon.py, WP-D1).

What this is the test of. Before the daemon, the store was owned by whoever
happened to open it: an MCP server process with a module-global connection,
and a GUI process with a second one against the same SQLite file. Two
connections to one file is the thing decision D8 exists to stop, and the
thing that breaks the moment two agents write at once. The daemon is the
answer — one process, one `Database`, one FastAPI app serving the GUI and
both MCP profiles — and this test drives the whole thing the way a real
client would, over HTTP, from outside the process.

It is a *subprocess* test on purpose. Testing the app in-process (TestClient,
like tests/gui_smoke_test.py) would pass even if the entry point, the token
file, daemon.json or the loopback guard were all broken, because none of
those run when you hand an app to a test client. Starting it the way a user
starts it is the only way to cover them.

Covered here:

1. An MCP request with no Authorization header is rejected with 401, and the
   same request with the token from `<db dir>/daemon.token` is not. That also
   proves the token file is where the spec says it is.
2. An authenticated fastmcp.Client over HTTP lists the ten producer tools at
   /mcp/producer and zero tools at /mcp/consumer (WP-F1 adds four).
3. Two concurrent authenticated clients doing 20 save_artifact calls each
   produce 40 distinct rows, 40 FTS rows, and no error anywhere — see the
   note on serialisation below.
4. GET / through the daemon returns 200 and shows the sessions the MCP
   clients just created, which is the point of the daemon serving the GUI:
   one store, one reader, no second process.
5. A second daemon over the same store refuses to start and says which
   daemon is running, rather than silently binding a second writer to the
   same file.

Plus the spec lines that cost one process launch each: DSOS_PORT as an
alternative to --port, a stale daemon.json not locking the next daemon out,
and the refusal of a non-loopback --host.

On serialisation (decision D13). Check 3 asserts the row count *and* that no
writer saw a lock error, and it is worth being precise about which of those
carries the weight: `busy_timeout=5000` absorbs ordinary contention, but
save_artifact's dedupe-and-insert is a read-then-write, and a read-then-write
under contention gets SQLITE_BUSY_SNAPSHOT back immediately, with no
busy-handler consultation. So "40 rows, no lock error, under two concurrent
MCP clients" fails the moment the daemon's write lock stops being held. The
*non-overlap* and duty-cycle assertions D13 asks for are not reproducible
from outside the daemon — a client's view of a call includes HTTP and MCP
overhead, so two client-side intervals overlapping proves nothing either way
— and they are asserted in-process, against the same `Database.write()` the
daemon's tools use, in tests/database_concurrency_smoke_test.py.

Run: .venv/Scripts/python.exe tests/daemon_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Absolute, unlike most tests' repo-relative DSOS_DB_PATH: this one hands the
# path to a *subprocess*, which does not inherit the runner's cwd. The
# directory name still follows the convention, so a stray store left behind by
# a crashed run is still recognisable.
DB_DIR = REPO_ROOT / "data" / "test-runs" / "daemon_smoke_test"
DB_PATH = DB_DIR / "store.db"
os.environ["DSOS_DB_PATH"] = str(DB_PATH)
shutil.rmtree(DB_DIR, ignore_errors=True)
DB_DIR.mkdir(parents=True, exist_ok=True)

import httpx  # noqa: E402
from fastmcp import Client  # noqa: E402

from dsos.db import connect  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []

# The producer surface as of WP-C1/B2R. Duplicated rather than imported: the
# point of the check is that the *daemon* serves exactly this set, so a change
# to the server package has to be made deliberately here too.
EXPECTED_PRODUCER_TOOLS = {
    "start_session", "search_artifacts", "get_artifact", "save_artifact",
    "run_sql", "run_python", "get_lineage", "mark", "list_templates", "save_template",
    "close_question", "record_decision",
}

CLIENTS = 2
SAVES_PER_CLIENT = 20
EXPECTED_ROWS = CLIENTS * SAVES_PER_CLIENT

# The daemon logs to a file rather than a pipe. A pipe nobody is draining
# fills and blocks the child, which on a multi-second test reads as a daemon
# that hangs rather than a test that filled a buffer.
LOG_DIR = DB_DIR / "logs"

# The MCP endpoint is mounted at /mcp/producer, and the mounted app's own route
# is "/", so the addressable URL carries the trailing slash. Without one
# Starlette answers a 307 redirect first, which is fine for a browser and a
# nuisance for a client that does not follow redirects.
INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "dsos-daemon-smoke-test", "version": "1"},
    },
}
INIT_HEADERS = {
    "Content-Type": "application/json",
    # The streamable-HTTP transport requires both media types on a response.
    "Accept": "application/json, text/event-stream",
}

DAEMON_URL = ""
DAEMON_TOKEN = ""


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def free_port() -> int:
    """An ephemeral port, released before the daemon is asked to bind it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def launch(tag: str, args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start `python -m dsos.daemon` the way a user would."""
    env = dict(os.environ)
    env["DSOS_DB_PATH"] = str(DB_PATH)
    # The child must import *this* worktree's dsos, not whatever an editable
    # install happens to point at. PYTHONPATH is the belt to cwd's braces
    # here: the child is launched with cwd=REPO_ROOT as well.
    inherited = [p for p in [env.get("PYTHONPATH", "")] if p]
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), *inherited])
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / f"{tag}.out.log", "w", encoding="utf-8")
    err = open(LOG_DIR / f"{tag}.err.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "dsos.daemon", *args],
        cwd=str(REPO_ROOT), env=env, stdout=out, stderr=err,
    )


def read_log(tag: str, stream: str = "err") -> str:
    path = LOG_DIR / f"{tag}.{stream}.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def healthz(base_url: str, timeout: float = 2.0) -> dict | None:
    """The daemon's own liveness answer, or None if it did not come back."""
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_until_serving(base_url: str, proc: subprocess.Popen, seconds: float = 120.0) -> dict | None:
    """Poll /healthz until it answers, or the child dies trying.

    The first request after start can be slow: the daemon opens the store
    (migrating it) before it binds, and the first write rather than this one
    pays for loading the embedding model.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        answer = healthz(base_url)
        if answer is not None:
            return answer
        time.sleep(0.25)
    return None


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


async def writer_task(url: str, token: str, index: int) -> list[dict]:
    """One client of the concurrent pair: open a session, then save."""
    async with Client(url, auth=token, timeout=300) as client:
        started = await client.call_tool("start_session", {"question": f"daemon probe {index}"})
        session_id = started.data["session_id"]
        results = []
        for i in range(SAVES_PER_CLIENT):
            outcome = await client.call_tool(
                "save_artifact",
                {
                    "type": "narrative",
                    "title": f"daemon probe {index}-{i}",
                    "description": "A note written concurrently through the daemon.",
                    "content_format": "markdown",
                    # Distinct content on every call: dedupe collapses identical
                    # writes by design, and 40 rows is the point of the count.
                    "content_text": f"# probe {index}-{i}\n\nwritten by client {index}",
                    "session_id": session_id,
                },
            )
            results.append(outcome.data)
        return results


async def mcp_scenario() -> list[dict]:
    """Checks 2 and 3 against one live daemon: the tool lists, then the two
    concurrent writers."""
    producer_url = DAEMON_URL + "/mcp/producer/"
    consumer_url = DAEMON_URL + "/mcp/consumer/"

    async with Client(producer_url, auth=DAEMON_TOKEN, timeout=180) as producer:
        tools = await producer.list_tools()
    names = sorted(t.name for t in tools)
    check(
        "an authenticated HTTP client lists the 12 producer tools at /mcp/producer",
        set(names) == EXPECTED_PRODUCER_TOOLS,
        f"{len(names)} tools: {', '.join(names)}",
    )

    async with Client(consumer_url, auth=DAEMON_TOKEN, timeout=180) as consumer:
        consumer_tools = await consumer.list_tools()
    check(
        "the consumer profile is mounted and has no tools until WP-F1",
        consumer_tools == [],
        f"{len(consumer_tools)} tools: {', '.join(sorted(t.name for t in consumer_tools))}",
    )

    began = time.perf_counter()
    batches = await asyncio.gather(
        *[writer_task(producer_url, DAEMON_TOKEN, i) for i in range(CLIENTS)]
    )
    elapsed = time.perf_counter() - began
    print(f"       ({sum(len(b) for b in batches)} concurrent saves in {elapsed:.1f}s)")
    return [result for batch in batches for result in batch]


def check_concurrent_saves(results: list[dict]) -> None:
    errors = [r for r in results if "error" in r]
    row_ids = [r.get("row_id") for r in results if r.get("row_id")]
    check(f"{EXPECTED_ROWS} save_artifact calls returned no error", not errors,
          json.dumps(errors[:2]) if errors else "")
    check(f"{EXPECTED_ROWS} distinct row_ids came back",
          len(row_ids) == EXPECTED_ROWS and len(set(row_ids)) == EXPECTED_ROWS,
          f"{len(set(row_ids))} distinct of {len(row_ids)}")

    # The assertion that carries the weight, for the reason in the module
    # docstring: save_artifact's dedupe lookup and its INSERT are one
    # read-then-write, and busy_timeout cannot mask a read-then-write losing
    # its snapshot. A daemon that stopped holding the write lock fails here.
    dump = " ".join(json.dumps(r) for r in results).lower()
    check("no writer hit a SQLite lock/busy error",
          "locked" not in dump and "busy" not in dump, "")

    conn = connect(DB_PATH)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM artifacts_fts").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()
    check(f"{EXPECTED_ROWS} artifact rows in the store", rows == EXPECTED_ROWS, str(rows))
    check(f"{EXPECTED_ROWS} FTS rows in the store", fts == EXPECTED_ROWS, str(fts))
    check("both clients' sessions were recorded", sessions >= CLIENTS, str(sessions))


def check_lifespan_composition() -> None:
    """The composed lifespan, entered and left by a host that runs it.

    This is the fragile part of the daemon and the part a subprocess on
    Windows cannot reach: Popen.terminate() is TerminateProcess, so nothing
    in a killed daemon ever gets to run its shutdown, and the manifest
    removal that hangs off the lifespan is untested there. TestClient does
    run a lifespan — startup on enter, shutdown on exit — so this covers the
    two claims the subprocess test cannot make: both MCP apps' lifespans are
    entered and exited without raising, and the manifest written at start is
    gone once the daemon has shut down.

    In-process, on its own store, so it cannot disturb the daemon the other
    checks share.
    """
    from fastapi.testclient import TestClient

    from dsos.daemon import build_app, resolve_token, write_manifest
    from dsos.db import Database
    from dsos.server import ServerConfig

    own_dir = DB_DIR / "lifespan"
    own_dir.mkdir(parents=True, exist_ok=True)
    own_store = own_dir / "store.db"
    config = ServerConfig(db=Database(own_store), python_path=sys.executable, base_url=None)
    app = build_app(config, own_store, own_dir)
    token = resolve_token(own_dir)
    # main() writes the manifest just before handing the app to uvicorn, which
    # is outside anything this in-process check can reach, so it is written
    # here with the same function: the removal below then has a real file to
    # remove, and would pass vacuously without one.
    write_manifest(own_dir, 8765, "http://127.0.0.1:8765", own_store)

    with TestClient(app) as client:
        check("a manifest written at start is present while the daemon is running",
              (own_dir / "daemon.json").exists(), "")
        response = client.post(
            "/mcp/producer/", json=INIT_BODY,
            headers={**INIT_HEADERS, "Authorization": f"Bearer {token}"},
        )
        check("a mounted MCP app serves once its lifespan has been entered",
              response.status_code == 200, f"got {response.status_code}")
        check("an unauthenticated request is still refused with the lifespan running",
              client.post("/mcp/producer/", json=INIT_BODY, headers=INIT_HEADERS).status_code == 401,
              "")

    check("the lifespan removes daemon.json when the daemon shuts down",
          not (own_dir / "daemon.json").exists(), "")


def main() -> int:
    global DAEMON_URL, DAEMON_TOKEN

    port = free_port()
    DAEMON_URL = f"http://127.0.0.1:{port}"
    manifest = DB_DIR / "daemon.json"

    proc = launch("daemon", ["--host", "127.0.0.1", "--port", str(port)])
    answer = wait_until_serving(DAEMON_URL, proc)
    if answer is None:
        print(f"[FAIL] the daemon never answered /healthz on {DAEMON_URL}")
        print("--- daemon stderr ---")
        print(read_log("daemon"))
        stop(proc)
        return 1

    check("the daemon answers /healthz", answer.get("ok") is True, json.dumps(answer))
    check("/healthz reports the store it opened", answer.get("db_path") == str(DB_PATH),
          str(answer.get("db_path")))

    record = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    # The pid is checked as "a plausible pid", not as Popen's: on a venv whose
    # python.exe is a launcher shim, Popen.pid is the shim's and the manifest's
    # is the interpreter's. What this test can say about the recorded pid is
    # that a later daemon must defer to it (check 5, below) and that a fresh
    # daemon replaces it.
    check("daemon.json records pid, port, base_url, db_path and started_at",
          isinstance(record.get("pid"), int) and record["pid"] > 0
          and record.get("port") == port
          and record.get("base_url") == DAEMON_URL
          and record.get("db_path") == str(DB_PATH)
          and bool(record.get("started_at")),
          json.dumps(record))

    token_file = DB_DIR / "daemon.token"
    check("the daemon wrote <db dir>/daemon.token", token_file.exists(), str(token_file))
    DAEMON_TOKEN = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    check("the token is long enough to be a real secret", len(DAEMON_TOKEN) >= 32,
          f"{len(DAEMON_TOKEN)} chars")

    # (1) No token -> 401. With the token from the file -> not 401.
    anonymous = httpx.post(f"{DAEMON_URL}/mcp/producer/", json=INIT_BODY, headers=INIT_HEADERS)
    check("an MCP request with no Authorization header is 401",
          anonymous.status_code == 401, f"got {anonymous.status_code}")
    authenticated = httpx.post(
        f"{DAEMON_URL}/mcp/producer/", json=INIT_BODY,
        headers={**INIT_HEADERS, "Authorization": f"Bearer {DAEMON_TOKEN}"},
    )
    check("the same request with the daemon.token bearer is accepted",
          authenticated.status_code == 200, f"got {authenticated.status_code}")
    consumer_anon = httpx.post(f"{DAEMON_URL}/mcp/consumer/", json=INIT_BODY, headers=INIT_HEADERS)
    check("the consumer endpoint requires the token too",
          consumer_anon.status_code == 401, f"got {consumer_anon.status_code}")

    # (2) + (3)
    check_concurrent_saves(asyncio.run(mcp_scenario()))

    # (4) The GUI, served by the same process over the same store.
    page = httpx.get(f"{DAEMON_URL}/", follow_redirects=True)
    check("GET / through the daemon is 200", page.status_code == 200,
          f"got {page.status_code}")
    check("the GUI shows the sessions the MCP clients created",
          "daemon probe 0" in page.text, "")

    # (5) A second daemon over the same store must refuse. The first daemon is
    # provably live here: this test started it and has just been talking to it
    # over HTTP, so the refusal cannot be a stale file this test left behind.
    second = launch("daemon-2", ["--host", "127.0.0.1", "--port", str(free_port())])
    try:
        second.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        second.kill()
        second.communicate()
        check("a second daemon over the same store refuses to start", False,
              "it kept running instead of refusing")
    else:
        message = read_log("daemon-2")
        check("a second daemon over the same store exits non-zero",
              second.returncode != 0, f"exit code {second.returncode}")
        check("the refusal names the first daemon's pid and port",
              str(record.get("pid")) in message and str(port) in message,
              message.strip()[:300])
    check("the first daemon is still serving after the refusal",
          healthz(DAEMON_URL) is not None, "")

    # --- tear the first daemon down, and look at what it leaves behind ---
    stop(proc)
    if os.name == "posix":
        # SIGTERM is a signal only on POSIX. On Windows Popen.terminate() is
        # TerminateProcess, which no atexit hook, signal handler or lifespan
        # finaliser ever gets to run, so the clean-shutdown path can only be
        # asserted where it can actually be exercised.
        deadline = time.time() + 20
        while manifest.exists() and time.time() < deadline:
            time.sleep(0.25)
        check("daemon.json is removed on a clean shutdown", not manifest.exists(),
              str(manifest))
    else:
        print("[skip] clean-shutdown removal of daemon.json over a signal — no POSIX "
              "SIGTERM here (covered in-process by the lifespan check below)")

    # A daemon.json left behind by a hard kill must not lock the next one out:
    # its pid is gone, so the file is stale and gets overwritten. Same store,
    # same port, which is the harder half of that claim.
    revived = launch("daemon-3", ["--host", "127.0.0.1", "--port", str(port)])
    check("a daemon.json left by a dead daemon is stale, not a lockout",
          wait_until_serving(DAEMON_URL, revived) is not None, read_log("daemon-3")[-400:])
    if manifest.exists():
        fresh = json.loads(manifest.read_text(encoding="utf-8"))
        check("the restarted daemon rewrote daemon.json with its own pid",
              fresh.get("pid") != record.get("pid") and isinstance(fresh.get("pid"), int),
              f"pid {record.get('pid')} -> {fresh.get('pid')}")
    stop(revived)

    # DSOS_PORT as an alternative to --port. Needs the store to itself.
    env_port = free_port()
    env_url = f"http://127.0.0.1:{env_port}"
    from_env = launch("daemon-4", [], {"DSOS_PORT": str(env_port)})
    check("DSOS_PORT sets the port when --port is not given",
          wait_until_serving(env_url, from_env) is not None, read_log("daemon-4")[-400:])
    if manifest.exists():
        check("a daemon started from DSOS_PORT records that port in daemon.json",
              json.loads(manifest.read_text(encoding="utf-8")).get("port") == env_port, "")
    stop(from_env)

    # The loopback guard, last: it is a refusal, so it needs neither a store
    # nor a free port, and it must not have bound anything.
    refused = launch("daemon-5", ["--host", "0.0.0.0", "--port", str(free_port())])
    try:
        refused.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        refused.kill()
        refused.communicate()
    message = read_log("daemon-5")
    check("a non-loopback --host is refused", refused.returncode != 0,
          f"exit code {refused.returncode}")
    check("the refusal says remote access is not supported yet",
          "remote access is not supported yet" in message, message.strip()[:300])

    check_lifespan_composition()

    shutil.rmtree(DB_DIR, ignore_errors=True)
    if FAILURES:
        print(f"\ndaemon smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\ndaemon smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
