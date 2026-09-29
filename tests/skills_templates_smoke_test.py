"""Layer-2c smoke test: the skill library and the template system.

Skills and publishing are no longer MCP tools (WP-B2) — the library code
behind them is deferred, not dead, and this test is where it keeps its
coverage. Seeding, save/edit versioning and validation go through dsos.seed
/ dsos.templating / dsos.publish / dsos.store directly. Templates came back
on the tool surface (WP-B2R, the maintainer's U1 decision), so the same
behaviours are also driven over the MCP wire in the "restored surface"
section below — that is the only place an agent meets them now. run_python
and get_artifact are still driven over MCP too, because the custom chart
style has to resolve through the real tool path.

Covers the cases the design specifically calls out:
- seeding is idempotent, and an agent-edited skill SURVIVES a re-seed (the
  clobbering bug a naive seeding would have)
- chart style is applied by default (dark house), never leaks between runs,
  and a bad reference fails in one call with the options listed
- custom templates validate at SAVE time and are selectable by BOTH
  row_id and the stable artifact_id
- save_template/list_templates work over MCP on a store that was never
  seeded, a bad kind is refused with the allowed set, and re-versioning
  keeps every existing reference resolving
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

from dsos import publish, seed as seed_mod, store as store_mod, templating  # noqa: E402
from dsos.db import connect  # noqa: E402
from dsos.mcp_server import DEFAULT_PYTHON_PATH  # noqa: E402 — after DSOS_DB_PATH is set
from dsos.mcp_server import mcp  # noqa: E402 — import after DSOS_DB_PATH is set


async def main() -> None:
    conn = connect(os.environ["DSOS_DB_PATH"])

    async with Client(mcp) as client:
        r = await client.call_tool("start_session", {"question": "skills + templates smoke test"})
        s1 = r.data["session_id"]

        # --- skill library: seeded at store-init, listed through the store ---
        # The server no longer seeds on import (WP-B2); the library is still
        # there as library code, so seed it explicitly — the same call the
        # old import-time bootstrap made.
        bootstrap = store_mod.start_session(conn, "bootstrap: seed skill library")
        seeded = seed_mod.seed_library(conn, session_id=bootstrap)
        assert len(seeded) == len(seed_mod.SKILL_IDS), f"seed_library returned {len(seeded)}"

        skills = store_mod.list_artifacts(conn, type="skill")
        assert len(skills) >= 6, f"expected the seeded library, got {[s.title for s in skills]}"
        seeded_ids = {s.artifact_id for s in skills if s.artifact_id in seed_mod.SKILL_IDS}
        assert set(seed_mod.SKILL_IDS) <= seeded_ids, seeded_ids
        print(f"[ok] seeded library -> {len(skills)} skills, {len(seeded_ids)} seeded workflows present")

        r = await client.call_tool(
            "search_artifacts", {"query": "how to make charts", "session_id": s1, "type": "skill"}
        )
        assert r.data["results"][0]["type"] == "skill", r.data["results"]
        charting_row = next(s.row_id for s in skills if s.artifact_id == "skill-charting")
        assert r.data["results"][0]["row_id"] == charting_row
        print("[ok] search_artifacts(type='skill') finds skills by content (charting skill ranks first)")

        r = await client.call_tool("get_artifact", {"row_id": charting_row, "session_id": s1})
        assert "style" in r.data["content"] and "output_type" in r.data["content"], r.data
        print("[ok] a skill's workflow content is readable via get_artifact")

        # --- save_skill: new custom skill, then an edit that versions it ---
        r = _save_skill(
            conn, session_id=s1,
            title="How we validate retention models here",
            description="House rules for retention model validation, learned from past sessions.",
            content="# Skill: retention validation\n\nCheck cohort sizes before trusting any retention number.\n",
        )
        custom_id = r["artifact_id"]
        assert custom_id.startswith("skill-") and r["version"] == 1, r
        print(f"[ok] save_skill created a custom skill -> {custom_id} v1")

        r = _save_skill(
            conn, session_id=s1,
            title="How we validate retention models here",
            description="House rules for retention model validation, learned from past sessions.",
            content="# Skill: retention validation v2\n\nAlso check the lookback window.\n",
            artifact_id=custom_id,
        )
        assert r["version"] == 2 and r["artifact_id"] == custom_id, r
        print("[ok] save_skill(edit) -> same artifact_id, new version (old version still readable)")

        # Editing an existing NON-skill artifact id must be refused, not
        # silently corrupt the version chain.
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "toy.csv"
            csv_path.write_text("team,score\na,10\nb,20\n", encoding="utf-8")
            r = await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Toy Scores",
                "description": "Synthetic dataset for the skills/templates smoke test.",
                "content_format": "parquet", "content_path": str(csv_path),
                "session_id": s1,
            })
            dataset_row = r.data["row_id"]
            dataset_artifact_id = await _get_artifact_id(client, s1, dataset_row)
        try:
            _save_skill(conn, session_id=s1, title="Not a dataset",
                        description="Type-conflict probe.", content="x",
                        artifact_id=dataset_artifact_id)
            conflict = "no error"
        except ValueError as exc:
            conflict = str(exc)
        assert "same type" in conflict, conflict
        print(f"[ok] save_skill on a dataset artifact_id refused: {conflict}")

        # --- chart styles: default applied, reset works, bad ref fails fast ---
        r = await client.call_tool("run_python", {
            "code": "import matplotlib.pyplot as plt; result = plt.rcParams['axes.spines.top']",
            "session_id": s1, "title": "Scratch default style probe",
            "description": "Scratch run probing the default chart style.", "input_row_ids": [],
            "scratch": True,
        })
        # the sandbox round-trips a bare bool through result.txt as str
        assert r.data["content"] == "False", r.data  # house style: top spine off
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
        assert r.data["content"] == "True", r.data  # raw matplotlib: top spine back
        print("[ok] style=None resets to raw defaults — no leakage from the previous styled run")

        r = await client.call_tool("run_python", {
            "code": "result = 1", "session_id": s1, "title": "Scratch bad style",
            "description": "Scratch run with an unknown style reference.",
            "input_row_ids": [], "scratch": True, "style": "not-a-real-style",
        })
        assert r.data["status"] == "error" and "dsos" in r.data["error"], r.data
        print(f"[ok] unknown style fails in one call, listing options: {r.data['error'][:90]}...")

        # --- save_template: validate-at-save, then use by id AND row_id ---
        try:
            _save_template(conn, session_id=s1, kind="chart-style",
                           content="this is !!! not valid rcParams !!!")
            rejected = "no error"
        except ValueError as exc:
            rejected = str(exc)
        assert "rcparams" in rejected.lower(), rejected
        print("[ok] invalid chart-style rejected at save time")

        r = _save_template(
            conn, session_id=s1, kind="chart-style", artifact_id="chart-style-bigmarkers",
            title="Big markers", description="House look with oversized markers.",
            content="lines.markersize: 9\naxes.spines.top: True\n",
        )
        custom_style_id = r["artifact_id"]
        custom_style_row = r["row_id"]
        print(f"[ok] save_template(chart-style) -> {custom_style_id} v{r['version']}")

        for ref in (custom_style_id, custom_style_row):  # BOTH reference forms must work
            r = await client.call_tool("run_python", {
                "code": "import matplotlib.pyplot as plt; result = plt.rcParams['lines.markersize']",
                "session_id": s1, "title": f"Scratch custom style via {ref[:12]}",
                "description": "Scratch run applying the custom chart style.",
                "input_row_ids": [], "scratch": True, "style": ref,
            })
            assert r.data["content"] == "9.0", (ref, r.data)
        print("[ok] custom chart style applies by artifact_id AND by row_id")

        try:
            _save_template(conn, session_id=s1, kind="report",
                           content="<html><body>no body token here</body></html>")
            no_body = "no error"
        except ValueError as exc:
            no_body = str(exc)
        assert "{{body}}" in no_body, no_body
        print("[ok] report template without a body token rejected at save time")

        r = _save_template(
            conn, session_id=s1, kind="report", artifact_id="report-custom-test",
            title="Custom report", description="Custom layout for the smoke test.",
            content=(
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>CUSTOM-TEMPLATE {{title}}</title></head><body>"
                "<h1>{{title}}</h1><p>{{session_question}} — {{published_at}}</p>"
                "CUSTOM-REPORT-MARKER {{body}}</body></html>"
            ),
        )
        custom_report_id = r["artifact_id"]
        print(f"[ok] save_template(report) -> {custom_report_id}")

        # base= copies an existing template's text (customize, don't rewrite)
        r = _save_template(
            conn, session_id=s1, kind="report", artifact_id="report-copy-of-minimal",
            base="minimal",
        )
        copied_row = r["row_id"]
        copied = store_mod.get_artifact_by_row_id(conn, copied_row, load_content=True)
        assert "<h1>{{title}}</h1>" in copied.content, copied.content[:200]
        assert copied.source == {"base": "minimal"}, copied.source
        print("[ok] save_template(base=) copies the base template's text, provenance kept in source")

        # re-versioning: an edit keeps the artifact_id, bumps the version
        r = _save_template(
            conn, session_id=s1, kind="chart-style", artifact_id=custom_style_id,
            title="Big markers v2", description="Bigger still.",
            content="lines.markersize: 11\n",
        )
        assert r["version"] == 2 and r["artifact_id"] == custom_style_id, r
        print("[ok] save_template(edit) -> same artifact_id, new version")

        # --- listing templates: built-ins and custom, per kind ---
        listing = _list_templates(conn)
        assert {b["name"] for b in listing["chart_styles"]["builtins"]} == {"dsos", "report", "minimal"}
        assert {b["name"] for b in listing["report_templates"]["builtins"]} == {"report", "default", "minimal"}
        assert custom_style_id in {t["artifact_id"] for t in listing["chart_styles"]["custom"]}
        assert custom_report_id in {t["artifact_id"] for t in listing["report_templates"]["custom"]}
        print("[ok] templating listing shows built-ins and custom templates for both kinds")

        styles_only = _list_templates(conn, kind=templating.CHART_STYLE_KIND)
        assert "chart_styles" in styles_only and "report_templates" not in styles_only, list(styles_only)
        print("[ok] templating listing(kind=) filters to one kind")

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

        result = publish.publish_report(conn, narrative_row, template=custom_report_id)
        html_text = Path(result["path"]).read_text(encoding="utf-8")
        assert "CUSTOM-REPORT-MARKER" in html_text, "custom template must wrap the body"
        assert "<title>CUSTOM-TEMPLATE Custom Template Report</title>" in html_text
        assert "skills + templates smoke test" in html_text  # {{session_question}} substituted
        assert "{{title}}" in html_text  # the narrative's literal token survived
        assert "{{artifact:" not in html_text  # embeds substituted inside the body
        assert "<table" in html_text and "data:image/png;base64," in html_text
        assert "Custom Template Report" in html_text
        print("[ok] publish_report(template=<custom id>) renders custom layout; literal tokens survive")

        # default publish = the dark house layout
        result = publish.publish_report(conn, narrative_row)
        html_text = Path(result["path"]).read_text(encoding="utf-8")
        assert "Analysis report" in html_text and "report-header" in html_text
        print("[ok] publish_report defaults to the dark house report layout")

        # bad template reference fails before writing any file
        try:
            publish.publish_report(conn, narrative_row, template="no-such-template")
            bad_template = "no error"
        except ValueError as exc:
            bad_template = str(exc)
        assert "report" in bad_template, bad_template
        print(f"[ok] unknown template fails cleanly: {bad_template[:80]}...")

        # --- the restored MCP surface (WP-B2R) ---------------------------------
        # Everything above drives templating.py at the library level. The
        # same contract, over the wire, is what an agent gets now: U1 put
        # save_template and list_templates back on the producer server (and
        # deliberately did not put publish_report back), so these two are
        # the whole of the agent-facing template API.
        saved = (await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style", "artifact_id": "chart-style-mcp",
            "title": "House markers (MCP)", "description": "Saved through save_template.",
            "content": "lines.markersize: 13\naxes.grid: True\n",
        })).data
        assert saved["artifact_id"] == "chart-style-mcp" and saved["version"] == 1, saved
        assert "error" not in saved, saved
        style_row, style_ref = saved["row_id"], saved["artifact_id"]

        listed = (await client.call_tool(
            "list_templates", {"session_id": s1, "kind": "chart-style"})).data
        assert "report_templates" not in listed, sorted(listed)
        customs = {t["artifact_id"]: t for t in listed["chart_styles"]["custom"]}
        assert style_ref in customs and customs[style_ref]["row_id"] == style_row, listed
        print(f"[ok] save_template/list_templates over MCP: {style_ref} listed as a custom chart style")

        # The row_id list_templates hands back is what run_python's style=
        # takes, so the agent never has to know about artifact_id at all.
        r = await client.call_tool("run_python", {
            "code": "import matplotlib.pyplot as plt; result = plt.rcParams['lines.markersize']",
            "session_id": s1, "title": "Scratch custom style via MCP row_id",
            "description": "Scratch run applying the MCP-saved chart style.",
            "input_row_ids": [], "scratch": True, "style": style_row,
        })
        assert r.data["content"] == "13.0", r.data
        print("[ok] run_python(style=<row_id>) resolves the MCP-saved chart style")

        # Validate at SAVE time, over the wire: one call, an error payload
        # the agent can act on, and nothing written to the store.
        before = len(store_mod.list_artifacts(conn, type="template"))
        bad = (await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style", "artifact_id": "chart-style-broken",
            "title": "Broken", "description": "Not rcParams at all.",
            "content": "this is !!! not valid rcParams !!!",
        })).data
        assert "error" in bad and "rcparams" in bad["error"].lower(), bad
        assert "row_id" not in bad and not bad["artifact_row_ids"], bad
        assert len(store_mod.list_artifacts(conn, type="template")) == before, (
            "a rejected template must not leave a row behind"
        )
        print(f"[ok] invalid chart style rejected by save_template in one call: {bad['error'][:80]}...")

        # A bad kind is a one-call error listing the allowed set, on both
        # tools — not a 500 and not a silent save.
        for call in (
            {"session_id": s1, "kind": "notebook", "content": "x"},
            {"session_id": s1, "kind": "notebook"},
        ):
            kind_err = (await client.call_tool("save_template", call)).data
            assert "error" in kind_err, kind_err
            assert "chart-style" in kind_err["error"] and "report" in kind_err["error"], kind_err
        list_err = (await client.call_tool(
            "list_templates", {"session_id": s1, "kind": "notebook"})).data
        assert "error" in list_err and "chart-style" in list_err["error"], list_err
        assert "chart_styles" not in list_err and "report_templates" not in list_err, list_err
        print(f"[ok] a bad kind is refused with the allowed set: {list_err['error']}")

        # re-versioning over MCP: the artifact_id is the stable reference, so
        # an edit must not break a run_python style= or a base= built on it.
        v2 = (await client.call_tool("save_template", {
            "session_id": s1, "kind": "chart-style", "artifact_id": style_ref,
            "title": "House markers v2", "description": "Even bigger markers.",
            "content": "lines.markersize: 21\n",
        })).data
        assert v2["artifact_id"] == style_ref and v2["version"] == 2, v2
        r = await client.call_tool("run_python", {
            "code": "import matplotlib.pyplot as plt; result = plt.rcParams['lines.markersize']",
            "session_id": s1, "title": "Scratch re-versioned style",
            "description": "Scratch run applying the re-versioned chart style.",
            "input_row_ids": [], "scratch": True, "style": style_ref,
        })
        assert r.data["content"] == "21.0", r.data
        assert "lines.markersize: 21" in templating.base_template_content(
            conn, templating.CHART_STYLE_KIND, style_ref
        )
        re_listed = (await client.call_tool(
            "list_templates", {"session_id": s1, "kind": "chart-style"})).data
        re_customs = {t["artifact_id"]: t for t in re_listed["chart_styles"]["custom"]}
        assert re_customs[style_ref]["version"] == 2 and re_customs[style_ref]["row_id"] == v2["row_id"]
        print("[ok] re-versioning keeps the same artifact_id resolvable (style=, base=, listing)")

        # No kind: both kinds, built-ins included — the discovery an agent
        # needs before it decides to write a template of its own.
        everything = (await client.call_tool("list_templates", {"session_id": s1})).data
        assert {b["name"] for b in everything["chart_styles"]["builtins"]} == {
            "dsos", "report", "minimal"}
        assert {b["name"] for b in everything["report_templates"]["builtins"]} == {
            "report", "default", "minimal"}
        assert custom_style_id in {t["artifact_id"] for t in everything["chart_styles"]["custom"]}
        assert custom_report_id in {t["artifact_id"] for t in everything["report_templates"]["custom"]}
        assert style_ref in {t["artifact_id"] for t in everything["chart_styles"]["custom"]}
        print("[ok] list_templates() with no kind lists both kinds, built-ins and custom")

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
        skills_before = {
            a.artifact_id: len(store_mod.list_versions(conn, a.artifact_id))
            for a in store_mod.list_artifacts(conn, type="skill")
        }
        reseed = store_mod.start_session(conn, "reseed check")
        seeded_now = seed_mod.seed_library(conn, session_id=reseed)
        assert seeded_now == [], f"a full library must re-seed nothing, got {seeded_now}"
        skills_after = {
            a.artifact_id: len(store_mod.list_versions(conn, a.artifact_id))
            for a in store_mod.list_artifacts(conn, type="skill")
        }
        assert skills_before == skills_after, "re-seeding must not touch existing (edited) skills"
        assert len(store_mod.list_versions(conn, custom_id)) == 2
        print(f"[ok] re-seeding is a no-op: {len(skills_before)} skills, edited skill still at v2")

    print("\nskills + templates smoke test passed.")


# --- the removed MCP tools, now exercised as the library calls they wrapped
# (WP-B2). The tools are gone; the behaviour they fronted is not. ---


def _save_skill(conn, *, session_id: str, title: str, description: str, content: str,
                artifact_id: str | None = None, tags: list[str] | None = None) -> dict:
    """Create a new skill or a new version of an existing one. Raises
    ValueError (what the tool surfaced as {"error": ...})."""
    if artifact_id is None:
        artifact_id = "skill-" + store_mod.safe_table_name(title)
    final_tags = ["skill"] + [t for t in (tags or []) if t != "skill"]
    row_id = store_mod.save_artifact(
        conn, artifact_id=artifact_id, type="skill", title=title,
        description=description, content=content, content_format="markdown",
        tags=final_tags, session_id=session_id,
    )
    art = store_mod.get_artifact_by_row_id(conn, row_id, load_content=False)
    return {"row_id": row_id, "artifact_id": artifact_id, "version": art.version}


def _save_template(conn, *, session_id: str, kind: str, content: str | None = None,
                   artifact_id: str | None = None, title: str | None = None,
                   description: str | None = None, tags: list[str] | None = None,
                   base: str | None = None) -> dict:
    """Create or re-version a template, validating at save time so a bad
    template fails here rather than two rounds later at use time."""
    if kind not in templating.TEMPLATE_KINDS:
        raise ValueError(f"kind must be one of {sorted(templating.TEMPLATE_KINDS)}, got {kind!r}")
    if base is not None:
        base_text = templating.base_template_content(conn, kind, base)
        if content is None:
            content = base_text
    if not content or not content.strip():
        raise ValueError(
            "content is required — pass content, or base to copy an existing template"
        )
    if kind == templating.CHART_STYLE_KIND:
        templating.validate_chart_style_text(content, DEFAULT_PYTHON_PATH)
    else:
        templating.validate_report_template_text(content)

    if artifact_id is None:
        artifact_id = f"{kind}-" + store_mod.safe_table_name(title or "custom")
    if title is None:
        title = f"Custom {kind} template" + (f" (based on {base})" if base else "")
    if description is None:
        description = (
            f"Custom {kind} template for consistent styling"
            + (f", customized from {base!r}." if base else ".")
        )
    final_tags = templating.template_tags(kind) + [
        t for t in (tags or []) if t not in ("template", kind)
    ]
    row_id = store_mod.save_artifact(
        conn, artifact_id=artifact_id, type="template", title=title,
        description=description, content=content,
        content_format=("mplstyle" if kind == templating.CHART_STYLE_KIND else "html"),
        tags=final_tags, session_id=session_id,
        source={"base": base} if base else None,
    )
    art = store_mod.get_artifact_by_row_id(conn, row_id, load_content=False)
    return {"row_id": row_id, "artifact_id": artifact_id, "version": art.version, "kind": kind}


def _list_templates(conn, kind: str | None = None) -> dict:
    """Built-in and custom templates, per kind."""
    if kind is not None and kind not in templating.TEMPLATE_KINDS:
        raise ValueError(f"kind must be one of {sorted(templating.TEMPLATE_KINDS)}, got {kind!r}")
    customs = store_mod.list_artifacts(conn, type="template")

    def _custom(kind_tag: str) -> list[dict]:
        return [
            {
                "artifact_id": a.artifact_id, "row_id": a.row_id, "version": a.version,
                "title": a.title, "description": a.description, "tags": a.tags,
            }
            for a in customs if kind_tag in a.tags
        ]

    result: dict = {"artifact_row_ids": [a.row_id for a in customs]}
    if kind in (None, templating.CHART_STYLE_KIND):
        result["chart_styles"] = {
            "builtins": [
                {"name": n, "description": d}
                for n, d in sorted(templating.CHART_STYLE_BUILTINS.items())
            ],
            "custom": _custom(templating.CHART_STYLE_KIND),
        }
    if kind in (None, templating.REPORT_KIND):
        result["report_templates"] = {
            "builtins": [
                {"name": n, "description": d}
                for n, d in sorted(templating.REPORT_TEMPLATE_BUILTINS.items())
            ],
            "custom": _custom(templating.REPORT_KIND),
        }
    return result


async def _get_artifact_id(client, session_id: str, row_id: str) -> str:
    """save_artifact's MCP response doesn't surface artifact_id; pull it
    from get_artifact for the type-conflict probe."""
    r = await client.call_tool("get_artifact", {"row_id": row_id, "session_id": session_id})
    return r.data["artifact_id"]


if __name__ == "__main__":
    asyncio.run(main())
