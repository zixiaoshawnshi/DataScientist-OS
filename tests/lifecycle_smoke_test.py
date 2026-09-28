"""Layer-2g smoke test: the lifecycle — exploratory / result / superseded,
`mark`, the derived validation verdict, and the fields search returns.

This is the spine the rest of the design is built on, so the test is mostly
about what is *not* allowed and what is *derived*:

1. A run defaults to `exploratory` (computation is a finding until someone
   claims it) and save_artifact to `result` (registering it is the claim).
2. Every illegal transition is rejected with a message that names the
   allowed ones, so a model that guessed wrong is corrected in the same
   call rather than by a second failure.
3. A superseded row disappears from default search but is still returned by
   get_artifact, and the row that replaced it is still searchable. This is
   the "kept, not deleted, no longer current" property: if superseded
   implemented itself as a delete or a hard hide, this test is what catches
   it — the row has to stay fully readable, it just stops being the answer
   to a search.
4. Two verdicts leave TWO validations rows, and a human verdict beats a
   LATER model verdict. That is only expressible because the table is
   append-only and the current verdict is derived from it; a mutable
   status column cannot represent it at all. (The human verdict is
   written directly, because nothing in the producer surface issues one.)
5. The derived stale check (D11): a dataset with refresh_after="1d" and
   fetched_at three days ago makes its descendants stale, and stale
   overrides `confirmed` — but not `contradicted`. Stale is computed on
   read and is never written to the database.
6. The two fields that are only worth having if they are hard to fill in
   dishonestly: caveats are short properties, and a confidence entry
   without a basis is refused (that requirement is the guard against a
   model answering "high" every time).

Plus the display half: the GUI artifact page shows the status, the caveats
and the validation history.

Run: .venv/Scripts/python.exe tests/lifecycle_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

DB_PATH = "data/test-runs/lifecycle_smoke_test/store.db"
os.environ["DSOS_DB_PATH"] = DB_PATH
shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dsos import db, store  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def row_status(conn: sqlite3.Connection, row_id: str) -> str | None:
    row = conn.execute("SELECT status FROM artifacts WHERE row_id = ?", (row_id,)).fetchone()
    return row["status"] if row else None


def validations_rows(conn: sqlite3.Connection, row_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM validations WHERE row_id = ? ORDER BY at, rowid", (row_id,)
        )
    ]


def append_validation(
    conn: sqlite3.Connection, row_id: str, *, verdict: str, by: str, basis: str, at: str
) -> None:
    """Write a validations row directly — the one thing the producer surface
    cannot do. `mark` always records by='model', because a model's verdict on
    its own artifact is the thing the append-only ledger exists to allow
    WITHOUT destroying; only a human outside this system can say 'confirmed'."""
    conn.execute(
        """INSERT INTO validations (id, row_id, verdict, by, session_id, at, basis)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (f"manual-{by}-{verdict}-{at}", row_id, verdict, by, "s1", at, basis),
    )
    conn.commit()


def _csv(tmp: Path, name: str, body: str) -> str:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return str(path)


async def main() -> None:
    conn = db.connect(DB_PATH)
    tmp = Path(tempfile.mkdtemp())

    async with Client(mcp) as client:
        s1 = (await client.call_tool(
            "start_session", {"question": "lifecycle smoke test"})).data["session_id"]

        # --- 1. the two defaults.
        dataset = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Order lines",
            "description": "One row per order line, fetched once for this test.",
            "content_path": _csv(tmp, "orders.csv", "order_id,qty,price\n1,2,10\n2,1,25\n"),
            "content_format": "csv", "session_id": s1,
        })).data
        check("save_artifact defaults to status=result",
              row_status(conn, dataset["row_id"]) == "result",
              str(row_status(conn, dataset["row_id"])))

        run = (await client.call_tool("run_sql", {
            "code": "SELECT order_id, qty * price AS total FROM in_1 ORDER BY order_id",
            "session_id": s1, "title": "Order totals",
            "description": "Line totals per order.",
            "input_row_ids": [dataset["row_id"]],
        })).data
        check("run_sql defaults to status=exploratory",
              row_status(conn, run["row_id"]) == "exploratory",
              str(row_status(conn, run["row_id"])))
        check("the run's response reports the artifact's lifecycle status",
              run.get("artifact_status") == "exploratory", str(run.get("artifact_status")))
        # ...and the run's own top-level status is still the run's outcome, not
        # the artifact's lifecycle: "ok" here, "error" on a failure.
        check("the run's top-level status is still the run's own outcome",
              run.get("status") == "ok", str(run.get("status")))

        claimed = (await client.call_tool("run_sql", {
            "code": "SELECT SUM(qty * price) AS revenue FROM in_1",
            "session_id": s1, "title": "Revenue",
            "description": "Total revenue across the fetched lines.",
            "input_row_ids": [dataset["row_id"]], "status": "result",
        })).data
        check("run_sql takes an explicit status",
              row_status(conn, claimed["row_id"]) == "result",
              str(row_status(conn, claimed["row_id"])))

        # --- 2. the illegal transitions, each with a naming message.
        back_to_exploratory = (await client.call_tool("mark", {
            "row_id": claimed["row_id"], "session_id": s1, "status": "exploratory",
        })).data
        check("result -> exploratory is rejected",
              "error" in back_to_exploratory, str(back_to_exploratory)[:160])
        check("the rejection names the allowed transitions",
              all(t in str(back_to_exploratory.get("error"))
                  for t in ("exploratory -> result", "exploratory -> superseded",
                            "result -> superseded")),
              str(back_to_exploratory.get("error"))[:200])

        neither = (await client.call_tool("mark", {
            "row_id": run["row_id"], "session_id": s1,
        })).data
        check("mark with neither status nor verdict is rejected",
              "error" in neither, str(neither)[:160])

        no_basis = (await client.call_tool("mark", {
            "row_id": run["row_id"], "session_id": s1, "verdict": "confirmed",
        })).data
        check("a verdict without a basis is rejected",
              "error" in no_basis and "basis" in str(no_basis.get("error")),
              str(no_basis.get("error"))[:160])

        no_replacement = (await client.call_tool("mark", {
            "row_id": run["row_id"], "session_id": s1, "status": "superseded",
        })).data
        check("superseded without superseded_by is rejected",
              "error" in no_replacement and "superseded_by" in str(no_replacement.get("error")),
              str(no_replacement.get("error"))[:160])

        unknown_replacement = (await client.call_tool("mark", {
            "row_id": run["row_id"], "session_id": s1, "status": "superseded",
            "superseded_by": "not-a-row-id",
        })).data
        check("superseded_by must be an existing row",
              "error" in unknown_replacement, str(unknown_replacement.get("error"))[:160])

        check("none of the rejected marks wrote anything",
              len(validations_rows(conn, run["row_id"])) == 0
              and row_status(conn, run["row_id"]) == "exploratory",
              f"status={row_status(conn, run['row_id'])}")

        # --- 3. superseded: kept, out of default search, still readable.
        legacy = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Order lines (first fetch)",
            "description": "The first fetch of the order lines, before the refill.",
            "content_path": _csv(tmp, "orders-old.csv", "order_id,qty,price\n1,1,9\n"),
            "content_format": "csv", "session_id": s1,
        })).data
        replacement = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Order lines (refilled)",
            "description": "The refetched order lines, with the late orders included.",
            "content_path": _csv(tmp, "orders-new.csv", "order_id,qty,price\n1,2,10\n2,1,25\n3,4,7\n"),
            "content_format": "csv", "session_id": s1,
        })).data
        marked = (await client.call_tool("mark", {
            "row_id": legacy["row_id"], "session_id": s1, "status": "superseded",
            "superseded_by": replacement["row_id"],
        })).data
        check("mark reports the new state", marked.get("status") == "superseded"
              and marked.get("superseded_by") == replacement["row_id"], str(marked)[:200])

        async def search(query: str, **kwargs) -> list[dict]:
            return (await client.call_tool(
                "search_artifacts",
                {"query": query, "session_id": s1, **kwargs})).data["results"]

        default_hits = await search("Order lines first fetch")
        check("a superseded row is gone from default search",
              not any(r["row_id"] == legacy["row_id"] for r in default_hits),
              str([r["title"] for r in default_hits]))
        with_hidden = await search("Order lines first fetch", include_superseded=True)
        check("include_superseded=True still returns it",
              any(r["row_id"] == legacy["row_id"] for r in with_hidden),
              str([r["title"] for r in with_hidden]))
        replacement_hits = await search("Order lines refilled")
        check("its replacement is still searchable",
              any(r["row_id"] == replacement["row_id"] for r in replacement_hits),
              str([r["title"] for r in replacement_hits]))
        check("the search hit carries the lifecycle fields",
              all(k in with_hidden[0] for k in
                  ("status", "created_at", "validation", "superseded_by")),
              str(sorted(with_hidden[0])))

        fetched = (await client.call_tool("get_artifact", {
            "row_id": legacy["row_id"], "session_id": s1})).data
        check("get_artifact still returns a superseded row, in full",
              fetched.get("row_id") == legacy["row_id"]
              and fetched.get("status") == "superseded"
              and fetched.get("superseded_by") == replacement["row_id"],
              str({k: fetched.get(k) for k in ("row_id", "status", "superseded_by")}))
        check("the blob is still readable on a superseded row",
              fetched.get("row_count") == 1 and bool(fetched.get("preview")),
              str({k: fetched.get(k) for k in ("row_count", "preview")}))

        again = (await client.call_tool("mark", {
            "row_id": legacy["row_id"], "session_id": s1, "status": "result",
        })).data
        check("superseded is terminal", "error" in again, str(again.get("error"))[:200])

        # --- 4. append-only validation: a human verdict beats a LATER model one.
        checked = (await client.call_tool("save_artifact", {
            "type": "query", "title": "Revenue by month",
            "description": "Revenue rolled up by month, ready to be checked.",
            "content_text": "month,revenue\n2026-01,120\n", "content_format": "csv",
            "session_id": s1,
        })).data
        first = (await client.call_tool("mark", {
            "row_id": checked["row_id"], "session_id": s1, "verdict": "needs_review",
            "basis": "the rollup used a different month boundary than the report",
        })).data
        check("mark reports the derived current verdict",
              first.get("validation", {}).get("current") == "needs_review",
              str(first.get("validation"))[:200])

        model_at = validations_rows(conn, checked["row_id"])[0]["at"]
        # The human verdict is stamped a minute BEFORE the model's, so the
        # next mark really is the later opinion in time, not merely in the
        # ledger — the whole point is a model speaking after a person.
        human_at = (
            datetime.fromisoformat(model_at).astimezone(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        append_validation(
            conn, checked["row_id"], verdict="confirmed", by="human", at=human_at,
            basis="a person checked the rollup against the invoice export",
        )
        after_human = store.validation_status(conn, checked["row_id"])
        check("two verdicts leave two validations rows",
              len(validations_rows(conn, checked["row_id"])) == 2,
              str([(r["by"], r["verdict"]) for r in validations_rows(conn, checked["row_id"])]))
        check("a human verdict beats an earlier model verdict",
              after_human["current"] == "confirmed", str(after_human["current"]))

        # ...and it still beats one that arrives LATER, which a mutable
        # status column could not express at all.
        await client.call_tool("mark", {
            "row_id": checked["row_id"], "session_id": s1, "verdict": "contradicted",
            "basis": "re-derived from the same export and it does not match",
        })
        after_late_model = (await client.call_tool("get_artifact", {
            "row_id": checked["row_id"], "session_id": s1})).data
        check("a human verdict beats a LATER model verdict",
              after_late_model["validation"]["current"] == "confirmed",
              str(after_late_model["validation"]["current"]))
        history = store.validation_status(conn, checked["row_id"])["history"]
        check("the history is the whole ledger, newest first",
              [(h["by"], h["verdict"]) for h in history]
              == [("model", "contradicted"), ("model", "needs_review"),
                  ("human", "confirmed")],
              str([(h["by"], h["verdict"]) for h in history]))
        check("stale is never written to the database",
              not any(r["verdict"] == "stale" for r in validations_rows(conn, checked["row_id"])),
              str([r["verdict"] for r in validations_rows(conn, checked["row_id"])]))

        # --- 5. the derived stale check (D11), and what it may not override.
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        aged = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Exchange rates (january)",
            "description": "Daily exchange rates for January, fetched from a public API.",
            "content_path": _csv(tmp, "fx.csv", "day,usdeur\n2026-01-02,1.09\n"),
            "content_format": "csv", "session_id": s1,
            "source": {"url": "https://example.test/fx", "fetched_at": three_days_ago,
                       "refresh_after": "1d"},
        })).data
        confirmed_desc = (await client.call_tool("run_sql", {
            "code": "SELECT AVG(usdeur) AS mean_usdeur FROM in_1",
            "session_id": s1, "title": "Mean EUR rate (january)",
            "description": "Mean rate over the fetched January days.",
            "input_row_ids": [aged["row_id"]], "status": "result",
        })).data
        contradicted_desc = (await client.call_tool("run_sql", {
            "code": "SELECT MIN(usdeur) AS min_usdeur FROM in_1",
            "session_id": s1, "title": "Lowest EUR rate (january)",
            "description": "The worst day in the fetched January days.",
            "input_row_ids": [aged["row_id"]], "status": "result",
        })).data
        await client.call_tool("mark", {
            "row_id": confirmed_desc["row_id"], "session_id": s1, "verdict": "confirmed",
            "basis": "recomputed the mean by hand over the same two rows",
        })
        await client.call_tool("mark", {
            "row_id": contradicted_desc["row_id"], "session_id": s1, "verdict": "contradicted",
            "basis": "the published figure uses a different quote convention",
        })
        confirmed_state = store.validation_status(conn, confirmed_desc["row_id"])
        contradicted_state = store.validation_status(conn, contradicted_desc["row_id"])
        check("an expired ancestor dataset makes its descendant stale",
              confirmed_state["current"] == "stale", str(confirmed_state["current"]))
        check("stale overrides confirmed", bool(confirmed_state["stale_reason"]),
              str(confirmed_state.get("stale_reason")))
        check("a contradicted descendant stays contradicted",
              contradicted_state["current"] == "contradicted",
              str(contradicted_state["current"]))
        check("the human-verified verdict is untouched in the ledger",
              [r["verdict"] for r in validations_rows(conn, confirmed_desc["row_id"])]
              == ["confirmed"],
              str([r["verdict"] for r in validations_rows(conn, confirmed_desc["row_id"])]))

        # The fallbacks, which are the normal case rather than the error case:
        # a dataset with no source, a "static" one, and a source whose
        # fetched_at never was an ISO timestamp.
        for label, source in (
            ("no source at all", None),
            ("a static source", {"url": "https://example.test/handbook",
                                 "fetched_at": three_days_ago, "refresh_after": "static"}),
            ("a non-ISO fetched_at", {"url": "https://example.test/handbook",
                                      "fetched_at": "sometime last month",
                                      "refresh_after": "1d"}),
        ):
            plain = (await client.call_tool("save_artifact", {
                "type": "dataset", "title": f"Handbook ({label})",
                "description": f"A static reference table, registered with {label}.",
                "content_path": _csv(tmp, "handbook.csv", "k,v\n1,2\n"),
                "content_format": "csv", "session_id": s1, "source": source,
            })).data
            child = (await client.call_tool("run_sql", {
                "code": "SELECT SUM(v) AS total FROM in_1", "session_id": s1,
                "title": f"Handbook total ({label})",
                "description": "A total over the reference table.",
                "input_row_ids": [plain["row_id"]], "status": "result",
            })).data
            check(f"a dataset with {label} is not stale",
                  store.validation_status(conn, child["row_id"])["current"] == "unvalidated",
                  str(store.validation_status(conn, child["row_id"])["current"]))

        # --- 6. the two fields that only work if they are hard to fake.
        big_caveat = "x" * 201
        too_many = (await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Order narrative",
            "description": "What the order lines say.",
            "content_text": "orders were up", "content_format": "markdown",
            "session_id": s1, "caveats": [f"caveat {i}" for i in range(6)],
        })).data
        check("more than five caveats is rejected",
              "caveats are short properties of a result" in str(too_many.get("error")),
              str(too_many.get("error"))[:160])
        too_long = (await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Order narrative (long caveat)",
            "description": "What the order lines say.",
            "content_text": "orders were up", "content_format": "markdown",
            "session_id": s1, "caveats": [big_caveat],
        })).data
        check("a caveat over 200 chars is rejected",
              "caveats are short properties of a result" in str(too_long.get("error")),
              str(too_long.get("error"))[:160])

        no_basis_confidence = (await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Order narrative (confidence)",
            "description": "What the order lines say, with a confidence claim.",
            "content_text": "orders were up", "content_format": "markdown",
            "session_id": s1,
            "confidence": [{"claim": "orders are up", "level": "high"}],
        })).data
        check("a confidence entry without a basis is rejected",
              "error" in no_basis_confidence and "basis" in str(no_basis_confidence.get("error")),
              str(no_basis_confidence.get("error"))[:200])
        bad_level = (await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Order narrative (level)",
            "description": "What the order lines say, with an invented level.",
            "content_text": "orders were up", "content_format": "markdown",
            "session_id": s1,
            "confidence": [{"claim": "orders are up", "level": "very high",
                            "basis": "because"}],
        })).data
        check("an unknown confidence level is rejected",
              "error" in bad_level and "level" in str(bad_level.get("error")),
              str(bad_level.get("error"))[:200])

        good = (await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Order narrative (qualified)",
            "description": "What the order lines say, qualified.",
            "content_text": "orders were up", "content_format": "markdown",
            "session_id": s1,
            "caveats": ["one fetch only; the January refill is not included"],
            "confidence": [{"claim": "orders are up", "level": "medium",
                            "basis": "one fetch of two rows, re-derived by hand"}],
        })).data
        read_back = (await client.call_tool("get_artifact", {
            "row_id": good["row_id"], "session_id": s1})).data
        check("caveats and confidence round-trip through get_artifact",
              read_back.get("caveats") == ["one fetch only; the January refill is not included"]
              and read_back.get("confidence") == [
                  {"claim": "orders are up", "level": "medium",
                   "basis": "one fetch of two rows, re-derived by hand"}],
              str({k: read_back.get(k) for k in ("caveats", "confidence")}))

        # --- the display half: the GUI page shows all three.
        from dsos import gui  # import after DSOS_DB_PATH is set

        page = TestClient(gui.app).get(f"/artifacts/{good['row_id']}")
        text = page.text
        check("the GUI artifact page is a 200", page.status_code == 200,
              f"HTTP {page.status_code}")
        check("it shows the status", "status result" in text, "")
        check("it shows the caveats", "January refill is not included" in text, "")
        check("it shows the confidence basis",
              "re-derived by hand" in text, "")

        validation_page = TestClient(gui.app).get(f"/artifacts/{checked['row_id']}")
        check("the GUI shows the validation history",
              "a person checked the rollup" in validation_page.text
              and "confirmed" in validation_page.text, "")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        sys.exit(1)
    print("lifecycle_smoke_test: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
