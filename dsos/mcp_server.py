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
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path

import pandas as pd
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from dsos import execution, present, publish, seed, store, templating, update_check
from dsos.db import connect

DB_PATH = os.environ.get("DSOS_DB_PATH", "data/store.db")
conn = connect(DB_PATH)

# The interpreter run_python targets when a call omits python_path — the
# user's own analysis Python (pandas/matplotlib/... already installed
# there), not this server's. Unset -> this server's own interpreter, so an
# existing/test install with no DSOS_PYTHON_PATH keeps working unchanged.
DEFAULT_PYTHON_PATH = os.environ.get("DSOS_PYTHON_PATH", sys.executable)

# Surfaced to the client at connect time via the MCP `initialize` response
# (protocol-level InitializeResult.instructions, not a tool docstring) — this
# is what should make the agent reach for this server unprompted, in any
# repo, with no CLAUDE.md/AGENTS.md to copy in. Keep it short: it's paid on
# every connection. Whether a given client actually injects it into the
# model's context (vs. just displaying it) is client-dependent — verify
# with a live session before trusting it for a demo.
#
# _UPDATE_NOTICE (if any) goes first, so it isn't lost if a client truncates
# a long instructions string — only matters to someone on an installed
# release (pip install git+...@vX.Y.Z); silently absent for a dev checkout.
_UPDATE_NOTICE = update_check.check_for_update()

INSTRUCTIONS = (f"{_UPDATE_NOTICE}\n\n" if _UPDATE_NOTICE else "") + """\
Use this server for any question that needs real data: trends, comparisons, \
correlations, "what predicts X", "how many/which", etc. Do not answer such \
questions from general or prior knowledge — every answer must be backed by \
a number this server actually computed.

Workflow, in order:
1. start_session(question) — first call, for every new question.
2. search_artifacts — check for reusable prior work before fetching anything new.
3. list_skills — common workflow skills (exploratory analysis, charting, \
statistics, modeling, reporting). Read the relevant one with get_artifact and \
follow it, so analysis stays consistent across sessions; customize or add \
your own with save_skill.
4. If nothing reusable: find and download real public data with your own \
tools, then save_artifact to register it (type="dataset", with source). An \
unregistered dataset is invisible to search, lineage, and every future question.
5. run_sql / run_python against the registered artifacts to compute the \
actual answer — never eyeball or summarize the raw data yourself. Their \
result is returned inline in the same response (a preview, row_count, \
columns) — do not call get_artifact right after just to see what you \
produced; it's already there. Charts: output_type="chart" — styled with \
the dark house style by default; style= picks another (list_templates).
6. Answer using the computed output, citing the row_ids you used.
7. If asked for a shareable writeup: save_artifact(type="narrative", ...) \
with {{artifact:row_id}} embeds for what it discusses, then publish_report \
to render it to one self-contained local .html file — template= picks the \
layout (dark house style by default; list_templates for options).

Every tool that returns an artifact — save_artifact, run_sql, run_python, \
search_artifacts — gives you a row_id. That row_id is the only id you need \
for get_artifact/run_sql/run_python's input_row_ids; there is no separate \
"artifact_id" to track.
"""

# Reported to the client in the MCP initialize handshake's serverInfo
# (protocol-standard field; clients read it via getServerVersion()).
try:
    _VERSION = _installed_version("dsos")
except PackageNotFoundError:
    _VERSION = "0.0.0"  # dev/local fallback

mcp = FastMCP("DS Artifact OS", instructions=INSTRUCTIONS, version=_VERSION)


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
    """Search over everything saved so far, across every past session, not
    just this one — an exact term (a title, a column name) reliably matches
    even with the fallback embedding model; a vaguer query still finds
    something via semantic similarity. Call this before fetching new data
    or rebuilding anything — if a past artifact already covers part of the
    question (this session's or an earlier one's), reuse it via
    get_artifact/run_sql/run_python instead of redoing the work."""
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
    payload = {**_artifact_payload(art), "artifact_row_ids": [art.row_id]}
    return _with_inline_image(payload, art)


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

    `source`: freeform, but for a fetched dataset prefer {"url": ...,
    "fetched_at": ... (ISO-ish timestamp/description of when), "method": ...
    (how it was fetched, e.g. "WebFetch"/"curl"/"Kaggle API"), "refresh_after":
    ... (how long this stays fresh, e.g. "7d"/"30d"/"static" — your call, not
    enforced)} plus whatever else identifies it (e.g. "survey": "Stack
    Overflow 2024") — this is the only provenance a later session/report has
    to go on, and the only record of how/when this was retrieved: dsos can't
    see a fetch you ran with your own tools, only what you put here. Nothing
    computes staleness automatically — before reusing a dataset, check
    `fetched_at`/`refresh_after` yourself and decide if it's worth refetching.

    The response's `table_name` is this artifact's title, normalized
    exactly the way run_sql/run_python will register it (lowercased,
    non-alphanumeric -> `_`) — use it directly as the table/variable name
    in your next run_sql/run_python call instead of guessing or waiting
    for a "table does not exist" error.
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
    return {
        "row_id": row_id, "table_name": store.safe_table_name(title),
        "artifact_row_ids": [row_id],
    }


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


@mcp.tool()
def list_skills(session_id: str) -> dict:
    """The skill library: common workflow skills — dataset discovery,
    exploratory analysis, charting, statistics, modeling, reporting — plus
    any skills saved in past sessions. Read one with get_artifact(row_id)
    and follow the relevant one so analysis stays consistent across
    sessions; search_artifacts finds them by content too. Customize or add
    your own with save_skill.
    """
    arts = store.list_artifacts(conn, type="skill")
    skills = [
        {
            "artifact_id": a.artifact_id, "row_id": a.row_id, "version": a.version,
            "title": a.title, "description": a.description, "tags": a.tags,
            "seeded": a.artifact_id in seed.SKILL_IDS,
        }
        for a in arts
    ]
    return {"skills": skills, "artifact_row_ids": [a.row_id for a in arts]}


@mcp.tool()
def save_skill(
    session_id: str, title: str, description: str, content: str,
    artifact_id: str | None = None, tags: list[str] | None = None,
) -> dict:
    """Create a new skill or a new version of an existing one — how this
    system learns the workflows that worked. An edited skill keeps its
    artifact_id, so references keep resolving, old versions stay readable
    (get_artifact by row_id), and re-seeding never overwrites your edit.

    - New skill: omit artifact_id (one is derived from the title).
    - Edit an existing one: pass its artifact_id from list_skills — the
      edit becomes the new version.

    `content` is markdown instructions, same shape as the seeded skills.
    `description` is what search ranks on — one real sentence, written for
    the next session that needs it.
    """
    try:
        if artifact_id is None:
            artifact_id = "skill-" + store.safe_table_name(title)
        final_tags = ["skill"] + [t for t in (tags or []) if t != "skill"]
        row_id = store.save_artifact(
            conn, artifact_id=artifact_id, type="skill", title=title,
            description=description, content=content, content_format="markdown",
            tags=final_tags, session_id=session_id,
        )
    except ValueError as exc:
        return {"error": str(exc), "artifact_row_ids": []}
    art = store.get_artifact_by_row_id(conn, row_id, load_content=False)
    return {
        "row_id": row_id, "artifact_id": artifact_id, "version": art.version,
        "artifact_row_ids": [row_id],
    }


def _execution_result_payload(row_id: str, input_row_ids: list[str]) -> dict | object:
    """Shared by run_sql/run_python: always inline the result (or, on
    failure, the diagnostics) — never make the agent make a second call
    just to see what its own run produced."""
    art = store.get_artifact_by_row_id(conn, row_id, load_content=True)
    info = store.get_execution(conn, row_id) or {}
    payload = {
        **_artifact_payload(art),
        "status": art.status,
        # This run's own output, normalized the same way as any input table
        # (feedback #1) — chain it straight into your next run_sql/
        # run_python call instead of re-deriving it from the title.
        "table_name": store.safe_table_name(art.title),
        "input_tables": {
            rid: store.safe_table_name(in_art.title)
            for rid in input_row_ids
            if (in_art := store.get_artifact_by_row_id(conn, rid, load_content=False))
        },
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
    return _with_inline_image(payload, art)


def _with_inline_image(payload: dict, art: store.Artifact):
    """A chart artifact's png bytes rendered as an actual inline image
    block in the tool response — not just a "Figure(1400x800)"-style text
    placeholder the agent has to trust blindly (feedback #2). structured_content
    stays the same payload dict either way, so callers/logging/tests don't
    need to branch on which shape came back."""
    if art.content_format == "png" and isinstance(art.content, bytes):
        from fastmcp.tools import ToolResult
        from fastmcp.utilities.types import Image

        return ToolResult(content=Image(data=art.content, format="png"), structured_content=payload)
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
    in the session's tool-call trace. The response's scratch_id lets you
    promote it later via promote_scratch(scratch_id=...) without
    re-running the query, if it turns out you do want to keep it after all.

    On failure, the error includes the schema (columns) of every registered
    input table, so a column/table typo is fixable from the error alone.

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
    requirements: list[str] | None = None, code_paths: list[str] | None = None,
    style: str | None = "dsos", python_path: str | None = None,
) -> dict:
    """Run Python against one or more artifacts. Each input row_id is bound
    to a variable named after that artifact's title (see `input_tables` in
    the response for the exact name used); `pd` (pandas) is available. Your
    code MUST assign a `result` variable — a DataFrame, a dict/list (saved
    as JSON), a string, or for output_type="chart" a matplotlib Figure/Axes
    (e.g. whatever `plt.gcf()`/`plt.subplots()` gives you — it's rendered to
    PNG for you) or raw png bytes directly. On a persisted (non-scratch)
    call the result is returned inline below as an actual rendered image
    for a chart (not just a text placeholder) — you do NOT need a second
    call to see it. It's also saved as a new artifact (default
    type="transform"; pass output_type="chart" for a plot), lineage-linked
    to every input and recorded automatically. On failure, status="error"
    and `error`/`stdout`/`stderr` below show the traceback and anything
    printed before it failed.

    scratch=True: run and return the result inline but persist nothing —
    no artifact, no lineage, not searchable (a chart still comes back as a
    text placeholder in scratch mode, not a rendered image — drop scratch
    once you're iterating on plot styling, to see it rendered). For rapid
    iteration (check a correlation, test an idea) where an artifact would
    be noise. The run still appears in the session's tool-call trace. The
    response's scratch_id lets you promote it later via
    promote_scratch(scratch_id=...) without re-running the code, once the
    idea works and it's worth keeping.

    Your code runs against python_path (below) — usually your own analysis
    Python, so whatever's already installed there just works. requirements=
    ["scikit-learn>=1.3", ...] fills gaps in that interpreter for THIS run
    only: uv resolves them into a throwaway environment layered on top of
    it, runs the code there, and discards it; python_path itself is never
    modified. Anything uv accepts works: PyPI specs, local package paths,
    wheels, git URLs (path-style entries need a filesystem shared with this
    server). First run pays the download, repeat runs ~1s (globally
    cached). Slower than the default path — only pass requirements when
    you actually need them.

    code_paths=["/abs/dir", ...]: directories holding your own unpackaged
    .py modules to import from — works with or without requirements.

    style: the chart style (the consistency layer) applied to rcParams
    BEFORE your code runs — "dsos" (the dark house style, the default),
    "report" (the same look sized for charts embedded in published reports
    — use it for charts a narrative will embed), "minimal" (bare light
    style), a custom chart-style template's row_id/artifact_id, or None for
    raw matplotlib defaults. Style never leaks between runs. See what
    exists with list_templates(kind="chart-style"); make your own with
    save_template.

    python_path: overrides the server default for THIS call only — e.g.
    point it at a specific repo's .venv interpreter to run against that
    project's exact dependencies, instead of whatever DSOS_PYTHON_PATH (or
    this server's own interpreter, if that's unset) has installed.

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
        requirements=requirements, code_paths=code_paths, style=style,
        python_path=python_path or DEFAULT_PYTHON_PATH,
    )
    return result if scratch else _execution_result_payload(result, input_row_ids)


@mcp.tool()
def promote_scratch(scratch_id: str, session_id: str, title: str, description: str) -> dict:
    """Persist a previous scratch=True run_sql/run_python call (by the
    scratch_id its response returned) as a real artifact — without
    re-running the code. The "spike, then keep it" shortcut: iterate freely
    with scratch=True, then promote the one that worked instead of copying
    its code into a fresh scratch=False call.

    `title`/`description` name the artifact now that it's staying — they
    don't have to match whatever the scratch call was originally titled.

    The cache this reads from is in-memory and per server process: it does
    not survive a restart, and only holds a bounded number of recent
    scratch runs. If scratch_id isn't found (aged out, wrong id, or already
    promoted), you'll get an error telling you to re-run with scratch=False.

    Example: promote_scratch(
        scratch_id="<scratch_id from a run_sql/run_python response>",
        session_id="s1", title="High scorers",
        description="Teams scoring above 10.",
    )
    """
    try:
        row_id = execution.promote_scratch(
            conn, scratch_id=scratch_id, session_id=session_id,
            title=title, description=description,
        )
    except ValueError as exc:
        return {"error": str(exc), "artifact_row_ids": []}
    input_row_ids = [a.row_id for a in store.get_lineage(conn, row_id, direction="ancestors")]
    return _execution_result_payload(row_id, input_row_ids)


@mcp.tool()
def get_lineage(row_id: str, session_id: str, direction: str = "ancestors") -> dict:
    """See what an artifact was built from (direction="ancestors", the
    default) or what has been built from it (direction="descendants")."""
    arts = store.get_lineage(conn, row_id, direction=direction)
    results = [{"row_id": a.row_id, "type": a.type, "title": a.title} for a in arts]
    return {"results": results, "artifact_row_ids": [row_id, *(a.row_id for a in arts)]}


@mcp.tool()
def list_templates(session_id: str, kind: str | None = None) -> dict:
    """Chart styles and report templates — the consistency layer, built-ins
    and custom. Chart styles are run_python's style= (default "dsos", the
    dark house style); report templates are publish_report's template=
    (default "report", the dark house layout). Custom templates made with
    save_template are referenced by row_id or artifact_id — the id is
    stable across version edits. kind="chart-style" or "report" to list
    one kind.
    """
    if kind is not None and kind not in templating.TEMPLATE_KINDS:
        return {
            "error": f"kind must be one of {sorted(templating.TEMPLATE_KINDS)}, got {kind!r}",
            "artifact_row_ids": [],
        }
    customs = store.list_artifacts(conn, type="template")

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


@mcp.tool()
def save_template(
    session_id: str, kind: str, content: str | None = None,
    artifact_id: str | None = None, title: str | None = None,
    description: str | None = None, tags: list[str] | None = None,
    base: str | None = None,
) -> dict:
    """Create or re-version a template — the customizable half of the
    consistency layer.

    kind: "chart-style" (matplotlib rcParams text, .mplstyle syntax — used
    via run_python's style=) or "report" (HTML with {{title}}/{{body}}/
    {{published_at}}/{{session_question}} tokens, where {{body}} is where
    the rendered narrative goes — used via publish_report's template=).

    base: an existing template (built-in name or row_id/artifact_id) to
    copy from — customize instead of rewriting from scratch. If you pass
    no content, the base's text becomes your starting point; the base is
    recorded in the new template's source either way.

    content: the template text (required unless base provides it).
    Validated at SAVE time, not first use — an unparsable .mplstyle or a
    report template without {{body}} fails here in one call.

    artifact_id: the stable id to re-version later — an edit becomes a new
    version with the same id, so references keep resolving. Omit to derive
    one from the title. title/description are what search ranks on — give
    them real ones.
    """
    try:
        if kind not in templating.TEMPLATE_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(templating.TEMPLATE_KINDS)}, got {kind!r}"
            )
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
            artifact_id = f"{kind}-" + store.safe_table_name(title or "custom")
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
        row_id = store.save_artifact(
            conn, artifact_id=artifact_id, type="template", title=title,
            description=description, content=content,
            content_format=(
                "mplstyle" if kind == templating.CHART_STYLE_KIND else "html"
            ),
            tags=final_tags, session_id=session_id,
            source={"base": base} if base else None,
        )
    except (ValueError, OSError) as exc:
        return {"error": str(exc), "artifact_row_ids": []}
    art = store.get_artifact_by_row_id(conn, row_id, load_content=False)
    return {
        "row_id": row_id, "artifact_id": artifact_id, "version": art.version,
        "kind": kind, "artifact_row_ids": [row_id],
    }


@mcp.tool()
def publish_report(
    row_id: str, session_id: str, dry_run: bool = False, template: str = "report",
) -> dict:
    """Render a narrative artifact and everything its {{artifact:...}}
    embeds reference — datasets/queries/transforms as HTML tables, charts as
    inlined images — into one self-contained local .html file. This is the
    seam where a finished report leaves this working layer (see the
    discovery skill and save_artifact for how things get INTO it).

    row_id must be a narrative artifact (save_artifact it first if you
    haven't). This does not create a new artifact row itself — a rendered
    export isn't a versioned analysis artifact.

    dry_run=True: resolve every {{artifact:...}} embed and report on it
    (row_id, type, title, and whether it actually resolved) without writing
    the HTML file — check that nothing is missing or stale before spending
    a real publish. `broken_row_ids` lists any embed that didn't resolve
    (a typo, or a row_id from a different session/store); fix those in the
    narrative's content (save_artifact a new version) before publishing
    for real.

    template: the HTML layout (the consistency layer) — "report" (the dark
    house layout, the default), "default" (the original plain light look),
    "minimal" (bare HTML), or a custom report-template artifact's
    row_id/artifact_id (make one with save_template(kind="report")).

    Example: publish_report(row_id="<narrative row_id>", session_id="s1")
    """
    try:
        if dry_run:
            result = publish.preview_report(conn, row_id)
        else:
            result = publish.publish_report(conn, row_id, template=template)
    except ValueError as exc:
        return {"error": str(exc), "artifact_row_ids": []}
    return result


def _ensure_seeded() -> None:
    """The skill library must exist before any agent connects — idempotent:
    a skill that's been edited via save_skill (a new version, same
    artifact_id) is never re-seeded over, so customization survives
    restarts and re-inits."""
    if all(store.get_artifact(conn, sid, load_content=False) for sid in seed.SKILL_IDS):
        return
    bootstrap = store.start_session(conn, "bootstrap: seed skill library")
    seed.seed_library(conn, session_id=bootstrap)


_ensure_seeded()

if __name__ == "__main__":
    mcp.run()
