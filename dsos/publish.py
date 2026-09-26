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
from pathlib import Path

import markdown as md_lib
import pandas as pd

from dsos.store import _EMBED_RE, Artifact, get_artifact_by_row_id

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a;
  }}
  .dsos-embed {{
    border: 1px solid #eee; border-radius: 8px; padding: 1rem;
    margin: 1.5rem 0; background: #fafafa;
  }}
  .dsos-embed h4 {{
    margin: 0 0 0.75rem; color: #555; font-size: 0.8rem;
    text-transform: uppercase; letter-spacing: 0.03em;
  }}
  table.dsos-table {{ border-collapse: collapse; width: 100%; }}
  table.dsos-table th, table.dsos-table td {{
    padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; text-align: left;
  }}
  img {{ max-width: 100%; border-radius: 6px; }}
  pre {{ background: #0f172a; color: #e2e8f0; padding: 1rem; border-radius: 8px; overflow-x: auto; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


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


def publish_report(
    conn: sqlite3.Connection, row_id: str, *, output_dir: str = "data/reports",
) -> dict:
    """Render a narrative artifact and every artifact it embeds into one
    self-contained .html file (images inlined as base64 data URIs — no
    sibling image files, per the Part 1 scope cut). Returns
    {"path": ..., "embeds": [...], "artifact_row_ids": [...]}.

    `embeds` (feedback #3) lists what actually got pulled into this report
    — row_id/type/title for each {{artifact:...}} reference, and whether it
    resolved — a quick confirmation of what's in the report without a
    separate get_lineage call.

    Does not create a new artifact row: a rendered export isn't itself a
    versioned analysis artifact, it's the exit from this layer — publish
    the narrative itself first (save_artifact) if it should stay reusable.
    """
    art = _get_narrative(conn, row_id)
    embeds = _resolve_embeds(conn, art.content)

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

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{art.artifact_id}-v{art.version}.html"
    out_path.write_text(
        _PAGE_TEMPLATE.format(title=html.escape(art.title), body=body_html), encoding="utf-8"
    )

    touched = [row_id, *(e["row_id"] for e in embeds if e["resolved"])]
    return {"path": str(out_path), "embeds": embeds, "artifact_row_ids": touched}
