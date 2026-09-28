"""Layer-2e smoke test: input binding by alias, and failed runs recorded as
executions rather than artifacts, over the MCP wire (same pattern as
tests/mcp_smoke_test.py).

Two bugs from the pilot report, and the fix this test now pins:

1. A title starting with a digit (e.g. "2025 headcount by department", a
   common real-world naming pattern) derived a table_name that was a
   SyntaxError as a Python identifier — a hard, unrecoverable crash for
   run_python, with no workaround. It used to be papered over with a "t_"
   prefix on the derived name; the fix was to stop deriving names from
   titles at all. Inputs are bound positionally as in_1..in_N in
   input_row_ids order (see tests/input_binding_smoke_test.py), so a
   digit-leading title is just an ordinary input name now.

2. A failed run_sql/run_python call used to register a dead, content-less
   ARTIFACT row, existing only so its row_id could carry error/stdout/
   stderr back to the caller. It was filtered out of search and lineage,
   but it was still an artifact: it counted, it appeared in the GUI
   gallery, and anything that walked the table saw a node that does not
   exist. A failed run is an EXECUTION, not an artifact — the error lives
   on the executions row (output_row_id NULL, plus the session and the
   inputs the call named), and the response reports execution_id with no
   row_id at all. No artifact row and no lineage edge is written on
   failure, including for dsos-side failures such as an unknown input id.

   The status='error' filters in search_artifacts/get_lineage stay, for
   stores that already hold those rows; no new one is ever written.

Run: .venv/Scripts/python.exe tests/execution_hygiene_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

DB_PATH = "data/test-runs/execution_hygiene_smoke_test/store.db"
os.environ["DSOS_DB_PATH"] = DB_PATH
shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos import db, embeddings, store  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def exec_row(conn: sqlite3.Connection, *, execution_id: str | None = None,
             output_row_id: str | None = None) -> dict | None:
    """One executions row as a plain dict, or None. A dict rather than a
    sqlite3.Row so that a column this store has not been given yet reads as
    a failed check instead of crashing the test on an IndexError."""
    if execution_id is not None:
        row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM executions WHERE output_row_id = ?",
                           (output_row_id,)).fetchone()
    return dict(row) if row is not None else None


# The schema a v0.3-era store had: the current baseline minus content_hash
# (which WP-A1 backfills), and with executions still demanding an output
# artifact. Hard-coded, like tests/migrations_smoke_test.py's, so this test
# keeps checking the migration against an old store rather than restating
# whatever dsos/db.py happens to say today.
LEGACY_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE artifacts (
    row_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    content_format TEXT NOT NULL,
    source TEXT,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL DEFAULT 'ready',
    UNIQUE (artifact_id, version)
);

CREATE INDEX idx_artifacts_artifact_id ON artifacts(artifact_id);
CREATE INDEX idx_artifacts_session_id ON artifacts(session_id);

CREATE VIRTUAL TABLE artifacts_fts USING fts5(
    row_id UNINDEXED,
    title,
    description,
    tags
);

CREATE TABLE lineage (
    child_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    parent_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    PRIMARY KEY (child_row_id, parent_row_id)
);

CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    output_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    error TEXT,
    output_summary TEXT NOT NULL
);

CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ts TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    artifact_row_ids TEXT NOT NULL
);
"""


def build_legacy_store(path: Path) -> None:
    """A pre-migration store holding the two rows this test cares about: a
    real dataset, and the dead 'error' artifact a failed run used to
    leave behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.execute("INSERT INTO sessions VALUES ('s0', 'legacy round', '2026-01-01')")
    con.executemany(
        "INSERT INTO artifacts (row_id, artifact_id, version, type, title, description,"
        " tags, content_ref, content_format, created_at, embedding, session_id, status)"
        " VALUES (?, ?, 1, ?, ?, ?, '[]', ?, 'csv', '2026-01-01', ?, 's0', ?)",
        [
            # A zero vector of the right size, not a stub byte: search's
            # semantic fallback does np.frombuffer over this column and a
            # 1-byte blob makes every search on the store blow up.
            ("dsrow", "ds", "dataset", "Legacy dataset", "a dataset from an old store",
             "d.csv", bytes(4 * embeddings.DIM), "ready"),
            ("errow", "er", "query", "Legacy broken run", "a run that crashed back then",
             "e.sql", bytes(4 * embeddings.DIM), "error"),
        ],
    )
    con.execute("INSERT INTO artifacts_fts (row_id, title, description, tags)"
                " VALUES ('errow', 'Legacy broken run', 'a run that crashed back then', '')")
    con.execute("INSERT INTO lineage VALUES ('errow', 'dsrow')")
    # An execution from before the rebuild, so the backfill of the two new
    # columns is checked against a real row and not just an empty table.
    con.execute(
        "INSERT INTO executions (id, output_row_id, kind, code, started_at, ended_at,"
        " status, stdout, stderr, output_summary)"
        " VALUES ('e0', 'errow', 'sql', 'SELECT 1', '2026-01-01', '2026-01-01',"
        " 'ok', 'hi', '', '{}')"
    )
    con.commit()
    con.close()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    csv = tmp / "headcount.csv"
    csv.write_text("dept,n\nEng,10\nSales,5\n", encoding="utf-8")

    # A second connection to the same file, for counting rows the MCP
    # server writes. WAL makes a reader/writer pair safe; the server's own
    # connection stays the one the tools use.
    conn = db.connect(DB_PATH)

    async with Client(mcp) as client:
        s1 = (await client.call_tool(
            "start_session", {"question": "hygiene smoke test"})).data["session_id"]

        ds = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "2025 headcount by department",
            "description": "headcount snapshot by department",
            "content_path": str(csv), "content_format": "csv", "session_id": s1,
        })).data
        # no title-derived name to look up or guess: the first input_row_id is
        # in_1, whatever the artifact is called (WP-B1)
        check("a digit-leading title is just an ordinary input",
              "table_name" not in ds, str(sorted(ds)))

        run_ok = (await client.call_tool("run_python", {
            "code": "result = in_1.n.sum()",
            "session_id": s1, "title": "Total headcount", "description": "sum n",
            "input_row_ids": [ds["row_id"]],
        })).data
        check("a digit-leading title binds as in_1",
              run_ok.get("input_tables") == {ds["row_id"]: "in_1"},
              str(run_ok.get("input_tables")))
        check("run_python binds the positional alias as a valid identifier",
              run_ok["status"] == "ok", run_ok.get("error"))

        # --- the success path is unchanged: an artifact, lineage to the
        # input, and an execution pointing back at it.
        ok_exec = exec_row(conn, output_row_id=run_ok["row_id"])
        check("a successful run still records an execution against its artifact",
              ok_exec is not None and ok_exec.get("kind") == "python"
              and ok_exec.get("status") == "ok",
              "no executions row" if ok_exec is None else str(ok_exec.get("status")))
        check("a successful run still lineage-links its output to its inputs",
              count(conn, "SELECT COUNT(*) FROM lineage WHERE child_row_id = ?",
                    (run_ok["row_id"],)) == 1)
        check("a successful run records the session and inputs on its execution row",
              ok_exec is not None and ok_exec.get("session_id") == s1
              and json.loads(ok_exec.get("input_row_ids") or "null") == [ds["row_id"]],
              "" if ok_exec is None else f"{ok_exec.get('session_id')} {ok_exec.get('input_row_ids')}")

        desc = (await client.call_tool("get_lineage", {
            "row_id": ds["row_id"], "session_id": s1, "direction": "descendants"})).data
        check("the successful run is a visible descendant",
              any(r["row_id"] == run_ok["row_id"] for r in desc["results"]),
              str([r["title"] for r in desc["results"]]))

        # --- the failure path: an execution, not an artifact.
        before = {
            "artifacts": count(conn, "SELECT COUNT(*) FROM artifacts"),
            "lineage": count(conn, "SELECT COUNT(*) FROM lineage"),
            "executions": count(conn, "SELECT COUNT(*) FROM executions"),
        }
        bad = (await client.call_tool("run_python", {
            "code": "result = 1 / 0",
            "session_id": s1, "title": "Deliberately broken", "description": "crash on purpose",
            "input_row_ids": [ds["row_id"]],
        })).data

        check("a failed run reports status=error", bad.get("status") == "error", str(bad.get("error")))
        check("a failed run reports an execution_id, not a row_id",
              bool(bad.get("execution_id")) and "row_id" not in bad,
              f"execution_id={bad.get('execution_id')!r} row_id={bad.get('row_id')!r}")
        check("a failed run still returns the error, stdout and stderr inline",
              bool(bad.get("error")) and "stderr" in bad, str(bad.get("error"))[:120])
        check("a failed run still echoes the input aliases it bound",
              bad.get("input_tables") == {ds["row_id"]: "in_1"}
              and bad.get("artifact_row_ids") == [ds["row_id"]],
              str(bad.get("input_tables")))

        after = {
            "artifacts": count(conn, "SELECT COUNT(*) FROM artifacts"),
            "lineage": count(conn, "SELECT COUNT(*) FROM lineage"),
            "executions": count(conn, "SELECT COUNT(*) FROM executions"),
        }
        check("a failed run creates no artifacts row",
              after["artifacts"] == before["artifacts"],
              f"{before['artifacts']} -> {after['artifacts']}")
        check("a failed run creates no lineage edge",
              after["lineage"] == before["lineage"],
              f"{before['lineage']} -> {after['lineage']}")
        check("a failed run creates exactly one executions row",
              after["executions"] == before["executions"] + 1,
              f"{before['executions']} -> {after['executions']}")

        row = exec_row(conn, execution_id=bad.get("execution_id"))
        check("the failed execution's output_row_id is NULL",
              row is not None and row.get("output_row_id", "MISSING") is None,
              "no execution row" if row is None else str(row.get("output_row_id", "MISSING")))
        check("the failed execution records the session it ran in",
              row is not None and row.get("session_id") == s1,
              "" if row is None else str(row.get("session_id")))
        check("the failed execution records the inputs the call named",
              row is not None and json.loads(row.get("input_row_ids") or "null") == [ds["row_id"]],
              "" if row is None else str(row.get("input_row_ids")))
        check("the failed execution keeps the error and the traceback",
              row is not None and row.get("status") == "error"
              and "ZeroDivisionError" in (row.get("stderr") or ""),
              "" if row is None else str(row.get("status")))
        total_artifacts = count(conn, "SELECT COUNT(*) FROM artifacts")
        check("the failed run left no artifact titled after it at all",
              count(conn, "SELECT COUNT(*) FROM artifacts WHERE title = 'Deliberately broken'") == 0,
              f"{total_artifacts} artifacts in the store")

        # --- a dsos-side failure (an input id that names nothing) is
        # recorded the same way: it never reached the sandbox at all.
        unknown = (await client.call_tool("run_python", {
            "code": "result = 1",
            "session_id": s1, "title": "Bogus input", "description": "names a row that is not there",
            "input_row_ids": ["0" * 32],
        })).data
        check("an unknown input fails the run without an artifact",
              unknown.get("status") == "error" and "row_id" not in unknown,
              str(unknown.get("error")))
        unknown_row = exec_row(conn, execution_id=unknown.get("execution_id"))
        check("an unknown input is still recorded as a failed execution",
              unknown_row is not None and unknown_row.get("output_row_id", "MISSING") is None
              and json.loads(unknown_row.get("input_row_ids") or "null") == ["0" * 32],
              "no execution row" if unknown_row is None
              else str(unknown_row.get("input_row_ids")))

    conn.close()

    # --- the session page is where a run that left nothing behind is
    # still visible: there is no artifact to navigate from.
    from fastapi.testclient import TestClient  # noqa: E402 — GUI deps, loaded on demand

    from dsos import gui  # noqa: E402 — reads DSOS_DB_PATH at import, same store

    page = TestClient(gui.app).get(f"/sessions/{s1}")
    check("the session page lists this session's failed runs",
          page.status_code == 200 and "Failed runs" in page.text
          and "unknown input_row_id" in page.text and "python" in page.text,
          "no failed-run section" if page.status_code == 200 else f"HTTP {page.status_code}")

    # --- a store from before this change still migrates, and its dead
    # 'error' artifact rows stay hidden (search/lineage keep filtering
    # status='error' for exactly this case).
    legacy_path = Path(DB_PATH).parent / "legacy" / "store.db"
    build_legacy_store(legacy_path)
    legacy = db.connect(legacy_path)
    try:
        check("a store holding a dead error artifact migrates",
              legacy.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS))
        hits = store.search_artifacts(legacy, "Legacy broken run", top_k=10)
        check("the legacy error artifact stays invisible to search",
              not any(a.row_id == "errow" for a, _ in hits), str([a.title for a, _ in hits]))
        check("the legacy dataset beside it is still searchable",
              any(a.row_id == "dsrow" for a, _ in hits), str([a.title for a, _ in hits]))
        ancestors = store.get_lineage(legacy, "errow", direction="ancestors")
        check("the legacy error artifact stays out of lineage",
              not any(a.row_id == "errow" for a in ancestors))
        legacy_artifacts = count(legacy, "SELECT COUNT(*) FROM artifacts")
        check("migrating a legacy store invents no artifacts", legacy_artifacts == 2,
              f"{legacy_artifacts} rows")
        check("the migration left the error row's status alone",
              legacy.execute("SELECT status FROM artifacts WHERE row_id = 'errow'").fetchone()[0]
              == "error")
        old_exec = exec_row(legacy, execution_id="e0")
        check("a pre-migration execution is backfilled with its output's session and inputs",
              old_exec is not None and old_exec.get("session_id") == "s0"
              and json.loads(old_exec.get("input_row_ids") or "null") == ["dsrow"],
              "" if old_exec is None else f"{old_exec.get('session_id')} "
                                           f"{old_exec.get('input_row_ids')}")
    finally:
        legacy.close()

    # The rebuild has to survive the store being reopened, and the two
    # things it was for — a NULL output_row_id, and the two lookups the
    # runtime makes — have to actually be there. Asserted, not assumed:
    # dropping and renaming a table drops its indexes along the way, and
    # this table's whole point is the NOT NULL that used to be there.
    legacy = db.connect(legacy_path)
    try:
        indexes = {r["name"] for r in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'executions'")}
        check("the rebuilt executions table has both new indexes",
              {"idx_executions_output_row_id", "idx_executions_session_id"} <= indexes,
              str(sorted(indexes)))
        legacy.execute(
            "INSERT INTO executions (id, output_row_id, kind, code, started_at, ended_at,"
            " status, output_summary, session_id, input_row_ids)"
            " VALUES ('e-null', NULL, 'sql', 'SELECT 1', 'now', 'now', 'error', '{}',"
            " 's0', '[\"dsrow\"]')"
        )
        legacy.commit()
        check("an execution with no output artifact is accepted after a reopen",
              legacy.execute("SELECT COUNT(*) FROM executions WHERE id = 'e-null'").fetchone()[0]
              == 1)
    finally:
        legacy.close()

    shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)
    if FAILURES:
        print(f"\nexecution hygiene smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nexecution hygiene smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
