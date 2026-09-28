"""Layer 3 (stretch): the read-only GUI. Browse-and-understand only: no
delete/retag/edit (see Part 3 design doc, "GUI scope").

The routes are a factory, `create_gui_router(db)`, closing over the
`Database` they are given rather than reaching for a module global. That is
not tidiness. `conn = connect(DB_PATH)` at module scope meant importing this
module opened a store, so a process that wanted the GUI *and* the MCP servers
— the daemon (WP-D1) — ended up with two connections and two migration runs
against the same file, which is precisely the arrangement decision D8
exists to eliminate. A route body that still said `conn` would quietly
reopen exactly that arrangement at runtime; there is no such name left in
this module, and every route gets its connection from the injected handle.

`app` is therefore built lazily, through the module `__getattr__` below, the
same way `dsos.mcp_server` exposes `mcp`: the standalone path (`python -m
dsos.gui`, and the TestClient the GUI smoke test drives) still works, and
nothing opens a store until something actually asks for the app. The daemon
never asks — it mounts the router, over its own Database.

Run: .venv/Scripts/python.exe -m dsos.gui
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dsos import present, store
from dsos.db import Database
from dsos.publish import render_report_html

DB_PATH = os.environ.get("DSOS_DB_PATH", "data/store.db")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# The port the standalone GUI has always used. The daemon does NOT use it: it
# serves the same routes on its own port, and a reader who has the standalone
# GUI open keeps working because this default did not move.
STANDALONE_PORT = 8420

# "template": chart styles / report layouts — artifacts like anything else
# (see dsos/templating.py). Authoring stays agent-only for now; the GUI
# just doesn't hide them from the gallery.
ARTIFACT_TYPES = ["dataset", "query", "transform", "chart", "narrative", "skill", "template"]


def create_gui_router(db: Database) -> APIRouter:
    """The GUI's routes, over one `Database`.

    Every handler is a closure over `db` and asks it for a connection per
    request. FastAPI runs these sync handlers in a thread pool, and
    `Database.conn()` is thread-local, so each pool thread gets its own
    connection to the one store the process owns — the same handle the MCP
    tools write through, and the same process-wide write lock.
    """
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def sessions_list(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {"sessions": store.list_sessions(db.conn())}
        )

    @router.get("/sessions/{session_id}", response_class=HTMLResponse)
    def session_detail(request: Request, session_id: str):
        session = store.get_session(db.conn(), session_id)
        if session is None:
            return HTMLResponse(f"<p>no session {session_id!r}</p>", status_code=404)
        artifacts = store.list_artifacts(db.conn(), session_id=session_id)
        return templates.TemplateResponse(
            request, "session_detail.html",
            {"session": session, "artifacts": artifacts,
             "failed_runs": store.failed_executions(db.conn(), session_id)},
        )

    @router.get("/sessions/{session_id}/tool_calls", response_class=HTMLResponse)
    def session_tool_calls(request: Request, session_id: str):
        """htmx-polled partial — the live tool-call feed, no websockets needed."""
        calls = store.list_tool_calls(db.conn(), session_id)
        return templates.TemplateResponse(request, "_tool_calls_feed.html", {"calls": calls})

    @router.get("/artifacts", response_class=HTMLResponse)
    def artifacts_gallery(
        request: Request,
        type: str | None = None,
        session_id: str | None = None,
        q: str | None = None,
    ):
        """`q` calls search_artifacts directly, so gallery search and agent
        search share one index (see design doc, GUI routes)."""
        if q:
            hits = store.search_artifacts(db.conn(), q, top_k=50, type=type)
            artifacts = [a for a, _score in hits]
            if session_id:
                artifacts = [a for a in artifacts if a.session_id == session_id]
        else:
            artifacts = store.list_artifacts(db.conn(), type=type, session_id=session_id)
        return templates.TemplateResponse(
            request,
            "artifacts_gallery.html",
            {
                "artifacts": artifacts, "types": ARTIFACT_TYPES,
                "type": type or "", "session_id": session_id or "", "q": q or "",
            },
        )

    @router.get("/artifacts/{row_id}", response_class=HTMLResponse)
    def artifact_detail(request: Request, row_id: str):
        conn = db.conn()
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
                "execution": store.get_execution(conn, output_row_id=row_id),
            },
        )

    @router.get("/artifacts/{row_id}/report", response_class=HTMLResponse)
    def artifact_report(request: Request, row_id: str, template: str = "report"):
        """Serve a narrative's published report straight from this process —
        the "serve it" alternative to opening publish_report's local .html file
        (design-doc's publish target stays local-first; this just changes how
        you *view* it). Rendered fresh on every request, nothing written to
        disk — a stale view isn't possible."""
        conn = db.conn()
        if store.get_artifact_by_row_id(conn, row_id, load_content=False) is None:
            return HTMLResponse(f"<p>no artifact with row_id {row_id!r}</p>", status_code=404)
        try:
            page, _art, _embeds = render_report_html(conn, row_id, template=template)
        except ValueError as exc:
            return HTMLResponse(f"<p>{exc}</p>", status_code=400)
        return HTMLResponse(page)

    @router.get("/artifacts/{row_id}/lineage", response_class=HTMLResponse)
    def artifact_lineage(request: Request, row_id: str):
        conn = db.conn()
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

    return router


def build_app(db: Database) -> FastAPI:
    """A FastAPI app serving just the GUI — the standalone entry point, and
    the shape the daemon mounts the same router into."""
    app = FastAPI(title="DS Artifact OS — GUI (read-only)")
    app.include_router(create_gui_router(db))
    return app


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


_app = None


def __getattr__(name: str):
    """Build the standalone GUI app on first access (PEP 562).

    `dsos.gui.app` was module-level state before the daemon existed, and three
    tests plus anyone importing this module reach for it that way. Exposing
    it through `__getattr__` keeps that working without reintroducing the
    module-scope `connect()` it used to sit next to: the store is opened when
    the app is asked for, not when the module is imported, and it is opened
    with a `Database` so the standalone process is as correct about one
    connection per thread as the daemon is.
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _app
    if _app is None:
        _app = build_app(Database(DB_PATH))
    return _app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(Database(DB_PATH)), host="127.0.0.1", port=STANDALONE_PORT)
