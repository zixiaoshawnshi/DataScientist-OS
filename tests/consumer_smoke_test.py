"""Smoke test: the consumer profile — the four tools a PM or reviewer uses to
decide whether to believe a number.

This is the first WP whose caller is not an analysis agent. Everything before
it was designed for a caller that has the data, writes Python, and can judge
its own output. This one has none of that: it can read, and it needs to know
when not to trust what it reads. So the assertions here are about labelling
and interpretation, not about computation.

The store is seeded THROUGH THE PRODUCER — the same twelve tools, over the
same store handle the consumer reads — because a consumer-only fixture would
test the consumer against rows the producer cannot produce. Five shapes are
seeded, and the first assertion is that only one of them comes back:

1. `find_evidence` returns only eligible rows: the latest version of a
   `status='result'` finding. The dataset (a source, not a finding), the
   exploratory row (nobody has claimed it) and the superseded row (something
   replaced it) are all in the store and all absent from the hits. The
   `contradicted` row IS returned — hiding it would make the tool useless —
   and is labelled, in the payload, unmissably. That single case is the whole
   thesis of the consumer profile: a PM backing "activation dropped after
   onboarding" finds a row that says exactly that, and learns in the same
   breath that it was later contradicted.
2. An off-topic claim returns the empty result and the hint, not a weak hit.
3. `get_claim`'s derivation reaches the dataset's source URL and the SQL that
   produced the finding — a step with no execution (a hand-registered
   dataset) carries no code and must not raise.
4. `cite` builds its URL from `base_url` when there is one and falls back to
   `dsos:<row_id>` when there is not.
5. `ask` puts a consumer's question where a producer's `start_session` sees
   it on the board, and a second identical ask deduplicates onto it.
6. The consumer server lists exactly four tools.
7. Consumer calls write no `artifacts` row: only `sessions`, `tool_calls`
   (D12) and `questions` change.

**Nothing here depends on real semantic quality.** `DSOS_EVIDENCE_FLOOR` is
calibrated against sentence-transformers' all-MiniLM-L6-v2, and dsos falls
back to a crc32-bucketed bag-of-words embedder when that extra is not
installed — whose similarities are not comparable to MiniLM's. Every
positive case below therefore matches by KEYWORD, which scores the 1.0
sentinel and is labelled `match: keyword`, so the floor never applies to it.
A test that passes with the model and fails without it is worse than no test.

Run: E:/Projects/DataScienceOS/.venv/Scripts/python.exe tests/consumer_smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "data/test-runs/consumer_smoke_test/store.db"
os.environ["DSOS_DB_PATH"] = DB_PATH
shutil.rmtree(Path(DB_PATH).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos import db  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.server import ServerConfig, build_consumer, build_producer  # noqa: E402

# A base_url for the half of the cite test that needs a daemon behind it. The
# consumer never talks to it — it only splices the string into a link — so a
# reserved-for-documentation host is enough and nothing has to be listening.
FAKE_BASE_URL = "http://127.0.0.1:8765"

EXPECTED_CONSUMER_TOOLS = {"find_evidence", "get_claim", "cite", "ask"}

# One claim, matched by keyword, that every seeded finding shares. `_fts_query`
# ANDs the terms as prefixes, so all three appear in both the eligible rows'
# titles and the ineligible ones' — the difference between them is lifecycle
# and type, which is the whole point of the first check.
CLAIM = "activation rate after onboarding"
# Zero lexical overlap with anything seeded, and semantically far from it.
OFF_TOPIC = "median price per carat by cut"
# The consumer's own words, and the producer's paraphrase of them: no shared
# wording beyond "q3"/"scored", which is what WP-E3R's OR-ranked matcher is
# for. The consumer hand-off depends on this working.
CONSUMER_QUESTION = "which player scored most in q3"
PRODUCER_QUESTION = "who scored the most on q3"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def hit_for(results: list[dict], row_id: str) -> dict | None:
    return next((h for h in results if h.get("row_id") == row_id), None)


async def seed(config: ServerConfig) -> dict:
    """Register the five shapes through the producer, and return their ids."""
    ids: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "onboarding.csv"
        csv_path.write_text(
            "user,week,active\n"
            "1,1,1\n2,1,0\n3,1,1\n"
            "1,2,1\n2,2,1\n3,2,0\n"
            "1,3,0\n2,3,0\n3,3,1\n"
            "1,4,0\n2,4,1\n3,4,0\n",
            encoding="utf-8",
        )

        async with Client(build_producer(config)) as client:
            s = (await client.call_tool(
                "start_session", {"question": "did activation drop after onboarding?"}
            )).data
            session_id = s["session_id"]

            r = (await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Onboarding events",
                "description": "Per-user onboarding activity by week, from the signup export.",
                "content_format": "parquet", "content_path": str(csv_path),
                "source": {"url": "https://example.org/onboarding.csv", "fetched_at": "now",
                           "refresh_after": "static"},
                "session_id": session_id,
            })).data
            ids["dataset"] = r["row_id"]

            # The result: a real run over the dataset, so it has an execution
            # (kind + code) for get_claim's derivation to report.
            r = (await client.call_tool("run_sql", {
                "code": "SELECT week, AVG(active) AS activation_rate FROM in_1 GROUP BY week",
                "session_id": session_id, "title": "Activation rate after onboarding",
                "description": "Weekly activation rate across the first four weeks after signup.",
                "input_row_ids": [ids["dataset"]], "status": "result",
            })).data
            ids["result"] = r["row_id"]

            r = (await client.call_tool("run_sql", {
                "code": "SELECT COUNT(*) AS n FROM in_1 WHERE active = 1",
                "session_id": session_id, "title": "Activation rate after onboarding, scratch cut",
                "description": "Quick row-count poke at the same question, not checked yet.",
                "input_row_ids": [ids["dataset"]],
            })).data
            ids["exploratory"] = r["row_id"]

            r = (await client.call_tool("save_artifact", {
                "type": "narrative", "title": "Activation rate after onboarding, first pass",
                "description": "Write-up of the activation rate trend, from the first query only.",
                "content_format": "markdown", "content_text": "# Activation\nIt fell.",
                "session_id": session_id,
            })).data
            ids["contradicted"] = r["row_id"]

            r = (await client.call_tool("save_artifact", {
                "type": "narrative", "title": "Activation rate after onboarding, old cut",
                "description": "Superseded write-up of the activation rate trend, kept for history.",
                "content_format": "markdown", "content_text": "# Activation (old)",
                "session_id": session_id,
            })).data
            ids["superseded"] = r["row_id"]

            await client.call_tool("mark", {
                "row_id": ids["contradicted"], "session_id": session_id,
                "verdict": "contradicted",
                "basis": "The second query used a different denominator; the drop was an artefact.",
            })
            await client.call_tool("mark", {
                "row_id": ids["superseded"], "session_id": session_id,
                "status": "superseded", "superseded_by": ids["result"],
            })
    return ids


async def main() -> None:
    handle = db.Database(DB_PATH)
    conn = db.connect(DB_PATH)
    config = ServerConfig(db=handle, python_path=sys.executable, base_url=None)
    ids = await seed(config)

    check("the seed registered all five shapes",
          all(ids.values()), ", ".join(f"{k}={v[:8]}" for k, v in ids.items()))

    # 6. The surface, first: everything below assumes these four exist and the
    #    producer's twelve did not leak in.
    consumer = build_consumer(config)
    tools = sorted(t.name for t in await consumer.list_tools())
    check("the consumer server lists exactly the four evidence tools",
          set(tools) == EXPECTED_CONSUMER_TOOLS, f"{len(tools)}: {', '.join(tools)}")
    producer_tools = {t.name for t in await build_producer(config).list_tools()}
    check("the consumer inherits none of the producer's tools",
          not set(tools) & producer_tools, f"{sorted(set(tools) & producer_tools)}")

    before = {t: count(conn, t) for t in ("artifacts", "tool_calls", "questions", "sessions")}

    async with Client(consumer) as client:
        # 1. Eligibility, and the contradicted row.
        found = (await client.call_tool("find_evidence", {"claim": CLAIM, "top_k": 10})).data
        results = found.get("results", [])
        returned = {h["row_id"] for h in results}
        check("find_evidence returns the result row",
              ids["result"] in returned, f"{len(results)} hit(s)")
        for label, key in (("the dataset (a source, not a finding)", "dataset"),
                           ("the exploratory row (nobody claimed it)", "exploratory"),
                           ("the superseded row (something replaced it)", "superseded")):
            check(f"find_evidence excludes {label}", ids[key] not in returned)
        check("find_evidence includes the contradicted row",
              ids["contradicted"] in returned)
        contradicted = hit_for(results, ids["contradicted"]) or {}
        check("the contradicted row is labelled with its verdict",
              contradicted.get("validation") == "contradicted",
              f"validation={contradicted.get('validation')!r}")
        check("the contradicted row carries a warning in the payload, not just a field",
              bool(contradicted.get("warning")), f"warning={contradicted.get('warning')!r}")
        hit = hit_for(results, ids["result"]) or {}
        check("a keyword hit is labelled keyword, so the floor cannot hide it",
              hit.get("match") == "keyword", f"match={hit.get('match')!r}")
        check("a hit reports the question it answered",
              "onboarding" in (hit.get("question") or "").lower(),
              f"question={hit.get('question')!r}")
        check("a hit reports validation, caveats, confidence and created_at",
              hit.get("created_at") and "validation" in hit
              and isinstance(hit.get("caveats"), list)
              and isinstance(hit.get("confidence"), list),
              str(sorted(hit.keys())))
        check("a table hit carries row_count, columns and at most 5 rows",
              hit.get("numbers", {}).get("row_count") == 4
              and hit.get("numbers", {}).get("columns") == ["week", "activation_rate"]
              and len(hit.get("numbers", {}).get("rows", [])) == 4,
              str(hit.get("numbers"))[:200])
        check("a text hit carries at most 500 characters",
              len((hit_for(results, ids["contradicted"]) or {}).get("numbers", {}).get("text", ""))
              <= 500, "")
        check("every hit carries a url",
              all(h.get("url") for h in results), "")

        # 2. Off-topic: empty, with the hint that says what to do instead.
        empty = (await client.call_tool("find_evidence", {"claim": OFF_TOPIC})).data
        check("an off-topic claim returns no hits",
              empty.get("results") == [], str(empty.get("results"))[:200])
        check("an off-topic claim returns the hint, not silence",
              "ask(question)" in (empty.get("hint") or ""), repr(empty.get("hint")))

        # 3. get_claim: the derivation, with the source URL and the code.
        claim = (await client.call_tool("get_claim", {"row_id": ids["result"]})).data
        steps = claim.get("derivation", [])
        check("get_claim's derivation is roots-first and ends at the claim itself",
              [s["row_id"] for s in steps][-1] == ids["result"]
              and steps[0]["row_id"] == ids["dataset"],
              " -> ".join(s["title"][:24] for s in steps))
        dataset_step = steps[0] if steps else {}
        check("the derivation carries the dataset's source URL",
              (dataset_step.get("source") or {}).get("url") == "https://example.org/onboarding.csv",
              str(dataset_step.get("source")))
        query_step = next((s for s in steps if s["row_id"] == ids["result"]), {})
        check("the derivation carries the code that produced the finding",
              query_step.get("kind") == "sql" and "AVG(active)" in (query_step.get("code") or ""),
              f"kind={query_step.get('kind')!r}")
        check("a step with no execution reports no code rather than raising",
              dataset_step.get("code") is None and dataset_step.get("kind") is None,
              f"kind={dataset_step.get('kind')!r} code={dataset_step.get('code')!r}")
        check("get_claim reports status, validation history, question and url",
              claim.get("status") == "result"
              and "history" in (claim.get("validation") or {})
              and claim.get("question") and claim.get("url"),
              str(sorted(claim.keys())))

        # 4. cite, with and without a base_url.
        cited = (await client.call_tool("cite", {"row_id": ids["result"]})).data
        # The date is whatever the store recorded, so the shape is asserted
        # (title, the 8-char id, an ISO date) rather than a literal.
        reference = cited.get("reference", "")
        date = reference.rsplit(", ", 1)[-1] if ", " in reference else ""
        check("cite's reference is <title> — dsos <id8>, <YYYY-MM-DD>",
              reference.startswith(
                  f"Activation rate after onboarding — dsos {ids['result'][:8]}, ")
              and len(date) == 10 and date[4] == "-" and date[7] == "-",
              f"{reference!r}")
        check("cite's url without a base_url is dsos:<row_id>",
              cited.get("url") == f"dsos:{ids['result']}", cited.get("url", ""))
        check("cite reports the row's status and validation",
              cited.get("status") == "result" and "current" in (cited.get("validation") or {}),
              str(cited.get("validation")))

        with_base = build_consumer(ServerConfig(
            db=handle, python_path=sys.executable, base_url=FAKE_BASE_URL))
        async with Client(with_base) as hosted:
            hosted_cite = (await hosted.call_tool("cite", {"row_id": ids["result"]})).data
            check("cite's url with a base_url is <base_url>/artifacts/<row_id>",
                  hosted_cite.get("url") == f"{FAKE_BASE_URL}/artifacts/{ids['result']}",
                  hosted_cite.get("url", ""))

        # cite warns rather than refuses on a row that is not a stated result.
        warned = (await client.call_tool("cite", {"row_id": ids["exploratory"]})).data
        check("cite warns rather than refuses on a non-result row",
              bool(warned.get("warning")) and warned.get("status") == "exploratory"
              and warned.get("reference"),
              f"status={warned.get('status')!r} warning={warned.get('warning')!r}")

        # get_claim warns on a superseded row and names what replaced it.
        dead = (await client.call_tool("get_claim", {"row_id": ids["superseded"]})).data
        check("get_claim warns on a superseded row and names its replacement",
              "superseded" in (dead.get("warning") or "").lower()
              and ids["result"] in (dead.get("warning") or ""),
              dead.get("warning", ""))
        check("get_claim reports superseded_by",
              dead.get("superseded_by") == ids["result"], str(dead.get("superseded_by")))

        # 5. ask: one question, then the same one again.
        asked = (await client.call_tool("ask", {"question": CONSUMER_QUESTION})).data
        check("ask creates an open question and returns its id",
              asked.get("question_id") and asked.get("status") == "open", str(asked))
        again = (await client.call_tool("ask", {"question": CONSUMER_QUESTION + " "})).data
        check("a second identical ask deduplicates onto the same question",
              again.get("question_id") == asked.get("question_id")
              and again.get("deduplicated") is True, str(again))
        check("a consumer's question is asked_by a consumer session, not a producer one",
              scalar(conn, "SELECT s.kind FROM questions q JOIN sessions s ON s.id = q.asked_by"
                           " WHERE q.id = ?", (asked["question_id"],)) == "consumer",
              str(scalar(conn, "SELECT q.asked_by FROM questions q WHERE q.id = ?",
                         (asked["question_id"],))))

        # D12: the call log is what "consumer reuse" is measured from, and a
        # consumer tool that took a session_id would defeat the whole point.
        logged = [r["tool_name"] for r in conn.execute(
            "SELECT tool_name FROM tool_calls ORDER BY ts").fetchall()]
        check("every consumer call is logged in tool_calls",
              "find_evidence" in logged and "cite" in logged and "ask" in logged,
              ", ".join(logged[-4:]))
        consumer_call = scalar(
            conn, "SELECT session_id FROM tool_calls WHERE tool_name = 'find_evidence'")
        check("a consumer tool call is logged against a kind='consumer' session",
              scalar(conn, "SELECT kind FROM sessions WHERE id = ?", (consumer_call,))
              == "consumer", str(consumer_call))
        check("no consumer tool takes a session_id parameter",
              all("session_id" not in (t.parameters.get("properties") or {})
                  for t in await consumer.list_tools()), "")

    # 5 (continued). A producer starting fresh must see the consumer's question
    # on the board — a paraphrase, not the same sentence.
    async with Client(build_producer(config)) as client:
        board = (await client.call_tool(
            "start_session", {"question": PRODUCER_QUESTION})).data
        related = board.get("related_questions", [])
        check("a new producer's related_questions includes the consumer's ask",
              any(e["question_id"] == asked["question_id"] for e in related),
              f"{len(related)} related: " + "; ".join(e["question"][:40] for e in related))

    # 7. The consumer wrote nothing into `artifacts`.
    after = {t: count(conn, t) for t in ("artifacts", "tool_calls", "questions", "sessions")}
    check("consumer calls add 0 artifacts rows",
          after["artifacts"] == before["artifacts"],
          f"{before['artifacts']} -> {after['artifacts']}")
    check("consumer calls do change tool_calls, questions and sessions (D12)",
          after["tool_calls"] > before["tool_calls"]
          and after["questions"] > before["questions"]
          and after["sessions"] > before["sessions"],
          f"tool_calls {before['tool_calls']}->{after['tool_calls']}, "
          f"questions {before['questions']}->{after['questions']}, "
          f"sessions {before['sessions']}->{after['sessions']}")

    # 8. ask is also the follow-up. Once a producer answers a consumer's
    #    question, asking it again hands back the answer rather than opening
    #    a duplicate; reopen=True asks afresh; an abandoned question is asked
    #    again as a new one that names the abandoned one.
    async with Client(build_producer(config)) as producer, Client(consumer) as client:
        claimed = (await producer.call_tool("start_session", {
            "question": CONSUMER_QUESTION, "question_id": asked["question_id"]})).data
        await producer.call_tool("close_question", {
            "session_id": claimed["session_id"], "question_id": asked["question_id"],
            "status": "answered", "artifact_row_id": ids["result"]})
        questions_before = count(conn, "questions")
        follow_up = (await client.call_tool("ask", {"question": CONSUMER_QUESTION})).data
        check("asking an answered question again returns its answer",
              follow_up.get("status") == "answered"
              and follow_up.get("question_id") == asked["question_id"]
              and (follow_up.get("answer") or {}).get("row_id") == ids["result"],
              str(follow_up))
        check("...with the answer's standing and url, and no warning on a clean result",
              (follow_up.get("answer") or {}).get("status") == "result"
              and (follow_up.get("answer") or {}).get("url") == f"dsos:{ids['result']}"
              and "warning" not in (follow_up.get("answer") or {}),
              str(follow_up.get("answer")))
        check("...and opens no new question", count(conn, "questions") == questions_before,
              f"{questions_before} -> {count(conn, 'questions')}")

        fresh = (await client.call_tool(
            "ask", {"question": CONSUMER_QUESTION, "reopen": True})).data
        check("reopen=True opens a new question instead of returning the answer",
              fresh.get("status") == "open" and fresh.get("question_id")
              and fresh.get("question_id") != asked["question_id"], str(fresh))

        # An answer that has been contradicted since carries the warning.
        doubtful_q = "is the onboarding activation estimate still right"
        doubtful = (await client.call_tool("ask", {"question": doubtful_q})).data
        s = (await producer.call_tool("start_session", {
            "question": doubtful_q, "question_id": doubtful["question_id"]})).data
        await producer.call_tool("close_question", {
            "session_id": s["session_id"], "question_id": doubtful["question_id"],
            "status": "answered", "artifact_row_id": ids["contradicted"]})
        doubted = (await client.call_tool("ask", {"question": doubtful_q})).data
        check("an answer contradicted since is returned with a CONTRADICTED warning",
              doubted.get("status") == "answered"
              and "CONTRADICTED" in ((doubted.get("answer") or {}).get("warning") or ""),
              str(doubted.get("answer")))

        dropped_q = "what was q4 churn by plan"
        dropped = (await client.call_tool("ask", {"question": dropped_q})).data
        s = (await producer.call_tool("start_session", {
            "question": dropped_q, "question_id": dropped["question_id"]})).data
        await producer.call_tool("close_question", {
            "session_id": s["session_id"], "question_id": dropped["question_id"],
            "status": "abandoned"})
        retry = (await client.call_tool("ask", {"question": dropped_q})).data
        check("asking an abandoned question again opens a new one naming the abandoned one",
              retry.get("status") == "open"
              and retry.get("question_id") != dropped["question_id"]
              and retry.get("previously_abandoned") == dropped["question_id"],
              str(retry))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        sys.exit(1)
    print("consumer_smoke_test: all checks passed")
