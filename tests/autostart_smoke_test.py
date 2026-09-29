"""Autostart smoke test: registering the shim is the whole install.

The shim (`python -m dsos.mcp_server`) used to exit with "daemon not
running" until somebody started `python -m dsos.daemon` by hand and kept it
alive. Now it starts one itself when none is serving its store. This drives
that the way an MCP client does — a stdio client launching the shim as a
subprocess — against stores no daemon has ever served.

Covered:

1. A shim with no daemon behind its store starts one, and the client gets the
   producer tools and a working start_session.
2. The daemon outlives the client that started it (it serves every agent),
   writes its output to `<store>.daemon.log`, and leaves no start lock.
3. A second shim for the same store joins that daemon instead of starting
   another.
4. Two shims starting at the same moment on a fresh store — an agent with a
   producer and a consumer registration — end up behind ONE daemon.
5. No autostart without an explicit DSOS_DB_PATH (it would create a store
   under the client's working directory), and none with DSOS_NO_AUTOSTART=1.
   Neither creates a store.

Run: .venv/Scripts/python.exe tests/autostart_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DB_DIR = REPO_ROOT / "data" / "test-runs" / "autostart_smoke_test"

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

FAILURES: list[str] = []
STARTED_PIDS: list[int] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def shim_env(db_path: Path | None, **overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    env["PYTHONIOENCODING"] = "utf-8"
    for name in ("DSOS_URL", "DSOS_TOKEN", "DSOS_PORT", "DSOS_NO_AUTOSTART", "DSOS_DB_PATH"):
        env.pop(name, None)
    if db_path is not None:
        env["DSOS_DB_PATH"] = str(db_path)
    env.update(overrides)
    return env


def shim_client(db_path: Path, profile: str = "producer") -> Client:
    return Client(
        StdioTransport(command=sys.executable,
                       args=["-m", "dsos.mcp_server", "--profile", profile],
                       env=shim_env(db_path), cwd=str(REPO_ROOT)),
        timeout=180,
    )


def manifest(db_path: Path) -> dict:
    path = db_path.parent / f"{db_path.name}.daemon.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def healthz(base_url: str | None) -> dict | None:
    if not base_url:
        return None
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def remember(db_path: Path) -> None:
    pid = manifest(db_path).get("pid")
    if isinstance(pid, int):
        STARTED_PIDS.append(pid)


def stop_daemons() -> None:
    for pid in STARTED_PIDS:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1.0)


async def tools_and_session(client: Client) -> tuple[set[str], dict]:
    async with client:
        names = {t.name for t in await client.list_tools()}
        session = {}
        if "start_session" in names:
            session = (await client.call_tool(
                "start_session", {"question": "autostart smoke probe"})).data
        return names, session


def check_single_start() -> None:
    store = DB_DIR / "single" / "store.db"
    names, session = asyncio.run(tools_and_session(shim_client(store)))
    remember(store)
    check("a shim with no daemon behind its store serves the producer tools",
          {"start_session", "run_python", "search_artifacts"} <= names, str(sorted(names)))
    check("...and start_session works through the daemon it started",
          bool(session.get("session_id")), str(session)[:300])

    record = manifest(store)
    time.sleep(1.0)  # the client has exited; give a wrongly-attached daemon time to die
    answer = healthz(record.get("base_url"))
    check("the daemon outlives the client that started it",
          bool(answer) and answer.get("db_path") == str(store), str(answer))
    log = store.parent / "store.db.daemon.log"
    check("it writes its output to <store>.daemon.log",
          log.exists() and "started by a dsos shim" in log.read_text(encoding="utf-8"),
          str(log))
    check("no start lock is left behind",
          not (store.parent / "store.db.daemon.starting").exists())

    names, _ = asyncio.run(tools_and_session(shim_client(store, "consumer")))
    check("a second shim for the same store joins that daemon",
          "find_evidence" in names and manifest(store).get("pid") == record.get("pid"),
          f"pid {record.get('pid')} -> {manifest(store).get('pid')}")


def check_concurrent_start() -> None:
    store = DB_DIR / "concurrent" / "store.db"

    async def both():
        return await asyncio.gather(
            tools_and_session(shim_client(store, "producer")),
            tools_and_session(shim_client(store, "consumer")),
        )

    (producer, _), (consumer, _) = asyncio.run(both())
    remember(store)
    check("two shims starting at once on a fresh store both connect",
          "start_session" in producer and "find_evidence" in consumer,
          f"{sorted(producer)} / {sorted(consumer)}")
    log = (store.parent / "store.db.daemon.log").read_text(encoding="utf-8")
    check("...and exactly one of them started a daemon",
          log.count("started by a dsos shim") == 1,
          " | ".join(line for line in log.splitlines()
                     if line.startswith(("---", "dsos:", "Traceback")) or "Error" in line))


def run_shim(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "dsos.mcp_server", "--profile", "producer"],
        cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8",
        stdin=subprocess.DEVNULL, timeout=120,
    )


def check_refusals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        done = run_shim(shim_env(None), cwd)
        check("with no DSOS_DB_PATH the shim does not start a daemon",
              done.returncode != 0 and "DSOS_DB_PATH is not set" in done.stderr,
              f"{done.returncode}: {done.stderr[-300:]}")
        check("...and creates no store under the client's working directory",
              not (cwd / "data").exists())

    store = DB_DIR / "disabled" / "store.db"
    done = run_shim(shim_env(store, DSOS_NO_AUTOSTART="1"), REPO_ROOT)
    check("DSOS_NO_AUTOSTART=1 refuses instead of starting",
          done.returncode != 0 and "automatic start is off" in done.stderr,
          f"{done.returncode}: {done.stderr[-300:]}")
    check("...and creates no store", not store.exists())


def main() -> int:
    # Not ignore_errors: a directory that cannot be cleared is held open by a
    # daemon a previous run failed to stop, and reusing its logs and
    # manifests would make every check below read stale state.
    if DB_DIR.exists():
        shutil.rmtree(DB_DIR)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        check_single_start()
        check_concurrent_start()
        check_refusals()
    finally:
        stop_daemons()
    shutil.rmtree(DB_DIR, ignore_errors=True)
    if FAILURES:
        print(f"\nautostart smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\nautostart smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
