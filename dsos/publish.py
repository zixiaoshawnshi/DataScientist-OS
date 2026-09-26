"""Publish: renders a narrative artifact and everything it {{artifact:...}}
embeds into one self-contained local HTML file — the seam where a
finished artifact leaves this working layer (design doc, Philosophy #5).

Core library, protocol-unaware, like store.py/execution.py. The MCP
server's publish_report tool is a thin wrapper over `publish_report` below.
"""

from __future__ import annotations

import base64
import html
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import markdown as md_lib
import pandas as pd

from dsos import templating
from dsos.store import _EMBED_RE, Artifact, get_artifact_by_row_id, get_session

# The page layout now comes from templating (dsos/assets/reports/*.html):
# built-in names map to package assets, custom report templates are
# `template` artifacts resolved by row_id/artifact_id. "default" there is
# this module's original inline template, kept as a file — byte-for-byte
# the look this module used to hardcode.


def _render_embed(conn: sqlite3.Connection, row_id: str) -> tuple[str, Artifact | None]:
    """One {{artifact:row_id}} match -> (html_fragment, the resolved
    artifact or None if the row_id doesn't exist — silently dropped, same
    as save_artifact's own lineage handling)."""
    art = get_artifact_by_row_id(conn, row_id, load_content=True)
    if art is None:
        return f"<p><em>[missing artifact {html.escape(row_id)}]</em></p>", None

    heading = f"<h4>{html.escape(art.title)}</h4>"
    if isinstance(art.content, pd.DataFrame):
        table_html = art.content.to_html(index=False, classes="dsos-table", border=0)
        fragment = f'<div class="dsos-embed">{heading}{table_html}</div>'
    elif isinstance(art.content, bytes):
        b64 = base64.b64encode(art.content).decode()
        img = f'<img src="data:image/png;base64,{b64}" alt="{html.escape(art.title)}">'
        fragment = f'<div class="dsos-embed">{heading}{img}</div>'
    else:
        text = art.content if isinstance(art.content, str) else str(art.content)
        fragment = f"<div class=\"dsos-embed\">{heading}<pre>{html.escape(text)}</pre></div>"
    return fragment, art


def _get_narrative(conn: sqlite3.Connection, row_id: str) -> Artifact:
    art = get_artifact_by_row_id(conn, row_id, load_content=True)
    if art is None:
        raise ValueError(f"no artifact with row_id {row_id!r}")
    if art.type != "narrative":
        raise ValueError(f"publish_report expects a narrative artifact, got type={art.type!r}")
    if art.content_error:
        raise ValueError(
            f"narrative {row_id!r}: its content blob can't be read ({art.content_error}) "
            f"— re-save the narrative"
        )
    return art


def _resolve_embeds(conn: sqlite3.Connection, content: str) -> list[dict]:
    """Every {{artifact:row_id}} in `content`, in order, with whether it
    resolved — the shared basis for both the real publish's embed list and
    preview_report's dry-run report (feedback #3, #7)."""
    embeds = []
    for m in _EMBED_RE.finditer(content):
        rid = m.group(1)
        resolved = get_artifact_by_row_id(conn, rid, load_content=False)
        embeds.append({
            "row_id": rid,
            "resolved": resolved is not None,
            "type": resolved.type if resolved else None,
            "title": resolved.title if resolved else None,
        })
    return embeds


def preview_report(conn: sqlite3.Connection, row_id: str) -> dict:
    """Dry-run for publish_report: resolve every {{artifact:...}} embed in
    the narrative and report on it — without rendering or writing anything
    — so a stale/typo'd row_id is caught before spending a real publish
    (feedback #7). `broken_row_ids` is the actionable part: fix those, then
    publish for real."""
    art = _get_narrative(conn, row_id)
    embeds = _resolve_embeds(conn, art.content)
    broken = [e["row_id"] for e in embeds if not e["resolved"]]
    return {
        "title": art.title,
        "embeds": embeds,
        "broken_row_ids": broken,
        "artifact_row_ids": [row_id, *(e["row_id"] for e in embeds if e["resolved"])],
    }


def render_report_html(
    conn: sqlite3.Connection, row_id: str, *, template: str = "report",
) -> tuple[str, Artifact, list[dict]]:
    """The rendering core of publish_report, minus the file write — shared
    with the GUI's on-demand `/artifacts/{row_id}/report` route (serve a
    narrative straight from the running server, no local .html file needed)
    so the two paths can't drift apart. Returns (page_html, narrative
    artifact, embeds)."""
    art = _get_narrative(conn, row_id)
    embeds = _resolve_embeds(conn, art.content)
    template_text = templating.resolve_report_template(conn, template)

    def _substitute(m: re.Match) -> str:
        fragment, _resolved = _render_embed(conn, m.group(1))
        return fragment

    # Substitute embeds into the HTML *after* markdown conversion, not into
    # the markdown source before it: an embed's fragment (e.g. pandas'
    # to_html() output) can contain blank lines, which the markdown parser's
    # blank-line-is-a-paragraph-break rule would otherwise use to split a
    # single injected <table> across multiple mismatched <p> blocks.
    body_html = md_lib.markdown(art.content, extensions=["tables", "fenced_code"])
    body_html = _EMBED_RE.sub(_substitute, body_html)

    # Layout is the template's job; tokens are substituted literally (never
    # format()/Jinja — the body's own {{artifact:...}} embeds and any literal
    # "{{title}}" in the narrative must survive substitution; see
    # templating.render_report, which puts {{body}} in LAST for exactly that).
    # title/question are HTML-escaped here; body is already rendered HTML.
    session = get_session(conn, art.session_id)
    page = templating.render_report(
        template_text,
        title=html.escape(art.title),
        body=body_html,
        published_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        session_question=html.escape(session["question"]) if session else "",
    )
    return page, art, embeds


def publish_report(
    conn: sqlite3.Connection, row_id: str, *, output_dir: str = "data/reports",
    template: str = "report",
) -> dict:
    """Render a narrative artifact and every artifact it embeds into one
    self-contained .html file (images inlined as base64 data URIs — no
    sibling image files, per the Part 1 scope cut). Returns
    {"path": ..., "embeds": [...], "artifact_row_ids": [...]}.

    `template`: the HTML layout (the consistency layer — dsos/templating.py).
    Built-ins: "report" (the dark house layout — the default), "default"
    (the original plain light look), "minimal" (bare HTML); or a custom
    report-template artifact's row_id/artifact_id. Resolved before
    rendering, so a bad reference fails before any file is written.

    `embeds` (feedback #3) lists what actually got pulled into this report
    — row_id/type/title for each {{artifact:...}} reference, and whether it
    resolved — a quick confirmation of what's in the report without a
    separate get_lineage call.

    Does not create a new artifact row: a rendered export isn't itself a
    versioned analysis artifact, it's the exit from this layer — publish
    the narrative itself first (save_artifact) if it should stay reusable.
    """
    page, art, embeds = render_report_html(conn, row_id, template=template)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{art.artifact_id}-v{art.version}.html"
    out_path.write_text(page, encoding="utf-8")

    touched = [row_id, *(e["row_id"] for e in embeds if e["resolved"])]
    return {"path": str(out_path), "embeds": embeds, "artifact_row_ids": touched}
