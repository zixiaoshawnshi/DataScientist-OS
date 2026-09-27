"""Layer-2c smoke test: re-registration dedupe, over the MCP wire (same
pattern as tests/mcp_smoke_test.py).

Registering the same content twice used to create a second near-duplicate
artifact. That is not just wasted disk: every later search_artifacts then
returns several indistinguishable copies of the same dataset, and the
agent has no way to tell which is canonical. The benchmark pilot hit this
in 7 of 10 reuse rounds.

Covers: collapse on identical content, distinct content still registers,
dedupe=False forces a real second copy, and the column migration on a
store created before content_hash existed.

Run: .venv/Scripts/python.exe tests/dedupe_smoke_test.py
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["DSOS_DB_PATH"] = "data/test-runs/dedupe_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    csv = tmp / "titanic.csv"
    csv.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
    other = tmp / "other.csv"
    other.write_text("a,b\n1,x\n2,DIFFERENT\n3,z\n", encoding="utf-8")

    async with Client(mcp) as client:
        s1 = (await client.call_tool("start_session", {"question": "dedupe round 1"})).data["session_id"]
        s2 = (await client.call_tool("start_session", {"question": "dedupe round 2"})).data["session_id"]
        s3 = (await client.call_tool("start_session", {"question": "dedupe round 3"})).data["session_id"]

        def save(sid, path, **kw):
            return {
                **kw,
                "type": "dataset", "title": "Titanic", "description": "the titanic dataset",
                "content_format": "csv", "session_id": sid, "content_path": str(path),
            }

        r1 = (await client.call_tool("save_artifact", save(s1, csv))).data
        check("first registration creates an artifact", r1.get("row_id") and not r1.get("deduplicated"))

        r2 = (await client.call_tool("save_artifact", save(s2, csv))).data
        check("re-registering identical content collapses",
              r2.get("row_id") == r1.get("row_id"),
              f"row {r2.get('row_id')}")
        check("collapse is reported as deduplicated", r2.get("deduplicated") is True)
        check("collapse explains itself", "already in the store" in (r2.get("note") or ""))

        r3 = (await client.call_tool("save_artifact", save(s3, other))).data
        check("different content still registers separately",
              r3.get("row_id") and r3.get("row_id") != r1.get("row_id"))

        r4 = (await client.call_tool("save_artifact", save(s3, csv, dedupe=False))).data
        check("dedupe=False forces a real second copy",
              r4.get("row_id") and r4.get("row_id") != r1.get("row_id"))

        n_rows = (await client.call_tool("run_sql", {
            "code": "SELECT COUNT(*) AS n FROM titanic", "session_id": s2,
            "title": "Row count", "description": "Count rows in the registered dataset.",
            "input_row_ids": [r1["row_id"]],
        })).data
        check("collapsed artifact is still queryable", n_rows.get("status") == "ok",
              f"status={n_rows.get('status')}")

    # A store written before content_hash existed must open, gain the column,
    # and keep its rows (db._add_missing_columns runs before SCHEMA).
    legacy_dir = Path("data/test-runs/dedupe_smoke_test/legacy")
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy = legacy_dir / "store.db"
    con = sqlite3.connect(legacy)
    con.executescript(
        "CREATE TABLE artifacts (row_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,"
        " version INTEGER NOT NULL, type TEXT NOT NULL, title TEXT NOT NULL,"
        " description TEXT NOT NULL, tags TEXT NOT NULL, content_ref TEXT NOT NULL,"
        " content_format TEXT NOT NULL, source TEXT, embedding BLOB NOT NULL,"
        " created_at TEXT NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL);"
        "INSERT INTO artifacts VALUES ('r0','a0',1,'dataset','old','d','[]','x.csv','csv',"
        "NULL,X'00','2026-01-01','s0','ready');"
    )
    con.commit()
    con.close()

    from dsos.db import connect

    migrated = connect(legacy)
    cols = {r["name"] for r in migrated.execute("PRAGMA table_info(artifacts)")}
    check("legacy store gains content_hash on open", "content_hash" in cols)
    check("legacy store keeps its rows",
          migrated.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"] == 1)
    migrated.close()
    connect(legacy).close()  # idempotent on reopen

    shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)
    if FAILURES:
        print(f"\ndedupe smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\ndedupe smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
