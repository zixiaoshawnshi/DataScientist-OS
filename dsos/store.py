"""ArtifactStore: the core, protocol-unaware library.

No MCP, no HTTP here — this is the layer everything else (MCP server,
execution runner, GUI) is built on top of.
"""

from __future__ import annotations

import json
import hashlib
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

# "template": chart styles (.mplstyle text) and report layouts (.html with
# {{title}}/{{body}} tokens) — the customizable styling layer behind
# run_python(style=...) and publish_report(template=...). See dsos/templating.py.
ARTIFACT_TYPES = {"dataset", "query", "transform", "chart", "narrative", "skill", "template"}

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
    """The rule for turning an artifact title into an id-safe slug — used to
    derive the `artifact_id` of a skill (save_skill) or a template
    (save_template) from its title, when the caller doesn't supply one.

    It is NOT how run inputs are named any more. An input is bound
    positionally, as in_1..in_N in input_row_ids order (see
    execution._alias), because a title-derived name collided when two inputs
    shared a title, needed the "t_" prefix below to stay a legal identifier
    for a digit-leading title, and broke whenever the input was re-titled.

    lowercase, non-alphanumeric -> `_`, never empty, never digit-leading. A
    title like "2025 headcount" would otherwise derive "2025_headcount" —
    not a valid Python identifier (a hard SyntaxError, no workaround) and an
    unquoted SQL identifier DuckDB rejects too, hence the "t_". That prefix
    only matters for the artifact_id case now, but the rule keeps producing
    it so existing skill/template ids stay stable."""
    name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "t"
    if name[0].isdigit():
        name = f"t_{name}"
    return name


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


def session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone() is not None


def _content_hash(content: Any, content_format: str) -> str | None:
    """Fingerprint of the stored content, for identifying an identical
    re-registration. Returns None when the content can't be hashed
    deterministically (the caller then just stores NULL).

    Tabular content hashes the DataFrame's values, not its serialized
    bytes: parquet encoding and float repr are not stable across pyarrow /
    pandas versions, and a hash that changes on upgrade would only cause
    missed dedupes, never a wrong merge.
    """
    if content is None:
        return None
    try:
        if content_format == "parquet" and hasattr(content, "columns"):
            import pandas as pd

            values = pd.util.hash_pandas_object(content, index=True).values.tobytes()
            header = repr([(str(c), str(t)) for c, t in zip(content.columns, content.dtypes)]).encode()
            payload = header + b"\x00" + values
        else:
            payload = str(content).encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()
    except Exception:
        return None


def find_by_content_hash(
    conn: sqlite3.Connection, type: str, content_hash: str | None
) -> str | None:
    """row_id of the earliest artifact of `type` with this exact content, or
    None. Used by save_artifact's MCP wrapper to collapse a re-registration
    of data already in the store into the existing row."""
    if not content_hash:
        return None
    row = conn.execute(
        """SELECT row_id FROM artifacts
           WHERE type = ? AND content_hash = ?
           ORDER BY created_at ASC, rowid ASC LIMIT 1""",
        (type, content_hash),
    ).fetchone()
    return row["row_id"] if row else None


# The consistency layer: skills teach workflow, templates carry styling.
# They are real artifacts — get_artifact, lineage and templating resolution
# all work on them — but they are not work products, so they are held out
# of search (unless type= names one) and out of the session-start signal.
# Counting them as "prior work" would make a store that holds nothing but
# instructions look like it has a history.
_NOT_PRIOR_WORK = ("skill", "template")

# The same exclusion as a SQL fragment, for search_artifacts' "no type was
# asked for" branch — one list, one rule, two spellings of it.
_NOT_TYPED_SQL = "a.type NOT IN ({})".format(",".join("?" * len(_NOT_PRIOR_WORK)))


def prior_work_signal(conn: sqlite3.Connection, question: str, top_k: int = 3) -> dict:
    """What this store already holds, relevant to `question`.

    Returned from start_session so a fresh session knows the store is not
    empty and that prior work may already cover the question. Without it
    the store is invisible until the agent goes looking: in the benchmark
    pilot it called search_artifacts zero times across ten reuse rounds
    and re-fetched data it had already registered.

    Candidate scores come from the same keyword-first/semantic-fallback
    ranking as search_artifacts, and the fallback embedder is weak —
    measured separation between a clearly relevant and a clearly
    irrelevant question was 0.39 vs 0.30. So no score threshold is applied
    and `match` is reported honestly (keyword = literal term hit,
    semantic = embedding similarity only): a candidate is a thing to
    check, not a claim that it fits.
    """
    placeholders = ",".join("?" * len(_NOT_PRIOR_WORK))
    counts = {
        r["type"]: r["n"]
        for r in conn.execute(
            f"SELECT type, COUNT(*) AS n FROM artifacts WHERE type NOT IN ({placeholders}) "
            f"GROUP BY type",
            _NOT_PRIOR_WORK,
        )
    }
    total = sum(counts.values())
    if not total:
        return {
            "prior_work": {"artifact_count": 0, "by_type": {}},
            "candidates": [],
            "note": "This store is empty — first session, so there is no prior work to reuse.",
        }

    candidates = []
    for art, score in search_artifacts(conn, question, top_k=top_k * 2):
        if art.type in _NOT_PRIOR_WORK:
            continue
        candidates.append({
            "row_id": art.row_id, "type": art.type, "title": art.title,
            "score": round(score, 3),
            "match": "keyword" if score >= 0.999 else "semantic",
        })
        if len(candidates) >= top_k:
            break

    by_type = ", ".join(f"{n} {t}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    note = (
        f"This store already holds {total} artifact(s) from earlier work ({by_type}). "
        f"Check the candidates below before fetching or rebuilding anything — if one covers "
        f"this question, reuse it via run_sql/run_python instead of redoing the work. "
        f"'semantic' candidates are embedding-similarity only and may well be irrelevant; "
        f"verify before relying on one. call search_artifacts for a fuller search."
    )
    return {"prior_work": {"artifact_count": total, "by_type": counts},
            "candidates": candidates, "note": note}


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

    if not is_new:
        existing = conn.execute(
            "SELECT type FROM artifacts WHERE artifact_id = ? ORDER BY version DESC LIMIT 1",
            (artifact_id,),
        ).fetchone()
        # A chosen artifact_id that no row uses yet is fine (that's how the
        # seeded skills register their stable ids). But versioning an
        # existing logical artifact across a *type change* (e.g. stacking a
        # narrative version onto a dataset id) is almost always an accident
        # that would silently corrupt the version chain — refuse it.
        if existing is not None and existing["type"] != type:
            raise ValueError(
                f"artifact_id {artifact_id!r} already holds type {existing['type']!r}; "
                f"a new version must keep the same type (got {type!r})"
            )

    if is_new:
        version = 1
    else:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        version = (row["v"] or 0) + 1

    row_id = _new_id()
    content_ref = _write_blob(conn, artifact_id, version, content, content_format)
    content_hash = _content_hash(content, content_format)
    vector = embeddings.embed(f"{title}\n{description}\n{' '.join(tags)}")

    conn.execute(
        """
        INSERT INTO artifacts (
            row_id, artifact_id, version, type, title, description, tags,
            content_ref, content_format, content_hash, source, embedding,
            created_at, session_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id, artifact_id, version, type, title, description, json.dumps(tags),
            content_ref, content_format, content_hash, json.dumps(source) if source else None,
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


def reembed_all(conn: sqlite3.Connection) -> int:
    """Recompute and overwrite every artifact's embedding with whatever
    backend dsos/embeddings.py currently resolves to. Returns the number of
    rows updated.

    Embeddings are computed once, at save_artifact time, and never touched
    again. Installing the sentence-transformers extra on a store that
    already has artifacts saved under the dependency-free hashing fallback
    doesn't error — the vectors are the same DIM either way — but a
    same-space cosine comparison between a new-model query vector and an
    old hash-fallback artifact vector is meaningless, silently degrading
    that artifact's semantic (not keyword — FTS never touches embeddings)
    discoverability with no visible symptom. Run this once, right after
    switching backends, so every existing artifact is embedded in the same
    space the new queries will be.
    """
    rows = conn.execute("SELECT row_id, title, description, tags FROM artifacts").fetchall()
    for row in rows:
        tags = json.loads(row["tags"])
        vector = embeddings.embed(f"{row['title']}\n{row['description']}\n{' '.join(tags)}")
        conn.execute(
            "UPDATE artifacts SET embedding = ? WHERE row_id = ?",
            (vector.astype(np.float32).tobytes(), row["row_id"]),
        )
    conn.commit()
    return len(rows)


def _write_blob(
    conn: sqlite3.Connection, artifact_id: str, version: int, content: Any, content_format: str
) -> str:
    ext = {"parquet": "parquet", "python": "py", "sql": "sql", "markdown": "md",
           "json": "json", "png": "png", "csv": "csv", "html": "html",
           "mplstyle": "mplstyle"}.get(content_format, "bin")
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
        except UnicodeDecodeError:
            # Same reasoning as the missing-blob case: a blob that's present
            # but not valid UTF-8 (partial write, wrong encoding at save
            # time, ...) shouldn't 500 every caller either.
            art.content_error = f"content blob is not valid UTF-8 (corrupt on disk): {art.content_ref}"
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
    more here than ranking keyword hits amongst themselves precisely. That
    sentinel means every keyword hit ties, so a stale "v2"/"FINAL"/archived
    near-duplicate could tie with (or, ordered arbitrarily by bm25, even
    rank above) the current artifact — no way for the caller to tell which
    to trust. Newest created_at now breaks that tie, both here and among
    genuinely-tied semantic scores below: it's not a freshness guarantee
    (an explicitly superseded artifact could still be newer than nothing),
    but it's a real, cheap signal that recency and "current" correlate
    far more often than not, with no schema change required.

    Skills and templates (the consistency layer) are excluded unless the
    caller passes `type="skill"`/`"template"`: they are instructions and
    styling, not results, and in a store where the server used to seed a
    skill library on first use they were most of what any search returned.
    The same exclusion applies to the session-start signal
    (see _NOT_PRIOR_WORK), so both routes share one rule.

    A run_sql/run_python call that failed used to still get a real artifact
    row, purely so its row_id could carry the error back to the caller.
    It no longer does — a failed run records an execution with a NULL
    output_row_id and writes no artifact at all (see execution._record_run)
    — so this filter now exists only for stores that still hold such rows
    from before that change, and is kept until they have all been seen.
    Excluded by status, not at the tool layer, so every caller of
    search_artifacts gets this for free.
    """
    if type:
        type_where, type_params = "a.type = ?", [type]
    else:
        type_where, type_params = _NOT_TYPED_SQL, list(_NOT_PRIOR_WORK)

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
                WHERE artifacts_fts MATCH ? AND {type_where} AND a.status != 'error'
                ORDER BY a.created_at DESC, bm25(artifacts_fts)
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
            f"SELECT a.* FROM artifacts a {_LATEST_VERSION_JOIN} "
            f"WHERE {type_where} AND a.status != 'error'",
            type_params,
        ).fetchall()
        q_vec = embeddings.embed(query)
        semantic = []
        for row in rows:
            if row["row_id"] in seen:
                continue
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            semantic.append((_row_to_artifact(row, load_content=False), embeddings.cosine_sim(q_vec, vec)))
        semantic.sort(key=lambda pair: (pair[1], pair[0].created_at), reverse=True)
        for art, score in semantic[: top_k - len(ordered)]:
            ordered.append(art)
            scores.append(score)

    return list(zip(ordered, scores))


def get_lineage(
    conn: sqlite3.Connection, row_id: str, *, direction: str = "ancestors",
) -> list[Artifact]:
    """`direction="ancestors"` walks parents (what this was built from);
    `direction="descendants"` walks children (what was built from this).

    A failed run_sql/run_python call used to get a real, lineage-linked
    artifact row (see search_artifacts's docstring for why that stopped
    being true), but a dead, content-less node is not a real step in
    anyone's lineage. Rows that old behaviour left behind are still
    excluded from the returned list, though traversal passes through them
    so a real node chained beyond one is still reachable."""
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
                    if art and art.status != "error":
                        result.append(art)
        frontier = next_frontier
    return result


def get_execution(
    conn: sqlite3.Connection, *, execution_id: str | None = None,
    output_row_id: str | None = None,
) -> dict | None:
    """One execution record (stdout/stderr/error/output_summary), looked up
    either by its own id or by the artifact it produced.

    The two lookups are not interchangeable since a failed run has no
    output artifact: given one of a row's id or its output's row_id, this
    returns the execution that produced it, if there was one. Used to
    surface diagnostics inline when a run fails — a failure has no
    artifact payload to read them from, so the response is built from this
    row instead.
    """
    if (execution_id is None) == (output_row_id is None):
        raise ValueError("pass exactly one of execution_id or output_row_id")
    if execution_id is not None:
        row = conn.execute(
            "SELECT * FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM executions WHERE output_row_id = ? ORDER BY started_at DESC LIMIT 1",
            (output_row_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "kind": row["kind"], "code": row["code"], "status": row["status"],
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
