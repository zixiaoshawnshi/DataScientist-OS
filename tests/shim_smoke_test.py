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
   the producer tools — and the shim's own `DSOS_DB_PATH`, a directory that
   holds a copied `daemon.json`/`daemon.token` and no store, still has no
   `store.db` afterwards. That second half is the load-bearing assertion: it
   is what makes this test fail against the pre-D2 module, which would have
   answered the same tool list by opening a brand new store of its own.
2. `DSOS_URL` + `DSOS_TOKEN` override the discovery files, so a shim pointed
   at a daemon it has no manifest for still works, and still opens nothing.
3. `--profile consumer` reaches the consumer endpoint and answers with
   nothing the producer also has. Deliberately not a count: the consumer
   surface is empty until WP-F1 and has four tools after it, and a hardcoded
   number here would be wrong twice.
4. With no daemon, the shim exits non-zero and names the store and the exact
   command to start one. This is the first thing a new user hits, and the
   message is the entire difference between "broken" and "not started yet".

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
# A second directory, used as the shim's DSOS_DB_PATH in the checks that need
# to prove the shim opened nothing. It carries a copy of the daemon's
# discovery files and no store at all; if the shim builds a Database the way
# the pre-D2 module did, `store.db` appears here and the test says so.
SHIM_DIR = DB_DIR / "client-view"
SHIM_DB_PATH = SHIM_DIR / "store.db"
# A third, for the DSOS_URL check: no manifest, no token file, no store.
URL_ONLY_DIR = DB_DIR / "url-only"
URL_ONLY_DB_PATH = URL_ONLY_DIR / "store.db"

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
    env = child_env(DSOS_DB_PATH=str(DB_PATH), DSOS_PORT=str(port))
    out = open(LOG_DIR / "daemon.out.log", "w", encoding="utf-8")
    err = open(LOG_DIR / "daemon.err.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "dsos.daemon", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=out, stderr=err,
    )


def seed_shim_dir() -> None:
    """Give the shim somewhere to look for a daemon, with no store in it.

    A verbatim copy of the daemon's own discovery files, so the only way the
    shim can serve a tool list is by finding the running daemon — and the
    only way it can *create* a store is by ignoring them.
    """
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("daemon.json", "daemon.token"):
        shutil.copyfile(DB_DIR / name, SHIM_DIR / name)


def stdio_client(profile: str, **env_overrides: str) -> Client:
    """A real MCP client, over stdio, pointed at the shim as a user would."""
    return Client(
        StdioTransport(
            command=sys.executable,
            args=["-m", "dsos.mcp_server", "--profile", profile],
            env=child_env(**{"DSOS_DB_PATH": str(DB_PATH), **env_overrides}),
            cwd=str(REPO_ROOT),
        ),
        timeout=180,
    )


async def list_over_stdio(profile: str, **env_overrides: str) -> list[str]:
    async with stdio_client(profile, **env_overrides) as client:
        tools = await client.list_tools()
    return sorted(tool.name for tool in tools)


def check_discovery_path() -> None:
    """Checks 1 and 3: daemon.json + daemon.token, and the consumer profile."""
    seed_shim_dir()
    env = {"DSOS_DB_PATH": str(SHIM_DB_PATH)}

    producer = asyncio.run(list_over_stdio("producer", **env))
    check(
        "a stdio client through the shim lists the producer tools",
        EXPECTED_PRODUCER_TOOLS.issubset(set(producer)),
        f"{len(producer)} tools: {', '.join(producer)}",
    )
    check(
        "the shim opened no store of its own (daemon.json + daemon.token discovery)",
        not SHIM_DB_PATH.exists(),
        f"looked for {SHIM_DB_PATH}",
    )

    consumer = asyncio.run(list_over_stdio("consumer", **env))
    check(
        "--profile consumer reaches the consumer endpoint, not the producer one",
        not set(consumer) & set(producer),
        f"{len(consumer)} tools: {', '.join(consumer) or 'none'}",
    )


def check_url_override(base_url: str, token: str) -> None:
    """Check 2: DSOS_URL + DSOS_TOKEN, with no discovery files present."""
    URL_ONLY_DIR.mkdir(parents=True, exist_ok=True)
    names = asyncio.run(
        list_over_stdio(
            "producer",
            DSOS_DB_PATH=str(URL_ONLY_DB_PATH),
            DSOS_URL=base_url,
            DSOS_TOKEN=token,
        )
    )
    check(
        "DSOS_URL + DSOS_TOKEN work with no daemon.json beside the store",
        EXPECTED_PRODUCER_TOOLS.issubset(set(names)),
        f"{len(names)} tools: {', '.join(names)}",
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
