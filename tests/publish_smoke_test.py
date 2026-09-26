"""Layer-2b smoke test: publish_report, over the MCP wire (same pattern as
tests/mcp_smoke_test.py). Builds a narrative that embeds both a tabular
artifact and a chart, publishes it, and checks the rendered HTML actually
contains the substituted table/image rather than the raw {{artifact:...}}
markers.

Run: .venv/Scripts/python.exe tests/publish_smoke_test.py
"""

from __future__ import annotations

import asyncio
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
os.environ["DSOS_DB_PATH"] = "data/test-runs/publish_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

# A minimal valid 1x1 transparent PNG.
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


async def main() -> None:
    async with Client(mcp) as client:
        r = await client.call_tool("start_session", {"question": "publish smoke test"})
        s1 = r.data["session_id"]

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "toy.csv"
            csv_path.write_text("team,score\na,10\nb,20\nc,15\n")
            r = await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Toy Scores",
                "description": "Synthetic dataset for the publish smoke test.",
                "content_format": "parquet", "content_path": str(csv_path),
                "session_id": s1,
            })
            dataset_row = r.data["row_id"]

        # save_artifact's content_path ingestion only special-cases
        # type="dataset" (csv/tsv/json/parquet -> DataFrame); a chart's raw
        # png bytes come from run_python's `result`, same as a real agent
        # would produce one (see run_python's docstring).
        r = await client.call_tool("run_python", {
            "code": f"import base64; result = base64.b64decode({_TINY_PNG_B64!r})",
            "session_id": s1, "title": "Score chart",
            "description": "Synthetic chart for the publish smoke test.",
            "input_row_ids": [dataset_row], "output_type": "chart",
        })
        assert r.data["status"] == "ok", r.data
        chart_row = r.data["row_id"]

        r = await client.call_tool("run_sql", {
            "code": "SELECT team, score FROM toy_scores WHERE score > 10",
            "session_id": s1, "title": "High scorers", "description": "Teams scoring above 10.",
            "input_row_ids": [dataset_row],
        })
        query_row = r.data["row_id"]

        narrative_md = (
            f"# Report\n\nTeams scored well. See {{{{artifact:{dataset_row}}}}} for the raw "
            f"data, {{{{artifact:{query_row}}}}} for the high scorers, and "
            f"{{{{artifact:{chart_row}}}}} for the chart."
        )
        r = await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Team Scores Report", "session_id": s1,
            "description": "A short report on team scores.", "content_format": "markdown",
            "content_text": narrative_md,
        })
        narrative_row = r.data["row_id"]
        print(f"[ok] saved narrative embedding dataset/query/chart -> {narrative_row}")

        r = await client.call_tool(
            "publish_report", {"row_id": narrative_row, "session_id": s1}
        )
        assert "error" not in r.data, r.data
        path = r.data["path"]
        assert Path(path).is_file(), f"publish_report should write a real file: {path}"
        print(f"[ok] publish_report wrote {path}")

        touched = set(r.data["artifact_row_ids"])
        assert {narrative_row, dataset_row, query_row, chart_row} <= touched, r.data
        print("[ok] artifact_row_ids covers the narrative and everything it embeds")

        html_text = Path(path).read_text(encoding="utf-8")
        assert "{{artifact:" not in html_text, "raw embed markers should be substituted away"
        assert "<table" in html_text, "dataset/query embeds should render as HTML tables"
        assert "team" in html_text and "score" in html_text
        assert "data:image/png;base64," in html_text, "chart embed should inline as base64 PNG"
        assert "<h1>Report</h1>" in html_text, "narrative markdown body should render to HTML"
        assert html_text.count("<table") == html_text.count("</table>"), (
            "embedded tables must not be split apart by markdown's blank-line paragraph rule"
        )
        assert "<p></table>" not in html_text, "a table must not be torn in half by a stray </p><p>"
        print("[ok] rendered HTML is self-contained: tables + inlined image, no raw embed markers")

        # publish_report should NOT create its own artifact row (Philosophy
        # #5: a rendered export exits this layer, it isn't itself a
        # versioned artifact) — search shouldn't surface the .html output.
        r = await client.call_tool(
            "search_artifacts", {"query": "Team Scores Report", "session_id": s1}
        )
        assert all(res["row_id"] != path for res in r.data["results"])
        print("[ok] publish_report did not register itself as a new artifact")

        # publish_report on a non-narrative row should fail cleanly.
        r = await client.call_tool(
            "publish_report", {"row_id": dataset_row, "session_id": s1}
        )
        assert "error" in r.data and "narrative" in r.data["error"]
        print(f"[ok] publish_report on a non-narrative row fails cleanly: {r.data['error']}")

    print("\npublish_report smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
