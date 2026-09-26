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

# A dedicated subdirectory, not "data/<name>.db" — that used to make
# Path(DB_PATH).parent resolve to the *shared* data/ root, so the rmtree
# below wiped the real store.db and data/blobs/ (and every other test's
# files) instead of just this test's own leftovers. Cost real demo data
# once already; don't repeat it.
os.environ["DSOS_DB_PATH"] = "data/test-runs/mcp_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set


async def main() -> None:
    async with Client(mcp) as client:
        assert client.instructions and "start_session" in client.instructions, (
            "server instructions missing from the initialize response"
        )
        print("[ok] server instructions present in initialize response")

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
        # Regression: run_sql used to return only {row_id, status,
        # artifact_row_ids} — the agent had no way to see what it computed
        # without a second call, and that second call (get_artifact by the
        # id run_sql actually returns) was itself broken. Both fixed now.
        assert r.data["row_count"] == 2, r.data
        assert r.data["preview"], "run_sql should inline a preview, not just a row_id"
        print(f"[ok] run_sql (over the wire) -> query row {query_row}, preview inlined")

        r = await client.call_tool("get_artifact", {"row_id": query_row, "session_id": s1})
        assert "error" not in r.data, r.data
        assert r.data["row_id"] == query_row
        print("[ok] get_artifact(row_id=<what run_sql returned>) resolves (previously: 'no artifact')")
        assert any(u["row_id"] == dataset_row for u in r.data.get("uses", [])), r.data
        print("[ok] get_artifact inlines `uses` (its lineage ancestors) with no separate get_lineage call")

        r = await client.call_tool("search_artifacts", {"query": "Toy Scores", "session_id": s1})
        assert r.data["results"][0]["row_id"] == dataset_row and r.data["results"][0]["score"] == 1.0
        print("[ok] exact-title search_artifacts query keyword-matches over the wire (score 1.0)")

        r = await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Report", "session_id": s1,
            "description": "A short report on team scores.", "content_format": "markdown",
            "content_text": f"Teams scored well. See {{{{artifact:{dataset_row}}}}} for the raw data.",
        })
        narrative_row = r.data["row_id"]
        r = await client.call_tool("get_artifact", {"row_id": narrative_row, "session_id": s1})
        assert any(u["row_id"] == dataset_row for u in r.data.get("uses", [])), r.data
        print("[ok] narrative's {{artifact:...}} embed auto-derived lineage, with no parent_row_ids passed")

        r = await client.call_tool("run_sql", {
            "code": "SELECT this_column_does_not_exist FROM toy_scores",
            "session_id": s1, "title": "Deliberately broken query",
            "description": "Exercises the error path.", "input_row_ids": [dataset_row],
        })
        assert r.data["status"] == "error", r.data
        assert r.data.get("error"), "a failed run_sql should surface why, not just status=error"
        assert "this_column_does_not_exist" in r.data["error"], (
            "error should be a concise agent-facing message, not just the exception class"
        )
        assert "Traceback" not in r.data["error"], "error should exclude internal frame noise"
        assert "Traceback" in r.data.get("stderr", ""), "full traceback should still be in stderr"
        print(f"[ok] failed run_sql surfaces a concise error: {r.data['error']}")

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

        # --- round 3: agent-facing diagnostics (feedback #4, #5, #10) ---
        r = await client.call_tool("run_python", {
            "code": "print('computing averages'); "
                    "result = toy_scores.groupby('team')['score'].mean().reset_index()",
            "session_id": s2, "title": "Mean by team",
            "description": "Mean score per team; also exercises stdout passthrough.",
            "input_row_ids": [dataset_row],
        })
        assert r.data["status"] == "ok", r.data
        assert "computing averages" in r.data.get("stdout", ""), (
            "successful runs must surface stdout top-level (feedback #10)"
        )
        print("[ok] run_python over the wire -> stdout surfaced on success (feedback #10)")

        r = await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Huge Report", "session_id": s2,
            "description": "Deliberately oversized narrative to exercise the inline content cap.",
            "content_format": "markdown", "content_text": "x" * 20_000,
        })
        huge_row = r.data["row_id"]
        r = await client.call_tool("get_artifact", {"row_id": huge_row, "session_id": s2})
        assert len(r.data["content"]) <= 4_000, "oversized content must be clipped in the payload"
        assert r.data.get("content_truncated"), (
            "clipped content must say so, and how to see the rest (feedback #5)"
        )
        print("[ok] oversized narrative arrives clipped with a content_truncated note (feedback #5)")

        # A blob deleted out from under the store (the exact scenario behind
        # feedback #4's cryptic '[Errno 2] No such file or directory').
        from dsos import store as store_mod
        from dsos.db import connect as db_connect
        diag_conn = db_connect(os.environ["DSOS_DB_PATH"])
        blob_ref = store_mod.get_artifact_by_row_id(
            diag_conn, dataset_row, load_content=False
        ).content_ref
        Path(blob_ref).unlink()

        r = await client.call_tool("get_artifact", {"row_id": dataset_row, "session_id": s2})
        assert r.data.get("content_error"), "missing blob must surface content_error, not crash"
        assert "Toy Scores" in r.data["content_error"], "message must name the artifact"
        assert "re-upload" in r.data["content_error"].lower(), "message must say how to fix it"
        print("[ok] get_artifact on a missing blob returns a named, actionable error (feedback #4)")

        r = await client.call_tool("run_sql", {
            "code": "SELECT * FROM toy_scores",
            "session_id": s2, "title": "Should fail",
            "description": "Input blob was deleted above.",
            "input_row_ids": [dataset_row],
        })
        assert r.data["status"] == "error", r.data
        assert "Toy Scores" in (r.data.get("error") or ""), (
            "run_sql against a missing-blob input must name the broken input"
        )
        print("[ok] run_sql against a missing-blob input names the broken input in its error")

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
