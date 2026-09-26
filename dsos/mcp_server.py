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

from dsos import execution, present, publish, seed, store
from dsos.db import connect

DB_PATH = os.environ.get("DSOS_DB_PATH", "data/store.db")
conn = connect(DB_PATH)

# Surfaced to the client at connect time via the MCP `initialize` response
# (protocol-level InitializeResult.instructions, not a tool docstring) — this
# is what should make the agent reach for this server unprompted, in any
# repo, with no CLAUDE.md/AGENTS.md to copy in. Keep it short: it's paid on
# every connection. Whether a given client actually injects it into the
# model's context (vs. just displaying it) is client-dependent — verify
# with a live session before trusting it for a demo.
INSTRUCTIONS = """\
Use this server for any question that needs real data: trends, comparisons, \
correlations, "what predicts X", "how many/which", etc. Do not answer such \
questions from general or prior knowledge — every answer must be backed by \
a number this server actually computed.

Workflow, in order:
1. start_session(question) — first call, for every new question.
2. search_artifacts — check for reusable prior work before fetching anything new.
3. If nothing reusable: find and download real public data with your own \
tools, then save_artifact to register it (type="dataset", with source). An \
unregistered dataset is invisible to search, lineage, and every future question.
4. run_sql / run_python against the registered artifacts to compute the \
actual answer — never eyeball or summarize the raw data yourself. Their \
result is returned inline in the same response (a preview, row_count, \
columns) — do not call get_artifact right after just to see what you \
produced; it's already there.
5. Answer using the computed output, citing the row_ids you used.
6. If asked for a shareable writeup: save_artifact(type="narrative", ...) \
with {{artifact:row_id}} embeds for what it discusses, then publish_report \
to render it to one self-contained local .html file.

Every tool that returns an artifact — save_artifact, run_sql, run_python, \
search_artifacts — gives you a row_id. That row_id is the only id you need \
for get_artifact/run_sql/run_python's input_row_ids; there is no separate \
"artifact_id" to track.
"""

mcp = FastMCP("DS Artifact OS", instructions=INSTRUCTIONS)


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
    return present.artifact_payload(conn, art)


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
    """Search over everything saved so far (datasets, queries, charts,
    narratives, skills) — an exact term (a title, a column name) reliably
    matches even with the fallback embedding model; a vaguer query still
    finds something via semantic similarity. Call this before fetching new
    data or rebuilding anything — if a past artifact already covers part of
    the question, reuse it via get_artifact/run_sql/run_python instead of
    redoing the work."""
    hits = store.search_artifacts(conn, query, top_k=top_k, type=type)
    results = [
        {
            "row_id": a.row_id, "type": a.type, "title": a.title,
            "description": a.description, "score": round(score, 3),
        }
        for a, score in hits
    ]
    return {"results": results, "artifact_row_ids": [r["row_id"] for r in results]}


@mcp.tool()
def get_artifact(row_id: str, session_id: str) -> dict:
    """Fetch an artifact's full metadata and content, by the row_id that
    save_artifact/run_sql/run_python/search_artifacts gave you — that row_id
    is the only id you need; there's no separate "artifact_id" to look up.
    Tabular artifacts (dataset/query/transform) return row_count, columns,
    and a 10-row preview, not the full table — use run_sql/run_python
    against this row_id to compute over the full data. `uses` lists what
    this artifact was built from or embeds (e.g. a narrative's datasets).

    Note: run_sql/run_python already return this same preview inline in
    their own response — call get_artifact only to re-fetch something from
    an earlier tool call (e.g. a search_artifacts hit), not right after
    running it yourself.

    Example: get_artifact(row_id="a1b2c3...", session_id="s1")
    """
    art = store.get_artifact_by_row_id(conn, row_id)
    if art is None:
        return {"error": f"no artifact with row_id {row_id!r}", "artifact_row_ids": []}
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
    - content_text: inline text, for markdown/python/sql/json content.
      `content_format` must describe it (markdown, python, sql, json, ...).
    - content_path: a local file you already produced with your own tools.
      For type="dataset" the format is read from the file extension —
      .csv/.tsv/.json/.parquet are all accepted and normalized to parquet
      internally; whatever you pass as content_format is ignored. For other
      types the file is read as text and content_format describes it.

    `description` must be a real 1-2 sentences (what this is, why it
    matters) — search_artifacts ranks on it, so a vague description makes
    this artifact unreachable to future questions.

    For a narrative: write `{{artifact:<row_id>}}` in content_text for each
    dataset/chart/query it discusses. Those row_ids are automatically added
    to this artifact's lineage — no need to also pass parent_row_ids for
    them — and show up as `uses` when this narrative is fetched later.
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
        # encoding explicit: read_text defaults to the locale codec (e.g.
        # GBK on Chinese-locale Windows), which chokes on Unicode content
        return p.read_text(encoding="utf-8"), content_format

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


def _execution_result_payload(row_id: str, input_row_ids: list[str]) -> dict:
    """Shared by run_sql/run_python: always inline the result (or, on
    failure, the diagnostics) — never make the agent make a second call
    just to see what its own run produced."""
    art = store.get_artifact_by_row_id(conn, row_id, load_content=True)
    info = store.get_execution(conn, row_id) or {}
    payload = {
        **_artifact_payload(art),
        "status": art.status,
        "artifact_row_ids": [row_id, *input_row_ids],
    }
    # stdout is top-level on success AND failure (feedback #10): it's where
    # progress prints and anything printed before a crash live, and it used
    # to be invisible on success and buried on failure.
    payload["stdout"] = (info.get("stdout") or "")[:2_000]
    if art.status != "ok":
        payload["error"] = info.get("error")
        # stderr's tail carries the traceback's final (most useful) frames.
        payload["stderr"] = (info.get("stderr") or "")[-4_000:]
    return payload


@mcp.tool()
def run_sql(
    code: str, session_id: str, title: str, description: str, input_row_ids: list[str],
    scratch: bool = False,
) -> dict:
    """Run SQL (DuckDB) against one or more artifacts. Each input row_id is
    available as a table named after that artifact's title (lowercased,
    non-alphanumeric -> _). The result is returned inline below (row_count,
    columns, a 10-row preview) — you do NOT need a second call to see it.
    It's also saved as a new `query` artifact, automatically lineage-linked
    to every input and recorded — no separate save_artifact call needed.
    On failure, status="error" and `error`/`stdout`/`stderr` below show why.

    scratch=True: run and return the result inline but persist nothing —
    no artifact, no lineage, not searchable. For quick checks (row counts,
    schema pokes) where an artifact would be noise. The run still appears
    in the session's tool-call trace. Do NOT use scratch for anything you
    or a later session might want to build on — then it never happened.

    Example:
      run_sql(
        code="SELECT team, score FROM toy_scores WHERE score > 10",
        session_id="s1", title="High scorers",
        description="Teams scoring above 10.",
        input_row_ids=["<row_id of the toy_scores dataset artifact>"],
      )
    """
    result = execution.run_sql(
        conn, code=code, session_id=session_id, title=title, description=description,
        input_row_ids=input_row_ids, scratch=scratch,
    )
    return result if scratch else _execution_result_payload(result, input_row_ids)


@mcp.tool()
def run_python(
    code: str, session_id: str, title: str, description: str, input_row_ids: list[str],
    output_type: str = "transform", scratch: bool = False,
) -> dict:
    """Run Python against one or more artifacts. Each input row_id is bound
    to a variable named after that artifact's title; `pd` (pandas) is
    available. Your code MUST assign a `result` variable — a DataFrame, a
    dict/list (saved as JSON), raw png bytes (a chart), or a string. The
    result is returned inline below — you do NOT need a second call to see
    it. It's also saved as a new artifact (default type="transform"; pass
    output_type="chart" for a plot), lineage-linked to every input and
    recorded automatically. On failure, status="error" and `error`/`stdout`/
    `stderr` below show the traceback and anything printed before it failed.

    scratch=True: run and return the result inline but persist nothing —
    no artifact, no lineage, not searchable. For rapid iteration (check a
    correlation, test an idea, debug a plot) where an artifact would be
    noise; flip to scratch=False once the idea works. The run still appears
    in the session's tool-call trace.

    Example:
      run_python(
        code="result = toy_scores.groupby('team')['score'].mean().reset_index()",
        session_id="s1", title="Average score by team",
        description="Mean score per team.",
        input_row_ids=["<row_id of the toy_scores dataset artifact>"],
      )
    """
    result = execution.run_python(
        conn, code=code, session_id=session_id, title=title, description=description,
        input_row_ids=input_row_ids, output_type=output_type, scratch=scratch,
    )
    return result if scratch else _execution_result_payload(result, input_row_ids)


@mcp.tool()
def get_lineage(row_id: str, session_id: str, direction: str = "ancestors") -> dict:
    """See what an artifact was built from (direction="ancestors", the
    default) or what has been built from it (direction="descendants")."""
    arts = store.get_lineage(conn, row_id, direction=direction)
    results = [{"row_id": a.row_id, "type": a.type, "title": a.title} for a in arts]
    return {"results": results, "artifact_row_ids": [row_id, *(a.row_id for a in arts)]}


@mcp.tool()
def publish_report(row_id: str, session_id: str) -> dict:
    """Render a narrative artifact and everything its {{artifact:...}}
    embeds reference — datasets/queries/transforms as HTML tables, charts as
    inlined images — into one self-contained local .html file. This is the
    seam where a finished report leaves this working layer (see the
    discovery skill and save_artifact for how things get INTO it).

    row_id must be a narrative artifact (save_artifact it first if you
    haven't). This does not create a new artifact row itself — a rendered
    export isn't a versioned analysis artifact.

    Example: publish_report(row_id="<narrative row_id>", session_id="s1")
    """
    try:
        result = publish.publish_report(conn, row_id)
    except ValueError as exc:
        return {"error": str(exc), "artifact_row_ids": []}
    return result


def _ensure_seeded() -> None:
    """The discovery skill must exist before any agent connects."""
    if store.get_artifact(conn, seed.DISCOVERY_SKILL_ID, load_content=False) is None:
        bootstrap = store.start_session(conn, "bootstrap")
        seed.seed(conn, session_id=bootstrap)


_ensure_seeded()

if __name__ == "__main__":
    mcp.run()
