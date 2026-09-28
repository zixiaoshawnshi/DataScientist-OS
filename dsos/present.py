"""Shared artifact presentation, used by both the MCP server and the
read-only GUI so they can't silently drift into two different views of the
same artifact (see Part 3 design doc, GUI section, "prerequisite refactor").

Feedback rules here (dsos_mcp_suggestions.md #4, #5): anything the agent
sees must be actionable — a missing blob gets a named, fix-oriented
message instead of a raw path, and oversized content gets truncated with a
note saying what was cut and how to see the rest, instead of either blowing
up the response or silently arriving in full.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pandas as pd

from dsos import store

if TYPE_CHECKING:
    # TYPE_CHECKING only, because execution.py imports this module: a
    # module-level `from dsos import execution` here would be a cycle.
    # The runtime never needs the name — the outcome is only read.
    from dsos.execution import RunOutcome

# Inline text content in a payload is capped: a big JSON/markdown artifact
# returned in full has crashed real clients with cryptic size errors
# (feedback #5, "another row available"). The cap keeps every payload
# bounded while `content_truncated` tells the agent what happened.
_INLINE_TEXT_LIMIT = 4_000

# One giant cell (e.g. a row containing a whole document) would otherwise
# blow the payload up through the back door of the 10-row preview.
_PREVIEW_CELL_LIMIT = 500


def _clip_cell(value):
    if isinstance(value, str) and len(value) > _PREVIEW_CELL_LIMIT:
        return f"{value[:_PREVIEW_CELL_LIMIT]}… ({len(value)} chars, clipped)"
    return value


def _missing_blob_message(art: store.Artifact) -> str:
    return (
        f"The data file for artifact '{art.title}' (row_id: {art.row_id}) is "
        f"missing from storage — {art.content_error}. Metadata, search, and "
        "lineage still work; re-upload the data or recreate the artifact to "
        "restore its content."
    )


def result_payload(result) -> dict:
    """Row/column/preview (or content) fields for any result value — the
    single rendering path shared by saved-artifact payloads and scratch-run
    responses, so they can't drift apart."""
    if isinstance(result, pd.DataFrame):
        records = result.head(10).to_dict(orient="records")
        return {
            "row_count": len(result),
            "columns": list(result.columns),
            "preview": [{k: _clip_cell(v) for k, v in row.items()} for row in records],
        }
    if isinstance(result, bytes):
        return {"content": f"<binary, {len(result)} bytes>"}
    text = (
        result if isinstance(result, str) else json.dumps(result, default=str)
    )
    if len(text) > _INLINE_TEXT_LIMIT:
        return {
            "content": text[:_INLINE_TEXT_LIMIT],
            "content_truncated": (
                f"Inline content clipped to the first {_INLINE_TEXT_LIMIT} of "
                f"{len(text)} characters. The full content is intact in storage "
                "— run_sql/run_python against this artifact to work with it, "
                "or select fewer rows/columns next time."
            ),
        }
    return {"content": result}


def artifact_payload(conn: sqlite3.Connection, art: store.Artifact) -> dict:
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
    uses = store.get_lineage(conn, art.row_id, direction="ancestors")
    if uses:
        # What this artifact was built from / embeds — a narrative's
        # {{artifact:row_id}} embeds surface here automatically, so "what
        # datasets does this narrative use" needs no separate get_lineage call.
        base["uses"] = [{"row_id": a.row_id, "type": a.type, "title": a.title} for a in uses]
    if art.content_error:
        # Named, actionable message (feedback #4): which artifact, which
        # row, what's wrong, and what to do about it — not a bare path.
        base["content_error"] = _missing_blob_message(art)
    else:
        base.update(result_payload(art.content))
    return base


def execution_payload(
    conn: sqlite3.Connection, outcome: "RunOutcome", input_row_ids: list[str],
) -> dict:
    """Shared by run_sql/run_python: always inline the result (or, on
    failure, the diagnostics) — never make the agent make a second call
    just to see what its own run produced.

    The two outcomes get different payloads because they are different
    kinds of thing. A successful run produced an artifact, so its response
    is that artifact's own payload plus the aliases its inputs were bound
    to. A failed run produced nothing — there is no artifact to describe —
    so the response is the execution's diagnostics and an `execution_id`,
    with no `row_id` key at all: a `row_id` that names nothing is worse
    than no key, because the agent will try to use it.

    MCP-free on purpose: a chart's png bytes are wrapped into a real image
    content block by the server, because that is an MCP concept, while the
    payload itself is the same dict the GUI and the tests read.
    """
    # Which alias each input got, by the same positional rule the run
    # itself used (execution.input_aliases) — so the agent can chain this
    # run's output into the next call without re-deriving anything.
    # Position, not title: an input re-titled or duplicated can't change
    # what in_1 means mid-session. Echoed on both paths, so an agent reads
    # the same field either way.
    input_tables = {rid: f"in_{i}" for i, rid in enumerate(input_row_ids, start=1)}
    info = store.get_execution(conn, execution_id=outcome.execution_id) or {}

    if outcome.row_id is None:
        # stdout is top-level on success AND failure (feedback #10): it's
        # where progress prints and anything printed before a crash live,
        # and it used to be invisible on success and buried on failure.
        return {
            "status": "error",
            "execution_id": outcome.execution_id,
            "error": info.get("error"),
            "stdout": (info.get("stdout") or "")[:2_000],
            # stderr's tail carries the traceback's final (most useful) frames.
            "stderr": (info.get("stderr") or "")[-4_000:],
            "input_tables": input_tables,
            "artifact_row_ids": list(input_row_ids),
        }

    art = store.get_artifact_by_row_id(conn, outcome.row_id, load_content=True)
    payload = {
        **artifact_payload(conn, art),
        "status": art.status,
        "input_tables": input_tables,
        "artifact_row_ids": [outcome.row_id, *input_row_ids],
    }
    payload["stdout"] = (info.get("stdout") or "")[:2_000]
    return payload
