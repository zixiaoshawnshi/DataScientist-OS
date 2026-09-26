"""ArtifactStore: the core, protocol-unaware library.

No MCP, no HTTP here — this is the layer everything else (MCP server,
execution runner, GUI) is built on top of.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from dsos import embeddings
from dsos.db import blob_dir_for

ARTIFACT_TYPES = {"dataset", "query", "transform", "chart", "narrative", "skill"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Artifact:
    row_id: str
    artifact_id: str
    version: int
    type: str
    title: str
    description: str
    tags: list[str]
    content_ref: str
    content_format: str
    source: dict | None
    created_at: str
    session_id: str
    status: str
    content: Any = None  # populated by get_artifact/search_artifacts on demand


def start_session(conn: sqlite3.Connection, question: str) -> str:
    """Marks the reuse boundary for this round. See design doc, Sessions."""
    session_id = _new_id()
    conn.execute(
        "INSERT INTO sessions (id, question, started_at) VALUES (?, ?, ?)",
        (session_id, question, _now()),
    )
    conn.commit()
    return session_id


def save_artifact(
    conn: sqlite3.Connection,
    *,
    type: str,
    title: str,
    description: str,
    content: Any,
    content_format: str,
    session_id: str,
    artifact_id: str | None = None,
    tags: list[str] | None = None,
    source: dict | None = None,
    parent_row_ids: list[str] | None = None,
    status: str = "ready",
) -> str:
    """Save a new artifact, or a new version of an existing one.

    A real 1-2 sentence `description` is required, not a filename — it's
    the only thing search_artifacts has to go on, and reuse quality depends
    on it directly.
    """
    if type not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact type {type!r}, expected one of {ARTIFACT_TYPES}")
    if not description or not description.strip():
        raise ValueError("save_artifact requires a real description, not a filename")

    tags = tags or []
    is_new = artifact_id is None
    artifact_id = artifact_id or _new_id()

    if is_new:
        version = 1
    else:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        version = (row["v"] or 0) + 1

    row_id = _new_id()
    content_ref = _write_blob(conn, artifact_id, version, content, content_format)
    vector = embeddings.embed(f"{title}\n{description}\n{' '.join(tags)}")

    conn.execute(
        """
        INSERT INTO artifacts (
            row_id, artifact_id, version, type, title, description, tags,
            content_ref, content_format, source, embedding,
            created_at, session_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id, artifact_id, version, type, title, description, json.dumps(tags),
            content_ref, content_format, json.dumps(source) if source else None,
            vector.astype(np.float32).tobytes(),
            _now(), session_id, status,
        ),
    )
    conn.execute(
        "INSERT INTO artifacts_fts (row_id, title, description, tags) VALUES (?, ?, ?, ?)",
        (row_id, title, description, " ".join(tags)),
    )
    for parent_row_id in parent_row_ids or []:
        conn.execute(
            "INSERT OR IGNORE INTO lineage (child_row_id, parent_row_id) VALUES (?, ?)",
            (row_id, parent_row_id),
        )
    conn.commit()
    return row_id


def _write_blob(
    conn: sqlite3.Connection, artifact_id: str, version: int, content: Any, content_format: str
) -> str:
    ext = {"parquet": "parquet", "python": "py", "sql": "sql", "markdown": "md",
           "json": "json", "png": "png", "csv": "csv"}.get(content_format, "bin")
    d = blob_dir_for(conn) / artifact_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"v{version}.{ext}"

    if content_format == "parquet":
        content.to_parquet(path)  # content: pandas.DataFrame
    elif content_format == "json":
        path.write_text(json.dumps(content, indent=2))
    elif content_format == "png":
        path.write_bytes(content)  # content: raw png bytes
    else:  # python, sql, markdown, csv, ... — treat as text
        path.write_text(content if isinstance(content, str) else str(content))
    return str(path)


def _load_blob(content_ref: str, content_format: str) -> Any:
    path = Path(content_ref)
    if content_format == "parquet":
        import pandas as pd
        return pd.read_parquet(path)
    if content_format == "json":
        return json.loads(path.read_text())
    if content_format == "png":
        return path.read_bytes()
    return path.read_text()


def _row_to_artifact(row: sqlite3.Row, *, load_content: bool) -> Artifact:
    art = Artifact(
        row_id=row["row_id"], artifact_id=row["artifact_id"], version=row["version"],
        type=row["type"], title=row["title"], description=row["description"],
        tags=json.loads(row["tags"]), content_ref=row["content_ref"],
        content_format=row["content_format"],
        source=json.loads(row["source"]) if row["source"] else None,
        created_at=row["created_at"], session_id=row["session_id"], status=row["status"],
    )
    if load_content:
        art.content = _load_blob(art.content_ref, art.content_format)
    return art


def get_artifact(
    conn: sqlite3.Connection, artifact_id: str, version: int | None = None,
    *, load_content: bool = True,
) -> Artifact | None:
    """Latest version if `version` is omitted, else that exact pinned version."""
    if version is None:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY version DESC LIMIT 1",
            (artifact_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND version = ?",
            (artifact_id, version),
        ).fetchone()
    return _row_to_artifact(row, load_content=load_content) if row else None


def get_artifact_by_row_id(
    conn: sqlite3.Connection, row_id: str, *, load_content: bool = True
) -> Artifact | None:
    row = conn.execute("SELECT * FROM artifacts WHERE row_id = ?", (row_id,)).fetchone()
    return _row_to_artifact(row, load_content=load_content) if row else None


def search_artifacts(
    conn: sqlite3.Connection, query: str, *, top_k: int = 5, type: str | None = None,
) -> list[tuple[Artifact, float]]:
    """Semantic search, ranked by cosine similarity over title+description+tags.

    Brute-force in Python: fine at demo scale (dozens–hundreds of rows), and
    it means no vector DB dependency. Only compares the latest version of
    each logical artifact.
    """
    sql = """
        SELECT a.* FROM artifacts a
        INNER JOIN (
            SELECT artifact_id, MAX(version) AS v FROM artifacts GROUP BY artifact_id
        ) latest ON a.artifact_id = latest.artifact_id AND a.version = latest.v
    """
    params: list[Any] = []
    if type:
        sql += " WHERE a.type = ?"
        params.append(type)
    rows = conn.execute(sql, params).fetchall()

    q_vec = embeddings.embed(query)
    scored = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        score = embeddings.cosine_sim(q_vec, vec)
        scored.append((_row_to_artifact(row, load_content=False), score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]


def get_lineage(
    conn: sqlite3.Connection, row_id: str, *, direction: str = "ancestors",
) -> list[Artifact]:
    """`direction="ancestors"` walks parents (what this was built from);
    `direction="descendants"` walks children (what was built from this)."""
    col_from, col_to = (
        ("child_row_id", "parent_row_id") if direction == "ancestors"
        else ("parent_row_id", "child_row_id")
    )
    seen: set[str] = set()
    frontier = [row_id]
    result = []
    while frontier:
        next_frontier = []
        for rid in frontier:
            for edge in conn.execute(
                f"SELECT {col_to} AS other FROM lineage WHERE {col_from} = ?", (rid,)
            ).fetchall():
                other = edge["other"]
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
                    art = get_artifact_by_row_id(conn, other, load_content=False)
                    if art:
                        result.append(art)
        frontier = next_frontier
    return result


def log_tool_call(
    conn: sqlite3.Connection, session_id: str, tool_name: str, args: dict,
    result_summary: str, artifact_row_ids: list[str],
) -> str:
    """The free agent trace. Called by the MCP server middleware around
    every tool invocation — not something the agent calls itself."""
    call_id = _new_id()
    conn.execute(
        """
        INSERT INTO tool_calls (id, session_id, ts, tool_name, args_json,
                                 result_summary, artifact_row_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (call_id, session_id, _now(), tool_name, json.dumps(args), result_summary,
         json.dumps(artifact_row_ids)),
    )
    conn.commit()
    return call_id


def reused_artifact_row_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Row ids touched by this session's tool calls that were created in a
    *different* (earlier) session — i.e. genuine reuse, not fresh work."""
    rows = conn.execute(
        "SELECT artifact_row_ids FROM tool_calls WHERE session_id = ?", (session_id,)
    ).fetchall()
    touched: set[str] = set()
    for row in rows:
        touched.update(json.loads(row["artifact_row_ids"]))
    if not touched:
        return []
    placeholders = ",".join("?" * len(touched))
    reused = conn.execute(
        f"""
        SELECT row_id FROM artifacts
        WHERE row_id IN ({placeholders}) AND session_id != ?
        """,
        (*touched, session_id),
    ).fetchall()
    return [r["row_id"] for r in reused]
