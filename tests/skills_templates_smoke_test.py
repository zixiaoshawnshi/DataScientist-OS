"""Layer-2c smoke test: the skill library and template system, over the MCP
wire (same pattern as tests/mcp_smoke_test.py).

Covers the cases the design specifically calls out:
- seeding is idempotent, and an agent-edited skill SURVIVES a re-seed (the
  clobbering bug a naive seeding would have)
- chart style is applied by default (dark house), never leaks between runs,
  and a bad reference fails in one call with the options listed
- custom templates validate at SAVE time and are selectable by BOTH
  row_id and the stable artifact_id
- report token substitution is order-safe: a literal token written in the
  narrative's own text must survive publishing untouched

Run: .venv/Scripts/python.exe tests/skills_templates_smoke_test.py
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
os.environ["DSOS_DB_PATH"] = "data/test-runs/skills_templates_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from fastmcp import Client  # noqa: E402

from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set


async def main() -> None:
    async with Client(mcp) as client:
        r = await client.call_tool("start_session", {"question": "skills + templates smoke test"})
        s1 = r.data["session_id"]

        # --- skill library: seeded at store-init, listed over the wire ---
        r = await client.call_tool("list_skills", {"session_id": s1})
        skills = r.data["skills"]
        assert len(skills) >= 6, f"expected the seeded library, got {[s['title'] for s in skills]}"
        seeded_ids = {s["artifact_id"] for s in skills if s["seeded"]}
        assert {
            "skill-dataset-discovery", "skill-exploratory-analysis", "skill-charting",
            "skill-statistical-analysis", "skill-modeling", "skill-reporting",
        } <= seeded_ids, seeded_ids
        print(f"[ok] list_skills -> {len(skills)} skills, six seeded workflows present")

        r = await client.call_tool(
            "search_artifacts", {"query": "how to make charts", "session_id": s1}
        )
        assert r.data["results"][0]["type"] == "skill", r.data["results"]
        charting_row = next(s["row_id"] for s in skills if s["artifact_id"] == "skill-charting")
        assert r.data["results"][0]["row_id"] == charting_row
        print("[ok] search_artifacts finds skills by content (charting skill ranks first)")

        r = await client.call_tool("get_artifact", {"row_id": charting_row, "session_id": s1})
        assert "style" in r.data["content"] and "output_type" in r.data["content"], r.data
        print("[ok] a skill's workflow content is readable via get_artifact")

        # --- save_skill: new custom skill, then an edit that versions it ---
        r = await client.call_tool("save_skill", {
            "session_id": s1,
            "title": "How we validate retention models here",
            "description": "House rules for retention model validation, learned from past sessions.",
            "content": "# Skill: retention validation\n\nCheck cohort sizes before trusting any retention number.\n",
        })
        assert "error" not in r.data, r.data
        custom_id = r.data["artifact_id"]
        assert custom_id.startswith("skill-") and r.data["version"] == 1, r.data
        print(f"[ok] save_skill created a custom skill -> {custom_id} v1")

        r = await client.call_tool("save_skill", {
            "session_id": s1,
            "title": "How we validate retention models here",
            "description": "House rules for retention model validation, learned from past sessions.",
            "content": "# Skill: retention validation v2\n\nAlso check the lookback window.\n",
            "artifact_id": custom_id,
        })
        assert r.data["version"] == 2 and r.data["artifact_id"] == custom_id, r.data
        print("[ok] save_skill(edit) -> same artifact_id, new version (old version still readable)")

        # Editing an existing NON-skill artifact id must be refused, not
        # silently corrupt the version chain.
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "toy.csv"
            csv_path.write_text("team,score\na,10\nb,20\n")
            r = await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Toy Scores",
                "description": "Synthetic dataset for the skills/templates smoke test.",
                "content_format": "parquet", "content_path": str(csv_path),
                "session_id": s1,
            })
            dataset_row = r.data["row_id"]
            dataset_artifact_id = await _get_artifact_id(client, s1, dataset_row)
        r = await client.call_tool("save_skill", {
            "session_id": s1, "title": "Not a dataset", "description": "Type-conflict probe.",
            "content": "x", "artifact_id": dataset_artifact_id,
        })
        assert "error" in r.data and "same type" in r.data["error"], r.data
        print(f"[ok] save_skill on a dataset artifact_id refused: {r.data['error']}")

        # --- chart styles: default applied, reset works, bad ref fails fast ---
        r = await client.call_tool("run_python", {
            "code": "import matplotlib.pyplot as plt; result = plt.rcParams['axes.spines.top']",
            "session_id": s1, "title": "Scratch default style probe",
            "description": "Scratch run probing the default chart style.", "input_row_ids": [],
            "scratch": True,
        })
        assert r.data["content"] is False, r.data  # house style: top spine off
        r = await client.call_tool("run_python", {
            "code": "import matplotlib.pyplot as plt; result = plt.rcParams['figure.facecolor']",
            "session_id": s1, "title": "Scratch dark bg probe",
            "description": "Scratch run probing the default dark background.", "input_row_ids": [],
            "scratch": True,
        })
        assert "#141a22" in str(r.data["content"]), r.data  # dark canvas
        print("[ok] run_python default style is the dark house style (spines off, dark bg)")

        r = await client.call_tool("run_python", {
            "code": "import matplotlib.pyplot as plt; result = plt.rcParams['axes.spines.top']",
            "session_id": s1, "title": "Scratch no-style probe",
            "description": "Scratch run proving style=None resets rcParams.", "input_row_ids": [],
            "scratch": True, "style": None,
        })
        assert r.data["content"] is True, r.data  # raw matplotlib: top spine back
        print("[ok] style=None resets to raw defaults — no leakage from the previous styled run")

        r = await client.call_tool("run_python", {
            "code": "result = 1", "session_id": s1, "title": "Scratch bad style",
            "description": "Scratch run with an unknown style reference.",
            "input_row_ids": [], "scratch": True, "style": "not-a-real-style",
        })
        assert r.data["status"] == "error" and "dsos" in r.data["error"], r.data
        print(f"[ok] unknown style fails in one call, listing options: {r.data['error'][:90]}...")

        # --- save_template: validate-at-save, then use by id AND row_id ---
        r = await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style",
            "content": "this is !!! not valid rcParams !!!",
        })
        assert "error" in r.data, r.data
        assert "rcparams" in r.data["error"].lower(), r.data["error"]
        print(f"[ok] invalid chart-style rejected at save time")

        r = await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style", "artifact_id": "chart-style-bigmarkers",
            "title": "Big markers", "description": "House look with oversized markers.",
            "content": "lines.markersize: 9\naxes.spines.top: True\n",
        })
        assert "error" not in r.data, r.data
        custom_style_id = r.data["artifact_id"]
        custom_style_row = r.data["row_id"]
        print(f"[ok] save_template(chart-style) -> {custom_style_id} v{r.data['version']}")

        for ref in (custom_style_id, custom_style_row):  # BOTH reference forms must work
            r = await client.call_tool("run_python", {
                "code": "import matplotlib.pyplot as plt; result = plt.rcParams['lines.markersize']",
                "session_id": s1, "title": f"Scratch custom style via {ref[:12]}",
                "description": "Scratch run applying the custom chart style.",
                "input_row_ids": [], "scratch": True, "style": ref,
            })
            assert r.data["content"] == 9.0, (ref, r.data)
        print("[ok] custom chart style applies by artifact_id AND by row_id")

        r = await client.call_tool("save_template", {
            "session_id": s1, "kind": "report",
            "content": "<html><body>no body token here</body></html>",
        })
        assert "error" in r.data and "{{body}}" in r.data["error"], r.data
        print(f"[ok] report template without a body token rejected at save time")

        r = await client.call_tool("save_template", {
            "session_id": s1, "kind": "report", "artifact_id": "report-custom-test",
            "title": "Custom report", "description": "Custom layout for the smoke test.",
            "content": (
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>CUSTOM-TEMPLATE {{title}}</title></head><body>"
                "<h1>{{title}}</h1><p>{{session_question}} — {{published_at}}</p>"
                "CUSTOM-REPORT-MARKER {{body}}</body></html>"
            ),
        })
        assert "error" not in r.data, r.data
        custom_report_id = r.data["artifact_id"]
        print(f"[ok] save_template(report) -> {custom_report_id}")

        # base= copies an existing template's text (customize, don't rewrite)
        r = await client.call_tool("save_template", {
            "session_id": s1, "kind": "report", "artifact_id": "report-copy-of-minimal",
            "base": "minimal",
        })
        assert "error" not in r.data, r.data
        copied_row = r.data["row_id"]
        r = await client.call_tool("get_artifact", {"row_id": copied_row, "session_id": s1})
        assert "<h1>{{title}}</h1>" in r.data["content"], r.data["content"][:200]
        assert r.data["source"] == {"base": "minimal"}, r.data
        print("[ok] save_template(base=) copies the base template's text, provenance kept in source")

        # re-versioning: an edit keeps the artifact_id, bumps the version
        r = await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style", "artifact_id": custom_style_id,
            "title": "Big markers v2", "description": "Bigger still.",
            "content": "lines.markersize: 11\n",
        })
        assert r.data["version"] == 2 and r.data["artifact_id"] == custom_style_id, r.data
        print("[ok] save_template(edit) -> same artifact_id, new version")

        # --- list_templates: built-ins and custom, per kind ---
        r = await client.call_tool("list_templates", {"session_id": s1})
        assert {b["name"] for b in r.data["chart_styles"]["builtins"]} == {"dsos", "report", "minimal"}
        assert {b["name"] for b in r.data["report_templates"]["builtins"]} == {"report", "default", "minimal"}
        assert custom_style_id in {t["artifact_id"] for t in r.data["chart_styles"]["custom"]}
        assert custom_report_id in {t["artifact_id"] for t in r.data["report_templates"]["custom"]}
        print("[ok] list_templates shows built-ins and custom templates for both kinds")

        r = await client.call_tool("list_templates", {"session_id": s1, "kind": "chart-style"})
        assert "chart_styles" in r.data and "report_templates" not in r.data, list(r.data)
        print("[ok] list_templates(kind=) filters to one kind")

        # --- publish with the custom template; token order-safety ---
        r = await client.call_tool("run_python", {
            "code": (
                "import matplotlib.pyplot as plt\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot([1, 2, 3])\n"
                "result = fig\n"
            ),
            "session_id": s1, "title": "Dark house chart",
            "description": "A chart in the default dark house style.",
            "input_row_ids": [dataset_row], "output_type": "chart",
        })
        assert r.data["status"] == "ok" and r.data["content_format"] == "png", r.data
        chart_row = r.data["row_id"]

        # The narrative's own text contains a literal {{title}} — it must
        # survive publishing untouched ({{body}} substitutes LAST, so the
        # body is never re-scanned for tokens).
        narrative_md = (
            f"# Token safety\n\nThe literal token {{{{title}}}} in this text must stay put. "
            f"See {{{{artifact:{dataset_row}}}}} and {{{{artifact:{chart_row}}}}}."
        )
        r = await client.call_tool("save_artifact", {
            "type": "narrative", "title": "Custom Template Report", "session_id": s1,
            "description": "Narrative published through a custom template.", "content_format": "markdown",
            "content_text": narrative_md,
        })
        narrative_row = r.data["row_id"]

        r = await client.call_tool("publish_report", {
            "row_id": narrative_row, "session_id": s1, "template": custom_report_id,
        })
        assert "error" not in r.data, r.data
        html_text = Path(r.data["path"]).read_text(encoding="utf-8")
        assert "CUSTOM-REPORT-MARKER" in html_text, "custom template must wrap the body"
        assert "<title>CUSTOM-TEMPLATE Custom Template Report</title>" in html_text
        assert "skills + templates smoke test" in html_text  # {{session_question}} substituted
        assert "{{title}}" in html_text  # the narrative's literal token survived
        assert "{{artifact:" not in html_text  # embeds substituted inside the body
        assert "<table" in html_text and "data:image/png;base64," in html_text
        assert "Custom Template Report" in html_text
        print("[ok] publish_report(template=<custom id>) renders custom layout; literal tokens survive")

        # default publish = the dark house layout
        r = await client.call_tool("publish_report", {"row_id": narrative_row, "session_id": s1})
        html_text = Path(r.data["path"]).read_text(encoding="utf-8")
        assert "Analysis report" in html_text and "report-header" in html_text
        print("[ok] publish_report defaults to the dark house report layout")

        # bad template reference fails before writing any file
        r = await client.call_tool("publish_report", {
            "row_id": narrative_row, "session_id": s1, "template": "no-such-template",
        })
        assert "error" in r.data and "report" in r.data["error"], r.data
        print(f"[ok] unknown template fails cleanly: {r.data['error'][:80]}...")

        # --- sandbox path gets the same chart styling ---
        from dsos import sandbox as dsos_sandbox
        if dsos_sandbox.find_uv() is None:
            print("[skip] uv not installed — sandbox styling round skipped")
        else:
            r = await client.call_tool("run_python", {
                "code": "import matplotlib.pyplot as plt; result = plt.rcParams['axes.spines.top']",
                "session_id": s1, "title": "Scratch sandbox style probe",
                "description": "Scratch run probing style inside the uv sandbox.",
                "input_row_ids": [], "scratch": True, "requirements": ["tabulate"],
            })
            # the sandbox round-trips a bare bool through result.txt as str
            assert r.data["status"] == "ok" and str(r.data["content"]) == "False", r.data
            print("[ok] run_python(requirements=...) applies the same chart styling in the sandbox")

        # --- the seeding edge case: an edited skill SURVIVES a re-seed ---
        from dsos import seed as seed_mod, store as store_mod
        from dsos.db import connect as db_connect
        diag = db_connect(os.environ["DSOS_DB_PATH"])
        skills_before = {
            a.artifact_id: len(store_mod.list_versions(diag, a.artifact_id))
            for a in store_mod.list_artifacts(diag, type="skill")
        }
        reseed = store_mod.start_session(diag, "reseed check")
        seeded_now = seed_mod.seed_library(diag, session_id=reseed)
        assert seeded_now == [], f"a full library must re-seed nothing, got {seeded_now}"
        skills_after = {
            a.artifact_id: len(store_mod.list_versions(diag, a.artifact_id))
            for a in store_mod.list_artifacts(diag, type="skill")
        }
        assert skills_before == skills_after, "re-seeding must not touch existing (edited) skills"
        assert len(store_mod.list_versions(diag, custom_id)) == 2
        print(f"[ok] re-seeding is a no-op: {len(skills_before)} skills, edited skill still at v2")

    print("\nskills + templates smoke test passed.")


async def _get_artifact_id(client, session_id: str, row_id: str) -> str:
    """save_artifact's MCP response doesn't surface artifact_id; pull it
    from get_artifact for the type-conflict probe."""
    r = await client.call_tool("get_artifact", {"row_id": row_id, "session_id": session_id})
    return r.data["artifact_id"]


if __name__ == "__main__":
    asyncio.run(main())
