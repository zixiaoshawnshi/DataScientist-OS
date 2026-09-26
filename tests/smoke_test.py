"""Layer-1 smoke test: exercises the store + execution runner end to end,
without MCP, without the real demo dataset. Uses a throwaway synthetic
table on purpose (see design doc: discovery/skill should be validated
against a *different* dataset than the live demo uses).

Run: .venv/Scripts/python.exe tests/smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from dsos.db import connect
from dsos.execution import run_sql
from dsos.seed import seed
from dsos.store import (
    get_artifact_by_row_id,
    get_lineage,
    log_tool_call,
    reused_artifact_row_ids,
    save_artifact,
    search_artifacts,
    start_session,
)


# A dedicated subdirectory, not "data/<name>.db" — that used to make
# Path(DB_PATH).parent resolve to the *shared* data/ root, so the rmtree
# below wiped the real store.db and data/blobs/ (and every other test's
# files) instead of just this test's own leftovers. Cost real demo data
# once already; don't repeat it.
DB_PATH = "data/test-runs/smoke_test/store.db"


def main() -> None:
    shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)
    conn = connect(DB_PATH)

    bootstrap = start_session(conn, "bootstrap")
    skill_row_id = seed(conn, session_id=bootstrap)
    print(f"[ok] seeded discovery skill: {skill_row_id}")

    # --- round 1: save a synthetic dataset (standing in for a fetched one) ---
    s1 = start_session(conn, "round 1: synthetic smoke-test question")
    toy = pd.DataFrame({"team": ["a", "b", "c"], "score": [10, 20, 15]})
    dataset_row = save_artifact(
        conn, type="dataset", title="Toy Scores",
        description="Synthetic 3-row dataset used only to smoke-test the store.",
        content=toy, content_format="parquet", session_id=s1,
        source={"url": "synthetic://smoke-test", "fetched_at": "now"},
    )
    log_tool_call(conn, s1, "save_artifact", {"title": "Toy Scores"}, "saved dataset", [dataset_row])
    print(f"[ok] saved dataset artifact: {dataset_row}")

    query_row = run_sql(
        conn, code="SELECT team, score FROM toy_scores WHERE score > 10",
        session_id=s1, title="High scorers", description="Teams scoring above 10.",
        input_row_ids=[dataset_row],
    )
    log_tool_call(conn, s1, "run_sql", {"title": "High scorers"}, "2 rows", [query_row, dataset_row])
    print(f"[ok] ran SQL, produced query artifact: {query_row}")

    ancestors = get_lineage(conn, query_row, direction="ancestors")
    assert any(a.row_id == dataset_row for a in ancestors), "lineage should link query -> dataset"
    print(f"[ok] lineage resolved: {[a.title for a in ancestors]}")

    hits = search_artifacts(conn, "how do I find and register a new dataset?", top_k=3)
    print("[ok] search_artifacts('how do I find and register a new dataset?') ->")
    for art, score in hits:
        print(f"      {score:.3f}  {art.type:10s}  {art.title}")
    assert hits[0][0].row_id == skill_row_id, "discovery skill should rank first for a discovery query"
    print("[ok] discovery skill ranked first (cold-start retrieval works)")

    exact_hits = search_artifacts(conn, "Toy Scores", top_k=3)
    assert exact_hits[0][0].row_id == dataset_row and exact_hits[0][1] == 1.0, exact_hits
    print("[ok] exact-title query keyword-matches (score 1.0), not just semantic ranking")

    # --- narrative embedding a dataset: lineage should be auto-derived,
    # with no parent_row_ids passed at all ---
    narrative_row = save_artifact(
        conn, type="narrative", title="Report", description="A short report on team scores.",
        content=f"Teams scored well. See {{{{artifact:{dataset_row}}}}} for the raw data.",
        content_format="markdown", session_id=s1,
    )
    narrative_uses = get_lineage(conn, narrative_row, direction="ancestors")
    assert any(a.row_id == dataset_row for a in narrative_uses), (
        "a {{artifact:row_id}} embed should auto-link lineage without parent_row_ids"
    )
    print("[ok] narrative's {{artifact:...}} embed auto-derived lineage to the dataset")

    # --- Unicode round-trip: blob write/read must be UTF-8 regardless of the
    # OS locale. This is a regression test for a real crash on a
    # GBK-locale Windows machine (emoji in a narrative hit
    # "'gbk' codec can't encode character '\U0001f3c6'" on save). ---
    unicode_text = "🏆 报告 — café naïve — λ² Σ ✓"
    unicode_row = save_artifact(
        conn, type="narrative", title="Unicode Report",
        description="Regression test for UTF-8 blob storage under any OS locale.",
        content=unicode_text, content_format="markdown", session_id=s1,
    )
    assert get_artifact_by_row_id(conn, unicode_row).content == unicode_text, (
        "blob text must round-trip byte-exact through UTF-8, not the locale codec"
    )
    print("[ok] emoji + CJK + accented text round-trips through blob storage")

    # --- round 2: reuse round 1's dataset ---
    s2 = start_session(conn, "round 2: deeper synthetic question")
    query2_row = run_sql(
        conn, code="SELECT AVG(score) AS avg_score FROM toy_scores",
        session_id=s2, title="Average score", description="Average score across all teams.",
        input_row_ids=[dataset_row],  # reusing round 1's dataset artifact
    )
    log_tool_call(conn, s2, "run_sql", {"title": "Average score"}, "1 row", [query2_row, dataset_row])

    reused = reused_artifact_row_ids(conn, s2)
    assert dataset_row in reused, "round 2 should show round 1's dataset as reused"
    print(f"[ok] reuse detected in round 2: {reused}")

    # --- a blob missing from disk (e.g. an accidental cleanup elsewhere)
    # should degrade gracefully, not crash every caller that fetches it ---
    dataset_ref = get_artifact_by_row_id(conn, dataset_row, load_content=False).content_ref
    Path(dataset_ref).unlink()
    art = get_artifact_by_row_id(conn, dataset_row)
    assert art.content is None and art.content_error, "missing blob should set content_error, not raise"
    print(f"[ok] get_artifact_by_row_id degrades gracefully on a missing blob: {art.content_error}")

    broken_query_row = run_sql(
        conn, code="SELECT * FROM toy_scores",
        session_id=s2, title="Should fail", description="Input's blob is gone.",
        input_row_ids=[dataset_row],
    )
    broken = get_artifact_by_row_id(conn, broken_query_row, load_content=False)
    assert broken.status == "error", "run_sql against a missing-blob input should fail cleanly, not crash"
    print("[ok] run_sql against a missing-blob input reports status=error instead of crashing")

    print("\nLayer 1 smoke test passed.")


if __name__ == "__main__":
    main()
