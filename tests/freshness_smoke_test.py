"""Layer-2f smoke test: recency tiebreak in search_artifacts, and
reembed_all after switching embedding backends.

From the pilot report: querying "sales by region" returned the live
dataset, a chart, a transform, AND the description-labeled "deprecated,
do not use" FINAL version, all tied at score 1.0, in no freshness-aware
order — for a headcount query the stale prior-year archive tied at 1.0
and was listed FIRST. Keyword hits all get the same sentinel score
(store.py's search_artifacts docstring explains why), so ties are
common; search_artifacts now breaks them by created_at, newest first.

Also covers reembed_all: installing the sentence-transformers extra
(dsos/embeddings.py) on a store that already has artifacts saved under
the dependency-free hashing fallback leaves those rows embedded in a
different, incomparable vector space until reembed_all rewrites them.

Run: .venv/Scripts/python.exe tests/freshness_smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["DSOS_DB_PATH"] = "data/test-runs/freshness_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

import numpy as np  # noqa: E402

from dsos import db, embeddings, store  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> None:
    conn = db.connect(os.environ["DSOS_DB_PATH"])

    old_row = store.save_artifact(
        conn, type="dataset", title="Sales by Region",
        description="Sales by region — DEPRECATED, do not use, kept for archive only.",
        content="region,sales\nEast,1\nWest,2\n", content_format="csv", session_id="s1",
    )
    new_row = store.save_artifact(
        conn, type="dataset", title="Sales by Region",
        description="Sales by region, current source of truth.",
        content="region,sales\nEast,10\nWest,20\nNorth,15\n", content_format="csv", session_id="s1",
    )
    check("old and new got distinct created_at (no sleep needed)",
          conn.execute("SELECT created_at FROM artifacts WHERE row_id = ?", (old_row,)).fetchone()[0]
          < conn.execute("SELECT created_at FROM artifacts WHERE row_id = ?", (new_row,)).fetchone()[0])

    hits = store.search_artifacts(conn, "Sales by Region", top_k=5)
    check("both artifacts tie at the keyword sentinel score",
          all(score == 1.0 for _, score in hits) and len(hits) == 2, str(hits))
    check("the newer artifact ranks first among the tie",
          hits[0][0].row_id == new_row, f"got {hits[0][0].row_id} first, expected {new_row}")
    check("the older/deprecated artifact ranks second, not dropped",
          hits[1][0].row_id == old_row)

    # reembed_all: fake a pre-upgrade hash-fallback embedding on a row, then
    # confirm reembed_all overwrites it to match the currently-active backend.
    fake_row = store.save_artifact(
        conn, type="dataset", title="Legacy Embedded Row",
        description="Simulates a row saved before switching embedding backends.",
        content="a,b\n1,2\n", content_format="csv", session_id="s1",
    )
    stale_vector = embeddings._hash_embed("some completely different text")
    conn.execute(
        "UPDATE artifacts SET embedding = ? WHERE row_id = ?",
        (stale_vector.astype(np.float32).tobytes(), fake_row),
    )
    conn.commit()
    before = np.frombuffer(
        conn.execute("SELECT embedding FROM artifacts WHERE row_id = ?", (fake_row,)).fetchone()[0],
        dtype=np.float32,
    )
    check("stale embedding really is different from the current backend's",
          not np.allclose(before, embeddings.embed("Legacy Embedded Row\nSimulates a row saved before switching embedding backends.\n")))

    n = store.reembed_all(conn)
    check("reembed_all reports every row updated", n == 3, f"n={n}")
    after = np.frombuffer(
        conn.execute("SELECT embedding FROM artifacts WHERE row_id = ?", (fake_row,)).fetchone()[0],
        dtype=np.float32,
    )
    expected = embeddings.embed("Legacy Embedded Row\nSimulates a row saved before switching embedding backends.\n")
    check("reembed_all rewrites the row into the current backend's space",
          np.allclose(after, expected))

    conn.close()
    shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)
    if FAILURES:
        print(f"\nfreshness smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nfreshness smoke test passed.")


if __name__ == "__main__":
    main()
