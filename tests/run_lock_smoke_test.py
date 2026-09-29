"""Layer-2 smoke test: a long run does not hold the store's write lock.

run_sql and run_python used to wrap the WHOLE execution — the DuckDB query,
or the Python subprocess, up to DSOS_SANDBOX_TIMEOUT — in `db.write()`,
which is the process-wide write lock (D8). ToolCallLogger takes that same
lock after every tool call to log it, and it does so on the event loop. So
one agent's slow run_python stalled every other client's calls, read-only
ones included: measured on the daemon, an 8s run made another client's
search_artifacts take 10.5s.

The lock exists for one read-then-write — store.save_artifact's version
read (D13) — and that lives only in the persist tail. So the tail is the
only part that holds it now, and this test pins that:

1. While client A's run_python sits in its subprocess for ~4s, client B's
   writers (start_session, save_artifact) and a read (search_artifacts)
   each come back in well under that. A marker file the run writes before
   sleeping is what says "A is inside the subprocess now", so the timing is
   not at the mercy of how long the interpreter takes to start.
2. The run itself still succeeds and persists the way it always did: an
   artifact, an executions row pointing at it, lineage to the input.
3. A lifecycle status a writer may not pass ("superseded" — only `mark`
   gets a row there — or a typo) is refused BEFORE any code runs: the
   response names the allowed values, the code's marker file never
   appears, and no executions row is written. It used to run the code and
   then fail in save_artifact with an uncaught ValueError.

Run: .venv/Scripts/python.exe tests/run_lock_smoke_test.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

DB_PATH = "data/test-runs/run_lock_smoke_test/store.db"
os.environ["DSOS_DB_PATH"] = DB_PATH
shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.db import Database  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.server import ServerConfig, build_producer  # noqa: E402

# How long the slow run sleeps, and how long a concurrent call may take.
# The budget is generous for a loaded CI box and still far below the sleep:
# a call that waited on the run's lock takes the rest of the sleep, not a
# fraction of a second more than it should.
RUN_SLEEP = 4.0
CALL_BUDGET = 1.5

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def count(conn, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


async def wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.05)
    return False


async def timed(coro) -> tuple[object, float]:
    t0 = time.monotonic()
    result = await coro
    return result, time.monotonic() - t0


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    csv = tmp / "scores.csv"
    csv.write_text("team,score\nA,10\nB,20\n", encoding="utf-8")
    started = tmp / "started.flag"
    superseded_marker = tmp / "superseded_ran.flag"

    handle = Database(DB_PATH)
    config = ServerConfig(db=handle, python_path=sys.executable, base_url=None)
    producer = build_producer(config)
    conn = handle.conn()

    async with Client(producer) as a, Client(producer) as b:
        sa = (await a.call_tool("start_session", {"question": "slow run"})).data["session_id"]
        ds = (await a.call_tool("save_artifact", {
            "type": "dataset", "title": "Team scores", "description": "score per team",
            "content_path": str(csv), "content_format": "csv", "session_id": sa,
        })).data["row_id"]

        # --- 1. a slow run does not block another client
        code = (
            "import pathlib, time\n"
            f"pathlib.Path({str(started)!r}).write_text('x', encoding='utf-8')\n"
            f"time.sleep({RUN_SLEEP})\n"
            "result = in_1.assign(score2=in_1.score * 2)\n"
        )
        slow = asyncio.create_task(timed(a.call_tool("run_python", {
            "code": code, "session_id": sa, "title": "Doubled scores",
            "description": "score doubled, slowly", "input_row_ids": [ds],
        })))

        in_run = await wait_for(started, timeout=60)
        check("the slow run reached its subprocess", in_run)

        res, t_start = await timed(b.call_tool("start_session", {"question": "meanwhile"}))
        sb = res.data["session_id"]
        check("start_session is not blocked by another client's run",
              t_start < CALL_BUDGET, f"{t_start:.2f}s")

        res, t_save = await timed(b.call_tool("save_artifact", {
            "type": "narrative", "title": "Note", "description": "a note written mid-run",
            "content_text": "hello", "content_format": "markdown", "session_id": sb,
        }))
        check("save_artifact is not blocked by another client's run",
              t_save < CALL_BUDGET and "row_id" in res.data, f"{t_save:.2f}s")

        _, t_search = await timed(b.call_tool("search_artifacts", {
            "query": "scores", "session_id": sb}))
        check("search_artifacts is not blocked by another client's run",
              t_search < CALL_BUDGET, f"{t_search:.2f}s")

        check("the concurrent calls finished while the run was still going",
              not slow.done())

        # --- 2. the run still persists as it always did
        res, t_slow = await slow
        run = res.data
        check("the slow run succeeded", run.get("status") == "ok", str(run.get("error")))
        row_id = run.get("row_id")
        check("the slow run saved an artifact",
              row_id is not None
              and count(conn, "SELECT COUNT(*) FROM artifacts WHERE row_id = ?", (row_id,)) == 1)
        check("the slow run recorded an execution against it",
              count(conn, "SELECT COUNT(*) FROM executions WHERE output_row_id = ? "
                          "AND status = 'ok'", (row_id,)) == 1)
        check("the slow run lineage-links its input",
              count(conn, "SELECT COUNT(*) FROM lineage WHERE child_row_id = ? "
                          "AND parent_row_id = ?", (row_id, ds)) == 1)
        check("the slow run's row is exploratory by default",
              run.get("artifact_status") == "exploratory", str(run.get("artifact_status")))

        # --- 3. a status a writer may not pass is refused before the code runs
        executions_before = count(conn, "SELECT COUNT(*) FROM executions")
        artifacts_before = count(conn, "SELECT COUNT(*) FROM artifacts")
        marker_code = (
            "import pathlib\n"
            f"pathlib.Path({str(superseded_marker)!r}).write_text('x', encoding='utf-8')\n"
            "result = in_1\n"
        )
        res = await a.call_tool("run_python", {
            "code": marker_code, "session_id": sa, "title": "Should not run",
            "description": "superseded is mark's job", "input_row_ids": [ds],
            "status": "superseded",
        }, raise_on_error=False)
        payload = res.structured_content or {}
        check("run_python(status='superseded') is an error response, not a crash",
              not res.is_error and "error" in payload, str(res.content)[:200])
        err = payload.get("error", "")
        check("the error names the allowed statuses",
              "exploratory" in err and "result" in err, err)
        check("the error response carries no artifact",
              payload.get("artifact_row_ids") == [], str(payload.get("artifact_row_ids")))
        check("run_python(status='superseded') never ran the code",
              not superseded_marker.exists())

        res = await a.call_tool("run_sql", {
            "code": "SELECT * FROM in_1", "session_id": sa, "title": "Should not run",
            "description": "typo'd status", "input_row_ids": [ds], "status": "resutl",
        }, raise_on_error=False)
        payload = res.structured_content or {}
        check("run_sql with an unknown status is an error response naming the allowed values",
              not res.is_error and "exploratory" in payload.get("error", "")
              and "result" in payload.get("error", ""), str(res.content)[:200])

        check("a refused status wrote no executions row",
              count(conn, "SELECT COUNT(*) FROM executions") == executions_before)
        check("a refused status wrote no artifact",
              count(conn, "SELECT COUNT(*) FROM artifacts") == artifacts_before)

        res = await a.call_tool("run_sql", {
            "code": "SELECT * FROM in_1", "session_id": sa, "title": "Claimed",
            "description": "asserted as the answer", "input_row_ids": [ds], "status": "result",
        })
        check("status='result' is still accepted",
              res.data.get("status") == "ok" and res.data.get("artifact_status") == "result",
              str(res.data.get("artifact_status")))

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall checks passed")
