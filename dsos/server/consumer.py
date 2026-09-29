"""The consumer profile: the four tools a PM or reviewer uses to decide
whether to believe a number.

Four tools: find_evidence, get_claim, cite and ask (WP-F1). The daemon
mounts this profile beside the producer at /mcp/consumer/ (WP-D1), over the
same store, and `python -m dsos.mcp_server --profile consumer` proxies it
over stdio.

**This is the other direction through the same store, and the caller is a
different kind of agent.** Everything on the producer side is designed for a
caller that has the data, writes Python, and can judge its own output. This
one has none of that: it can read, and it has to decide whether to back a
number it did not compute. So these four tools are shaped around one
question — should I believe this? — and the design consequence runs through
all of them:

- **A warning is not a refusal, and a warning must be unmissable.** A tool
  that quietly returned a superseded row as though it were current would be
  worse than one that refused, because the reader has no way to notice. A
  tool that refused would be useless for the same reason from the other side:
  the reader still needs to know what the retracted number WAS, to explain
  the change to whoever quoted it. So a non-result row is returned in full
  and carries a `warning` string naming what is wrong with it, in the payload
  rather than in a field the caller has to know to check.
- **Nothing here is a second opinion.** find_evidence does not re-rank by
  how authoritative a row looks; it reports status and validation and lets
  the caller weigh them. A tool that decided which rows were believable would
  be making the judgement this profile exists to support.
- **One tool call should be enough to interpret a hit.** Every hit carries
  the finding, the numbers behind it, its validation verdict, the question it
  answered, and a link. A reader who has to make three more calls to find out
  whether a number is trustworthy will make two of them and guess.

`ask` is the only write. It creates work for a producer, which is a real
cost, and it is here deliberately: the alternative is a PM with no evidence
and no way to say so, who either guesses or goes around the store. There is
no rate limiting on it, and that is a known gap rather than an oversight —
nothing stops a loop from opening a hundred questions. The dedupe below is
what keeps the ordinary case cheap.

Tools take no `session_id` (D12): a reader has no session to manage, and
`ConsumerSessions` in `common.py` establishes and logs one per MCP client
session — see there for what that means on MCP 2026-07-28, which has no
sessions of its own.
"""

from __future__ import annotations

from fastmcp import FastMCP

from dsos import spine, store
from dsos.server.common import (
    CONSUMER_NAME,
    ConsumerSessions,
    ServerConfig,
    consumer_session_id,
    server_version,
)
from dsos.server.instructions import CONSUMER_INSTRUCTIONS

# How much of a text artifact find_evidence inlines. A narrative that is
# being cited for one sentence should not cost the reader the other two
# thousand, and get_claim is the call for the whole thing.
_EVIDENCE_TEXT_CHARS = 500

# Rows in a tabular hit. Enough to see whether a number is one row or a
# distribution, and few enough that five hits stay one readable payload.
_EVIDENCE_PREVIEW_ROWS = 5

# Per-cell clip, matching what present.py already does for a producer's
# preview: one 4000-character cell would otherwise blow up the payload
# through the back door of the 5-row preview.
_CELL_CHARS = 500


def build_consumer(config: ServerConfig) -> FastMCP:
    """A FastMCP server over the same store, carrying the four evidence tools.

    Like build_producer, a factory over a `ServerConfig` rather than a
    module: two consumers over two stores in one process is not a special
    case, and nothing here opens a connection or reads an environment
    variable at build time.

    `base_url` is the one thing the consumer reads off the config that the
    producer does not use: cite() splices it into the link it hands out, so a
    consumer behind a daemon can point at the GUI page and one on the stdio
    path (base_url None) falls back to a `dsos:` reference.
    """
    db = config.db
    mcp = FastMCP(
        CONSUMER_NAME, instructions=CONSUMER_INSTRUCTIONS, version=server_version()
    )
    mcp.add_middleware(ConsumerSessions(db))

    def url_for(row_id: str) -> str:
        """The GUI page for a row, where there is a daemon to serve it.

        `dsos:<row_id>` on the stdio path rather than a bare row_id: a
        reference has to be recognisable as a reference in a document that
        outlives the terminal it was pasted into, and "8 chars of hex" pasted
        into a deck reads as a typo.
        """
        if config.base_url:
            return f"{config.base_url}/artifacts/{row_id}"
        return f"dsos:{row_id}"

    def _cell(value):
        if isinstance(value, str) and len(value) > _CELL_CHARS:
            return f"{value[:_CELL_CHARS]}… ({len(value)} chars, clipped)"
        return value

    def _numbers(art: store.Artifact, url: str) -> dict:
        """The numbers behind a hit, shaped to what the artifact IS.

        Three cases because three different things are being asked of them. A
        table needs its shape and a few rows, or "activation fell to 0.4" is
        one number with no denominator. A chart cannot be inlined at all, so
        it gets a pointer and says so. Prose gets its opening, because a
        narrative is cited for a sentence and the sentence is at the top.
        """
        if art.content_format == "png":
            return {"note": f"a chart — open it at {url} to see it"}
        if art.content_error:
            return {"note": f"content is unreadable: {art.content_error}"}
        if isinstance(art.content, str):
            return {"text": art.content[:_EVIDENCE_TEXT_CHARS]}
        try:
            rows = art.content.head(_EVIDENCE_PREVIEW_ROWS)
            return {
                "row_count": int(len(art.content)),
                "columns": [str(c) for c in art.content.columns],
                "rows": [
                    {str(k): _cell(v) for k, v in row.items()}
                    for row in rows.to_dict(orient="records")
                ],
            }
        except AttributeError:
            return {"text": str(art.content)[:_EVIDENCE_TEXT_CHARS]}

    def _validation_warning(validation: dict) -> str | None:
        """Why this row should not be taken at face value, or None.

        A `contradicted` or `stale` verdict is the store saying out loud that
        the number is doubtful, and a consumer tool that passes it through as
        a plain string is the one place that warning gets lost: the reader
        asked whether to believe a number, not whether one exists. Both are
        therefore promoted out of the `validation` field and into a sentence,
        so a caller that reads only the top of the payload still sees it.
        """
        verdict = validation.get("current")
        if verdict == "contradicted":
            return (
                f"this result was later CONTRADICTED — do not quote it without "
                f"reading why: {validation.get('history') or 'no basis recorded'}"
            )
        if verdict == "stale":
            return (
                f"this result is STALE — it was built from an input that has "
                f"since gone out of date: {validation.get('stale_reason', 'no reason recorded')}"
            )
        return None

    def _status_warning(art: store.Artifact) -> str | None:
        """Why a row that is not a stated result should not be quoted as one."""
        if art.status == "superseded":
            replacement = art.superseded_by
            return (
                f"this row is SUPERSEDED — something replaced it"
                f"{f' (row {replacement})' if replacement else ''}, so it is no "
                f"longer the current answer. It is returned because a pinned "
                f"citation to it has to stay readable and explainable, not "
                f"because it is what to believe."
            )
        if art.status == "exploratory":
            return (
                "this row is EXPLORATORY — it is the output of a run nobody "
                "claimed, so it is a finding rather than a stated result. "
                "Treat it as a lead, not as an answer."
            )
        return None

    @mcp.tool()
    def find_evidence(claim: str, top_k: int = 5) -> dict:
        """Find the stated results in this store that speak to a claim, with the numbers behind them.

        Pass the claim as a reader would state it — a sentence, or the phrase
        they heard it as. Every hit carries the finding in one line, the
        numbers or text behind it, its current validation verdict, the
        question it answered, and a link to it, so one call is enough to
        decide whether to rely on it.

        Only claimed results are searched: the latest version of each
        finding, of type query/transform/chart/narrative/decision. Exploratory
        rows (a run nobody has claimed) and superseded rows (something
        replaced them) are left out, because "what should I believe" and
        "what have we got" are different questions. Datasets are excluded too
        — a dataset is a source, not a finding. get_claim still reads any of
        them, and says clearly when a row is not a stated result.

        Read `validation` on every hit before quoting it. A result later
        marked `contradicted` is still returned — hiding it would make this
        tool useless exactly when it matters — and is labelled in a `warning`
        that names the problem. A result built on an expired input is
        `stale`, and says which input.

        `match` says how a hit was found: `keyword` is a literal term match
        and is the stronger of the two, `semantic` means the embedding judged
        the two to be about the same thing and you should read it before
        repeating it. Semantic matches below a similarity floor are dropped
        rather than shown as weak evidence; raise or lower that floor with
        DSOS_EVIDENCE_FLOOR.

        An empty result means this store holds no stated result about the
        claim. That is a real answer, not a failure — use ask() to put the
        question to an analysis agent rather than guessing."""
        hits = store.find_evidence(db.conn(), claim, top_k=top_k)
        conn = db.conn()
        results = []
        for art, score, match in hits:
            art = store.get_artifact_by_row_id(conn, art.row_id, load_content=True)
            validation = store.validation_status(conn, art.row_id)
            hit = {
                "row_id": art.row_id,
                "type": art.type,
                "title": art.title,
                "finding": art.description,
                "status": art.status,
                "validation": validation["current"],
                "caveats": art.caveats or [],
                "confidence": art.confidence or [],
                "question": store.answered_question(conn, art.row_id),
                "created_at": art.created_at,
                "url": url_for(art.row_id),
                "match": match,
                "score": round(score, 3),
                "numbers": _numbers(art, url_for(art.row_id)),
            }
            warning = _validation_warning(validation)
            if warning:
                hit["warning"] = warning
            results.append(hit)
        if not results:
            return {
                "results": [],
                "hint": "No stated result covers this. ask(question) requests an analysis.",
            }
        return {"results": results, "artifact_row_ids": [r["row_id"] for r in results]}

    @mcp.tool()
    def get_claim(row_id: str) -> dict:
        """Get one result in full: what it says, how well it is backed, and how it was computed.

        Use this on a row_id from find_evidence, cite, or ask's board, when
        you need to judge the claim rather than just see it. It answers three
        questions in one call: what the result actually says (`description`,
        plus the numbers), how much to trust it (`status`, `caveats`,
        `confidence`, `validation` with its full history), and where it came
        from (`derivation`).

        `derivation` is the claim and everything it was built from, in the
        order it was built — the source dataset first, the claim last. Each
        step carries its `source` (the URL a dataset was fetched from, and
        when) and the `kind` and `code` of the run that produced it. A step
        with no code was registered by hand rather than computed, which is
        itself provenance: it means nobody wrote code that produced it.

        `status` is the lifecycle, and a row that is not a stated result is
        returned in full rather than refused — a pinned citation to a
        superseded number has to stay explainable — but it carries a
        `warning` naming what is wrong with it, and a superseded row also
        reports `superseded_by`, the row that replaced it. Read that warning
        before quoting anything this returns.

        `validation` is the current verdict plus the whole append-only
        history with each verdict's basis, because a verdict without its
        reasoning is a label you cannot argue with. `question` is what the
        claim was computed to answer."""
        conn = db.conn()
        art = store.get_artifact_by_row_id(conn, row_id, load_content=True)
        if art is None:
            return {"error": f"no artifact with row_id {row_id!r}", "artifact_row_ids": []}
        validation = store.validation_status(conn, art.row_id)
        payload = {
            "row_id": art.row_id,
            "artifact_id": art.artifact_id,
            "version": art.version,
            "type": art.type,
            "title": art.title,
            "finding": art.description,
            "tags": art.tags,
            "content_format": art.content_format,
            "status": art.status,
            "caveats": art.caveats or [],
            "confidence": art.confidence or [],
            "validation": validation,
            "derivation": store.derivation_steps(conn, art.row_id),
            "question": store.answered_question(conn, art.row_id),
            "created_at": art.created_at,
            "url": url_for(art.row_id),
            "numbers": _numbers(art, url_for(art.row_id)),
            "artifact_row_ids": [art.row_id],
        }
        if art.superseded_by:
            payload["superseded_by"] = art.superseded_by
        warning = _status_warning(art) or _validation_warning(validation)
        if warning:
            payload["warning"] = warning
        return payload

    @mcp.tool()
    def cite(row_id: str) -> dict:
        """Turn a result into a reference you can paste into a document, with its standing attached.

        Returns a `reference` line for a write-up, deck or email, plus the
        `url` it resolves to (the artifact's page in the GUI, or a `dsos:`
        reference where this server is not reachable over HTTP), the row's
        `status`, and its current `validation` verdict.

        Cite is not a gate. Passing a row that is not a stated result returns
        a reference and a `warning` rather than an error, because the case
        that needs citing is often the one that changed: you have to be able
        to say what the old number was, what replaced it, and why. What you
        must not do is paste the reference and drop the warning — a
        `contradicted` or `stale` verdict means do not quote this without
        reading the basis recorded with it.

        Get the row's own caveats and derivation with get_claim before you
        cite it, if the claim is load-bearing."""
        conn = db.conn()
        art = store.get_artifact_by_row_id(conn, row_id, load_content=False)
        if art is None:
            return {"error": f"no artifact with row_id {row_id!r}", "artifact_row_ids": []}
        validation = store.validation_status(conn, art.row_id)
        payload = {
            "row_id": art.row_id,
            "reference": f"{art.title} — dsos {art.row_id[:8]}, {art.created_at[:10]}",
            "url": url_for(art.row_id),
            "status": art.status,
            "validation": validation,
            "artifact_row_ids": [art.row_id],
        }
        if art.superseded_by:
            payload["superseded_by"] = art.superseded_by
        warning = _status_warning(art) or _validation_warning(validation)
        if warning:
            payload["warning"] = warning
        return payload

    @mcp.tool()
    def ask(question: str, context: str | None = None) -> dict:
        """Put a question this store cannot answer to an analysis agent, and return its id.

        Use this when find_evidence comes back empty, or when what it returns
        does not actually settle the question. It creates an OPEN question on
        the store's coordination board, which the next analysis session sees
        in its `related_questions` — that is the whole hand-off, and it takes
        minutes to hours rather than being a synchronous computation.

        `context` is optional and is stored with the question as its
        hypothesis: what you already know, what decision the answer is for,
        and what you have already ruled out. It is the difference between a
        producer picking this up in one pass and starting over, and it is
        worth writing even when you have nothing.

        Asking the same question twice returns the existing question instead
        of opening a second one, so a retry or a re-read costs nothing and
        the board stays readable. Comparison is on the normalised text — case,
        whitespace and a trailing question mark do not make a new question —
        and it applies to questions that are open or being worked on right
        now. An answered question is not a duplicate: asking again means you
        want it looked at afresh, which is a new question.

        This is a request, not a reservation. Nothing is queued behind it and
        no agent is committed to picking it up; it is the one write this
        profile has, and it is a real cost to the analysis side, so ask once
        with the context attached rather than several times without it."""
        if not question or not question.strip():
            return {
                "error": "ask needs the question itself — it is what an analysis agent "
                         "picks up and what the board matches on.",
                "artifact_row_ids": [],
            }
        conn = db.conn()
        wanted = spine.normalise_question_text(question)
        with db.write() as conn:
            # Dedupe on the same normalisation spine's board uses, rather
            # than a second spelling of it: a consumer's "Which team won q3?"
            # and a producer's "which  team won q3" are one question, and two
            # spellings of that rule is the drift TTD U4 is about.
            for existing in store.unfinished_questions(conn):
                if spine.normalise_question_text(existing["question"]) == wanted:
                    return {
                        "question_id": existing["id"],
                        "status": existing["status"],
                        "deduplicated": True,
                        "note": f"an unfinished question already asks this (asked "
                                f"{existing['created_at'][:10]}). It is on the board; "
                                f"no second one was opened.",
                    }
            question_id = spine.create_question(
                conn, question=question, status="open",
                hypothesis=context, asked_by=consumer_session_id(),
            )
        return {"question_id": question_id, "status": "open"}

    return mcp
