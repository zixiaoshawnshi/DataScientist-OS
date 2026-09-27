"""Layer-2d smoke test: the prior-work signal returned by start_session,
and rejection of an unknown session_id.

start_session used to return only a session_id, so a fresh session had no
way to know the store held anything. In the benchmark pilot the agent
called search_artifacts zero times across ten reuse rounds and re-fetched
data it had already registered. The response now reports what the store
already holds plus candidate artifacts.

Covers: the empty-store message, counts that exclude seeded skills (a
brand-new store is seeded with 7 skills and must still read as empty),
by_type breakdown, keyword vs semantic match labelling, and the caveat
that travels with weak candidates.

Run: .venv/Scripts/python.exe tests/prior_work_smoke_test.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["DSOS_DB_PATH"] = "data/test-runs/prior_work_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> None:
    async with Client(mcp) as client:
        # First call: the server seeds its skill library on first use. A
        # store holding only those skills must still report zero prior work,
        # or every fresh session looks like it has a history.
        first = (await client.call_tool(
            "start_session", {"question": "what makes a hackathon project win?"})).data
        s1 = first["session_id"]
        check("empty store reports zero prior work",
              first["prior_work"]["artifact_count"] == 0,
              f"count={first['prior_work']['artifact_count']}")
        check("empty store says so plainly",
              "first session" in first["note"], first["note"][:60])
        check("seeded skills are not counted as prior work",
              "skill" not in first["prior_work"]["by_type"])
        check("empty store has no candidates", first["candidates"] == [])

        # Register a dataset, then a second session must see it.
        await client.call_tool("save_artifact", {
            "type": "dataset", "title": "Hotel Reviews Dataset",
            "description": "Hotel listings with review counts and ratings.",
            "content_text": "name,reviews,rating\nA,100,4.2\nB,900,3.9\nC,450,4.5",
            "content_format": "csv", "session_id": s1,
        })

        second = (await client.call_tool(
            "start_session", {"question": "what is the average number of reviews per hotel?"})).data
        check("session_id still returned (unchanged contract)", bool(second["session_id"]))
        check("populated store reports its artifact count",
              second["prior_work"]["artifact_count"] == 1,
              f"count={second['prior_work']['artifact_count']}")
        check("count breaks down by type",
              second["prior_work"]["by_type"] == {"dataset": 1},
              str(second["prior_work"]["by_type"]))
        check("the dataset is offered as a candidate",
              any("Hotel" in c["title"] for c in second["candidates"]),
              str([c["title"][:30] for c in second["candidates"]]))
        check("candidates carry a row_id usable downstream",
              all(c.get("row_id") for c in second["candidates"]))
        check("candidates are labelled keyword or semantic",
              all(c["match"] in ("keyword", "semantic") for c in second["candidates"]),
              str([c["match"] for c in second["candidates"]]))
        check("note points at reuse before re-fetching",
              "reuse" in second["note"] and "search_artifacts" in second["note"])

        # A weak semantic-only candidate must carry the "may be irrelevant"
        # caveat, so an agent isn't told to trust an embedding coincidence.
        if any(c["match"] == "semantic" for c in second["candidates"]):
            check("semantic candidates are caveated",
                  "may well be irrelevant" in second["note"])

        # The signal must not break the tools that consume session_id.
        found = (await client.call_tool(
            "search_artifacts", {"query": "hotel reviews", "session_id": second["session_id"]})).data
        check("search_artifacts still works with the new session", bool(found["results"]))

        # A mistyped session_id must be rejected, not silently logged against
        # an id no session owns — such a call is invisible to the per-session
        # trace and silently dropped by the reuse metric.
        bogus = second["session_id"][:-1] + ("e" if second["session_id"][-1] != "e" else "a")
        try:
            await client.call_tool("get_artifact", {
                "row_id": second["candidates"][0]["row_id"], "session_id": bogus})
            check("mistyped session_id is rejected", False, "call succeeded")
        except Exception as exc:  # fastmcp raises ToolError
            check("mistyped session_id is rejected", True, type(exc).__name__)
            # The message must survive to the caller: an uncaught ValueError
            # reaches the agent as a bare "Internal server error" and it
            # learns nothing about how to fix the call.
            check("rejection explains the fix",
                  "start_session" in str(exc) and "unknown session_id" in str(exc),
                  str(exc)[:90])

        after = (await client.call_tool("list_skills", {"session_id": second["session_id"]})).data
        check("valid session still works after a rejection", bool(after))

    shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)
    if FAILURES:
        print(f"\nprior_work smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nprior_work smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
