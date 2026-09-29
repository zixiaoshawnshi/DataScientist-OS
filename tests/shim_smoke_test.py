"""Lane-D smoke test: the stdio shim is a proxy, not a second server (WP-D2).

What this is the test of. `python -m dsos.mcp_server` used to *be* the
producer server: a stdio MCP client launched it, it opened its own
`Database` on `DSOS_DB_PATH` and migrated the file, and the GUI opened a
third connection to the same file. WP-D1 moved the store behind one daemon
and left the module as the thing MCP clients actually launch, so the shim's
only remaining job is to find that daemon and forward to it. Getting this
wrong is not subtle — an MCP client pointed at the old code would serve a
store the daemon does not own, and the two would quietly diverge — but it is
easy to get wrong *silently*, because a shim that fails to connect can look
exactly like a server with no tools. So most of what follows is about
proving the tools came from the daemon rather than from this process.

Subprocesses throughout, for the same reason tests/daemon_smoke_test.py is:
an in-process import would skip the entry point entirely, and the entry
point is the thing being changed.

Covered here:

1. With a live daemon, a stdio fastmcp.Client driven through the shim lists
   the producer tools, and a `run_python` call through it succeeds even
   though the *shim's* `DSOS_PYTHON_PATH` names an interpreter that does not
   exist. That second half is the load-bearing assertion: the call can only
   have run in the daemon, whose own `DSOS_PYTHON_PATH` is the one that
   counts — a shim that served the tools itself, the way the pre-D2 module
   did, would have tried the bogus interpreter and failed. It is also the
   claim AGENTS.md makes about where that variable belongs.
2. `DSOS_URL` + `DSOS_TOKEN` override the discovery files, so a shim pointed
   at a daemon it has no manifest for still works — with `DSOS_DB_PATH`
   naming that daemon's store, or with no `DSOS_DB_PATH` at all.
3. `--profile consumer` reaches the consumer endpoint: it serves the
   consumer's evidence tools and nothing the producer also has.
4. With no daemon, the shim exits non-zero and names the store and the exact
   command to start one. This is the first thing a new user hits, and the
   message is the entire difference between "broken" and "not started yet".
5. A daemon that answers, but for a *different* store — a copied
   manifest (`store.db.daemon.json`), or a `DSOS_URL` left pointing at another store's daemon —
   is not a daemon for this one. The shim exits non-zero naming both stores,
   and opens nothing of its own on the way out. (PR #15 review: before this,
   /healthz answering was the whole check, and a shim would happily proxy a
   client to the wrong store.)

On the tool list: assertions name tools that have existed since WP-B2R and
are load-bearing for every lane since, never a count.

Run: .venv/Scripts/python.exe tests/shim_smoke_test.py
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

# The shim's FastMCP banner goes to the inherited stderr, and its box-drawing
# characters are outside the default Windows console codepage. Print them
# anyway rather than letting an assertion's detail die on a UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DB_DIR = REPO_ROOT / "data" / "test-runs" / "shim_smoke_test"
DB_PATH = DB_DIR / "store.db"
# A second directory, holding a verbatim copy of the daemon's discovery files
# and no store at all: a manifest that points at a live daemon serving
# *another* store. The shim must refuse it — and if it builds a Database on
# the way out, the way the pre-D2 module did, `store.db` appears here and the
# test says so.
SHIM_DIR = DB_DIR / "client-view"
SHIM_DB_PATH = SHIM_DIR / "store.db"
# A third, for the DSOS_URL mismatch: no manifest, no token file, no store.
URL_ONLY_DIR = DB_DIR / "url-only"
URL_ONLY_DB_PATH = URL_ONLY_DIR / "store.db"
# The shim's DSOS_PYTHON_PATH in check 1: an interpreter that does not exist.
BOGUS_PYTHON = DB_DIR / "no-such-python" / "python.exe"

os.environ["DSOS_DB_PATH"] = str(DB_PATH)
shutil.rmtree(DB_DIR, ignore_errors=True)
DB_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DB_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

FAILURES: list[str] = []

# Tools that have been on the producer surface since WP-B2R and that every
# later lane builds on. Asserting these rather than a set of names or a count
# keeps the test honest while WP-E2 and WP-F1 are still moving the surface.
EXPECTED_PRODUCER_TOOLS = {"start_session", "search_artifacts", "get_artifact", "save_artifact"}
# The consumer's four (D6, WP-F1).
EXPECTED_CONSUMER_TOOLS = {"find_evidence", "get_claim", "cite", "ask"}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def child_env(**overrides: str) -> dict[str, str]:
    """The environment a shim or daemon subprocess is launched with.

    PYTHONPATH is the belt to cwd's braces: the child has to import *this*
    worktree's dsos, not whatever an editable install happens to point at.
    """
    env = dict(os.environ)
    inherited = [p for p in [env.get("PYTHONPATH", "")] if p]
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), *inherited])
    env["PYTHONIOENCODING"] = "utf-8"
    # A shim must not inherit these: DSOS_URL would short-circuit the
    # discovery files the first check is about.
    env.pop("DSOS_URL", None)
    env.pop("DSOS_TOKEN", None)
    env.update(overrides)
    return env


def healthz(base_url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_until_serving(base_url: str, proc: subprocess.Popen, seconds: float = 120.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if healthz(base_url) is not None:
            return True
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


def launch_daemon(port: int) -> subprocess.Popen:
    # The daemon's DSOS_PYTHON_PATH is a real interpreter; check 1 gives the
    # shim a bogus one and relies on the daemon's being the one that is used.
    env = child_env(
        DSOS_DB_PATH=str(DB_PATH), DSOS_PORT=str(port), DSOS_PYTHON_PATH=sys.executable,
    )
    out = open(LOG_DIR / "daemon.out.log", "w", encoding="utf-8")
    err = open(LOG_DIR / "daemon.err.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "dsos.daemon", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=out, stderr=err,
    )


def seed_shim_dir() -> None:
    """A directory whose discovery files point at a live daemon for another store.

    A verbatim copy of the daemon's own manifest (store.db.daemon.json) and daemon.token, and no
    store: the manifest is real and its daemon answers, but that daemon is
    serving DB_PATH, not SHIM_DB_PATH.
    """
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("store.db.daemon.json", "daemon.token"):
        shutil.copyfile(DB_DIR / name, SHIM_DIR / name)


def stdio_client(profile: str, unset: tuple[str, ...] = (), **env_overrides: str) -> Client:
    """A real MCP client, over stdio, pointed at the shim as a user would."""
    env = child_env(**{"DSOS_DB_PATH": str(DB_PATH), **env_overrides})
    for name in unset:
        env.pop(name, None)
    return Client(
        StdioTransport(
            command=sys.executable,
            args=["-m", "dsos.mcp_server", "--profile", profile],
            env=env,
            cwd=str(REPO_ROOT),
        ),
        timeout=180,
    )


async def list_over_stdio(
    profile: str, unset: tuple[str, ...] = (), **env_overrides: str
) -> list[str]:
    async with stdio_client(profile, unset, **env_overrides) as client:
        tools = await client.list_tools()
    return sorted(tool.name for tool in tools)


async def run_python_over_stdio(**env_overrides: str) -> dict:
    """One start_session and one scratch run_python, through the shim."""
    async with stdio_client("producer", **env_overrides) as client:
        started = await client.call_tool("start_session", {"question": "shim probe"})
        session_id = started.data["session_id"]
        outcome = await client.call_tool("run_python", {
            "code": "import sys\nresult = sys.executable",
            "session_id": session_id, "title": "which interpreter",
            "description": "the interpreter run_python used", "input_row_ids": [],
            "scratch": True,
        })
    return outcome.data


def check_discovery_path() -> None:
    """Checks 1 and 3: the manifest + daemon.token, and the consumer profile."""
    env = {"DSOS_DB_PATH": str(DB_PATH), "DSOS_PYTHON_PATH": str(BOGUS_PYTHON)}

    producer = asyncio.run(list_over_stdio("producer", **env))
    check(
        "a stdio client through the shim lists the producer tools",
        EXPECTED_PRODUCER_TOOLS.issubset(set(producer)),
        f"{len(producer)} tools: {', '.join(producer)}",
    )
    ran = asyncio.run(run_python_over_stdio(**env))
    check(
        "run_python through the shim runs in the daemon, ignoring the shim's "
        "DSOS_PYTHON_PATH",
        ran.get("status") == "ok",
        json.dumps(ran)[:400],
    )

    consumer = asyncio.run(list_over_stdio("consumer", **env))
    check(
        "--profile consumer reaches the consumer endpoint and its evidence tools",
        EXPECTED_CONSUMER_TOOLS.issubset(set(consumer)),
        f"{len(consumer)} tools: {', '.join(consumer) or 'none'}",
    )
    check(
        "--profile consumer serves none of the producer's tools",
        not set(consumer) & set(producer),
        f"{sorted(set(consumer) & set(producer))}",
    )


def check_url_override(base_url: str, token: str) -> None:
    """Check 2: DSOS_URL + DSOS_TOKEN, with no discovery files present.

    The files are moved aside rather than pointed around: the shim has to be
    given the daemon's own store, or the db_path check (check 5) would refuse
    it. The daemon read its token at start-up and reads its manifest only at
    the next start-up, so neither misses them while they are away.
    """
    moved = []
    for name in ("store.db.daemon.json", "daemon.token"):
        aside = DB_DIR / f"{name}.aside"
        (DB_DIR / name).replace(aside)
        moved.append((aside, DB_DIR / name))
    try:
        names = asyncio.run(
            list_over_stdio(
                "producer",
                DSOS_DB_PATH=str(DB_PATH),
                DSOS_URL=base_url,
                DSOS_TOKEN=token,
            )
        )
        # DSOS_URL alone names no store, so there is nothing to mismatch: the
        # shim takes the URL at its word rather than comparing the daemon's
        # store with a cwd-relative default nobody asked for.
        unnamed = asyncio.run(
            list_over_stdio(
                "producer", ("DSOS_DB_PATH",), DSOS_URL=base_url, DSOS_TOKEN=token,
            )
        )
    finally:
        for aside, source in moved:
            aside.replace(source)
    check(
        "DSOS_URL + DSOS_TOKEN work with no manifest beside the store",
        EXPECTED_PRODUCER_TOOLS.issubset(set(names)),
        f"{len(names)} tools: {', '.join(names)}",
    )
    check(
        "DSOS_URL + DSOS_TOKEN with no DSOS_DB_PATH at all still work",
        EXPECTED_PRODUCER_TOOLS.issubset(set(unnamed)),
        f"{len(unnamed)} tools: {', '.join(unnamed)}",
    )


def run_shim(**env_overrides: str) -> subprocess.CompletedProcess:
    """The shim as a subprocess that is expected to refuse, not to serve.

    stdin is closed, so a shim that wrongly decides to serve sees EOF and
    exits instead of hanging the test; the timeout is the second belt.
    """
    return subprocess.run(
        [sys.executable, "-m", "dsos.mcp_server", "--profile", "producer"],
        cwd=str(REPO_ROOT), env=child_env(**env_overrides), capture_output=True,
        text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120,
    )


def check_other_store(base_url: str, token: str) -> None:
    """Check 5: a daemon that answers for a different store is not this store's."""
    seed_shim_dir()
    done = run_shim(DSOS_DB_PATH=str(SHIM_DB_PATH))
    check(
        "a manifest naming a daemon for another store: the shim exits non-zero",
        done.returncode != 0,
        f"returncode {done.returncode}",
    )
    check(
        "the refusal names this store and the one that daemon is serving",
        f"dsos daemon not running for {SHIM_DB_PATH.resolve()}" in done.stderr
        and str(DB_PATH) in done.stderr,
        f"stderr: {done.stderr.strip()[-600:]!r}",
    )
    check(
        "the shim opened no store of its own on the way out",
        not SHIM_DB_PATH.exists(),
        f"looked for {SHIM_DB_PATH}",
    )

    URL_ONLY_DIR.mkdir(parents=True, exist_ok=True)
    done = run_shim(DSOS_DB_PATH=str(URL_ONLY_DB_PATH), DSOS_URL=base_url, DSOS_TOKEN=token)
    check(
        "a DSOS_URL pointing at another store's daemon: the shim exits non-zero",
        done.returncode != 0,
        f"returncode {done.returncode}",
    )
    check(
        "the refusal names DSOS_URL, this store, and the store that daemon serves",
        f"dsos daemon not running for {URL_ONLY_DB_PATH.resolve()}" in done.stderr
        and str(DB_PATH) in done.stderr and "DSOS_URL" in done.stderr,
        f"stderr: {done.stderr.strip()[-600:]!r}",
    )
    check(
        "the URL-only shim opened no store either",
        not URL_ONLY_DB_PATH.exists(),
        f"looked for {URL_ONLY_DB_PATH}",
    )


def check_no_daemon() -> None:
    """Check 4: no daemon behind DSOS_DB_PATH, and what the shim says about it."""
    missing_dir = DB_DIR / "no-daemon"
    missing_dir.mkdir(parents=True, exist_ok=True)
    missing_db = missing_dir / "store.db"
    env = child_env(DSOS_DB_PATH=str(missing_db))
    env.pop("DSOS_URL", None)
    env.pop("DSOS_TOKEN", None)
    done = subprocess.run(
        [sys.executable, "-m", "dsos.mcp_server", "--profile", "producer"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, encoding="utf-8",
    )
    expected = (
        f"dsos daemon not running for {missing_db.resolve()}. "
        f"Start it: {sys.executable} -m dsos.daemon"
    )
    check(
        "with no daemon the shim exits non-zero",
        done.returncode != 0,
        f"returncode {done.returncode}",
    )
    check(
        "the failure names the store and the exact command to start one",
        expected in done.stderr,
        f"stderr: {done.stderr.strip()!r}",
    )
    check(
        "the no-daemon path did not create a store on its way out",
        not missing_db.exists(),
        f"looked for {missing_db}",
    )


def main() -> int:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    daemon = launch_daemon(port)
    try:
        if not wait_until_serving(base_url, daemon):
            err = LOG_DIR / "daemon.err.log"
            check("the daemon came up", False, err.read_text(encoding="utf-8")[-2000:])
            return 1
        check("the daemon came up", True, base_url)
        check_discovery_path()
        token = (DB_DIR / "daemon.token").read_text(encoding="utf-8").strip()
        check_url_override(base_url, token)
        check_other_store(base_url, token)
    finally:
        stop(daemon)
    check_no_daemon()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("shim_smoke_test: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
