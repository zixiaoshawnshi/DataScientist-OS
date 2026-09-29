"""Layer-2c smoke test: re-registration dedupe, over the MCP wire (same
pattern as tests/mcp_smoke_test.py).

Registering the same content twice used to create a second near-duplicate
artifact. That is not just wasted disk: every later search_artifacts then
returns several indistinguishable copies of the same dataset, and the
agent has no way to tell which is canonical. The benchmark pilot hit this
in 7 of 10 reuse rounds.

Covers: collapse on identical content, distinct content still registers,
dedupe=False forces a real second copy, that only a current result (the
latest version, status='result') is a collapse target — never a superseded
row, an exploratory one or an older version — and the column migration on a
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
            "code": "SELECT COUNT(*) AS n FROM in_1", "session_id": s2,
            "title": "Row count", "description": "Count rows in the registered dataset.",
            "input_row_ids": [r1["row_id"]],
        })).data
        check("collapsed artifact is still queryable", n_rows.get("status") == "ok",
              f"status={n_rows.get('status')}")

        # --- dedupe respects the lifecycle: only a CURRENT result is a thing
        # a re-registration may collapse into. Collapsing into a superseded
        # row hands back a row search hides and whose status stays
        # superseded, so the claim the caller just made is invisible; into an
        # exploratory one, the claim silently stays unclaimed.
        def narrative(sid, text, title, **kw):
            return {
                **kw,
                "type": "narrative", "title": title,
                "description": f"Which region led Q3, as called in {title}.",
                "content_format": "markdown", "session_id": sid, "content_text": text,
            }

        first_call = (await client.call_tool("save_artifact", narrative(
            s1, "red leads q3", "Q3 leader first call"))).data
        corrected = (await client.call_tool("save_artifact", narrative(
            s1, "blue leads q3", "Q3 leader corrected call"))).data
        await client.call_tool("mark", {
            "row_id": first_call["row_id"], "session_id": s1, "status": "superseded",
            "superseded_by": corrected["row_id"],
        })
        reclaimed = (await client.call_tool("save_artifact", narrative(
            s2, "red leads q3", "Q3 leader rederived call",
            caveats=["west region only"]))).data
        check("identical content to a SUPERSEDED row registers a new row",
              reclaimed.get("row_id") and reclaimed.get("row_id") != first_call["row_id"]
              and not reclaimed.get("deduplicated"), str(reclaimed)[:200])
        from dsos import db, store  # import after DSOS_DB_PATH is set

        conn = db.connect(os.environ["DSOS_DB_PATH"])
        row = conn.execute(
            "SELECT status, caveats FROM artifacts WHERE row_id = ?", (reclaimed.get("row_id"),)
        ).fetchone()
        check("...as a result carrying the new caveats",
              row is not None and row["status"] == "result"
              and "west region only" in (row["caveats"] or ""),
              str(dict(row) if row else None))
        check("...and the superseded row is left exactly as it was",
              conn.execute("SELECT status, superseded_by FROM artifacts WHERE row_id = ?",
                           (first_call["row_id"],)).fetchone()["superseded_by"]
              == corrected["row_id"])
        hits = (await client.call_tool("search_artifacts", {
            "query": "Q3 leader rederived", "session_id": s2})).data["results"]
        check("...and the re-registration is searchable",
              any(h["row_id"] == reclaimed.get("row_id") for h in hits),
              str([h["title"] for h in hits]))
        check("...and is evidence to a consumer",
              any(a.row_id == reclaimed.get("row_id")
                  for a, _, _ in store.find_evidence(conn, "Q3 leader rederived")))

        same_as_current = (await client.call_tool("save_artifact", narrative(
            s3, "blue leads q3", "Q3 leader corrected again"))).data
        check("identical content to a current RESULT still collapses",
              same_as_current.get("row_id") == corrected["row_id"]
              and same_as_current.get("deduplicated") is True, str(same_as_current)[:200])

        parked = (await client.call_tool("save_artifact", narrative(
            s1, "green leads q4", "Q4 leader parked", status="exploratory"))).data
        claimed = (await client.call_tool("save_artifact", narrative(
            s2, "green leads q4", "Q4 leader claimed"))).data
        check("identical content to an EXPLORATORY row registers a new result",
              claimed.get("row_id") and claimed.get("row_id") != parked.get("row_id")
              and not claimed.get("deduplicated"), str(claimed)[:200])
        claimed_again = (await client.call_tool("save_artifact", narrative(
            s3, "green leads q4", "Q4 leader claimed twice"))).data
        check("...and the next identical save collapses into that result, not the lead",
              claimed_again.get("row_id") == claimed.get("row_id"), str(claimed_again)[:200])

        # An older VERSION of a logical artifact is no more current than a
        # superseded row: search reads the latest version only, so a collapse
        # into v1 would hand back a row nothing can find.
        v1 = store.save_artifact(
            conn, type="narrative", title="Q2 leader", description="Which region led Q2.",
            content="amber leads q2", content_format="markdown", session_id=s1,
            artifact_id="q2-leader",
        )
        v2 = store.save_artifact(
            conn, type="narrative", title="Q2 leader", description="Which region led Q2.",
            content="violet leads q2", content_format="markdown", session_id=s1,
            artifact_id="q2-leader",
        )
        check("content only an OLDER version holds does not collapse",
              store.find_by_content_hash(
                  conn, "narrative", store._content_hash("amber leads q2", "markdown")) is None)
        check("content the latest version holds does",
              store.find_by_content_hash(
                  conn, "narrative", store._content_hash("violet leads q2", "markdown")) == v2,
              f"v1={v1}")
        conn.close()

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
