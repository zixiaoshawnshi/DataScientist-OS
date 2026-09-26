"""ArtifactStore: the core, protocol-unaware library.

No MCP, no HTTP here — this is the layer everything else (MCP server,
execution runner, GUI) is built on top of.
"""

from __future__ import annotations

import json
import re
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

# {{artifact:<row_id>}} — how a narrative (or any text artifact) embeds a
# reference to another artifact. row_id already pins a specific version (a
# new version gets a new row_id), so no separate @version suffix is needed.
_EMBED_RE = re.compile(r"\{\{artifact:([\w-]+)\}\}")


def _embedded_row_ids(content: Any) -> set[str]:
    return set(_EMBED_RE.findall(content)) if isinstance(content, str) else set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def safe_table_name(title: str) -> str:
    """The rule for turning an artifact title into a variable/table name:
    lowercase, non-alphanumeric -> `_`, never empty. Lives here (not in
    execution.py) because both run paths — in-process namespace binding and
    the sandbox's input re-binding — must produce identical names."""
    import re
    name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return name or "t"


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
    content_error: str | None = None  # set instead of content if the blob is missing on disk


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

    Any `{{artifact:<row_id>}}` reference found in text `content` (e.g. a
    narrative embedding the datasets/charts it discusses) is automatically
    added to this artifact's lineage, on top of whatever `parent_row_ids`
    was explicitly passed — a narrative doesn't need both. References to a
    row_id that doesn't exist are silently dropped rather than raising, same
    as get_lineage silently skips missing rows elsewhere.
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
    candidate_parents = set(parent_row_ids or []) | _embedded_row_ids(content)
    if candidate_parents:
        placeholders = ",".join("?" * len(candidate_parents))
        existing_parents = {
            r["row_id"] for r in conn.execute(
                f"SELECT row_id FROM artifacts WHERE row_id IN ({placeholders})",
                tuple(candidate_parents),
            ).fetchall()
        }
        for parent_row_id in existing_parents:
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
        # encoding is explicit because write_text defaults to the locale
        # codec (GBK on Chinese-locale Windows), which can't encode emoji
        # or other common Unicode — a real crash seen in a live session.
        path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    elif content_format == "png":
        path.write_bytes(content)  # content: raw png bytes
    else:  # python, sql, markdown, csv, ... — treat as text
        path.write_text(
            content if isinstance(content, str) else str(content), encoding="utf-8"
        )
    return str(path)


def _load_blob(content_ref: str, content_format: str) -> Any:
    path = Path(content_ref)
    if content_format == "parquet":
        import pandas as pd
        return pd.read_parquet(path)
    if content_format == "json":
        return json.loads(path.read_text(encoding="utf-8"))
    if content_format == "png":
        return path.read_bytes()
    return path.read_text(encoding="utf-8")  # must match _write_blob's encoding


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
        try:
            art.content = _load_blob(art.content_ref, art.content_format)
        except FileNotFoundError:
            # The row's metadata (title, description, lineage) is still
            # real and useful even if the blob itself is gone from disk
            # (moved store, cleared cache, ...) — surface that distinctly
            # instead of crashing every caller that fetches this artifact.
            art.content_error = f"content blob missing on disk: {art.content_ref}"
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


_LATEST_VERSION_JOIN = """
    INNER JOIN (
        SELECT artifact_id, MAX(version) AS v FROM artifacts GROUP BY artifact_id
    ) latest ON a.artifact_id = latest.artifact_id AND a.version = latest.v
"""


def _fts_query(text: str) -> str:
    """Free text -> an FTS5 MATCH expression: each word becomes a prefix
    match, AND'd together, so "hackathon" also matches "hackathons" but a
    multi-word natural-language query (how an agent actually phrases
    search_artifacts) still requires every word present. That's deliberate:
    OR'd terms make a long question match almost anything via common words,
    drowning the "exact term wins" signal in false positives. AND correctly
    falls through to semantic search when nothing matches every term, rather
    than over-matching. "" (skip keyword search) if there are no word-like
    tokens at all.
    """
    return " AND ".join(f"{w}*" for w in re.findall(r"\w+", text))


def search_artifacts(
    conn: sqlite3.Connection, query: str, *, top_k: int = 5, type: str | None = None,
) -> list[tuple[Artifact, float]]:
    """Keyword-first, semantic fallback — so an exact term (a title, a
    column name) reliably wins, while a query with no literal overlap still
    finds something via embedding cosine similarity. Only the latest version
    of each logical artifact is considered either way.

    Keyword hits are given a sentinel score of 1.0 (max confidence) rather
    than a normalized bm25 score — ranking keyword above semantic matters
    more here than ranking keyword hits amongst themselves precisely.
    """
    type_clause = " AND a.type = ?" if type else ""
    type_params = [type] if type else []

    ordered: list[Artifact] = []
    scores: list[float] = []
    seen: set[str] = set()

    fts_query = _fts_query(query)
    if fts_query:
        try:
            rows = conn.execute(
                f"""
                SELECT a.* FROM artifacts_fts
                JOIN artifacts a ON a.row_id = artifacts_fts.row_id
                {_LATEST_VERSION_JOIN}
                WHERE artifacts_fts MATCH ?{type_clause}
                ORDER BY bm25(artifacts_fts)
                LIMIT ?
                """,
                [fts_query, *type_params, top_k],
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []  # malformed FTS syntax from raw query text — semantic search still runs
        for row in rows:
            art = _row_to_artifact(row, load_content=False)
            ordered.append(art)
            scores.append(1.0)
            seen.add(art.row_id)

    if len(ordered) < top_k:
        rows = conn.execute(
            f"SELECT a.* FROM artifacts a {_LATEST_VERSION_JOIN}"
            + (" WHERE a.type = ?" if type else ""),
            type_params,
        ).fetchall()
        q_vec = embeddings.embed(query)
        semantic = []
        for row in rows:
            if row["row_id"] in seen:
                continue
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            semantic.append((_row_to_artifact(row, load_content=False), embeddings.cosine_sim(q_vec, vec)))
        semantic.sort(key=lambda pair: pair[1], reverse=True)
        for art, score in semantic[: top_k - len(ordered)]:
            ordered.append(art)
            scores.append(score)

    return list(zip(ordered, scores))


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


def get_execution(conn: sqlite3.Connection, output_row_id: str) -> dict | None:
    """The execution record (stdout/stderr/error/output_summary) that
    produced `output_row_id`, if it was produced by run_sql/run_python.
    Used to surface diagnostics inline when a run fails."""
    row = conn.execute(
        "SELECT * FROM executions WHERE output_row_id = ? ORDER BY started_at DESC LIMIT 1",
        (output_row_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": row["kind"], "status": row["status"],
        "stdout": row["stdout"], "stderr": row["stderr"], "error": row["error"],
        "output_summary": json.loads(row["output_summary"]),
    }


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


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "question": row["question"], "started_at": row["started_at"]}


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    """All sessions, most recent first, with each one's artifact/reuse counts
    — the GUI's home page. Reuse count is a summary of
    `reused_artifact_row_ids`, computed per session at read time (no reuse
    count is persisted anywhere)."""
    rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
    results = []
    for row in rows:
        artifact_count = conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE session_id = ?", (row["id"],)
        ).fetchone()["n"]
        results.append({
            "id": row["id"], "question": row["question"], "started_at": row["started_at"],
            "artifact_count": artifact_count,
            "reused_count": len(reused_artifact_row_ids(conn, row["id"])),
        })
    return results


def list_tool_calls(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """A session's tool-call feed, oldest first — the free agent trace from
    the design doc, rendered for the GUI's session detail page."""
    rows = conn.execute(
        "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY ts", (session_id,)
    ).fetchall()
    return [
        {
            "id": r["id"], "ts": r["ts"], "tool_name": r["tool_name"],
            "args": json.loads(r["args_json"]), "result_summary": r["result_summary"],
            "artifact_row_ids": json.loads(r["artifact_row_ids"]),
        }
        for r in rows
    ]


def list_artifacts(
    conn: sqlite3.Connection, *, type: str | None = None, session_id: str | None = None,
) -> list[Artifact]:
    """Latest version of each logical artifact, newest first, optionally
    filtered by type and/or the session that created that version — the
    GUI's un-searched gallery view and a session's "artifacts it created"."""
    clauses, params = [], []
    if type:
        clauses.append("a.type = ?")
        params.append(type)
    if session_id:
        clauses.append("a.session_id = ?")
        params.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT a.* FROM artifacts a {_LATEST_VERSION_JOIN} {where} ORDER BY a.created_at DESC",
        params,
    ).fetchall()
    return [_row_to_artifact(r, load_content=False) for r in rows]


def list_versions(conn: sqlite3.Connection, artifact_id: str) -> list[Artifact]:
    """Every version of one logical artifact, oldest first — the GUI's
    "other versions" list on the artifact detail page."""
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY version", (artifact_id,)
    ).fetchall()
    return [_row_to_artifact(r, load_content=False) for r in rows]


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
