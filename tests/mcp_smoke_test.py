"""Layer-2 smoke test: drives the MCP server over the real wire protocol via
FastMCP's in-process Client — the same tool-calling interface a real agent
(Claude Code, Claude Desktop) speaks, unlike tests/smoke_test.py which calls
dsos.store/dsos.execution functions directly in plain Python.

Uses a synthetic CSV file, on purpose — not the real demo dataset.

Run: .venv/Scripts/python.exe tests/mcp_smoke_test.py
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

os.environ["DSOS_DB_PATH"] = "data/mcp_smoke_test.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set


async def main() -> None:
    async with Client(mcp) as client:
        tools = {t.name for t in (await client.list_tools())}
        expected = {
            "start_session", "search_artifacts", "get_artifact", "save_artifact",
            "run_sql", "run_python", "get_lineage",
        }
        assert expected <= tools, f"missing tools: {expected - tools}"
        print(f"[ok] server exposes {sorted(tools)}")

        # --- round 1 ---
        r = await client.call_tool("start_session", {"question": "round 1: synthetic mcp test"})
        s1 = r.data["session_id"]
        print(f"[ok] start_session -> {s1}")

        r = await client.call_tool(
            "search_artifacts", {"query": "how do I find and register data?", "session_id": s1}
        )
        assert r.data["results"], "search should find the seeded discovery skill"
        assert r.data["results"][0]["type"] == "skill"
        print(f"[ok] search_artifacts (over the wire) -> {r.data['results'][0]['title']}")

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "toy.csv"
            csv_path.write_text("team,score\na,10\nb,20\nc,15\n")

            r = await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Toy Scores",
                "description": "Synthetic 3-row dataset used only to smoke-test the MCP server.",
                "content_format": "parquet", "content_path": str(csv_path),
                "source": {"url": "synthetic://mcp-smoke-test", "fetched_at": "now"},
                "session_id": s1,
            })
            assert "row_id" in r.data, r.data
            dataset_row = r.data["row_id"]
            print(f"[ok] save_artifact ingested csv -> dataset row {dataset_row}")

        r = await client.call_tool("run_sql", {
            "code": "SELECT team, score FROM toy_scores WHERE score > 10",
            "session_id": s1, "title": "High scorers", "description": "Teams scoring above 10.",
            "input_row_ids": [dataset_row],
        })
        assert r.data["status"] == "ok", r.data
        query_row = r.data["row_id"]
        print(f"[ok] run_sql (over the wire) -> query row {query_row}")

        r = await client.call_tool(
            "get_lineage", {"row_id": query_row, "session_id": s1, "direction": "ancestors"}
        )
        assert any(a["row_id"] == dataset_row for a in r.data["results"])
        print("[ok] get_lineage resolved query -> dataset")

        # --- round 2: reuse round 1's dataset ---
        r = await client.call_tool("start_session", {"question": "round 2: deeper question"})
        s2 = r.data["session_id"]
        r = await client.call_tool("run_sql", {
            "code": "SELECT AVG(score) AS avg_score FROM toy_scores",
            "session_id": s2, "title": "Average score", "description": "Average score, round 2.",
            "input_row_ids": [dataset_row],
        })
        assert r.data["status"] == "ok", r.data
        print("[ok] round 2 reused round 1's dataset via run_sql")

    # --- verify the tool-call log middleware actually wrote rows, with no
    # explicit logging call anywhere in the test above ---
    from dsos.db import connect
    conn = connect(os.environ["DSOS_DB_PATH"])
    count = conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    assert count >= 6, f"expected tool_calls to be auto-logged, got {count} rows"
    print(f"[ok] middleware auto-logged {count} tool calls with zero explicit logging code")

    names = [r["tool_name"] for r in conn.execute("SELECT tool_name FROM tool_calls ORDER BY ts")]
    print(f"[ok] logged call sequence: {names}")

    print("\nLayer 2 (MCP server) smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
