"""Shared artifact presentation, used by both the MCP server and the
read-only GUI so they can't silently drift into two different views of the
same artifact (see Part 3 design doc, GUI section, "prerequisite refactor").
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from dsos import store


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
        base["content_error"] = art.content_error
    elif isinstance(art.content, pd.DataFrame):
        base["row_count"] = len(art.content)
        base["columns"] = list(art.content.columns)
        base["preview"] = art.content.head(10).to_dict(orient="records")
    elif isinstance(art.content, bytes):
        base["content"] = f"<binary, {len(art.content)} bytes>"
    else:
        base["content"] = art.content
    return base
