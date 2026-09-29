"""ArtifactStore: the core, protocol-unaware library.

No MCP, no HTTP here — this is the layer everything else (MCP server,
execution runner, GUI) is built on top of.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from dsos import embeddings
from dsos.db import blob_dir_for

# "template": chart styles (.mplstyle text) and report layouts (.html with
# {{title}}/{{body}} tokens) — the customizable styling layer behind
# run_python(style=...) and publish_report(template=...). See dsos/templating.py.
# "decision": a call the workstream made and why (WP-E3's record_decision) —
# it is an artifact like any other, because the reason a choice was made is
# the thing a later question needs and cannot recompute.
ARTIFACT_TYPES = {
    "dataset", "query", "transform", "chart", "narrative", "decision", "skill", "template",
}

# The lifecycle (D2/D3), on the artifacts.status column the store already
# had. `exploratory` is what a run produces — a finding nobody has claimed
# yet; `result` is what a deliberate registration asserts; `superseded` is
# the terminal state for a row something replaced, and it is kept rather
# than deleted so the chain of what replaced what is still readable.
#
# The transitions below are the whole rule set. `superseded` maps to nothing
# on purpose: reviving a row would mean the thing that replaced it might
# also be replaced later, and two current answers to one question is the
# failure this state exists to prevent. Reviving means saving a new version.
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "exploratory": ("result", "superseded"),
    "result": ("superseded",),
    "superseded": (),
}

# The states a row may be BORN in. Not `superseded`: that state means
# "replaced by superseded_by", and only mark requires the pointer — a writer
# that could save a superseded row would produce superseded_by=NULL, the one
# shape mark exists to refuse. A row is created current and then retired.
WRITABLE_STATUSES = ("exploratory", "result")

# caveats and confidence are the two fields a model can fill in dishonestly
# for free, so both are bounded and both demand evidence. caveats is capped
# at 5 x 200 chars because a caveat that needs more than that is a narrative
# artifact, not a caveat on this one — and the message says so, because the
# fix is "write the thing properly", not "work within the limit".
MAX_CAVEATS = 5
MAX_CAVEAT_CHARS = 200
CAVEATS_ERROR = (
    "caveats are short properties of a result; write a narrative for longer notes"
)
CONFIDENCE_LEVELS = ("high", "medium", "low")

# The verdict vocabulary is the schema's CHECK constraint (M3); the table
# repeats it so a caller can render the options without a DB round trip.
VERDICTS = ("confirmed", "contradicted", "stale", "needs_review")
# A `human` verdict outranks a `model` one however late the model speaks —
# the person who checked the work is the authority on it, and a model that
# keeps re-asserting its own opinion must not be able to bury that.
VERDICT_AUTHORITY = ("human", "model")
# The verdicts the derived stale check may replace — a whitelist, so a
# verdict added to the vocabulary later is left alone until someone argues it
# in. See validation_status for why contradicted and needs_review are not.
_STALE_PROMOTES = ("confirmed", "unvalidated")

# "7d"/"12h"/"2w": how long a fetched source stays fresh. Anything else —
# "static", "when the site changes", a typo — is not an interval this code
# can measure, so it is treated as "does not expire" rather than guessed at.
# A dataset with no refresh_after at all is the same case: dsos cannot know
# when someone else's data goes out of date, and inventing an interval would
# mark every dataset in a store stale.
_REFRESH_AFTER_RE = re.compile(r"^(\d+)([hdw])$")
_REFRESH_UNIT_SECONDS = {"h": 3600, "d": 86_400, "w": 604_800}

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
    caveats: list[str] | None = None
    confidence: list[dict] | None = None
    superseded_by: str | None = None


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
    """row_id of the earliest CURRENT artifact of `type` with this exact
    content, or None. Used by save_artifact's MCP wrapper to collapse a
    re-registration of data already in the store into the existing row.

    Current means `status='result'` on the latest version of its logical
    artifact — the only rows a collapse can hand back without losing the
    caller's claim. A superseded row is hidden from search and stays
    superseded, so collapsing into it makes the re-registration invisible
    (and drops its caveats, which are never written). An exploratory row
    is excluded for the same reason from the other side: a save_artifact
    IS a claim, and collapsing it into a row nobody claimed would leave the
    content unclaimed while telling the caller it was stored. An older
    version is not what search or find_evidence read either. In all three
    cases the save registers a new row instead, which is what makes the
    content findable again — and the next identical save collapses into
    that one.

    The latest-version test is a correlated NOT EXISTS rather than the
    _LATEST_VERSION_JOIN search uses: that join groups the whole table,
    and this lookup runs inside save_artifact's write lock on every save,
    where the (type, content_hash) and artifact_id indexes make it a
    handful of index probes instead.
    """
    if not content_hash:
        return None
    row = conn.execute(
        """SELECT a.row_id FROM artifacts a
           WHERE a.type = ? AND a.content_hash = ? AND a.status = 'result'
             AND NOT EXISTS (
                 SELECT 1 FROM artifacts newer
                 WHERE newer.artifact_id = a.artifact_id AND newer.version > a.version
             )
           ORDER BY a.created_at ASC, a.rowid ASC LIMIT 1""",
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
    # Exploratory rows are excluded (WP-G1): a finding nobody has claimed is
    # not prior work a session can reuse — it is the same landfill the search
    # default now hides. Counting them would advertise a history the store
    # does not have, and the candidate list below asks search for results
    # only for the same reason.
    counts = {
        r["type"]: r["n"]
        for r in conn.execute(
            f"SELECT type, COUNT(*) AS n FROM artifacts "
            f"WHERE type NOT IN ({placeholders}) AND status != 'exploratory' "
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
    for art, score in search_artifacts(
        conn, question, top_k=top_k * 2, include_exploratory=False
    ):
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


def _validated_caveats(caveats: list[str] | None) -> list[str]:
    """At most MAX_CAVEATS strings of at most MAX_CAVEAT_CHARS each.

    The cap is not a formatting preference, it is the distinction the field
    exists to draw: a caveat is a property of THIS result that a reader
    would otherwise get wrong ("excludes refunds", "one day only"). Text
    that needs more room than that is not a caveat, it is the analysis, and
    the error message says to write it as a narrative instead of telling the
    caller to compress — which is the only response that doesn't produce a
    lossy summary passed off as a caveat.
    """
    if caveats is None:
        return []
    if not isinstance(caveats, list) or any(not isinstance(c, str) for c in caveats):
        raise ValueError("caveats must be a list of strings")
    if len(caveats) > MAX_CAVEATS:
        raise ValueError(CAVEATS_ERROR)
    if any(len(c) > MAX_CAVEAT_CHARS for c in caveats):
        raise ValueError(CAVEATS_ERROR)
    return [c for c in caveats if c.strip()]


def _validated_confidence(confidence: list[dict] | None) -> list[dict]:
    """[{claim, level, basis}], with `basis` required and non-empty.

    The basis requirement is the whole point of the field. A bare
    {claim, level} is something a model can emit for any claim at all,
    always in the confident direction, at no cost — which makes a store full
    of "high" worth nothing. Requiring the evidence that supports the level
    is what makes the field carry information: "high" backed by "re-derived
    from the invoice export" is checkable, and "high" backed by nothing is
    not accepted at all. Validation is a schema check here rather than a
    warning, because a warning is something a model learns to ignore.
    """
    if confidence is None:
        return []
    if not isinstance(confidence, list):
        raise ValueError("confidence must be a list of {claim, level, basis} entries")
    checked = []
    for entry in confidence:
        if not isinstance(entry, dict):
            raise ValueError("confidence must be a list of {claim, level, basis} entries")
        claim = (entry.get("claim") or "").strip()
        level = (entry.get("level") or "").strip().lower()
        basis = (entry.get("basis") or "").strip()
        if not claim:
            raise ValueError("confidence entries need a claim")
        if level not in CONFIDENCE_LEVELS:
            raise ValueError(
                f"confidence level must be one of {list(CONFIDENCE_LEVELS)}, got {level!r}"
            )
        if not basis:
            raise ValueError(
                f"confidence entry for {claim!r} has no basis: say what the level rests on "
                f"(the check that was run, the source, the sample size). A level with no "
                f"basis is worth nothing to a later reader."
            )
        checked.append({"claim": claim, "level": level, "basis": basis})
    return checked


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
    status: str = "result",
    caveats: list[str] | None = None,
    confidence: list[dict] | None = None,
) -> str:
    """Save a new artifact, or a new version of an existing one.

    A real 1-2 sentence `description` is required, not a filename — it's
    the only thing search_artifacts has to go on, and reuse quality depends
    on it directly.

    `status` is the lifecycle (D2/D3) and every writer passes it explicitly:
    'result' here, because registering something deliberately IS the claim,
    and 'exploratory' for the output of a run_sql/run_python, which is a
    finding until someone claims it. Nothing falls back to the column's
    default — the column still says 'ready' so a fresh and a migrated store
    have identical schemas, and a writer that leaned on it would put 'ready'
    next to the 'result' its own migrated rows carry. The one place this
    function's default is the thing that matters is the two words in it.
    Those two are the only ones accepted (WRITABLE_STATUSES): 'superseded'
    is reached through mark, which is what demands the superseded_by.

    Any `{{artifact:<row_id>}}` reference found in text `content` (e.g. a
    narrative embedding the datasets/charts it discusses) is automatically
    added to this artifact's lineage, on top of whatever `parent_row_ids`
    was explicitly passed — a narrative doesn't need both. References to a
    row_id that doesn't exist are silently dropped rather than raising, same
    as get_lineage silently skips missing rows elsewhere.

    `caveats` and `confidence` are validated here, at save time, and raise
    ValueError — see _validated_caveats and _validated_confidence for why
    each of them is bounded.
    """
    if type not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact type {type!r}, expected one of {ARTIFACT_TYPES}")
    if not description or not description.strip():
        raise ValueError("save_artifact requires a real description, not a filename")
    if status not in WRITABLE_STATUSES:
        raise ValueError(
            f"a new artifact's status must be 'exploratory' or 'result', got {status!r}. "
            f"superseded is reached only by mark(row_id=..., status=\"superseded\", "
            f"superseded_by=...) on an existing row, which names what replaced it."
        )
    caveats = _validated_caveats(caveats)
    confidence = _validated_confidence(confidence)

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
            created_at, session_id, status, caveats, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id, artifact_id, version, type, title, description, json.dumps(tags),
            content_ref, content_format, content_hash, json.dumps(source) if source else None,
            vector.astype(np.float32).tobytes(),
            _now(), session_id, status,
            json.dumps(caveats) if caveats else None,
            json.dumps(confidence) if confidence else None,
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
        caveats=_json_column(row, "caveats"), confidence=_json_column(row, "confidence"),
        superseded_by=row["superseded_by"],
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


def _json_column(row: sqlite3.Row, name: str) -> Any:
    """A JSON text column as the Python value it holds, or None.

    Null is the normal case for both of these (most artifacts have no
    caveats and no confidence), and a row written by a build that predates
    the column has no value to parse at all. Neither is an error, so this
    never raises on missing data — a malformed value would, and rightly,
    because that is corruption rather than absence.
    """
    raw = row[name] if name in row.keys() else None
    return json.loads(raw) if raw else None


def _parse_iso(ts: str) -> datetime | None:
    """An ISO timestamp as an aware datetime, or None if it isn't one.

    `source.fetched_at` is freeform by design (save_artifact's docstring
    says "ISO-ish timestamp/description of when"), so the store really
    does hold values like "sometime last month". Those are not parseable,
    and treating them as errors would make an ordinary dataset unreadable;
    the caller falls back to created_at instead.
    """
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        parsed = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None
    # A naive timestamp is assumed UTC: every timestamp dsos itself writes
    # is aware, so a naive one came from a human's source dict, and the
    # store's own clock is the only reference available for it.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _expired_sources(conn: sqlite3.Connection, row_id: str) -> list[str]:
    """Human-readable reasons `row_id`'s inputs have outlived their
    refresh_after, one per expired ancestor dataset (plus the row itself if
    it is a dataset — its own source is what it was built from).

    The dataset's clock starts at source.fetched_at when that parses as ISO
    and at the dataset's created_at otherwise, which is the honest fallback:
    a dataset registered three days ago with no parseable fetch time is
    three days old as far as anything here can tell. `static` — and any
    refresh_after that is not a `^\d+[hdw]$` interval — never expires.

    Computed on read and never written (D11): a source that goes stale does
    not stop being what it was, and a scheduled sweep would have to be
    correct about wall-clock time and about datasets nobody ever touches
    again. Both cases — no source at all, and a source that says nothing
    about freshness — mean "not known to be expired", which is the default
    for a store where most datasets never declared a refresh window.
    """
    rows = conn.execute(
        """
        WITH RECURSIVE anc(row_id) AS (
            SELECT ? UNION SELECT l.parent_row_id FROM lineage l
            JOIN anc ON l.child_row_id = anc.row_id
        )
        SELECT a.row_id, a.title, a.created_at, a.source
        FROM artifacts a JOIN anc ON a.row_id = anc.row_id
        WHERE a.type = 'dataset'
        """,
        (row_id,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    reasons = []
    for row in rows:
        try:
            source = json.loads(row["source"]) if row["source"] else None
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(source, dict):
            continue
        match = _REFRESH_AFTER_RE.match(str(source.get("refresh_after") or ""))
        if not match:
            continue
        since = _parse_iso(source.get("fetched_at")) or _parse_iso(row["created_at"])
        if since is None:
            continue
        window = timedelta(
            seconds=int(match.group(1)) * _REFRESH_UNIT_SECONDS[match.group(2)]
        )
        if now - since > window:
            reasons.append(
                f"{row['title']!r} was fetched over {source.get('refresh_after')} ago "
                f"and is past its refresh_after"
            )
    return reasons


def validation_status(conn: sqlite3.Connection, row_id: str) -> dict:
    """The current validation verdict for one artifact, and the whole
    ledger behind it: {current, history, stale_reason?}.

    `current` is derived, never stored, and derived in a specific order:

    - the latest `human` verdict if there is one, else the latest `model`
      one, else 'unvalidated'. A person's check outranks a model's however
      late the model speaks afterwards — a model re-asserting its own
      opinion must not be able to overwrite the fact that someone looked.
    - then the derived stale check (D11), which promotes 'confirmed' and
      'unvalidated' to 'stale' while the inputs are past their refresh
      window, and nothing else. It does NOT promote 'contradicted':
      something already known to be wrong is not improved into a different
      kind of doubt, and silently downgrading a known-bad result to "stale"
      would be the one case where the automation is confidently wrong. Nor
      'needs_review': that verdict already says "do not lean on this yet"
      and, unlike stale, says what to check — replacing a specific request
      for review with the generic staleness label loses the more useful of
      the two. (A stored 'stale' verdict is already stale.)

    `history` is the append-only ledger itself, newest first, with every
    entry's basis. It is part of the return value because a verdict
    without its reasoning is a label, and a reader who disagrees with the
    label needs the reasoning to say so. `stale_reason` is present whenever
    the inputs are past their window — whether or not that changed
    `current` — and names the expired dataset, so a needs_review or
    contradicted row whose inputs have also expired still says so.

    The table has BEFORE UPDATE / BEFORE DELETE triggers aborting on it, so
    a verdict can only ever be added — which is what makes "a human verdict
    beats a later model verdict" representable at all.
    """
    rows = conn.execute(
        """
        SELECT id, verdict, by, session_id, at, basis FROM validations
        WHERE row_id = ? ORDER BY at DESC, rowid DESC
        """,
        (row_id,),
    ).fetchall()
    history = [dict(r) for r in rows]
    current = "unvalidated"
    for authority in VERDICT_AUTHORITY:
        latest = next((h for h in history if h["by"] == authority), None)
        if latest is not None:
            current = latest["verdict"]
            break
    reasons = _expired_sources(conn, row_id)
    stale_reason = "; ".join(reasons) if reasons else None
    if stale_reason and current in _STALE_PROMOTES:
        current = "stale"
    result = {"current": current, "history": history}
    if stale_reason:
        result["stale_reason"] = stale_reason
    return result


def mark(
    conn: sqlite3.Connection, *, row_id: str, session_id: str, status: str | None = None,
    verdict: str | None = None, basis: str | None = None,
    superseded_by: str | None = None,
) -> dict:
    """Move one artifact along its lifecycle, and/or append a validation.

    One tool rather than two, because the two are the same gesture seen
    from two sides: this row is now a result (or is now dead), and here is
    what I know about it and why. Both may be passed in one call.

    The status rules are STATUS_TRANSITIONS, and the error for an illegal
    one names every allowed transition — a model that guessed wrong is
    corrected by this message alone, without a second failed call to work
    out what it did wrong. 'superseded' is terminal and says so; the way
    back is a new version, which is a different row rather than a
    resurrected one.

    `superseded_by` is required for a superseded status (a row that is
    dead with nothing said about what replaced it is just a deletion with
    extra steps) and must name a row that exists, so the chain is walkable
    from either end. It is accepted ONLY with status='superseded': on a
    result row it would be a pointer claiming a replacement the status
    denies, and a verdict-only call does not touch the column at all.

    It also may not name a row that is itself superseded. That one rule is
    what keeps every chain ending at a current row: A->B then B->A would
    otherwise leave a question with no current answer at all, and pointing
    at a dead row just makes a reader walk further for the same answer. The
    error names that row's own replacement, which is almost always what the
    caller meant.

    A `verdict` is appended with by='model' and requires a `basis`, for the
    same reason a confidence level does: an unsupported verdict is a label
    a later reader cannot act on. Returns the row's new state and its
    freshly derived validation.
    """
    if status is None and verdict is None:
        raise ValueError(
            "mark needs at least one of status or verdict — pass the new status, a verdict, "
            "or both."
        )
    row = conn.execute(
        "SELECT status, title, superseded_by FROM artifacts WHERE row_id = ?", (row_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no artifact with row_id {row_id!r}")
    if superseded_by is not None and status != "superseded":
        raise ValueError(
            "superseded_by is only accepted with status=\"superseded\" — it names what "
            "replaced a retired row, and "
            + (f"status={status!r} does not retire this one."
               if status is not None else "a verdict alone does not retire anything.")
        )

    if status is not None:
        current = row["status"]
        allowed = STATUS_TRANSITIONS.get(current, ())
        if status not in allowed:
            raise ValueError(
                f"cannot mark {row_id} {current} -> {status}. Allowed transitions: "
                + "; ".join(
                    f"{a} -> {b}" for a, targets in STATUS_TRANSITIONS.items() for b in targets
                )
                + (". superseded is terminal — to bring this back, save a new version."
                   if current == "superseded" else ".")
            )
        if status == "superseded":
            if not superseded_by:
                raise ValueError(
                    "marking a row superseded requires superseded_by — the row_id of what "
                    "replaced it, so the chain stays walkable."
                )
            if superseded_by == row_id:
                raise ValueError("superseded_by must be a different row — an artifact cannot replace itself")
            target = conn.execute(
                "SELECT status, superseded_by FROM artifacts WHERE row_id = ?", (superseded_by,)
            ).fetchone()
            if target is None:
                raise ValueError(f"superseded_by {superseded_by!r} is not an existing row_id")
            if target["status"] == "superseded":
                raise ValueError(
                    f"superseded_by {superseded_by!r} is itself superseded (by "
                    f"{target['superseded_by']!r}), so it cannot be what replaced {row_id}: "
                    f"point superseded_by at a current row — most likely "
                    f"{target['superseded_by']!r}, or the end of its chain."
                )

    if verdict is not None:
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {list(VERDICTS)}, got {verdict!r}")
        if not basis or not basis.strip():
            raise ValueError(
                "a verdict requires a basis — what was checked, and against what. A verdict "
                "with no basis is a label a later reader cannot act on."
            )
        conn.execute(
            """
            INSERT INTO validations (id, row_id, verdict, by, session_id, at, basis)
            VALUES (?, ?, ?, 'model', ?, ?, ?)
            """,
            (_new_id(), row_id, verdict, session_id, _now(), basis),
        )

    if status is not None:
        conn.execute(
            "UPDATE artifacts SET status = ?, superseded_by = ? WHERE row_id = ?",
            (status, superseded_by, row_id),
        )
    conn.commit()
    return {
        "row_id": row_id,
        "status": row["status"] if status is None else status,
        # A verdict-only call left the column alone, so report what it holds
        # rather than the None that was (not) passed.
        "superseded_by": row["superseded_by"] if status is None else superseded_by,
        "validation": validation_status(conn, row_id),
    }


# How the consumer profile is told a question was answered, and what a claim
# was built from. Both are reads the consumer tools need and nothing else
# does, so they live here rather than in a sibling module's private SQL.
#
# `questions.artifact_row_id` is the authoritative link — it is what
# close_question writes, so it survives an artifact whose creating session has
# since been one of many. The session's own `question` column is the fallback
# for a row registered by a producer that never opened a question, and it is
# the ONLY provenance a save_artifact outside any question has.
def answered_question(conn: sqlite3.Connection, row_id: str) -> str | None:
    """The question this artifact answered, by either route, or None.

    The question row first (the question this row was registered as *the
    answer to*), then the creating session's question. A row saved with no
    question anywhere behind it has no stated question, and None says so
    rather than inventing one.
    """
    row = conn.execute(
        "SELECT question FROM questions WHERE artifact_row_id = ? "
        "ORDER BY closed_at DESC, created_at DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    if row is not None and row["question"]:
        return row["question"]
    row = conn.execute(
        "SELECT s.question AS question FROM artifacts a "
        "JOIN sessions s ON s.id = a.session_id WHERE a.row_id = ?",
        (row_id,),
    ).fetchone()
    return row["question"] if row is not None and row["question"] else None


def derivation_steps(conn: sqlite3.Connection, row_id: str) -> list[dict]:
    """`row_id` and its ancestors, roots first, each with its inputs and the
    code that produced it.

    Ordered topologically rather than by the BFS `get_lineage` returns, and
    that ordering is the point: a PM reading a derivation needs to see the
    dataset before the transform built on it, and a breadth-first walk from
    the claim puts the claim's direct inputs first and its grandparents last
    — the exact reverse of how the number was made. Kahn's algorithm over
    the ancestor subgraph, ties broken by created_at so the order is stable.

    `code` is the execution that produced the step, when there was one. A
    hand-registered dataset has none, and that is provenance rather than a
    gap: the key is present and None, so a reader can tell "registered by
    hand" from "the code is missing from the record". The code is returned
    whole — a PM is being told how a number was computed, and truncating it
    would be exactly the kind of quiet softening this profile exists to
    avoid.
    """
    rows = conn.execute(
        """WITH RECURSIVE anc(row_id) AS (
               SELECT ?
               UNION
               SELECT l.parent_row_id FROM lineage l JOIN anc ON l.child_row_id = anc.row_id
           )
           SELECT a.* FROM artifacts a JOIN anc ON a.row_id = anc.row_id""",
        (row_id,),
    ).fetchall()
    by_id = {r["row_id"]: r for r in rows}
    parents: dict[str, set[str]] = {rid: set() for rid in by_id}
    for rid in by_id:
        for edge in conn.execute(
            "SELECT parent_row_id FROM lineage WHERE child_row_id = ?", (rid,)
        ).fetchall():
            if edge["parent_row_id"] in by_id:
                parents[rid].add(edge["parent_row_id"])

    ordered: list[dict] = []
    placed: set[str] = set()
    # Roots first: a step is emitted only once every one of its inputs inside
    # this subgraph has been. A cycle would leave nodes unemitted; lineage is
    # a DAG by construction (a row can only be saved after the rows it
    # references exist), so this terminates.
    ready = sorted(
        (rid for rid, ps in parents.items() if not ps),
        key=lambda rid: (by_id[rid]["created_at"], rid),
    )
    while ready:
        rid = ready.pop(0)
        if rid in placed:
            continue
        placed.add(rid)
        row = by_id[rid]
        execution = get_execution(conn, output_row_id=rid)
        ordered.append({
            "row_id": rid,
            "type": row["type"],
            "title": row["title"],
            "source": json.loads(row["source"]) if row["source"] else None,
            "kind": execution["kind"] if execution else None,
            "code": execution["code"] if execution else None,
        })
        for child, ps in parents.items():
            if rid in ps:
                ps.discard(rid)
                if not ps and child not in placed:
                    ready.append(child)
        ready.sort(key=lambda r: (by_id[r]["created_at"], r))
    return ordered


# How low an embedding similarity may be and still be called evidence.
#
# Read from the environment on every call rather than at import, for the
# reason `spine.lease_minutes` reads its TTL there: a value cached at import
# is a constant of whichever process imported first, and this one is a
# calibration against whichever embedder is installed.
#
# The default is calibrated against the real model (all-MiniLM-L6-v2), where
# a paraphrase of a stored finding scores around 0.6-0.7 and an unrelated
# sentence scores near 0. It is NOT calibrated against the crc32 hash
# fallback dsos/embeddings.py uses when sentence-transformers is absent, whose
# similarities are not comparable to MiniLM's — so on a store embedded under
# the fallback this number means less than it does on a store with the extra
# installed. It is a floor for the semantic branch only; a keyword hit scores
# the 1.0 sentinel and is never dropped by it, which is what keeps a claim
# phrased in the words of a stored title findable on either backend.
EVIDENCE_FLOOR_ENV = "DSOS_EVIDENCE_FLOOR"
DEFAULT_EVIDENCE_FLOOR = 0.35

# What a consumer is allowed to be shown evidence FOR. A dataset is a source
# rather than a finding — it is what a claim was built from, which is what
# get_claim's derivation reports — and skill/template are the consistency
# layer, not results. Same exclusion as `_NOT_PRIOR_WORK` and for the same
# reason, which is why it is spelled as a tuple of types rather than a
# complement: this list is what a finding IS, and a new type should have to
# be argued in rather than silently included.
EVIDENCE_TYPES = ("query", "transform", "chart", "narrative", "decision")


def evidence_floor() -> float:
    """The semantic-similarity floor, from `DSOS_EVIDENCE_FLOOR` on every
    call. An unset, unparseable or out-of-range value falls back to the
    default: a typo in an env var must not silently turn off the floor (every
    semantic hit, however unrelated) or invert it (no semantic hit ever)."""
    raw = os.environ.get(EVIDENCE_FLOOR_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_EVIDENCE_FLOOR
    try:
        floor = float(raw)
    except ValueError:
        return DEFAULT_EVIDENCE_FLOOR
    return floor if 0.0 <= floor <= 1.0 else DEFAULT_EVIDENCE_FLOOR


def find_evidence(
    conn: sqlite3.Connection, claim: str, *, top_k: int = 5,
) -> list[tuple[Artifact, float, str]]:
    """The stated results that speak to `claim`, as (artifact, score, match).

    The consumer profile's search, and it is deliberately NOT
    `search_artifacts` with different flags, because "what should I believe"
    and "what have we got" are different questions with different tolerances:

    - only the LATEST version of each logical artifact, `status='result'`, and
      a type in `EVIDENCE_TYPES`. An exploratory row is a finding nobody has
      claimed and a superseded one is a finding something replaced; showing
      either to somebody deciding whether to back a number would be answering
      a different question than the one they asked. They are not hidden from
      the store — `get_claim` still reads them, with a warning.
    - ranked exactly as `search_artifacts` ranks (keyword hits first, on
      the 1.0 sentinel and ordered by bm25; embedding cosine as the
      fallback; newest first among ties),
      so the two profiles cannot rank the same store two different ways.
    - but the SEMANTIC branch is floored. A reader with no data and no
      tolerance for a wrong number is worse served by a 0.1-cosine
      near-miss than by an honest empty list, and unlike the producer there is
      no semantic fallback behind this one to catch what the floor drops.
    - and every hit reports which branch found it, so a `keyword` hit is a
      literal term match and a `semantic` one is "the embedder thought these
      were about the same thing" — a distinction the caller can act on and a
      bare score cannot.

    The floor is applied to the semantic branch only, and after ranking, so a
    keyword hit is never dropped by a threshold calibrated for cosine.
    """
    type_where = "a.type IN ({})".format(",".join("?" * len(EVIDENCE_TYPES)))
    # status='result' written out rather than assembled from
    # _lifecycle_sql's two negations: that function answers "hide these two",
    # this one asks "show me exactly this one", and only the second is
    # robust to a status being added to the vocabulary.
    eligible = f"{type_where} AND a.status = 'result'"

    ordered: list[Artifact] = []
    scores: list[float] = []
    matches: list[str] = []
    seen: set[str] = set()

    fts_query = _fts_query(claim)
    if fts_query:
        try:
            rows = conn.execute(
                f"""
                SELECT a.* FROM artifacts_fts
                JOIN artifacts a ON a.row_id = artifacts_fts.row_id
                {_LATEST_VERSION_JOIN}
                WHERE artifacts_fts MATCH ? AND {eligible}
                ORDER BY bm25(artifacts_fts), a.created_at DESC
                LIMIT ?
                """,
                [fts_query, *EVIDENCE_TYPES, top_k],
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []  # malformed FTS syntax from raw claim text
        for row in rows:
            art = _row_to_artifact(row, load_content=False)
            ordered.append(art)
            scores.append(1.0)
            matches.append("keyword")
            seen.add(art.row_id)

    if len(ordered) < top_k:
        rows = conn.execute(
            f"SELECT a.* FROM artifacts a {_LATEST_VERSION_JOIN} WHERE {eligible}",
            EVIDENCE_TYPES,
        ).fetchall()
        floor = evidence_floor()
        q_vec = embeddings.embed(claim)
        semantic = [
            (_row_to_artifact(row, load_content=False), embeddings.cosine_sim(q_vec,
             np.frombuffer(row["embedding"], dtype=np.float32)))
            for row in rows if row["row_id"] not in seen
        ]
        semantic.sort(key=lambda pair: (pair[1], pair[0].created_at), reverse=True)
        for art, score in semantic[: top_k - len(ordered)]:
            if score < floor:
                # Sorted descending, so the first one under the floor ends
                # the branch rather than being skipped over.
                break
            ordered.append(art)
            scores.append(score)
            matches.append("semantic")

    return list(zip(ordered, scores, matches))


def unfinished_questions(
    conn: sqlite3.Connection, statuses: tuple[str, ...] = ("open", "in_progress"),
) -> list[sqlite3.Row]:
    """Every question in `statuses`, newest first.

    A whole-table scan of a small table, and it is one for the same reason
    `spine.related_questions` has one: SQLite has no case-folding or
    whitespace-collapsing function, so a "same question" comparison has to
    happen in Python, and a question is a row per top-level question rather
    than per artifact. The consumer's `ask` compares normalised text against
    these; the normalisation itself is `spine.normalise_question_text`, which
    is not re-implemented here (TTD U4's argument, one rule one spelling).
    """
    return conn.execute(
        f"SELECT * FROM questions WHERE status IN ({','.join('?' * len(statuses))}) "
        f"ORDER BY created_at DESC, rowid DESC",
        statuses,
    ).fetchall()


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


def _lifecycle_sql(*, include_superseded: bool, include_exploratory: bool) -> str:
    """The `a.status` predicate search_artifacts applies, as a SQL fragment
    (ANDed onto both its keyword and its semantic branch).

    Superseded rows are hidden by default (D7): they were marked as
    replaced by a specific row, so leaving them in would mean a search
    answers with something a human already retired. They are hidden, not
    deleted — include_superseded=True returns them, and get_artifact reads
    them regardless, which is the whole point of keeping them.

    Exploratory rows are HIDDEN by default (WP-G1 cutover). A run's output
    is a finding nobody has claimed yet, and showing every finding to every
    agent is what made the store read as the landfill Doc II warns about.
    The default was flipped at cutover, once the population was mostly
    results; a caller that genuinely wants unclaimed findings passes
    include_exploratory=True explicitly rather than leaning on the default.
    """
    if include_superseded:
        clause = ""
    else:
        clause = " AND a.status != 'superseded'"
    if not include_exploratory:
        clause += " AND a.status != 'exploratory'"
    return clause


def search_artifacts(
    conn: sqlite3.Connection, query: str, *, top_k: int = 5, type: str | None = None,
    include_superseded: bool = False, include_exploratory: bool = False,
) -> list[tuple[Artifact, float]]:
    """Keyword-first, semantic fallback — so an exact term (a title, a
    column name) reliably wins, while a query with no literal overlap still
    finds something via embedding cosine similarity. Only the latest version
    of each logical artifact is considered either way.

    Keyword hits are given a sentinel score of 1.0 (max confidence) rather
    than a normalized bm25 score — ranking keyword above semantic matters
    more here than exposing a number whose scale means nothing to a caller.
    The sentinel is only the reported score, though: keyword hits are
    ORDERED by bm25, so an old row that is squarely about the query ranks
    above a new one that mentions the words once in passing. Ordering them
    newest-first instead (as this did) made recency the ranking and bm25 a
    tie-break that almost never fired, which also let the LIMIT cut the
    best match off a top_k list in favour of whatever was saved last.

    Newest created_at is the tie-break, among equal bm25 and among
    genuinely-tied semantic scores below. That is what the recency order
    was for — a stale "v2"/"FINAL"/archived near-duplicate with the same
    title and wording scores the same bm25 as the current artifact, and the
    newer one is far more often the current one. It is not a freshness
    guarantee (an explicitly superseded artifact could still be newer than
    nothing); `mark(status="superseded")` is the real answer to a stale
    duplicate, and it hides the row outright.

    Skills and templates (the consistency layer) are excluded unless the
    caller passes `type="skill"`/`"template"`: they are instructions and
    styling, not results, and in a store where the server used to seed a
    skill library on first use they were most of what any search returned.
    The same exclusion applies to the session-start signal
    (see _NOT_PRIOR_WORK), so both routes share one rule.

    Lifecycle rows are filtered by default too: superseded ones (something
    replaced them) and, since the cutover (WP-G1), exploratory ones (a
    finding nobody has claimed is a lead, not the answer to "what do I
    have"). `include_superseded`/`include_exploratory` return each again.

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
    lifecycle_sql = _lifecycle_sql(
        include_superseded=include_superseded, include_exploratory=include_exploratory
    )

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
                WHERE artifacts_fts MATCH ? AND {type_where} AND a.status != 'error'{lifecycle_sql}
                ORDER BY bm25(artifacts_fts), a.created_at DESC
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
            f"WHERE {type_where} AND a.status != 'error'{lifecycle_sql}",
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


def failed_executions(
    conn: sqlite3.Connection, session_id: str, limit: int = 20
) -> list[dict]:
    """One session's failed runs, newest first — the GUI's session-detail
    list, and the only way to see a run that produced nothing.

    A failed run has no artifact to navigate from, which is exactly why it
    needs a listing: `get_execution` finds a run by its id or by the row it
    produced, and a run that produced nothing has neither a row to be found
    from nor a page to be found from. The `executions.session_id` column
    added by WP-B3's M2 is what makes it possible at all.

    This lived as a bare `conn.execute` inside the GUI route, which was
    correct under the WP that wrote it (that WP's store.py ownership was
    `get_execution` only) and is wrong now that the GUI is a mounted router
    over the daemon's shared Database: SQL in a route is a connection
    outside the store layer, and store.py is the layer the GUI already reads.
    """
    rows = conn.execute(
        """
        SELECT kind, error, ended_at FROM executions
        WHERE session_id = ? AND status = 'error'
        ORDER BY ended_at DESC LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


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
