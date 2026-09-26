"""Layer 2: the MCP server. Thin FastMCP wrapper over the layer-1 core
library (dsos.store, dsos.execution) — no logic lives here beyond
translating between MCP's JSON-argument world and Python objects, and the
tool-call log middleware.

Run: .venv/Scripts/python.exe -m dsos.mcp_server
Point a real MCP client (Claude Code, Claude Desktop) at this to test with
a real agent, instead of tests/smoke_test.py's direct function calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from dsos import execution, seed, store
from dsos.db import connect

DB_PATH = os.environ.get("DSOS_DB_PATH", "data/store.db")
conn = connect(DB_PATH)

mcp = FastMCP("DS Artifact OS")


class ToolCallLogger(Middleware):
    """Logs every tool call automatically, after it completes — see design
    doc, Philosophy #4 and the `tool_calls` table. The agent never calls a
    logging tool itself; this is the only place that writes to it."""

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        args = context.message.arguments or {}
        payload = result.structured_content or {}
        session_id = args.get("session_id") or payload.get("session_id")
        if session_id:
            store.log_tool_call(
                conn,
                session_id,
                context.message.name,
                args,
                json.dumps(payload, default=str)[:300],
                payload.get("artifact_row_ids", []),
            )
        return result


mcp.add_middleware(ToolCallLogger())


def _artifact_payload(art: store.Artifact) -> dict:
    base = {
        "row_id": art.row_id,
        "artifact_id": art.artifact_id,
        "version": art.version,
        "type": art.type,
        "title": art.title,
        "description": art.description,
        "content_format": art.content_format,
        "source": art.source,
    }
    if isinstance(art.content, pd.DataFrame):
        base["row_count"] = len(art.content)
        base["columns"] = list(art.content.columns)
        base["preview"] = art.content.head(10).to_dict(orient="records")
    elif isinstance(art.content, bytes):
        base["content"] = f"<binary, {len(art.content)} bytes>"
    else:
        base["content"] = art.content
    return base


@mcp.tool()
def start_session(question: str) -> dict:
    """Start a new round of work. Call this once, first, for every new
    top-level question — before searching, fetching, or running anything.
    Reuse the returned session_id in every other tool call for this round."""
    session_id = store.start_session(conn, question)
    return {"session_id": session_id}


@mcp.tool()
def search_artifacts(
    query: str, session_id: str, top_k: int = 5, type: str | None = None
) -> dict:
    """Semantic search over everything saved so far (datasets, queries,
    charts, narratives, skills). Call this before fetching new data or
    rebuilding anything — if a past artifact already covers part of the
    question, reuse it via get_artifact/run_sql/run_python instead of
    redoing the work."""
    hits = store.search_artifacts(conn, query, top_k=top_k, type=type)
    results = [
        {
            "row_id": a.row_id, "artifact_id": a.artifact_id, "version": a.version,
            "type": a.type, "title": a.title, "description": a.description,
            "score": round(score, 3),
        }
        for a, score in hits
    ]
    return {"results": results, "artifact_row_ids": [r["row_id"] for r in results]}


@mcp.tool()
def get_artifact(artifact_id: str, session_id: str, version: int | None = None) -> dict:
    """Fetch an artifact's metadata and content. Tabular artifacts (dataset/
    query/transform) return a preview (first 10 rows) plus row_count, not the
    full table — use run_sql/run_python against the row_id to work with it."""
    art = store.get_artifact(conn, artifact_id, version=version)
    if art is None:
        return {"error": f"no artifact {artifact_id!r}", "artifact_row_ids": []}
    return {**_artifact_payload(art), "artifact_row_ids": [art.row_id]}


@mcp.tool()
def save_artifact(
    type: str,
    title: str,
    description: str,
    content_format: str,
    session_id: str,
    content_text: str | None = None,
    content_path: str | None = None,
    tags: list[str] | None = None,
    source: dict | None = None,
    parent_row_ids: list[str] | None = None,
) -> dict:
    """Register something as a real artifact. A dataset you fetched isn't
    real to this system — invisible to search, lineage, and reuse for every
    future question — until you call this.

    Pass exactly one of:
    - content_text: inline text, for markdown/python/sql/json content
    - content_path: a local file you already produced with your own tools.
      For type="dataset", csv/tsv/json/parquet files are read and normalized
      to parquet automatically — pass content_format="parquet" either way.

    `description` must be a real 1-2 sentences (what this is, why it
    matters) — search_artifacts ranks on it, so a vague description makes
    this artifact unreachable to future questions.
    """
    if content_path:
        try:
            content, content_format = _ingest_path(content_path, type, content_format)
        except Exception as exc:
            return {"error": str(exc), "artifact_row_ids": []}
    elif content_text is not None:
        content = content_text
    else:
        return {"error": "must pass content_text or content_path", "artifact_row_ids": []}

    try:
        row_id = store.save_artifact(
            conn, type=type, title=title, description=description, content=content,
            content_format=content_format, session_id=session_id, tags=tags, source=source,
            parent_row_ids=parent_row_ids,
        )
    except ValueError as exc:
        return {"error": str(exc), "artifact_row_ids": []}
    return {"row_id": row_id, "artifact_row_ids": [row_id]}


def _ingest_path(path: str, type: str, content_format: str) -> tuple:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"no such file: {path}")
    if type != "dataset":
        return p.read_text(), content_format

    ext = p.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(p)
    elif ext == ".tsv":
        df = pd.read_csv(p, sep="\t")
    elif ext == ".json":
        df = pd.read_json(p)
    elif ext == ".parquet":
        df = pd.read_parquet(p)
    else:
        raise ValueError(f"don't know how to ingest {ext!r} as a dataset (csv/tsv/json/parquet)")
    return df, "parquet"


@mcp.tool()
def run_sql(
    code: str, session_id: str, title: str, description: str, input_row_ids: list[str]
) -> dict:
    """Run SQL (DuckDB) against one or more artifacts. Each input row_id is
    available as a table named after that artifact's title (lowercased,
    non-alphanumeric -> _). The result becomes a new `query` artifact,
    automatically lineage-linked to every input and recorded — no separate
    save_artifact call needed."""
    row_id = execution.run_sql(
        conn, code=code, session_id=session_id, title=title, description=description,
        input_row_ids=input_row_ids,
    )
    art = store.get_artifact_by_row_id(conn, row_id, load_content=False)
    return {
        "row_id": row_id, "status": art.status,
        "artifact_row_ids": [row_id, *input_row_ids],
    }


@mcp.tool()
def run_python(
    code: str, session_id: str, title: str, description: str, input_row_ids: list[str],
    output_type: str = "transform",
) -> dict:
    """Run Python against one or more artifacts. Each input row_id is bound
    to a variable named after that artifact's title; `pd` (pandas) is
    available. Your code MUST assign a `result` variable — a DataFrame, a
    dict/list (saved as JSON), raw png bytes (a chart), or a string. That
    becomes a new artifact (default type="transform"; pass output_type="chart"
    for a plot), lineage-linked to every input and recorded automatically."""
    row_id = execution.run_python(
        conn, code=code, session_id=session_id, title=title, description=description,
        input_row_ids=input_row_ids, output_type=output_type,
    )
    art = store.get_artifact_by_row_id(conn, row_id, load_content=False)
    return {
        "row_id": row_id, "status": art.status,
        "artifact_row_ids": [row_id, *input_row_ids],
    }


@mcp.tool()
def get_lineage(row_id: str, session_id: str, direction: str = "ancestors") -> dict:
    """See what an artifact was built from (direction="ancestors", the
    default) or what has been built from it (direction="descendants")."""
    arts = store.get_lineage(conn, row_id, direction=direction)
    results = [{"row_id": a.row_id, "type": a.type, "title": a.title} for a in arts]
    return {"results": results, "artifact_row_ids": [row_id, *(a.row_id for a in arts)]}


def _ensure_seeded() -> None:
    """The discovery skill must exist before any agent connects."""
    if store.get_artifact(conn, seed.DISCOVERY_SKILL_ID, load_content=False) is None:
        bootstrap = store.start_session(conn, "bootstrap")
        seed.seed(conn, session_id=bootstrap)


_ensure_seeded()

if __name__ == "__main__":
    mcp.run()
