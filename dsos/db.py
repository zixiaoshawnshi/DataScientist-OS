"""SQLite schema and connection helper for the DS Artifact OS store.

Layer 1 (core library): no MCP, no HTTP — just the data model from the
design doc (Design/DS Artifact OS — Design Doc.md, "Data model" section).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    started_at TEXT NOT NULL
);

-- One row per *version*, not per logical artifact.
-- (artifact_id, version) is the pin used by lineage and narrative embeds.
CREATE TABLE IF NOT EXISTS artifacts (
    row_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    type TEXT NOT NULL,              -- dataset | query | transform | chart | narrative | skill
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,              -- JSON list
    content_ref TEXT NOT NULL,       -- path under data/blobs/
    content_format TEXT NOT NULL,    -- csv | parquet | python | sql | markdown | json | png
    source TEXT,                     -- JSON: {url, fetched_at} for fetched datasets, else NULL
    embedding BLOB NOT NULL,         -- float32 vector, see dsos/embeddings.py
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL DEFAULT 'ready',
    UNIQUE (artifact_id, version)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_artifact_id ON artifacts(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_session_id ON artifacts(session_id);

-- Keyword/tag fallback alongside embedding search. Plain (non-external-content)
-- FTS5 table, kept in sync manually in store.save_artifact — artifacts.row_id
-- is a TEXT uuid, which external-content FTS5 (rowid-aliased) doesn't support.
CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    row_id UNINDEXED,
    title,
    description,
    tags
);

-- Edges between specific versions (row_ids), not logical artifacts, so
-- {{artifact:id@v2}} embeds and future staleness checks always resolve
-- to a fixed row.
CREATE TABLE IF NOT EXISTS lineage (
    child_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    parent_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    PRIMARY KEY (child_row_id, parent_row_id)
);

-- The transparency layer. output_row_id is the artifact this execution
-- produced; inputs are just that row's parents in `lineage` — no separate
-- input field.
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    output_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    kind TEXT NOT NULL,              -- sql | python
    code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok | error
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    error TEXT,
    output_summary TEXT NOT NULL     -- JSON: row count, shape, etc.
);

-- The free agent trace. artifact_row_ids drives the reuse badge/metric:
-- any row touched whose session_id differs from the current session is
-- a genuine reuse, no manual bookkeeping.
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ts TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    artifact_row_ids TEXT NOT NULL   -- JSON list
);
"""


class _Connection(sqlite3.Connection):
    """Plain sqlite3.Connection has no __dict__ (C extension type), so it
    can't hold a stashed db_path attribute directly — subclassing it is the
    standard way to get one. Used only so store.py can find a connection's
    blob dir without every call site (execution.py, seed.py, ...) having to
    thread db_path through separately."""

    _dsos_db_path: str


def connect(db_path: str | Path = "data/store.db") -> sqlite3.Connection:
    """Open (and if needed, initialize) the store's SQLite database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    conn._dsos_db_path = str(path)
    return conn


def blob_dir_for(conn: sqlite3.Connection) -> Path:
    """Filesystem root for this connection's artifact blob content, sibling
    to its db file. Requires the connection to have come from `connect()`."""
    db_path = getattr(conn, "_dsos_db_path", None)
    if db_path is None:
        raise ValueError("connection was not opened via dsos.db.connect()")
    d = Path(db_path).parent / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return d
