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
            assert r.data.get("table_name") == "toy_scores", (
                "save_artifact should surface the normalized table/variable name "
                "up front, not make the agent guess or wait for an error (feedback #1)"
            )
            print(f"[ok] save_artifact ingested csv -> dataset row {dataset_row}, table_name inlined")

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
        assert r.data.get("table_name") == "high_scorers", (
            "the output artifact's own table name should be inlined too, for chaining "
            "into a later run_sql/run_python call (feedback #1)"
        )
        assert r.data.get("input_tables") == {dataset_row: "toy_scores"}, (
            "run_sql should map each input row_id to the table name it registered "
            "it under (feedback #1)"
        )
        print(f"[ok] run_sql (over the wire) -> query row {query_row}, preview + table names inlined")

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
        assert "toy_scores(team, score)" in r.data["error"], (
            "a failed run_sql should name the available tables/columns, not just "
            "the bad reference (feedback #8)"
        )
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

        # --- round 3: scratch mode + ImportError hint (feedback #7, #8) ---
        r = await client.call_tool("run_sql", {
            "code": "SELECT COUNT(*) AS n FROM high_scorers",
            "session_id": s2, "title": "Scratch count",
            "description": "Scratch run — must NOT become an artifact.",
            "input_row_ids": [query_row], "scratch": True,
        })
        assert r.data["status"] == "ok", r.data
        assert r.data.get("scratch") is True, r.data
        assert r.data["row_count"] == 1, r.data
        print("[ok] run_sql scratch=True -> inline result, scratch flag set (feedback #8)")

        # Also proves the analysis stack (PR #1) is importable end-to-end
        # through the running server.
        r = await client.call_tool("run_python", {
            "code": "import sklearn; result = sklearn.__version__",
            "session_id": s2, "title": "Scratch sklearn version",
            "description": "Scratch run — also proves the analysis stack is importable.",
            "input_row_ids": [], "scratch": True,
        })
        assert r.data["status"] == "ok", r.data
        import re as _re
        assert _re.match(r"\d+\.", str(r.data["content"])), r.data
        print("[ok] run_python scratch imports sklearn — analysis stack live end-to-end")

        # ImportError lists what IS available instead of a bare crash (feedback #7)
        r = await client.call_tool("run_python", {
            "code": "import definitely_not_a_real_package_xyz",
            "session_id": s2, "title": "Scratch bad import",
            "description": "Scratch run exercising the ImportError hint.",
            "input_row_ids": [], "scratch": True,
        })
        assert r.data["status"] == "error", r.data
        assert "available" in r.data["error"].lower(), r.data
        assert "pandas" in r.data["error"].lower(), "hint should list the sandbox's packages"
        print("[ok] ImportError surfaces an available-packages hint (feedback #7)")

        r = await client.call_tool("search_artifacts", {"query": "Scratch", "session_id": s2})
        assert not any(h["title"].startswith("Scratch") for h in r.data["results"]), (
            "scratch runs must not be searchable"
        )
        print("[ok] scratch runs are invisible to search_artifacts (feedback #8)")

        # --- round 4: subprocess sandbox — requirements / code_paths (feedback #1, Option B) ---
        with tempfile.TemporaryDirectory() as tmp:
            helper_dir = Path(tmp) / "helpers"
            helper_dir.mkdir()
            (helper_dir / "semantic_helper.py").write_text(
                "def answer():\n    return 42\n", encoding="utf-8"
            )
            r = await client.call_tool("run_python", {
                "code": "import semantic_helper; result = semantic_helper.answer()",
                "session_id": s2, "title": "Scratch via code_paths",
                "description": "Scratch run importing the agent's own unpackaged module.",
                "input_row_ids": [], "scratch": True,
                "code_paths": [str(helper_dir)],
            })
            assert r.data["status"] == "ok", r.data
            assert r.data["content"] == 42, r.data
            # and the injected path must not leak into the server process
            import sys as _sys
            assert str(helper_dir) not in _sys.path, "code_paths must not leak into sys.path"
        print("[ok] run_python code_paths binds an unpackaged module, no sys.path leak")

        from dsos import sandbox as dsos_sandbox
        if dsos_sandbox.find_uv() is None:
            print("[skip] uv not installed — requirements round skipped")
        else:
            # a package the sandbox lacks, plus a DataFrame input round-trip
            # (parquet out, parquet back in), resolved per-call by uv
            r = await client.call_tool("run_python", {
                "code": "import tabulate; "
                        "result = tabulate.tabulate(high_scorers.to_dict('records'), headers='keys')",
                "session_id": s2, "title": "Scratch via uv",
                "description": "Scratch run pulling a missing package via uv, with a DataFrame input.",
                "input_row_ids": [query_row], "scratch": True,
                "requirements": ["tabulate"],
            })
            assert r.data["status"] == "ok", r.data
            assert "b" in r.data["content"] and "c" in r.data["content"], (
                "the subprocess should have seen the input artifact's rows"
            )
            print("[ok] run_python requirements=[tabulate] resolves via uv, input round-trips")

            r = await client.call_tool("run_python", {
                "code": "result = 1",
                "session_id": s2, "title": "Scratch bad requirement",
                "description": "Scratch run with an unresolvable requirement.",
                "input_row_ids": [], "scratch": True,
                "requirements": ["definitely-not-a-real-package-xyz"],
            })
            assert r.data["status"] == "error", r.data
            assert "definitely-not-a-real-package-xyz" in (r.data.get("error") or ""), (
                "unresolvable requirements should fail with a clear uv error"
            )
            print("[ok] unresolvable requirement fails with a clear uv error")

        # --- round 5: agent-facing diagnostics (feedback #4, #5, #10) ---
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

        # --- round 6: chart auto-render, scratch promotion, publish dry-run
        # (feedback #1, #2, #3, #4, #7) ---
        r = await client.call_tool("run_python", {
            "code": (
                "import matplotlib.pyplot as plt\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot(toy_scores['score'])\n"
                "result = fig\n"
            ),
            "session_id": s2, "title": "Score chart", "description": "A plot of scores.",
            "input_row_ids": [dataset_row], "output_type": "chart",
        })
        assert r.data["status"] == "ok", r.data
        assert r.data["content_format"] == "png", (
            "assigning a matplotlib Figure as `result` should auto-render to png, "
            "not land in storage as a 'Figure(...)' string (feedback #2)"
        )
        assert any(c.type == "image" for c in r.content), (
            "a chart result should come back as an actual inline image content "
            "block, not just a text placeholder (feedback #2)"
        )
        print("[ok] run_python: a matplotlib Figure `result` auto-renders to an inline PNG image")

        r = await client.call_tool("run_sql", {
            "code": "SELECT COUNT(*) AS n FROM toy_scores",
            "session_id": s2, "title": "Scratch to promote",
            "description": "A scratch run worth keeping after all.",
            "input_row_ids": [dataset_row], "scratch": True,
        })
        assert r.data["status"] == "ok", r.data
        scratch_id = r.data.get("scratch_id")
        assert scratch_id, "a scratch run should return a scratch_id (feedback #4)"
        assert r.data.get("input_tables") == {dataset_row: "toy_scores"}
        print(f"[ok] scratch run_sql -> scratch_id {scratch_id}, input_tables inlined")

        r = await client.call_tool("promote_scratch", {
            "scratch_id": scratch_id, "session_id": s2,
            "title": "Row count", "description": "Promoted from a scratch check.",
        })
        assert "error" not in r.data, r.data
        promoted_row = r.data["row_id"]
        assert r.data["row_count"] == 1, r.data
        print(f"[ok] promote_scratch persisted the cached scratch run without re-running it -> {promoted_row}")

        r = await client.call_tool(
            "search_artifacts", {"query": "Row count", "session_id": s2}
        )
        assert any(h["row_id"] == promoted_row for h in r.data["results"]), (
            "a promoted scratch run must become a real, searchable artifact"
        )
        print("[ok] promoted artifact is searchable, unlike the scratch run it came from")

        r = await client.call_tool("promote_scratch", {
            "scratch_id": scratch_id, "session_id": s2,
            "title": "Row count again", "description": "Re-promoting an already-consumed id.",
        })
        assert "error" in r.data and "no cached scratch run" in r.data["error"], (
            "promoting an already-consumed (or unknown) scratch_id should fail clearly, "
            "not silently re-run or duplicate"
        )
        print("[ok] promote_scratch on a consumed/unknown scratch_id fails clearly")

        r = await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Preview Report", "session_id": s2,
            "description": "Narrative for exercising publish_report's dry-run mode.",
            "content_format": "markdown",
            "content_text": (
                f"See {{{{artifact:{dataset_row}}}}} and "
                "{{artifact:not-a-real-row-id}} for details."
            ),
        })
        preview_narrative_row = r.data["row_id"]
        r = await client.call_tool("publish_report", {
            "row_id": preview_narrative_row, "session_id": s2, "dry_run": True,
        })
        assert "error" not in r.data, r.data
        assert "path" not in r.data, "dry_run must not write an HTML file"
        assert any(e["row_id"] == dataset_row and e["resolved"] for e in r.data["embeds"]), r.data
        assert "not-a-real-row-id" in r.data["broken_row_ids"], (
            "dry_run should flag an embed that doesn't resolve (feedback #3, #7)"
        )
        print(f"[ok] publish_report(dry_run=True) reports resolved/broken embeds without writing a file: "
              f"broken={r.data['broken_row_ids']}")

        r = await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Real Report", "session_id": s2,
            "description": "Narrative for exercising the real publish's embeds list.",
            "content_format": "markdown",
            "content_text": f"See {{{{artifact:{dataset_row}}}}} for details.",
        })
        real_narrative_row = r.data["row_id"]
        r = await client.call_tool("publish_report", {
            "row_id": real_narrative_row, "session_id": s2,
        })
        assert "error" not in r.data, r.data
        assert any(e["row_id"] == dataset_row and e["resolved"] for e in r.data["embeds"]), (
            "a real publish should also report which artifacts it embedded (feedback #3)"
        )
        print("[ok] publish_report (real) reports its resolved embeds alongside the file path")

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
