"""WP-B1 smoke test: inputs are bound by POSITION (in_1..in_N), not by a
name derived from the artifact's title — over the MCP wire (same pattern as
tests/execution_hygiene_smoke_test.py).

Deriving the binding name from the title was the source of three bug
classes, all exercised here:

1. Two inputs with the *same* title collided — the second silently
   shadowed the first, so a join across both returned nonsense.
2. A digit-leading title ("2025 headcount", a common real-world naming
   pattern) is not a valid Python identifier or an unquoted SQL
   identifier, and had to be papered over with a "t_" prefix.
3. Re-titling an artifact (a new version with a better name) broke code
   that had been written against the old name, with nothing to rename.

Positional aliases have none of those failure modes: the code names in_1
and means "the first input of this call", whatever it is called.

Also covered: an unknown or duplicated input_row_id is a clear error
(it used to surface as an AttributeError on None), and a column typo
lists the aliases with their titles and columns.

Run: .venv/Scripts/python.exe tests/input_binding_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["DSOS_DB_PATH"] = "data/test-runs/input_binding_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

import pandas as pd  # noqa: E402
from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    orders_a = tmp / "orders_a.csv"
    orders_a.write_text("team,score\na,10\nb,20\nc,15\n", encoding="utf-8")
    orders_b = tmp / "orders_b.csv"
    orders_b.write_text("team,score\na,1\nb,2\nd,5\n", encoding="utf-8")
    headcount = tmp / "headcount.csv"
    headcount.write_text("dept,n\nEng,10\nSales,5\n", encoding="utf-8")

    async with Client(mcp) as client:
        s1 = (await client.call_tool(
            "start_session", {"question": "input binding smoke test"})).data["session_id"]

        # Two DIFFERENT artifacts that share a title, plus a digit-leading
        # one — the two shapes a title-derived binding name cannot survive.
        a = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Orders",
            "description": "Synthetic orders, part one.",
            "content_path": str(orders_a), "content_format": "csv", "session_id": s1,
        })).data
        b = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Orders",
            "description": "Synthetic orders, part two — same title as the other one.",
            "content_path": str(orders_b), "content_format": "csv", "session_id": s1,
        })).data
        hc = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "2025 headcount",
            "description": "Digit-leading title, to exercise the old t_ prefix.",
            "content_path": str(headcount), "content_format": "csv", "session_id": s1,
        })).data
        check("two same-titled datasets are two distinct artifacts",
              a["row_id"] != b["row_id"], f'{a["row_id"]} / {b["row_id"]}')
        check("save_artifact no longer returns a title-derived table_name",
              "table_name" not in a, str(sorted(a)))

        # --- 1. identical titles, addressable as in_1 / in_2, in both run paths
        sql = (await client.call_tool("run_sql", {
            "code": "SELECT in_1.team, in_1.score AS a_score, in_2.score AS b_score "
                    "FROM in_1 JOIN in_2 ON in_1.team = in_2.team ORDER BY in_1.team",
            "session_id": s1, "title": "Orders joined", "description": "Join both Orders.",
            "input_row_ids": [a["row_id"], b["row_id"]],
        })).data
        check("run_sql binds same-titled inputs as in_1/in_2",
              sql.get("status") == "ok" and sql.get("row_count") == 2,
              str(sql.get("error") or sql.get("row_count")))
        check("run_sql maps each input row_id to its alias",
              sql.get("input_tables") == {a["row_id"]: "in_1", b["row_id"]: "in_2"},
              str(sql.get("input_tables")))

        py = (await client.call_tool("run_python", {
            "code": "result = in_1.merge(in_2, on='team', suffixes=('_a', '_b'))",
            "session_id": s1, "title": "Orders merged in python",
            "description": "Merge both same-titled Orders in python.",
            "input_row_ids": [a["row_id"], b["row_id"]],
        })).data
        check("run_python binds same-titled inputs as in_1/in_2",
              py.get("status") == "ok" and py.get("row_count") == 2,
              str(py.get("error") or py.get("row_count")))

        # --- 2. a digit-leading title needs no t_ prefix anywhere
        digit = (await client.call_tool("run_python", {
            "code": "result = in_1['n'].sum()",
            "session_id": s1, "title": "Total headcount", "description": "Sum of n.",
            "input_row_ids": [hc["row_id"]],
        })).data
        check("a digit-leading title binds as in_1 and runs",
              digit.get("status") == "ok" and str(digit.get("content")) == "15",
              str(digit.get("error") or digit.get("content")))
        dump = json.dumps(digit, default=str)
        # the old rule derived "t_2025_headcount" here; nothing may carry
        # that prefix any more (the field name input_tables is not a name)
        check("no t_-prefixed binding name appears in the response",
              "t_2025_headcount" not in dump, dump if "t_2025_headcount" in dump else "")

        # --- 3. inputs[row_id] addresses an input by id
        by_id = (await client.call_tool("run_python", {
            "code": "result = inputs[%r]['score'].sum()" % a["row_id"],
            "session_id": s1, "title": "Total score by row_id",
            "description": "Reach an input through the inputs dict.",
            "input_row_ids": [a["row_id"], b["row_id"]],
        })).data
        check("inputs['<row_id>'] resolves in run_python",
              by_id.get("status") == "ok" and str(by_id.get("content")) == "45",
              str(by_id.get("error") or by_id.get("content")))

        # --- 4. code written against in_1 survives a re-titled new version
        from dsos import store as store_mod
        from dsos.db import connect

        conn = connect(os.environ["DSOS_DB_PATH"])
        original = store_mod.get_artifact_by_row_id(conn, a["row_id"], load_content=False)
        renamed_row = store_mod.save_artifact(
            conn, artifact_id=original.artifact_id, type="dataset",
            title="Orders (v2, renamed)",
            description="A newer version of Orders, under a different title.",
            content=pd.read_csv(orders_b),
            content_format="parquet", session_id=s1,
        )
        conn.close()
        check("the re-titled version is a new row", renamed_row != a["row_id"],
              renamed_row)

        again = (await client.call_tool("run_python", {
            "code": "result = in_1['score'].sum()",
            "session_id": s1, "title": "Total score on the renamed version",
            "description": "Same code as before, against a differently-titled input.",
            "input_row_ids": [renamed_row],
        })).data
        check("code using in_1 still works after a re-titled new version",
              again.get("status") == "ok" and str(again.get("content")) == "8",
              str(again.get("error") or again.get("content")))

        # --- 5. unknown / duplicated input ids are named errors
        for tool in ("run_sql", "run_python"):
            code = "SELECT COUNT(*) AS n FROM in_1" if tool == "run_sql" else "result = 1"
            dup = (await client.call_tool(tool, {
                "code": code, "session_id": s1, "title": "Duplicate input",
                "description": "The same input listed twice.",
                "input_row_ids": [a["row_id"], a["row_id"]],
            })).data
            check(f"{tool} rejects a duplicate input_row_id",
                  dup.get("status") == "error"
                  and f"duplicate input_row_id '{a['row_id']}'" in (dup.get("error") or ""),
                  str(dup.get("error")))

            missing = (await client.call_tool(tool, {
                "code": code, "session_id": s1, "title": "Unknown input",
                "description": "An input_row_id that isn't in the store.",
                "input_row_ids": ["not-a-real-row-id"],
            })).data
            check(f"{tool} rejects an unknown input_row_id",
                  missing.get("status") == "error"
                  and "unknown input_row_id 'not-a-real-row-id'" in (missing.get("error") or ""),
                  str(missing.get("error")))

        # --- 6. a column typo lists the alias with its title and columns
        typo = (await client.call_tool("run_sql", {
            "code": "SELECT no_such_column FROM in_1",
            "session_id": s1, "title": "Deliberate column typo",
            "description": "Exercises the schema hint.",
            "input_row_ids": [a["row_id"]],
        })).data
        check("a column typo lists in_1 with the input's title and columns",
              typo.get("status") == "error"
              and 'in_1 "Orders" (team, score)' in (typo.get("error") or ""),
              str(typo.get("error")))

    shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)
    if FAILURES:
        print(f"\ninput binding smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\ninput binding smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
