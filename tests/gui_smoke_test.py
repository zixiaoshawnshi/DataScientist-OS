"""Layer-3 smoke test: drives the read-only GUI (dsos/gui.py) over HTTP via
FastAPI's TestClient, against a store populated the same way
tests/smoke_test.py does — plus a chart artifact, to exercise the
inlined-PNG rendering path the other smoke tests don't touch.

Run: .venv/Scripts/python.exe tests/gui_smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base64

import pandas as pd
from fastapi.testclient import TestClient

from dsos.db import connect
from dsos.execution import run_sql
from dsos.seed import seed
from dsos.store import get_artifact_by_row_id, save_artifact, start_session

# A dedicated subdirectory, not "data/<name>.db" — that used to make
# Path(DB_PATH).parent resolve to the *shared* data/ root, so the rmtree
# below wiped the real store.db and data/blobs/ (and every other test's
# files) instead of just this test's own leftovers. Cost real demo data
# once already; don't repeat it.
DB_PATH = "data/test-runs/gui_smoke_test/store.db"

# A minimal valid 1x1 transparent PNG — no need for a real plotting library
# just to exercise the GUI's inlined-base64-PNG rendering path.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def main() -> None:
    shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)
    conn = connect(DB_PATH)

    bootstrap = start_session(conn, "bootstrap")
    seed(conn, session_id=bootstrap)

    s1 = start_session(conn, "round 1: gui smoke-test question")
    toy = pd.DataFrame({"team": ["a", "b", "c"], "score": [10, 20, 15]})
    dataset_row = save_artifact(
        conn, type="dataset", title="Toy Scores",
        description="Synthetic 3-row dataset used only to smoke-test the GUI.",
        content=toy, content_format="parquet", session_id=s1,
        source={"url": "synthetic://gui-smoke-test", "fetched_at": "now"},
    )
    query_row = run_sql(
        conn, code="SELECT team, score FROM toy_scores WHERE score > 10",
        session_id=s1, title="High scorers", description="Teams scoring above 10.",
        input_row_ids=[dataset_row],
    )
    chart_row = save_artifact(
        conn, type="chart", title="Score chart",
        description="A toy chart, to exercise the GUI's inlined-PNG path.",
        content=_TINY_PNG, content_format="png", session_id=s1,
        parent_row_ids=[dataset_row],
    )
    narrative_row = save_artifact(
        conn, type="narrative", title="Report", description="A short report on team scores.",
        content=(
            f"Teams scored well. See {{{{artifact:{dataset_row}}}}} for the raw data "
            f"and {{{{artifact:{chart_row}}}}} for the chart."
        ),
        content_format="markdown", session_id=s1,
    )
    # a second version of the dataset, to exercise "other versions"
    dataset_artifact_id = get_artifact_by_row_id(conn, dataset_row, load_content=False).artifact_id
    save_artifact(
        conn, type="dataset", title="Toy Scores", artifact_id=dataset_artifact_id,
        description="v2: same synthetic dataset, re-saved to test version history.",
        content=toy, content_format="parquet", session_id=s1,
    )

    s2 = start_session(conn, "round 2: deeper gui smoke-test question")
    run_sql(
        conn, code="SELECT AVG(score) AS avg_score FROM toy_scores",
        session_id=s2, title="Average score", description="Average score, round 2.",
        input_row_ids=[dataset_row],
    )

    import os
    os.environ["DSOS_DB_PATH"] = DB_PATH
    from dsos import gui  # import after DSOS_DB_PATH is set, like mcp_smoke_test.py

    client = TestClient(gui.app)

    r = client.get("/")
    assert r.status_code == 200 and "round 1: gui smoke-test question" in r.text
    print("[ok] GET / lists sessions")

    r = client.get(f"/sessions/{s1}")
    assert r.status_code == 200 and "Toy Scores" in r.text
    print("[ok] GET /sessions/{id} shows artifacts created in that session")

    r = client.get(f"/sessions/{s1}/tool_calls")
    assert r.status_code == 200
    print("[ok] GET /sessions/{id}/tool_calls (htmx partial) responds")

    r = client.get("/artifacts")
    assert r.status_code == 200 and "Toy Scores" in r.text
    print("[ok] GET /artifacts (unfiltered gallery) lists artifacts")

    r = client.get("/artifacts", params={"type": "chart"})
    assert r.status_code == 200 and "Score chart" in r.text and "Report" not in r.text
    print("[ok] GET /artifacts?type=chart filters by type")

    r = client.get("/artifacts", params={"q": "Toy Scores"})
    assert r.status_code == 200 and "Toy Scores" in r.text
    print("[ok] GET /artifacts?q=... shares search_artifacts with the agent's index")

    r = client.get(f"/artifacts/{dataset_row}")
    assert r.status_code == 200
    assert "team" in r.text and "score" in r.text, "tabular preview should render as a table"
    assert "Other versions" in r.text, "a second version should surface version history"
    print("[ok] GET /artifacts/{row_id} renders a tabular preview + version history")

    r = client.get(f"/artifacts/{query_row}")
    assert r.status_code == 200 and "Execution" in r.text and "sql" in r.text
    print("[ok] GET /artifacts/{row_id} for a query shows its execution trace")

    # A blob missing from disk (this is exactly what happened to the real
    # store: a test's cleanup step once rmtree'd the shared data/ dir,
    # wiping data/blobs/ out from under it) must render a friendly message,
    # not crash with a raw FileNotFoundError traceback / 500.
    orphan_row = save_artifact(
        conn, type="dataset", title="Orphaned dataset",
        description="Its blob is deleted below, to test the missing-blob path.",
        content=toy, content_format="parquet", session_id=s1,
    )
    orphan_ref = get_artifact_by_row_id(conn, orphan_row, load_content=False).content_ref
    Path(orphan_ref).unlink()
    r = client.get(f"/artifacts/{orphan_row}")
    assert r.status_code == 200, "a missing blob must not 500 the whole page"
    assert "content blob missing on disk" in r.text
    print("[ok] GET /artifacts/{row_id} with a missing blob renders a friendly message, not a 500")

    r = client.get(f"/artifacts/{chart_row}")
    assert r.status_code == 200 and "data:image/png;base64," in r.text
    print("[ok] GET /artifacts/{row_id} for a chart inlines the PNG as a base64 data URI")

    r = client.get(f"/artifacts/{narrative_row}")
    assert r.status_code == 200
    assert "Toy Scores" in r.text and "Score chart" in r.text, "uses should list both embeds"
    print("[ok] GET /artifacts/{row_id} for a narrative lists both {{artifact:...}} uses")

    r = client.get(f"/artifacts/{dataset_row}/lineage")
    assert r.status_code == 200 and "graph LR" in r.text and query_row in r.text
    print("[ok] GET /artifacts/{row_id}/lineage renders a Mermaid graph with neighbors")

    r = client.get("/artifacts/does-not-exist")
    assert r.status_code == 404
    print("[ok] an unknown row_id 404s instead of crashing")

    print("\nLayer 3 (GUI) smoke test passed.")


if __name__ == "__main__":
    main()
