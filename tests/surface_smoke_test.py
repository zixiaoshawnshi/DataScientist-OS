"""Layer-2f smoke test: the trimmed MVP tool surface (WP-B2, amended by WP-B2R).

Doc II argues the trim is what pays back the 25% registration premium an
agent pays for every tool in the tool list. WP-B2 cut six tools; the
maintainer's U1 decision put two of them back, so the cut that stands is
four — the skill library and the publish path — and everything those
needed is still reachable as library code (dsos/seed.py, dsos/publish.py).
What must not survive is the *tool* itself.

Five things this pins down:
1. list_tools() returns exactly the twelve tools — not "at least", not "at
   most": the seven MVP tools, the two U1 brought back, and the three the
   spine added (mark in WP-E2, close_question and record_decision in
   WP-E3). A thirteenth surviving by accident costs every session that
   has to read it, and a missing one means a restored tool quietly didn't.
2. A scratch run has no scratch_id: with the promotion cache gone there is
   nothing for an id to point at.
3. A fresh store has zero skill rows after its first start_session — the
   server no longer seeds a library on import, and the restoration did not
   re-enable it. save_template is the one restored tool that must work on
   that unseeded store.
4. skills/templates are hidden from search and from the prior-work signal
   unless the caller asks for one by type=.
5. list_skills is still gone: restoring templates is not restoring skills.

Run: .venv/Scripts/python.exe tests/surface_smoke_test.py
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
os.environ["DSOS_DB_PATH"] = "data/test-runs/surface_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set

# The producer tools: the seven MVP ones, the two the maintainer's U1
# decision restored, and the three the spine added (mark in WP-E2,
# close_question and record_decision in WP-E3). Not a subset — both
# directions are load-bearing. A thirteenth tool is something that should
# not have come back; a ninth here is something that was dropped.
EXPECTED_TOOLS = {
    "start_session", "search_artifacts", "get_artifact", "save_artifact",
    "run_sql", "run_python", "get_lineage", "mark", "save_template", "list_templates",
    "close_question", "record_decision",
}
# Still cut after U1; kept here so a regression names the tool, not a diff.
# save_template/list_templates were in this set between WP-B2 and WP-B2R.
REMOVED_TOOLS = {"promote_scratch", "list_skills", "save_skill", "publish_report"}

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> None:
    async with Client(mcp) as client:
        tools = {t.name for t in (await client.list_tools())}
        check("list_tools() returns exactly the twelve producer tools",
              tools == EXPECTED_TOOLS,
              f"extra={sorted(tools - EXPECTED_TOOLS)} missing={sorted(EXPECTED_TOOLS - tools)}")
        check("no still-cut tool survives", not (tools & REMOVED_TOOLS),
              str(sorted(tools & REMOVED_TOOLS)))

        s1 = (await client.call_tool(
            "start_session", {"question": "surface smoke test"})).data["session_id"]

        # 3. No seeding. The server used to import-time seed seven workflow
        #    skills into every fresh store, so that an agent connecting
        #    anywhere found a library waiting; the trim drops that, and the
        #    library is still there as library code (dsos/seed.py).
        from dsos import store as store_mod
        from dsos.db import connect
        conn = connect(os.environ["DSOS_DB_PATH"])
        n_skills = conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE type = 'skill'"
        ).fetchone()["n"]
        check("a fresh store has zero skill rows after the first start_session",
              n_skills == 0, f"found {n_skills}")
        check("start_session still reports an empty store as empty",
              (await client.call_tool(
                  "start_session", {"question": "second question"})).data["prior_work"]
              ["artifact_count"] == 0)

        # 2. scratch=True with no scratch_id: promotion (and the in-process
        #    cache it read from) is gone, so there is no id to hand back.
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "toy.csv"
            csv.write_text("team,score\na,10\nb,20\n", encoding="utf-8")
            ds = (await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Toy Scores",
                "description": "Synthetic dataset for the surface smoke test.",
                "content_path": str(csv), "content_format": "csv", "session_id": s1,
            })).data
        scratch = (await client.call_tool("run_sql", {
            "code": "SELECT COUNT(*) AS n FROM in_1", "session_id": s1,
            "title": "Scratch count", "description": "Scratch run, nothing persisted.",
            "input_row_ids": [ds["row_id"]], "scratch": True,
        })).data
        check("a scratch run still comes back inline",
              scratch.get("scratch") is True and scratch.get("row_count") == 1,
              str(sorted(scratch))[:80])
        check("a scratch response has no scratch_id", "scratch_id" not in scratch,
              str(sorted(scratch))[:80])

        # 4. Consistency-layer rows (skill/template) are not analysis, so a
        #    search that didn't ask for one must not return one — otherwise
        #    the first thing a new store ranks is its own instructions.
        skill_row = store_mod.save_artifact(
            conn, artifact_id="skill-charting", type="skill",
            title="How we make charts", description="House charting workflow.",
            content="# Charts\n\nAlways pass output_type=\"chart\".\n",
            content_format="markdown", tags=["skill"], session_id=s1,
        )
        template_row = store_mod.save_artifact(
            conn, artifact_id="chart-style-big", type="template",
            title="Big markers", description="Custom chart style.",
            content="lines.markersize: 9\n", content_format="mplstyle",
            tags=["template", "chart-style"], session_id=s1,
        )
        found = (await client.call_tool(
            "search_artifacts", {"query": "how we make charts", "session_id": s1})).data
        check("a search with no type excludes the skill row",
              not any(r["row_id"] == skill_row for r in found["results"]),
              str([(r["type"], r["title"][:24]) for r in found["results"]]))
        check("a search with no type excludes the template row",
              not any(r["row_id"] == template_row for r in found["results"]))
        check("the same search still finds real work",
              any(r["row_id"] == ds["row_id"] for r in found["results"]),
              str([r["title"][:24] for r in found["results"]]))

        by_skill = (await client.call_tool("search_artifacts", {
            "query": "how we make charts", "session_id": s1, "type": "skill",
        })).data
        check("type=\"skill\" still reaches the skill row",
              any(r["row_id"] == skill_row for r in by_skill["results"]),
              str([r["title"][:24] for r in by_skill["results"]]))
        by_template = (await client.call_tool("search_artifacts", {
            "query": "big markers chart style", "session_id": s1, "type": "template",
        })).data
        check("type=\"template\" still reaches the template row",
              any(r["row_id"] == template_row for r in by_template["results"]),
              str([r["title"][:24] for r in by_template["results"]]))

        # A skill is not "prior work" either: start_session must not tell a
        # fresh session that a store of instructions has a history.
        signal = (await client.call_tool(
            "start_session", {"question": "is there prior work here?"})).data
        check("prior work counts neither the skill nor the template",
              signal["prior_work"]["by_type"] == {"dataset": 1},
              str(signal["prior_work"]["by_type"]))
        check("neither is offered as a reuse candidate",
              not any(c["row_id"] in (skill_row, template_row) for c in signal["candidates"]),
              str([c["title"][:24] for c in signal["candidates"]]))

        # 3 (continued). The one tool U1 put back has to work on a store
        #     that was never seeded: templates are store rows, not library
        #     rows, and nothing about save_template may depend on seeding.
        saved = (await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style", "artifact_id": "chart-style-unseeded",
            "title": "Unseeded house style", "description": "Saved on a store with no skill library.",
            "content": "lines.markersize: 7\n",
        })).data
        check("save_template works on a store that was never seeded",
              "row_id" in saved and saved.get("version") == 1, str(sorted(saved))[:120])
        listed = (await client.call_tool(
            "list_templates", {"session_id": s1, "kind": "chart-style"})).data
        check("list_templates finds it, and list_skills is still not a tool",
              "chart-style-unseeded" in {t["artifact_id"] for t in listed["chart_styles"]["custom"]}
              and "list_skills" not in tools)

    shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)
    if FAILURES:
        print(f"\nsurface smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nsurface smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
