"""Layer 3 (stretch): the read-only GUI. A separate process from the MCP
server, reading dsos.store directly off the same SQLite file — not through
MCP. Browse-and-understand only: no delete/retag/edit (see Part 3 design
doc, "GUI scope").

Run: .venv/Scripts/python.exe -m dsos.gui
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dsos import present, store
from dsos.db import connect
from dsos.publish import render_report_html

DB_PATH = os.environ.get("DSOS_DB_PATH", "data/store.db")
conn = connect(DB_PATH)

app = FastAPI(title="DS Artifact OS — GUI (read-only)")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# "template": chart styles / report layouts — artifacts like anything else
# (see dsos/templating.py). Authoring stays agent-only for now; the GUI
# just doesn't hide them from the gallery.
ARTIFACT_TYPES = ["dataset", "query", "transform", "chart", "narrative", "skill", "template"]


@app.get("/", response_class=HTMLResponse)
def sessions_list(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"sessions": store.list_sessions(conn)}
    )


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(request: Request, session_id: str):
    session = store.get_session(conn, session_id)
    if session is None:
        return HTMLResponse(f"<p>no session {session_id!r}</p>", status_code=404)
    artifacts = store.list_artifacts(conn, session_id=session_id)
    return templates.TemplateResponse(
        request, "session_detail.html", {"session": session, "artifacts": artifacts}
    )


@app.get("/sessions/{session_id}/tool_calls", response_class=HTMLResponse)
def session_tool_calls(request: Request, session_id: str):
    """htmx-polled partial — the live tool-call feed, no websockets needed."""
    calls = store.list_tool_calls(conn, session_id)
    return templates.TemplateResponse(request, "_tool_calls_feed.html", {"calls": calls})


@app.get("/artifacts", response_class=HTMLResponse)
def artifacts_gallery(
    request: Request,
    type: str | None = None,
    session_id: str | None = None,
    q: str | None = None,
):
    """`q` calls search_artifacts directly, so gallery search and agent
    search share one index (see design doc, GUI routes)."""
    if q:
        artifacts = [a for a, _score in store.search_artifacts(conn, q, top_k=50, type=type)]
        if session_id:
            artifacts = [a for a in artifacts if a.session_id == session_id]
    else:
        artifacts = store.list_artifacts(conn, type=type, session_id=session_id)
    return templates.TemplateResponse(
        request,
        "artifacts_gallery.html",
        {
            "artifacts": artifacts, "types": ARTIFACT_TYPES,
            "type": type or "", "session_id": session_id or "", "q": q or "",
        },
    )


@app.get("/artifacts/{row_id}", response_class=HTMLResponse)
def artifact_detail(request: Request, row_id: str):
    art = store.get_artifact_by_row_id(conn, row_id, load_content=True)
    if art is None:
        return HTMLResponse(f"<p>no artifact with row_id {row_id!r}</p>", status_code=404)
    payload = present.artifact_payload(conn, art)
    png_b64 = (
        base64.b64encode(art.content).decode()
        if art.content_format == "png" and isinstance(art.content, bytes)
        else None
    )
    uses = store.get_lineage(conn, row_id, direction="ancestors")
    used_by = store.get_lineage(conn, row_id, direction="descendants")
    return templates.TemplateResponse(
        request,
        "artifact_detail.html",
        {
            "art": art,
            "payload": payload,
            "png_b64": png_b64,
            "uses": uses,
            "used_by": used_by,
            "graph": _mermaid_graph(art, uses, used_by),
            "versions": store.list_versions(conn, art.artifact_id),
            "execution": store.get_execution(conn, row_id),
        },
    )


@app.get("/artifacts/{row_id}/report", response_class=HTMLResponse)
def artifact_report(request: Request, row_id: str, template: str = "report"):
    """Serve a narrative's published report straight from this process —
    the "serve it" alternative to opening publish_report's local .html file
    (design-doc's publish target stays local-first; this just changes how
    you *view* it). Rendered fresh on every request, nothing written to
    disk — a stale view isn't possible."""
    if store.get_artifact_by_row_id(conn, row_id, load_content=False) is None:
        return HTMLResponse(f"<p>no artifact with row_id {row_id!r}</p>", status_code=404)
    try:
        page, _art, _embeds = render_report_html(conn, row_id, template=template)
    except ValueError as exc:
        return HTMLResponse(f"<p>{exc}</p>", status_code=400)
    return HTMLResponse(page)


@app.get("/artifacts/{row_id}/lineage", response_class=HTMLResponse)
def artifact_lineage(request: Request, row_id: str):
    art = store.get_artifact_by_row_id(conn, row_id, load_content=False)
    if art is None:
        return HTMLResponse(f"<p>no artifact with row_id {row_id!r}</p>", status_code=404)
    ancestors = store.get_lineage(conn, row_id, direction="ancestors")
    descendants = store.get_lineage(conn, row_id, direction="descendants")
    return templates.TemplateResponse(
        request,
        "lineage.html",
        {"art": art, "graph": _mermaid_graph(art, ancestors, descendants)},
    )


def _mermaid_node(a: store.Artifact) -> str:
    # The template renders the graph with |safe (Mermaid syntax can't survive
    # HTML escaping), so strip angle brackets here — a title containing HTML
    # must not end up injected raw into the page.
    title = a.title.replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")
    return f'{a.row_id}["{title} ({a.type})"]'


def _mermaid_graph(
    art: store.Artifact, ancestors: list[store.Artifact], descendants: list[store.Artifact]
) -> str:
    """Server-generated Mermaid graph text for a small lineage neighborhood
    — rendered client-side by Mermaid.js, no new Python dependency."""
    lines = ["graph LR"]
    seen: set[str] = set()
    for a in [art, *ancestors, *descendants]:
        if a.row_id not in seen:
            lines.append(f"    {_mermaid_node(a)}")
            seen.add(a.row_id)
    for a in ancestors:
        lines.append(f"    {a.row_id} --> {art.row_id}")
    for d in descendants:
        lines.append(f"    {art.row_id} --> {d.row_id}")
    return "\n".join(lines)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8420)
