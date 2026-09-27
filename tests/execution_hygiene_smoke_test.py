"""Layer-2e smoke test: digit-leading artifact titles, and failed runs kept
out of search/lineage, over the MCP wire (same pattern as
tests/mcp_smoke_test.py).

Two bugs from the pilot report:

1. A title starting with a digit (e.g. "2025 headcount by department", a
   common real-world naming pattern) derived a table_name that was a
   SyntaxError as a Python identifier — a hard, unrecoverable crash for
   run_python, with no workaround. safe_table_name now prefixes such names
   with "t_".

2. A failed run_sql/run_python call still needs a real row (its row_id
   carries error/stdout/stderr back to the caller), but that dead,
   content-less node used to be indistinguishable from real work in
   search_artifacts and get_lineage — a crashed experiment left a
   permanent, discoverable ghost in the graph. Both now exclude
   status="error" artifacts.

Run: .venv/Scripts/python.exe tests/execution_hygiene_smoke_test.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["DSOS_DB_PATH"] = "data/test-runs/execution_hygiene_smoke_test/store.db"
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
    csv = tmp / "headcount.csv"
    csv.write_text("dept,n\nEng,10\nSales,5\n", encoding="utf-8")

    async with Client(mcp) as client:
        s1 = (await client.call_tool("start_session", {"question": "hygiene smoke test"})).data["session_id"]

        ds = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "2025 headcount by department",
            "description": "headcount snapshot by department",
            "content_path": str(csv), "content_format": "csv", "session_id": s1,
        })).data
        table_name = ds["table_name"]
        check("digit-leading title gets a letter-prefixed table_name",
              table_name == "t_2025_headcount_by_department", table_name)

        run_ok = (await client.call_tool("run_python", {
            "code": f"result = {table_name}.n.sum()",
            "session_id": s1, "title": "Total headcount", "description": "sum n",
            "input_row_ids": [ds["row_id"]],
        })).data
        check("run_python binds the prefixed name as a valid identifier",
              run_ok["status"] == "ok", run_ok.get("error"))

        bad = (await client.call_tool("run_python", {
            "code": "result = 1 / 0",
            "session_id": s1, "title": "Deliberately broken", "description": "crash on purpose",
            "input_row_ids": [ds["row_id"]],
        })).data
        check("a failed run still gets a real row_id (diagnostics path)",
              bad["status"] == "error" and bool(bad.get("row_id")))
        bad_row = bad["row_id"]

        found = (await client.call_tool(
            "search_artifacts", {"query": "Deliberately broken", "session_id": s1})).data
        check("failed run is invisible to search_artifacts",
              not any(r["row_id"] == bad_row for r in found["results"]),
              str([r["title"] for r in found["results"]]))

        desc = (await client.call_tool("get_lineage", {
            "row_id": ds["row_id"], "session_id": s1, "direction": "descendants"})).data
        check("failed run is invisible to get_lineage (descendants)",
              not any(r["row_id"] == bad_row for r in desc["results"]),
              str([r["title"] for r in desc["results"]]))
        check("the successful run is still a visible descendant",
              any(r["row_id"] == run_ok["row_id"] for r in desc["results"]))

        diag = (await client.call_tool("get_artifact", {"row_id": bad_row, "session_id": s1})).data
        check("the dead row is still directly fetchable by its own row_id",
              diag.get("row_id") == bad_row)

    shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)
    if FAILURES:
        print(f"\nexecution hygiene smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nexecution hygiene smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
