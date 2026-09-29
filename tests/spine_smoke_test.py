"""Smoke test: the spine — questions, the activity-based lease, and the
coordination board that two agents read before they start.

The interesting property of this layer is what is NOT stored. The lease
exists nowhere in the schema: it is derived from `tool_calls.ts` on every
read, which is what makes renewal free (any tool call renews it) and what
means an agent that dies mid-question releases its claim without a sweeper.
The tests below are therefore mostly about DERIVED state, and they reach
for `DSOS_LEASE_MINUTES=0.001` plus backdated timestamps rather than
sleeping, so expiry is reachable deterministically and in milliseconds.

1. A second session asking the same text sees the first one's question as
   live `in_progress` — the board is what stops the duplicate work, so it
   has to be right about who is holding what.
2. Claiming a live-leased question fails and names the holder's last
   activity; the same claim succeeds once the lease has lapsed.
3. A tool call by the claimant renews the lease — liveness before the call,
   lapsed after backdating, live again after one real call.
4. `answered` is refused without a result row, and refused against an
   `exploratory` one: a question closed against a finding nobody claimed is
   exactly the failure the lifecycle exists to prevent.
5. record_decision links its evidence as lineage parents and closes the
   question against the decision it just wrote.
6. A question asked by a *consumer* (status open, a different asked_by)
   reaches a producer's related_questions, ranked below live in-progress
   work and above an already-answered question.
7. `DSOS_DISABLE_BOARD=1` suppresses the board and nothing else — the
   question is still created, because H3's control condition must differ
   from its treatment condition in the board alone.
8. Board RECALL (WP-E3R): a paraphrase of a question already on the board
   reaches the producer who asked it, an unrelated question reaches
   nothing at all, and the ranking is by shared content words *within* the
   status bands rather than across them.
9. Claiming an `open` question makes it `in_progress`, so the board ranks
   it as live work; a question that is already answered or abandoned
   refuses a second close (and record_decision refuses it before writing a
   decision); and a decision's `source` column stays NULL, because source
   is provenance and not the question link.

Run: E:/Projects/DataScienceOS/.venv/Scripts/python.exe tests/spine_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "data/test-runs/spine_smoke_test/store.db"
os.environ["DSOS_DB_PATH"] = DB_PATH
shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos import db, spine, store  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []

# The lease TTL, read from the environment on every call, so a test can hold
# it at a fraction of a minute and reach expiry without sleeping.
LEASE_ENV = "DSOS_LEASE_MINUTES"
BOARD_ENV = "DSOS_DISABLE_BOARD"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def question_row(conn: sqlite3.Connection, question_id: str) -> dict:
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    return dict(row) if row else {}


def entry_for(board: list[dict], question_id: str) -> dict | None:
    return next((e for e in board if e.get("question_id") == question_id), None)


def backdate(conn: sqlite3.Connection, session_id: str, question_id: str, minutes: int) -> None:
    """Move a claimer's last activity into the past. This is the only way a
    lease lapses, because there is no lease column to set: last activity IS
    max(claimed_at, latest tool_calls.ts of claimed_by)."""
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn.execute("UPDATE tool_calls SET ts = ? WHERE session_id = ?", (when, session_id))
    conn.execute("UPDATE questions SET claimed_at = ? WHERE id = ?", (when, question_id))
    conn.commit()


def _csv(tmp: Path, name: str, body: str) -> str:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return str(path)


async def main() -> None:
    conn = db.connect(DB_PATH)
    tmp = Path(tempfile.mkdtemp())
    question_text = "which team scored the most on q3"

    async with Client(mcp) as client:
        s1 = (await client.call_tool(
            "start_session", {"question": question_text})).data
        check("start_session returns a session_id and a question_id",
              bool(s1.get("session_id")) and bool(s1.get("question_id")), str(s1.keys()))

        q1 = s1["question_id"]
        row1 = question_row(conn, q1)
        check("a question asked for the first time is in_progress, asked and claimed by that session",
              row1.get("status") == "in_progress" and row1.get("asked_by") == s1["session_id"]
              and row1.get("claimed_by") == s1["session_id"], str(row1))
        check("sessions.question_id is set",
              conn.execute("SELECT question_id FROM sessions WHERE id = ?",
                           (s1["session_id"],)).fetchone()["question_id"] == q1, "")
        check("the first session's board does not list its own question",
              entry_for(s1.get("related_questions", []), q1) is None, str(s1.get("related_questions")))

        evidence = (await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Team scores q3",
            "description": "One row per team with its q3 score, fetched once for this test.",
            "content_path": _csv(tmp, "scores.csv", "team,score\nred,12\nblue,9\n"),
            "content_format": "csv", "session_id": s1["session_id"],
        })).data
        check("save_artifact still returns a row_id here", bool(evidence.get("row_id")),
              str(evidence))

        # --- 1. the board, seen by a second session asking the same thing.
        s2 = (await client.call_tool(
            "start_session", {"question": question_text})).data
        board = s2.get("related_questions", [])
        seen = entry_for(board, q1)
        check("a second session with the same question text sees it as live in_progress",
              bool(seen) and seen.get("status") == "in_progress" and seen.get("claimed") is True
              and seen.get("lease_expires_at") is not None, str(seen))
        check("the board never lists the session's own question",
              entry_for(board, s2["question_id"]) is None, str(board))
        check("the board entry carries the keys an agent acts on",
              set(seen or {}) == {"question_id", "question", "status", "claimed",
                                  "lease_expires_at", "artifact_row_id"}, str(sorted(seen or {})))
        check("session 1 still holds a live lease",
              spine.lease_state(conn, conn.execute(
                  "SELECT * FROM questions WHERE id = ?", (q1,)).fetchone())["live"] is True, "")

        # --- 2. claiming a live lease fails; a lapsed one does not.
        sessions_before = len(store.list_sessions(conn))
        clash = (await client.call_tool(
            "start_session", {"question": question_text, "question_id": q1})).data
        check("claiming a live-leased question fails", "error" in clash, str(clash))
        check("the error names the holder's last activity and what to do about it",
              "last activity" in str(clash.get("error", "")).lower()
              and ("related" in str(clash.get("error", "")).lower()
                   or "wait" in str(clash.get("error", "")).lower()), str(clash.get("error")))
        check("a failed claim leaves no session behind",
              len(store.list_sessions(conn)) == sessions_before,
              f"{sessions_before} -> {len(store.list_sessions(conn))}")

        # TTL of 0.001 min = 60ms, and the holder's activity moved 10 minutes
        # back: the lease is gone without a sweeper and without sleeping.
        os.environ[LEASE_ENV] = "0.001"
        backdate(conn, s1["session_id"], q1, minutes=10)
        expired = spine.lease_state(conn, conn.execute(
            "SELECT * FROM questions WHERE id = ?", (q1,)).fetchone())
        check("a claim with no recent activity is not live", expired["live"] is False, str(expired))
        check("an expired lease still reports when it lapsed",
              bool(expired.get("expires_at")), str(expired))

        s3 = (await client.call_tool(
            "start_session", {"question": question_text, "question_id": q1})).data
        check("claiming succeeds once the lease has expired and returns that question",
              s3.get("question_id") == q1 and "error" not in s3, str(s3))
        check("the new claimant holds it", question_row(conn, q1)["claimed_by"] == s3["session_id"],
              str(question_row(conn, q1)["claimed_by"]))

        # --- 3. a tool call by the claimant renews the lease.
        os.environ[LEASE_ENV] = "0.001"
        backdate(conn, s3["session_id"], q1, minutes=10)
        lapsed = spine.lease_state(conn, conn.execute(
            "SELECT * FROM questions WHERE id = ?", (q1,)).fetchone())
        await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Q3 scoreboard",
            "description": "A short write-up of the top-scoring team in q3.",
            "content_text": "Red led q3.", "content_format": "markdown",
            "session_id": s3["session_id"],
        })
        renewed = spine.lease_state(conn, conn.execute(
            "SELECT * FROM questions WHERE id = ?", (q1,)).fetchone())
        check("the lease lapses when the claimant goes quiet", lapsed["live"] is False, str(lapsed))
        check("one tool call by the claimant renews it",
              renewed["live"] is True and renewed["expires_at"] > lapsed["expires_at"],
              f"{lapsed['expires_at']} -> {renewed['expires_at']}")

        # --- 4. answered needs a row somebody actually claimed.
        os.environ.pop(LEASE_ENV, None)
        finding = (await client.call_tool("run_sql", {
            "code": "SELECT team, score FROM in_1 ORDER BY score DESC LIMIT 1",
            "session_id": s3["session_id"], "title": "Top team q3",
            "description": "The highest-scoring team in q3.",
            "input_row_ids": [evidence["row_id"]],
        })).data
        check("a run is exploratory, so it is not a result", finding.get("artifact_status") ==
              "exploratory", str(finding.get("artifact_status")))

        no_row = (await client.call_tool("close_question", {
            "session_id": s3["session_id"], "question_id": q1, "status": "answered",
        })).data
        check("answered without an artifact_row_id is rejected", "error" in no_row, str(no_row))
        bad_row = (await client.call_tool("close_question", {
            "session_id": s3["session_id"], "question_id": q1, "status": "answered",
            "artifact_row_id": finding["row_id"],
        })).data
        check("answered against an exploratory row is rejected", "error" in bad_row, str(bad_row))
        check("the rejected closes did not change the question",
              question_row(conn, q1)["status"] == "in_progress",
              str(question_row(conn, q1)["status"]))
        bad_status = (await client.call_tool("close_question", {
            "session_id": s3["session_id"], "question_id": q1, "status": "in_progress",
        })).data
        check("a status that is not answered/abandoned is rejected, naming the allowed ones",
              "error" in bad_status and "abandoned" in str(bad_status.get("error", "")),
              str(bad_status))

        # --- 5. record_decision: evidence in lineage, question closed by it.
        decision = (await client.call_tool("record_decision", {
            "session_id": s3["session_id"],
            "decision": "Report red as the q3 leader",
            "rationale": "red scored 12 to blue's 9 on the only fetch of the season table.",
            "evidence_row_ids": [evidence["row_id"], finding["row_id"]],
            "revisit_if": "the season table is refetched with q4 in it",
            "question_id": q1,
        })).data
        check("record_decision returns a decision row", bool(decision.get("row_id")), str(decision))
        dec_art = conn.execute("SELECT type, status, content_ref FROM artifacts WHERE row_id = ?",
                               (decision["row_id"],)).fetchone()
        check("the decision is a result artifact of type decision",
              dec_art["type"] == "decision" and dec_art["status"] == "result", str(dict(dec_art)))
        body = json.loads(Path(dec_art["content_ref"]).read_text(encoding="utf-8"))
        check("the decision's content carries the call, the reason and the revisit trigger",
              body.get("decision") == "Report red as the q3 leader"
              and body.get("revisit_if") == "the season table is refetched with q4 in it"
              and body.get("rationale", "").startswith("red scored 12"), str(body.keys()))
        ancestors = {a.row_id for a in store.get_lineage(conn, decision["row_id"])}
        check("both evidence rows are lineage parents of the decision",
              ancestors == {evidence["row_id"], finding["row_id"]}, str(sorted(ancestors)))
        closed = question_row(conn, q1)
        check("the question is answered by the decision, claim cleared, closed_at set",
              closed["status"] == "answered" and closed["artifact_row_id"] == decision["row_id"]
              and closed["claimed_by"] is None and closed["closed_at"] is not None, str(closed))
        no_evidence = (await client.call_tool("record_decision", {
            "session_id": s3["session_id"], "decision": "Trust the leaderboard",
            "rationale": "It looked right.", "evidence_row_ids": [],
        })).data
        check("a decision with no evidence is refused", "error" in no_evidence, str(no_evidence))

        # --- 6. a consumer's open question reaches the producer board.
        # Asked the same question, in the punctuation and case a human
        # actually types, by a session that is not a producer.
        open_q = spine.create_question(
            conn, question="  Which team scored the most on Q3?  ", status="open",
            asked_by="consumer-session-1",
        )
        s4 = (await client.call_tool(
            "start_session", {"question": question_text})).data
        board = s4.get("related_questions", [])
        found = entry_for(board, open_q)
        check("an open question from a consumer appears on a producer's board",
              found is not None and found["status"] == "open", str(found))
        # s2 never went quiet, so its claim is still live: the board has to
        # put that first, the waiting consumer's question second, and the
        # finished one last.
        in_flight = entry_for(board, s2["question_id"])
        finished = entry_for(board, q1)
        check("live in-progress work outranks an open question, and answered work comes last",
              bool(in_flight) and in_flight["status"] == "in_progress" and bool(found)
              and bool(finished)
              and board.index(in_flight) < board.index(found) < board.index(finished),
              f"order={[(e['question_id'][:6], e['status']) for e in board]}")
        check("an answered question reports the row that answered it",
              (finished or {}).get("artifact_row_id") == decision["row_id"], str(finished))
        check("an unclaimed question reports no lease",
              (found or {}).get("claimed") is False
              and (found or {}).get("lease_expires_at") is None, str(found))

        # A paraphrase of a question in flight is exercised in section 8
        # below: it used NOT to reach the board, and now it must.

        # --- 7. the board flag suppresses the board and only the board.
        os.environ[BOARD_ENV] = "1"
        s5 = (await client.call_tool(
            "start_session", {"question": question_text})).data
        check("DSOS_DISABLE_BOARD=1 empties related_questions",
              s5.get("related_questions") == [], str(s5.get("related_questions")))
        check("the question is still created with the board off",
              question_row(conn, s5.get("question_id", "")).get("status") == "in_progress"
              and question_row(conn, s5.get("question_id", ""))["claimed_by"] == s5["session_id"],
              str(question_row(conn, s5.get("question_id", ""))))
        check("prior_work is still reported with the board off",
              "prior_work" in s5 or "candidates" in s5, str(sorted(s5)))
        os.environ.pop(BOARD_ENV, None)
        restored = spine.related_questions(conn, question_text, exclude_id=q1)
        check("the flag is read per call, so clearing it restores the board immediately",
              len(restored) >= 2, f"{len(restored)} entries")

        # --- 8. board recall (WP-E3R).
        #
        # The board exists to tell a producer that somebody is already on
        # this, and two independent agents never phrase a question the
        # same way. Under the AND-prefix rule it inherited from artifact
        # search, "which player leads q3?" and "is red really the q3
        # leader?" shared only "q3", the AND failed, and the producer
        # started the work a second time. So the matcher OR's the CONTENT
        # words and ranks by how many of them a candidate shares.
        #
        # Each check below plants its own fixture in a private vocabulary
        # (gearboxes, penguins, narwhals), so a hit can only be the
        # question that check created and not a leftover from the tests
        # above — except the first two, which reuse the exact pair the
        # defect was measured on.
        paraphrase_q = spine.create_question(
            conn, question="is red really the q3 leader?", status="open",
            asked_by="consumer-session-1",
        )
        leads = spine.related_questions(conn, "which player leads q3?")
        check("a paraphrase of a question on the board reaches it",
              entry_for(leads, paraphrase_q) is not None,
              f"{len(leads)} hits: {[e['question'] for e in leads]}")
        leading = spine.related_questions(conn, "who is leading q3")
        check("a paraphrase in a different verb form reaches it too",
              entry_for(leading, paraphrase_q) is not None,
              f"{len(leading)} hits: {[e['question'] for e in leading]}")

        # The precision half, and the check that keeps the loosening
        # honest: a different subject entirely has to match nothing.
        # Nothing in this store mentions price, carat or cut.
        price = spine.related_questions(conn, "what is the median price per carat by cut?")
        check("an unrelated question reaches nothing at all", price == [], str(price))

        # Identical phrasing still wins its band. The exact-text flag
        # outranks the shared-term count, so the literally-same question is
        # never crowded out by a close paraphrase.
        exact_q = spine.create_question(
            conn, question="does the gearbox widget jam in fourth gear", status="open",
        )
        near_q = spine.create_question(
            conn, question="gearbox jam widget", status="open",
        )
        gearbox = spine.related_questions(conn, "Does the gearbox widget jam in fourth gear?")
        check("the identical phrasing is first in its band, the paraphrase behind it",
              bool(gearbox) and gearbox[0]["question_id"] == exact_q
              and entry_for(gearbox, near_q) is not None,
              str([(e["question"], e["status"]) for e in gearbox]))

        # Ranking is by how many DISTINCT content words a candidate shares.
        # `weak_q` is created LATER, so a board that fell back to recency
        # inside a band would put it first and fail this — the count is
        # doing the work, not the timestamp.
        strong_q = spine.create_question(
            conn, question="how many penguins migrate to the antarctic each season, "
                           "and how many of those are adults", status="open",
        )
        weak_q = spine.create_question(
            conn, question="season penguins", status="open",
        )
        penguins = spine.related_questions(conn, "how many penguins migrate to the antarctic each season?")
        check("within a band, the question sharing more content words comes first",
              entry_for(penguins, strong_q) is not None and entry_for(penguins, weak_q) is not None
              and penguins.index(entry_for(penguins, strong_q))
              < penguins.index(entry_for(penguins, weak_q)),
              str([(e["question"][:40], e["status"]) for e in penguins]))

        # The bands are not re-ranked by that score. A finished question
        # that matches every word still sorts below live work, because the
        # point of the board is to stop duplicate work NOW.
        live_q = spine.create_question(
            conn, question="narwhal sex", status="in_progress", claimed_by=s1["session_id"],
        )
        waiting_q = spine.create_question(
            conn, question="narwhal tusk length", status="open",
        )
        answered_q = spine.create_question(
            conn, question="narwhal tusk length by sex in adult males", status="answered",
        )
        narwhal = spine.related_questions(conn, "narwhal tusk length by sex")
        check("live in-progress work outranks open, and open outranks answered, "
              "even when the answered one matches best",
              [e["question_id"] for e in narwhal[:3]] == [live_q, waiting_q, answered_q],
              str([(e["question"], e["status"]) for e in narwhal]))

        # The two conditions H3 measures must still differ in nothing but
        # whether rows come back.
        without_self = spine.related_questions(
            conn, "narwhal tusk length by sex", exclude_id=live_q
        )
        check("exclude_id still suppresses the caller's own question",
              entry_for(without_self, live_q) is None
              and entry_for(without_self, waiting_q) is not None, str(without_self))
        os.environ[BOARD_ENV] = "1"
        check("DSOS_DISABLE_BOARD=1 still empties the board at this level too",
              spine.related_questions(conn, "narwhal tusk length by sex") == [], "")
        os.environ.pop(BOARD_ENV, None)

        # --- 9. claiming moves a question on, and a finished one stays finished.
        #
        # A consumer's question is created `open`. Claiming it has to make it
        # `in_progress`, or the board goes on reading "somebody is waiting"
        # for a question somebody is already on — which is the band a second
        # producer is most likely to act on, so it is the worst one to be
        # wrong in. Private vocabulary (platypus) as in section 8.
        platypus_text = "how long do platypus eggs take to incubate"
        asked_q = spine.create_question(
            conn, question=platypus_text, status="open", asked_by="consumer-session-2",
        )
        waiting_platypus = spine.create_question(
            conn, question="platypus egg clutch size", status="open",
            asked_by="consumer-session-2",
        )
        taker = (await client.call_tool(
            "start_session", {"question": platypus_text, "question_id": asked_q})).data
        taken = question_row(conn, asked_q)
        check("claiming an open question makes it in_progress, held by the claimant",
              "error" not in taker and taken.get("status") == "in_progress"
              and taken.get("claimed_by") == taker.get("session_id")
              and taken.get("asked_by") == "consumer-session-2", str(taken))
        onlooker = (await client.call_tool(
            "start_session", {"question": platypus_text})).data
        platypus_board = onlooker.get("related_questions", [])
        claimed_entry = entry_for(platypus_board, asked_q)
        check("a second session sees the claimed question as live in_progress work",
              bool(claimed_entry) and claimed_entry["status"] == "in_progress"
              and claimed_entry["claimed"] is True
              and claimed_entry["lease_expires_at"] is not None, str(claimed_entry))
        check("... ranked in the live band, ahead of the question still waiting",
              bool(claimed_entry) and entry_for(platypus_board, waiting_platypus) is not None
              and platypus_board.index(claimed_entry)
              < platypus_board.index(entry_for(platypus_board, waiting_platypus)),
              str([(e["question"], e["status"]) for e in platypus_board]))
        rank = spine._board_rank(
            conn.execute("SELECT * FROM questions WHERE id = ?", (asked_q,)).fetchone(),
            spine.lease_state(conn, conn.execute(
                "SELECT * FROM questions WHERE id = ?", (asked_q,)).fetchone())["live"],
        )
        check("the claimed question ranks as live in_progress (band 0)", rank == 0, str(rank))
        rerun = (await client.call_tool(
            "start_session", {"question": platypus_text, "question_id": asked_q})).data
        check("a claimed-then-in_progress question still refuses a second live claimant",
              "error" in rerun, str(rerun))

        # A finished question is closed once. Re-closing an answered one would
        # overwrite the row the board hands every later session, and closing
        # somebody else's answer as abandoned would erase it.
        answer = (await client.call_tool("record_decision", {
            "session_id": taker["session_id"],
            "decision": "Platypus eggs incubate for about ten days",
            "rationale": "The fixture dataset is the only evidence, and it is enough here.",
            "evidence_row_ids": [evidence["row_id"]], "question_id": asked_q,
        })).data
        answered_row = question_row(conn, asked_q)
        check("record_decision answers the claimed question",
              answered_row.get("status") == "answered"
              and answered_row.get("artifact_row_id") == answer.get("row_id"), str(answered_row))
        other = (await client.call_tool("record_decision", {
            "session_id": onlooker["session_id"],
            "decision": "Platypus eggs incubate for about four weeks",
            "rationale": "A different reading of the same fixture, recorded on its own.",
            "evidence_row_ids": [evidence["row_id"]],
        })).data
        check("a decision with no question_id is still recorded", bool(other.get("row_id")),
              str(other))
        reclose = (await client.call_tool("close_question", {
            "session_id": onlooker["session_id"], "question_id": asked_q,
            "status": "answered", "artifact_row_id": other["row_id"],
        })).data
        check("re-closing an answered question is refused, pointing at the existing answer",
              "error" in reclose and answer["row_id"] in str(reclose.get("error", "")),
              str(reclose))
        abandon = (await client.call_tool("close_question", {
            "session_id": onlooker["session_id"], "question_id": asked_q,
            "status": "abandoned",
        })).data
        check("closing someone else's answered question as abandoned is refused",
              "error" in abandon, str(abandon))
        after = question_row(conn, asked_q)
        check("the refused closes left the answer untouched",
              after.get("status") == "answered"
              and after.get("artifact_row_id") == answer["row_id"]
              and after.get("closed_at") == answered_row.get("closed_at"), str(after))

        gave_up = spine.create_question(conn, question="platypus venom spur yield", status="open")
        spine.close_question(conn, session_id=onlooker["session_id"], question_id=gave_up,
                             status="abandoned")
        revive = (await client.call_tool("close_question", {
            "session_id": onlooker["session_id"], "question_id": gave_up,
            "status": "answered", "artifact_row_id": other["row_id"],
        })).data
        check("an abandoned question cannot be closed again either",
              "error" in revive and question_row(conn, gave_up).get("status") == "abandoned"
              and question_row(conn, gave_up).get("artifact_row_id") is None, str(revive))

        # record_decision has to find that out BEFORE it writes the decision,
        # so the caller learns the question is closed while it can still act
        # on that, rather than after a row it did not mean to write.
        decisions_before = conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE type = 'decision'").fetchone()["n"]
        late = (await client.call_tool("record_decision", {
            "session_id": onlooker["session_id"],
            "decision": "Platypus eggs incubate for about two weeks",
            "rationale": "Arrived after the question was already answered.",
            "evidence_row_ids": [evidence["row_id"]], "question_id": asked_q,
        })).data
        decisions_after = conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE type = 'decision'").fetchone()["n"]
        check("record_decision against an answered question is refused",
              "error" in late and answer["row_id"] in str(late.get("error", "")), str(late))
        check("... before any decision row is written",
              decisions_after == decisions_before, f"{decisions_before} -> {decisions_after}")
        check("... and the question still points at its original answer",
              question_row(conn, asked_q).get("artifact_row_id") == answer["row_id"], "")

        # `source` is provenance ({url, fetched_at, refresh_after}); the
        # decision -> question link lives on questions.artifact_row_id, and
        # the question_id is in the decision's own content where it is data.
        for label, row_id in (("with a question_id", answer["row_id"]),
                              ("from section 5", decision["row_id"]),
                              ("with no question", other["row_id"])):
            src = conn.execute("SELECT source, content_ref FROM artifacts WHERE row_id = ?",
                               (row_id,)).fetchone()
            check(f"a decision's source column is NULL ({label})", src["source"] is None,
                  str(src["source"]))
        answer_body = json.loads(Path(conn.execute(
            "SELECT content_ref FROM artifacts WHERE row_id = ?", (answer["row_id"],)
        ).fetchone()["content_ref"]).read_text(encoding="utf-8"))
        check("the decision's content names the question it answered",
              answer_body.get("question_id") == asked_q, str(answer_body))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        sys.exit(1)
    print("spine_smoke_test: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
