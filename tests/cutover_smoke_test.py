"""Layer-2h smoke test: the cutover (WP-G1).

Until this WP every exploratory row was shown to every agent, so a store
full of unclaimed findings looked exactly like the landfill Design Doc II
warns about. This test pins the three behaviours that make the lifecycle
visible:

1. `search_artifacts` hides exploratory rows by default and returns them
   with `include_exploratory=True`. That default now reaches every caller
   (the prior-work signal, the GUI gallery, producer search), so it is
   checked at the store's public surface.
2. A question with no activity for `DSOS_ABANDON_DAYS` (default 14) is
   abandoned by the sweep that runs inside the next `start_session`, while a
   day-old one is untouched. Activity is `max(created_at, claimed_at, the
   CLAIMANT's latest tool call)` — so a question whose claim is still live
   (lease default 30 min) is never swept, even if `DSOS_ABANDON_DAYS` is set
   below the lease window. That last case is the guard that keeps the two
   thresholds from contradicting each other.
3. The GUI gallery hides exploratory rows unless `?include=exploratory`.

Run: .venv/Scripts/python.exe tests/cutover_smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "data/test-runs/cutover_smoke_test/store.db"
os.environ["DSOS_DB_PATH"] = DB_PATH
os.environ.pop("DSOS_DISABLE_BOARD", None)
shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402

from dsos import db, gui, spine, store  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _ago(*, days: float = 0, minutes: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, minutes=minutes)).isoformat()


def _question(conn: sqlite3.Connection, question_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


async def main() -> None:
    conn = db.connect(DB_PATH)

    async with Client(mcp) as client:
        s1 = (await client.call_tool(
            "start_session", {"question": "cutover smoke test"})).data["session_id"]

        # --- 1. the exploratory default.
        csv_path = Path(tempfile.mkdtemp()) / "cutover.csv"
        csv_path.write_text("team,score\na,10\nb,20\n", encoding="utf-8")
        dataset = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Cutover registered dataset",
            "description": "A registered dataset, to prove results stay searchable.",
            "content_path": str(csv_path), "content_format": "csv",
            "session_id": s1,
        })).data
        exploratory = (await client.call_tool("run_sql", {
            "code": "SELECT team, score FROM in_1",
            "session_id": s1, "title": "Cutover exploratory finding",
            "description": "An unclaimed finding, to prove it is hidden by default.",
            "input_row_ids": [dataset["row_id"]],
        })).data
        check("the run is exploratory, as the cutover assumes",
              exploratory.get("artifact_status") == "exploratory",
              str(exploratory.get("artifact_status")))
        exploratory_id = exploratory["row_id"]

        default_hits = (await client.call_tool("search_artifacts", {
            "query": "Cutover exploratory finding", "session_id": s1})).data["results"]
        check("an exploratory row is absent from default search",
              not any(r["row_id"] == exploratory_id for r in default_hits),
              str([r["title"] for r in default_hits]))
        with_flag = (await client.call_tool("search_artifacts", {
            "query": "Cutover exploratory finding", "session_id": s1,
            "include_exploratory": True})).data["results"]
        check("include_exploratory=True returns it",
              any(r["row_id"] == exploratory_id for r in with_flag),
              str([r["title"] for r in with_flag]))
        # A caller that needed the old behaviour passes the flag explicitly;
        # a result is still found with no flag at all.
        result_hits = (await client.call_tool("search_artifacts", {
            "query": "Cutover registered dataset", "session_id": s1})).data["results"]
        check("claimed results are still found with no flag",
              any(r["title"] == "Cutover registered dataset" for r in result_hits),
              str([r["title"] for r in result_hits]))

        # --- 2. the abandon sweep, on the next start_session.
        dead = store.start_session(conn, "cutover: a dead claim")
        q_old = spine.create_question(
            conn, question="cutover: fifteen-day-idle question", status="open",
            asked_by=dead, claimed_by=dead,
        )
        q_recent = spine.create_question(
            conn, question="cutover: one-day-idle question", status="open", asked_by=dead,
        )
        q_answered = spine.create_question(
            conn, question="cutover: finished long ago", status="answered", asked_by=dead,
        )
        conn.execute(
            "UPDATE questions SET created_at = ?, claimed_at = ? WHERE id = ?",
            (_ago(days=15), _ago(days=15), q_old),
        )
        conn.execute(
            "UPDATE questions SET created_at = ? WHERE id = ?", (_ago(days=1), q_recent)
        )
        conn.execute(
            "UPDATE questions SET created_at = ?, closed_at = ? WHERE id = ?",
            (_ago(days=15), _ago(days=15), q_answered),
        )
        conn.commit()

        await client.call_tool("start_session", {"question": "cutover: trigger the sweep"})

        old = _question(conn, q_old)
        check("a 15-day-idle question is abandoned on the next start_session",
              old["status"] == "abandoned", str(old["status"]))
        check("the swept question records closed_at",
              bool(old["closed_at"]) and old["closed_at"] >= _ago(days=15), str(old["closed_at"]))
        check("the swept question's claim is cleared",
              old["claimed_by"] is None and old["claimed_at"] is None,
              f"claimed_by={old['claimed_by']!r}")
        check("a 1-day-idle question is untouched",
              _question(conn, q_recent)["status"] == "open",
              str(_question(conn, q_recent)["status"]))
        check("a finished question is not swept",
              _question(conn, q_answered)["status"] == "answered",
              str(_question(conn, q_answered)["status"]))

        # The lease guard: a live claim is never swept, whatever the abandon
        # window says. Set it BELOW the 30-minute lease, then leave a question
        # whose only activity is 20 minutes old — inside the lease, outside
        # the (now 14.4-minute) abandon window. Activity alone would sweep it;
        # the lease must not.
        claimant = store.start_session(conn, "cutover: live claimant")
        q_live = spine.create_question(
            conn, question="cutover: live-lease question", status="in_progress",
            asked_by=claimant, claimed_by=claimant,
        )
        live_ts = _ago(minutes=20)
        store.log_tool_call(conn, claimant, "run_sql", {}, "renewal", [])
        conn.execute("UPDATE tool_calls SET ts = ? WHERE session_id = ?", (live_ts, claimant))
        conn.execute(
            "UPDATE questions SET created_at = ?, claimed_at = ? WHERE id = ?",
            (_ago(days=15), live_ts, q_live),
        )
        conn.commit()

        os.environ["DSOS_ABANDON_DAYS"] = "0.01"  # 14.4 min, below the 30-min lease
        try:
            await client.call_tool("start_session", {"question": "cutover: sweep with a live lease"})
            live = _question(conn, q_live)
            check("a question whose lease is still live is not swept",
                  live["status"] == "in_progress" and live["claimed_by"] == claimant,
                  f"status={live['status']!r} claimed_by={live['claimed_by']!r}")
        finally:
            os.environ.pop("DSOS_ABANDON_DAYS", None)

    # --- 3. the GUI gallery.
    client = TestClient(gui.app)
    r = client.get("/artifacts")
    check("the gallery hides exploratory rows by default",
          r.status_code == 200 and "Cutover exploratory finding" not in r.text
          and "Cutover registered dataset" in r.text,
          f"status={r.status_code}")
    r = client.get("/artifacts", params={"include": "exploratory"})
    check("?include=exploratory shows them again",
          r.status_code == 200 and "Cutover exploratory finding" in r.text,
          f"status={r.status_code}")

    shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)
    if FAILURES:
        print(f"\ncutover smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\ncutover smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
