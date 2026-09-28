"""The spine: questions, the coordination board, and the claim lease.

Everything else in dsos is a *store* — artifacts, lineage, validation. This
module is the other half: what a workstream is currently trying to answer,
who is on it, and what came of it. Two agents working the same store need
to be able to see each other, or the store grows the same analysis twice
and neither copy knows about the other.

Three ideas carry it:

**The lease is computed, never stored (D10).** A claim is live while the
claiming session made any tool call within `DSOS_LEASE_MINUTES` (default
30), and last activity is `max(claimed_at, latest tool_calls.ts)`. There is
no lease column and no heartbeat, and that is the design: renewal is free
because every tool call the agent already makes renews the lease, and an
agent that dies mid-question releases its claim by going quiet, with
nothing to sweep and nothing to rot. The cost is that the lease can only be
as fresh as the tool-call log, which is the right trade — the log is the
only thing here that is written anyway.

**The board is a read.** `related_questions` is what a producer is shown
at the top of a new session: who is already on this, who asked it and is
waiting, and which answered questions can simply be reused. It ranks live
in-progress work first (that is the duplicate-work warning), then open
questions (usually a consumer waiting for an answer), then answered ones
(with the row that answered them, because the best outcome is not doing the
work again). `DSOS_DISABLE_BOARD=1` empties it and nothing else: the
question is still created, the claim is still taken, the session is still
recorded. Benchmark arm H3 compares duplicate-work rate with the board on
and off, and that comparison is only honest if the two conditions differ in
the board and in nothing else.

**A claim is not a lock.** It is a statement of who is working on what, and
the only thing it refuses is a *second* worker on a live question. It does
not decide who is right, and `close_question` deliberately does not check
that the closer is the claimer: a finished question is a fact about the
store, and a stale claim is the abandon sweep's business (WP-G1), not a
permission check.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from dsos import store

# A private cross-module import, and deliberately so (TTD U4). The
# AND-prefix FTS rule is one rule with two callers now — artifact search
# inside store.py, and this board — and copying it here would give the
# codebase two spellings of it, which is the exact drift this project keeps
# paying to undo: tune the rule in store.py and the board would silently
# keep the old behaviour. The tidier end state is promoting it to a public
# `fts_match`, but store.py is owned by four WPs and this is not one of
# them, so it is recorded as U4 and promoted by whoever is next in that file
# for another reason. Two lines, both call sites.
# `store._new_id` / `store._now` are borrowed the same way, and for the
# same reason: the question table's ids and timestamps have to be the same
# shape the rest of the store's rows use, and duplicating a uuid4 and an
# isoformat to get a "different" id generator would be a store whose two
# halves cannot be reasoned about together.
_fts_query = store._fts_query

# How long a claim survives without activity. Read per call, never at import:
# a test (and an operator) must be able to change it on a live server, and
# a cached value would make the lease TTL a constant of whoever imported
# first.
LEASE_MINUTES_ENV = "DSOS_LEASE_MINUTES"
DEFAULT_LEASE_MINUTES = 30.0

# H3's control condition. Read inside related_questions for the same reason
# the TTL is: a benchmark arm flipping an env var must take effect on the
# next call, whatever this module did at import time.
BOARD_ENV = "DSOS_DISABLE_BOARD"

# Questions this board is willing to show. `abandoned` is the one status
# that never appears: a question nobody finished and nobody holds is not
# information, it is noise on the one screen an agent reads before it
# starts working.
_BOARD_STATUSES = ("in_progress", "open", "answered")

_CLOSABLE = ("answered", "abandoned")

# A decision is prose, and a title column is a title. The full text is
# always in the content (and in the description, which is what search
# ranks on); these caps only stop a paragraph ending up as a row's label.
_TITLE_CHARS = 120
_DESCRIPTION_CHARS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(ts: str) -> datetime | None:
    """Parse a stored timestamp as an aware UTC datetime, or None if it is
    not one. The lease compares timestamps written by different hands — a
    Python `datetime.isoformat()` and a backdated row from a test — so the
    comparison has to be on datetimes, and a naive string has to be pinned
    to UTC rather than raising."""
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lease_minutes() -> float:
    """The claim TTL, from `DSOS_LEASE_MINUTES` on every call. An unset,
    unparseable or non-positive value falls back to the 30-minute default:
    a typo in an env var must not silently make every lease instant, which
    would look like the board working while nobody holds anything."""
    raw = os.environ.get(LEASE_MINUTES_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_LEASE_MINUTES
    try:
        minutes = float(raw)
    except ValueError:
        return DEFAULT_LEASE_MINUTES
    return minutes if minutes > 0 else DEFAULT_LEASE_MINUTES


def board_enabled() -> bool:
    """Whether related_questions returns anything. `DSOS_DISABLE_BOARD` in
    its affirmative spellings empties the board; anything else (including
    unset) leaves it on."""
    return os.environ.get(BOARD_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def normalise_question_text(text: str) -> str:
    """The question text reduced to what makes two questions the same one:
    lowercased, whitespace collapsed, trailing `?` dropped. A consumer's
    "Which team won q3?" and a producer's "which  team won q3" are the same
    question, and the board exists to notice that.

    Public, and deliberately so: the consumer's `ask` (WP-F1) has to dedupe
    on the same rule, and a second private spelling of it in a sibling
    module would be the same two-spellings problem the `_fts_query` import
    above is about.
    """
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip("?").strip()


def _last_activity(conn: sqlite3.Connection, question: sqlite3.Row) -> datetime | None:
    """When the claim was last touched: `max(claimed_at, latest tool_calls.ts
    of claimed_by)`. ISO-8601 UTC strings from one writer compare correctly
    as text, but this compares as datetimes anyway so a row written by
    something other than `store._now()` (a backdated test row, a future
    migration) cannot flip the order."""
    stamps = [t for t in (_as_utc(question["claimed_at"] or ""),) if t is not None]
    claimed_by = question["claimed_by"]
    if claimed_by:
        row = conn.execute(
            "SELECT MAX(ts) AS ts FROM tool_calls WHERE session_id = ?", (claimed_by,)
        ).fetchone()
        if row is not None and row["ts"]:
            called = _as_utc(row["ts"])
            if called is not None:
                stamps.append(called)
    return max(stamps) if stamps else None


def lease_state(conn: sqlite3.Connection, question: sqlite3.Row) -> dict:
    """Is this claim live, and when does it lapse?

    `{live, expires_at}` — expires_at is the moment the claim goes stale, and
    it is reported even when the lease has already lapsed, because "it died
    at 14:02" is what a second agent needs to decide whether to take over.
    An unclaimed question has neither: live False, expires_at None.
    """
    if not question["claimed_by"]:
        return {"live": False, "expires_at": None}
    last = _last_activity(conn, question) or _as_utc(question["created_at"])
    if last is None:
        return {"live": False, "expires_at": None}
    expires = last + timedelta(minutes=lease_minutes())
    return {"live": _now() <= expires, "expires_at": expires.isoformat()}


def create_question(
    conn: sqlite3.Connection, *, question: str, status: str = "open",
    hypothesis: str | None = None, asked_by: str | None = None,
    claimed_by: str | None = None,
) -> str:
    """Write a question row and its search row, and return the id.

    Both writes happen here, in one place, because the board reads the
    question table (the normalised-text path) and `questions_fts` (the
    keyword path) and a question with a row in only one of them is a
    question the board can fail to show — which is a coordination bug with
    no symptom of its own. There is no trigger tying them together in M3,
    and adding one is a migration this WP does not own.
    """
    if not question or not question.strip():
        raise ValueError("a question needs text — it is what the board matches on")
    if status not in ("open", "in_progress", "answered", "abandoned"):
        raise ValueError(
            f"status must be one of ['open', 'in_progress', 'answered', 'abandoned'], "
            f"got {status!r}"
        )
    question_id = store._new_id()
    now = store._now()
    conn.execute(
        """INSERT INTO questions (id, question, hypothesis, status, asked_by, claimed_by,
                                  claimed_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (question_id, question, hypothesis, status, asked_by, claimed_by,
         now if claimed_by else None, now),
    )
    conn.execute(
        "INSERT INTO questions_fts (id, question, hypothesis) VALUES (?, ?, ?)",
        (question_id, question, hypothesis),
    )
    conn.commit()
    return question_id


def get_question(conn: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def check_claimable(
    conn: sqlite3.Connection, question_id: str, *, requester: str | None = None
) -> sqlite3.Row:
    """Raise unless this question can be claimed right now, and return it.

    Split from `claim_question` so a caller that has not got a session id yet
    can ask the question first. `start_session` does exactly that: it would
    otherwise create a session row and only then discover the claim is
    refused, leaving an empty session behind that nothing ever writes to and
    the GUI lists.

    The refusal is the lease and nothing more — a live claim by *another*
    session. Your own live claim is not a conflict, and neither is a lapsed
    one, which is how a dead agent's question becomes claimable again with
    no sweep. A question that is answered or abandoned is not claimable at
    all: it is finished, and re-opening it is what a new question is for.
    """
    question = get_question(conn, question_id)
    if question is None:
        raise ValueError(
            f"no question with id {question_id!r} in this store — pass the question_id "
            f"start_session reported, or call start_session with only the question to "
            f"start a new one."
        )
    if question["status"] not in ("open", "in_progress"):
        raise ValueError(
            f"question {question_id} is {question['status']}, so it cannot be claimed — "
            f"it is finished. Start a new question instead, and cite the answered row "
            f"({question['artifact_row_id']}) if that is what you need."
        )
    if question["claimed_by"] and question["claimed_by"] != requester:
        lease = lease_state(conn, question)
        if lease["live"]:
            last = _last_activity(conn, question)
            raise ValueError(
                f"question {question_id} is already being worked on by session "
                f"{question['claimed_by']} (last activity "
                f"{last.isoformat() if last else 'unknown'}, lease expires "
                f"{lease['expires_at']}). Do not start the same work twice: call "
                f"start_session with no question_id and read related_questions for what "
                f"is already in flight, or wait for the lease to lapse and claim it then."
            )
    return question


def claim_question(conn: sqlite3.Connection, question_id: str, *, session_id: str) -> str:
    """Take the question, or re-take one this session already holds."""
    check_claimable(conn, question_id, requester=session_id)
    conn.execute(
        "UPDATE questions SET claimed_by = ?, claimed_at = ? WHERE id = ?",
        (session_id, store._now(), question_id),
    )
    conn.commit()
    return question_id


def _board_rank(question: sqlite3.Row, live: bool) -> int:
    """Board order: live in-progress first, then open (someone is waiting),
    then answered (reuse). An in_progress claim whose lease has lapsed lands
    with the answered questions rather than at the top — the warning that
    matters is "someone is on it NOW", and a lapsed claim is not that."""
    if question["status"] == "in_progress" and question["claimed_by"] and live:
        return 0
    return {"open": 1, "in_progress": 2, "answered": 2}[question["status"]]


def related_questions(
    conn: sqlite3.Connection, text: str, exclude_id: str | None = None, limit: int = 5
) -> list[dict]:
    """What else is in flight, waiting, or already answered, for this text.

    Two matching paths, and they are not redundant:

    - `questions_fts` via the same AND-prefix rule artifact search uses. It
      is the only path that sees `hypothesis`, and the only one that ranks.
    - exact match on the normalised text. It is the backstop for questions
      FTS cannot see at all: a row written before this search table existed,
      or by anything that inserted into `questions` without its FTS row, is
      invisible to the JOIN and would otherwise never be reported. The
      comparison is a scan of the question table in Python because SQLite
      has no case-folding or whitespace-collapsing function, which is
      affordable precisely because a question is a row per top-level
      question, not per artifact.

    Ordered live in-progress, then open, then answered; within a band, the
    exact text match first and then the most recent. `exclude_id` is the
    caller's own question, which the board must never tell them about.
    """
    if not board_enabled():
        return []
    limit = max(int(limit), 0)
    wanted = normalise_question_text(text)
    if not wanted or limit == 0:
        return []

    candidates: dict[str, sqlite3.Row] = {}
    fts_query = _fts_query(text)
    if fts_query:
        try:
            rows = conn.execute(
                f"""
                SELECT q.* FROM questions_fts
                JOIN questions q ON q.id = questions_fts.id
                WHERE questions_fts MATCH ?
                  AND q.status IN ({','.join('?' * len(_BOARD_STATUSES))})
                ORDER BY bm25(questions_fts) LIMIT ?
                """,
                (fts_query, *_BOARD_STATUSES, limit * 4),
            ).fetchall()
        except sqlite3.OperationalError:
            # Same guard as search_artifacts: raw question text can produce
            # FTS syntax the parser rejects, and the normalised pass below
            # still stands.
            rows = []
        for row in rows:
            if row["id"] != exclude_id:
                candidates[row["id"]] = row

    # The backstop pass, and it is a separate pass on purpose: it adds the
    # rows FTS could not return, rather than seeding the candidate set with
    # every question in the store and making the keyword path redundant.
    for row in conn.execute(
        f"SELECT * FROM questions WHERE status IN ({','.join('?' * len(_BOARD_STATUSES))})",
        _BOARD_STATUSES,
    ).fetchall():
        if row["id"] in candidates or row["id"] == exclude_id:
            continue
        if normalise_question_text(row["question"]) == wanted:
            candidates[row["id"]] = row

    if not candidates:
        return []

    # Exact-text matches rank first inside a band, so the one question that
    # is literally the same one is never crowded out by its own neighbours.
    exact = {
        qid for qid, row in candidates.items()
        if normalise_question_text(row["question"]) == wanted
    }
    # One lease evaluation per candidate, reused for the ranking and for the
    # expires_at the board reports — the lease is derived, so it costs a
    # query, and computing it twice per row would double that for no gain.
    leases = {qid: lease_state(conn, row) for qid, row in candidates.items()}
    ordered = sorted(
        candidates.values(),
        key=lambda r: (
            _board_rank(r, leases[r["id"]]["live"]),
            0 if r["id"] in exact else 1,
            -(_as_utc(r["created_at"]) or _now()).timestamp(),
        ),
    )[:limit]
    return [
        {
            "question_id": r["id"],
            "question": r["question"],
            "status": r["status"],
            "claimed": bool(r["claimed_by"]),
            "lease_expires_at": leases[r["id"]]["expires_at"] if r["claimed_by"] else None,
            "artifact_row_id": r["artifact_row_id"],
        }
        for r in ordered
    ]


def close_question(
    conn: sqlite3.Connection, *, session_id: str, question_id: str, status: str,
    artifact_row_id: str | None = None, note: str | None = None,
) -> dict:
    """Close a question as `answered` or `abandoned`.

    `answered` requires an `artifact_row_id` that is genuinely a result: a
    current claim, or a decision. Not merely a row that exists — closing a
    question against an exploratory run would be a claim nobody ever made,
    asserted in the one place a later agent looks for the answer.

    There is deliberately no check that the caller holds the claim. A
    finished question is a fact about the store rather than a permission,
    and the two callers that matter — a producer closing its own work, and
    WP-G1's abandon sweep closing questions nobody holds — would be
    asymmetric under such a rule. `session_id` is therefore accepted and
    recorded in the tool-call trace, but not enforced here.

    `note` is returned and logged, not stored: M3's questions table has no
    column for it, and a WP that adds a migration to carry one sentence of
    free text is not worth the schema version it costs. The tool-call
    arguments already record it, so the note is not lost — it just is not
    part of the question's row.
    """
    if status not in _CLOSABLE:
        raise ValueError(
            f"a question closes as one of {list(_CLOSABLE)}, got {status!r} — a question "
            f"that is still in progress is one somebody is working on, and closing it "
            f"silently is how work gets lost."
        )
    question = get_question(conn, question_id)
    if question is None:
        raise ValueError(f"no question with id {question_id!r} in this store")

    if status == "answered":
        if not artifact_row_id:
            raise ValueError(
                f"closing {question_id} answered requires artifact_row_id — the row that "
                f"answers it. A question closed with nothing behind it is an answer "
                f"nobody can check. Pass the result row, or close it abandoned."
            )
        row = conn.execute(
            "SELECT row_id, type, status, title FROM artifacts WHERE row_id = ?",
            (artifact_row_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no artifact with row_id {artifact_row_id!r}")
        # A decision is saved as status='result' anyway, so the first branch
        # already covers it; the second keeps a current decision acceptable
        # even if the lifecycle is later given a separate state for it, while
        # still refusing one that something has superseded.
        if row["status"] != "result" and not (row["type"] == "decision" and row["status"] != "superseded"):
            raise ValueError(
                f"{artifact_row_id} is a {row['status']} {row['type']!r} row, so it is not "
                f"an answer to {question_id!r}: a question can only be closed by a result "
                f"row or a decision. Claim it with mark(row_id=..., status=\"result\") "
                f"first if it really is the answer."
            )

    conn.execute(
        """UPDATE questions
           SET status = ?, artifact_row_id = COALESCE(?, artifact_row_id),
               claimed_by = NULL, claimed_at = NULL, closed_at = ?
           WHERE id = ?""",
        (status, artifact_row_id, store._now(), question_id),
    )
    conn.commit()
    return {
        "question_id": question_id,
        "status": status,
        "artifact_row_id": artifact_row_id or question["artifact_row_id"],
        "note": note,
    }


def record_decision(
    conn: sqlite3.Connection, *, session_id: str, decision: str, rationale: str,
    evidence_row_ids: list[str], revisit_if: str | None = None,
    question_id: str | None = None,
) -> dict:
    """Record a call the workstream made, through the normal artifact path.

    A decision is an artifact like any other, because the reason a choice
    was made is the thing a later question needs and cannot recompute. It
    goes through `store.save_artifact` — which owns the lifecycle
    validation, the search row and the embedding — rather than a direct
    INSERT, because a row that search can see and lineage cannot is worse
    than no row: a decision with no lineage is an assertion.

    Evidence is required and must exist. `save_artifact` silently drops
    lineage parents it cannot find, which is right for a narrative that
    embeds a row_id someone deleted, and wrong here: a decision citing
    evidence that is not in the store is a claim with nothing behind it.
    So the ids are checked first, here.

    The evidence rows become the decision's lineage parents, so get_lineage
    answers "what was this call based on" without a second mechanism.

    With `question_id`, the question is closed as answered by this decision
    — after the decision row exists and is committed. The order is the
    point: a failure between the two would otherwise leave a question
    answered by a row that was never written.
    """
    if not decision or not decision.strip():
        raise ValueError("a decision needs the call itself — what was decided, in one line")
    if not rationale or not rationale.strip():
        raise ValueError(
            "a decision requires a rationale — what it rests on. A decision with no reason "
            "is indistinguishable from an accident, which is the one thing a later "
            "session most needs to be able to tell."
        )
    evidence = [r for r in (evidence_row_ids or []) if r]
    if not evidence:
        raise ValueError(
            "a decision requires at least one evidence row — record_decision(evidence_row_ids=[...]) "
            "naming what the call rests on. An unevidenced decision cannot be re-checked by "
            "anyone, which is the entire reason this tool exists."
        )
    placeholders = ",".join("?" * len(evidence))
    known = {
        r["row_id"] for r in conn.execute(
            f"SELECT row_id FROM artifacts WHERE row_id IN ({placeholders})", tuple(evidence)
        ).fetchall()
    }
    missing = [r for r in evidence if r not in known]
    if missing:
        raise ValueError(
            f"unknown evidence row_id(s) {missing} — every row a decision rests on has to "
            f"exist in the store, or the decision is a claim with nothing behind it."
        )

    body: dict[str, Any] = {
        "decision": decision.strip(),
        "rationale": rationale.strip(),
        "revisit_if": revisit_if,
    }
    if question_id:
        # Fail before writing anything if the question is not one that can be
        # answered, rather than after: a decision with no question attached
        # is a perfectly good artifact, and worth keeping on its own terms.
        if get_question(conn, question_id) is None:
            raise ValueError(
                f"no question with id {question_id!r} in this store — pass the "
                f"question_id start_session or related_questions reported."
            )

    row_id = store.save_artifact(
        conn,
        type="decision",
        title=_clip(decision.strip(), _TITLE_CHARS),
        description=_clip(rationale.strip(), _DESCRIPTION_CHARS),
        content=body,
        content_format="json",
        session_id=session_id,
        parent_row_ids=evidence,
        status="result",
        source={"question_id": question_id} if question_id else None,
    )

    closed: dict | None = None
    if question_id:
        closed = close_question(
            conn, session_id=session_id, question_id=question_id, status="answered",
            artifact_row_id=row_id,
        )
    return {
        "row_id": row_id,
        "artifact_row_ids": [row_id],
        "decision": body["decision"],
        "evidence_row_ids": evidence,
        "question_id": question_id,
        "closed": closed,
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
