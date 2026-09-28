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

# `store._new_id` / `store._now` are borrowed privately, and deliberately:
# the question table's ids and timestamps have to be the same shape the
# rest of the store's rows use, and duplicating a uuid4 and an isoformat to
# get a "different" id generator would be a store whose two halves cannot
# be reasoned about together.
#
# The AND-prefix FTS rule is deliberately NOT borrowed. It is right for
# `search_artifacts` and wrong for this board — see `_board_fts_query` and
# the docstring on `related_questions` for the argument, because that
# argument is the reason and a future reader needs it more than the rule.
# TTD U4 is about promoting that rule out of store.py for artifact search,
# which now has one caller again; nothing here depends on how that lands.

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

# How long a question may sit with no activity before the sweep inside
# start_session abandons it. Read per call for the same reason the lease
# TTL is: a test and an operator both need to change it on a live server.
ABANDON_DAYS_ENV = "DSOS_ABANDON_DAYS"
DEFAULT_ABANDON_DAYS = 14.0

# Questions this board is willing to show. `abandoned` is the one status
# that never appears: a question nobody finished and nobody holds is not
# information, it is noise on the one screen an agent reads before it
# starts working.
_BOARD_STATUSES = ("in_progress", "open", "answered")

# Question words, filler and pronouns, dropped before the board builds a
# query. A question is a sentence, so most of its tokens are scaffolding —
# and an OR'd match over scaffolding matches nearly every row on the board,
# which would leave the ranking to do all the work and turn "related" into
# "any question with a preposition in it".
#
# Short and English-only on purpose. It is not a general-purpose stopword
# list and must not grow into one: a word that looks like filler here is
# often the whole question in an analysis store, and "most" is the obvious
# example — dropped, `which team scored the most on q3` matches every team
# scoring question equally well. Negation is kept for the same reason
# (`not` stays a content word), and each addition has to earn its place by
# being scaffolding in a question sentence.
_BOARD_STOPWORDS = frozenset({
    "a", "about", "actually", "all", "also", "am", "an", "and", "any", "are",
    "as", "at", "be", "been", "being", "both", "but", "by", "can", "could",
    "did", "do", "does", "each", "for", "from", "had", "has", "have", "he",
    "her", "here", "his", "how", "i", "if", "in", "into", "is", "it", "just",
    "me", "my", "of", "on", "or", "our", "she", "should", "so", "some", "than",
    "that", "the", "their", "there", "these", "they", "this", "those", "to",
    "up", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "why", "will", "with", "would", "you",
})

# How many rows the keyword pass pulls back before Python ranks them. The
# AND'd rule this replaced could only return a handful of rows, so a small
# multiple of `limit` was enough. An OR'd match returns every question that
# shares a single content word, and bm25 is now only a pre-filter for the
# ranking — so the window has to be wide enough for that ranking to have
# something to choose from, or it would discard exactly the well-matched
# rows it exists to promote. Affordable regardless: the window is bounded
# and a question is a row per top-level question, not per artifact.
_BOARD_CANDIDATE_FLOOR = 32
_BOARD_CANDIDATE_FACTOR = 8

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


def _content_terms(text: str) -> list[str]:
    """The words of a question that could tell it apart from another one:
    the same tokenisation `store._fts_query` uses, minus the question words
    and filler in `_BOARD_STOPWORDS`. Duplicates are left in — the caller
    that counts shared terms wants a set, and the one that builds a MATCH
    expression wants them all."""
    return [t for t in re.findall(r"\w+", (text or "").lower()) if t not in _BOARD_STOPWORDS]


def _board_fts_query(text: str) -> str:
    """The board's MATCH expression: every content word OR'd in, each as a
    prefix match so `migrat` still finds `migrates`.

    Built here rather than borrowed from `store._fts_query`, and it is not
    the same rule: that one ANDs every word, which for artifact titles is
    right and for questions means a paraphrase of the question in flight
    matches nothing at all (TTD U5). Each term is quoted, which
    `_fts_query` does not do — a question is free prose and can contain
    FTS5's own keywords, so an unquoted `NEAR` or `OR` in a question about
    a team's nearest rivals would be a syntax error the guard below
    swallows into a silently worse board. A quoted term with a trailing `*`
    is the same prefix match, just not read as syntax.

    Empty for text that is nothing but stopwords, which skips the keyword
    pass entirely and leaves the exact-text backstop to answer alone.
    """
    return " OR ".join(f'"{term}"*' for term in _content_terms(text))


def _board_candidate_window(limit: int) -> int:
    """How many candidate rows the keyword pass fetches for this `limit`."""
    return max(limit * _BOARD_CANDIDATE_FACTOR, _BOARD_CANDIDATE_FLOOR)


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


def abandon_days() -> float:
    """The abandon window, from `DSOS_ABANDON_DAYS` on every call. Like the
    lease TTL, an unset, unparseable or non-positive value falls back to the
    default rather than making every question instantly abandonable."""
    raw = os.environ.get(ABANDON_DAYS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_ABANDON_DAYS
    try:
        days = float(raw)
    except ValueError:
        return DEFAULT_ABANDON_DAYS
    return days if days > 0 else DEFAULT_ABANDON_DAYS


def sweep_abandoned(conn: sqlite3.Connection) -> int:
    """Abandon questions nobody has touched for `DSOS_ABANDON_DAYS`, and
    return how many were swept. Runs lazily inside every `start_session`
    (WP-G1) — there is no background job, because the daemon already wakes
    on every session start and a timer that runs when nothing is happening
    is a process nobody asked for.

    One UPDATE, not a Python loop over the question table: this is on the
    session-start hot path, and the `questions(status)` index (M3) keeps
    the candidate set to the open/in-progress rows.

    Activity is `max(created_at, claimed_at, the CLAIMANT's latest tool
    call)` — not any tool call by anyone. A question claimed by an agent
    that is still working is live even if its last action was minutes ago,
    and a call by some *other* session must not silently renew it.

    The lease is evaluated in the same statement, which is what keeps the
    two thresholds from contradicting each other: a question whose claim is
    still live is never swept, whatever `DSOS_ABANDON_DAYS` says. The
    default relationship already implies this (14 days >> 30 minutes), but
    an operator who sets the abandon window below the lease must still not
    have in-flight work cancelled underneath the agent holding it.
    """
    lease_cutoff = (_now() - timedelta(minutes=lease_minutes())).isoformat()
    abandon_cutoff = (_now() - timedelta(days=abandon_days())).isoformat()
    cursor = conn.execute(
        """
        UPDATE questions
        SET status = 'abandoned', closed_at = ?, claimed_by = NULL, claimed_at = NULL
        WHERE status IN ('open', 'in_progress')
          AND max(created_at,
                  COALESCE(claimed_at, created_at),
                  COALESCE((SELECT MAX(ts) FROM tool_calls
                            WHERE session_id = questions.claimed_by), created_at)) < ?
          AND (
            claimed_by IS NULL
            OR max(COALESCE(claimed_at, created_at),
                   COALESCE((SELECT MAX(ts) FROM tool_calls
                             WHERE session_id = questions.claimed_by), created_at)) <= ?
          )
        """,
        (store._now(), abandon_cutoff, lease_cutoff),
    )
    conn.commit()
    return cursor.rowcount


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

    **Recall beats precision here, and that is the whole design of the
    matcher.** A spurious related question costs an agent one glance at a
    row it can dismiss. A missed one means two agents run the same analysis
    and neither knows of the other — the exact failure this board exists to
    prevent, and the duplicate-work rate benchmark arm H3 is instrumented to
    measure. So the keyword path OR's the content words instead of AND'ing
    them, and a paraphrase of a question already on the board reaches it.

    The inverse is true of `search_artifacts`, which is why it still ANDs
    (`store._fts_query`): artifact titles are short and keyword-like, and
    search has a semantic fallback behind it, so precision buys something and
    recall does not have to. Questions are long sentences with no semantic
    fallback, and AND over two differently-phrased sentences almost never
    holds — "which player leads q3?" against a stored "is red really the q3
    leader?" shares one content word, which under AND matched nothing.
    **Do not "fix" the OR back to AND: that is the defect, not the defect's
    absence.** (TTD U5.)

    Two matching paths, and they are not redundant:

    - `questions_fts`, OR'd over content words and ranked below. It is the
      only path that sees `hypothesis`.
    - exact match on the normalised text. It is the backstop for questions
      FTS cannot see at all: a row written before this search table existed,
      or by anything that inserted into `questions` without its FTS row, is
      invisible to the JOIN and would otherwise never be reported. It is
      also the tie-winner inside a band, so the one question that is
      literally the same one is never crowded out by its own neighbours.
      The comparison is a scan of the question table in Python because
      SQLite has no case-folding or whitespace-collapsing function, which
      is affordable precisely because a question is a row per top-level
      question, not per artifact.

    Ranking is by how many DISTINCT content words a candidate shares, not
    by bm25: bm25 over short documents is not comparable across documents
    of different lengths, and "shares the most words with me" is the thing
    the board actually means. bm25 still orders the SQL fetch, purely as a
    cheap pre-filter over a deliberately wide candidate window.

    Ordered live in-progress, then open, then answered, and the ranking
    refines WITHIN a band rather than across them: a finished question that
    matches every word still sorts below live work, because the board's
    job is to stop duplicate work *now*. `exclude_id` is the caller's own
    question, which the board must never tell them about.
    """
    if not board_enabled():
        return []
    limit = max(int(limit), 0)
    wanted = normalise_question_text(text)
    if not wanted or limit == 0:
        return []
    wanted_terms = set(_content_terms(wanted))

    candidates: dict[str, sqlite3.Row] = {}
    fts_query = _board_fts_query(wanted)
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
                (fts_query, *_BOARD_STATUSES, _board_candidate_window(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            # Same guard as search_artifacts: raw question text can produce
            # FTS syntax the parser rejects, and the normalised pass below
            # still stands. `_board_fts_query` quotes its terms to make this
            # hard to trigger, not impossible.
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

    # What each candidate is worth to the caller: the exact-text flag, which
    # outranks the shared-word count inside a band, and the count itself —
    # DISTINCT content words shared, over the question and the hypothesis
    # together, because the hypothesis is indexed like the question and is
    # often where the substance is. A candidate sharing nothing and not
    # matching exactly is dropped: the FTS tokenizer and `_content_terms`
    # agree in practice, and a row we share no word with is not one we can
    # say anything useful about.
    scored: list[tuple[str, sqlite3.Row, bool, int]] = []
    for qid, row in candidates.items():
        exact = normalise_question_text(row["question"]) == wanted
        shared = len(wanted_terms & set(
            _content_terms(f"{row['question']} {row['hypothesis'] or ''}")
        ))
        if exact or shared:
            scored.append((qid, row, exact, shared))

    if not scored:
        return []

    # One lease evaluation per candidate, reused for the ranking and for the
    # expires_at the board reports — the lease is derived, so it costs a
    # query, and computing it twice per row would double that for no gain.
    leases = {qid: lease_state(conn, row) for qid, row, _, _ in scored}
    ordered = sorted(
        scored,
        key=lambda item: (
            _board_rank(item[1], leases[item[0]]["live"]),
            0 if item[2] else 1,
            -item[3],
            -(_as_utc(item[1]["created_at"]) or _now()).timestamp(),
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
        for _, r, _, _ in ordered
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
