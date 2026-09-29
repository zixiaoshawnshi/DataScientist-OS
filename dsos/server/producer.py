"""The producer profile: the twelve tools an analysis agent calls.

`build_producer(config)` is a factory, not a module. Every tool is a closure
over one `ServerConfig`, so two producers over two stores can exist in one
process — which is what the daemon (WP-D1) needs, and what the
module-global connection this replaced made impossible. Nothing here opens a
store: reads go through `config.db.conn()`, writes take
`config.db.write()` first, and both are the same `Database` the caller
passed in.

The tool bodies are unchanged from `dsos/mcp_server.py`, which is the point
of that WP: same names, same parameters, same responses — the docstrings
are most of the product, since an agent decides what to call from them,
and they carry the lifecycle parameters and the spine's question tools.

Docstring convention (WP-C2's follow-up, adopted here): every tool's
docstring opens with ONE unwrapped summary line — a complete sentence on a
single physical line — and the prose beneath it is hard-wrapped. The
contract generator renders that first sentence into Doc II's tool table, so
a docstring that starts mid-sentence forces it into sentence-splitting
heuristics, and the 7 "e.g."s in the old docstrings each needed an
abbreviation guard to avoid being cut in half. Line 1 is the whole
contract; the rest is the manual.
"""

from __future__ import annotations

from fastmcp import FastMCP

from dsos import execution, ingest, present, spine, store, templating
from dsos.server.common import (
    PRODUCER_NAME,
    ServerConfig,
    ToolCallLogger,
    execution_response,
    server_version,
    with_inline_image,
)
from dsos.server.instructions import PRODUCER_INSTRUCTIONS


def build_producer(config: ServerConfig) -> FastMCP:
    """A FastMCP server over `config.db`, carrying the producer tools.

    Pure: it registers tools and returns. It touches no file, opens no
    connection and reads no environment variable, so building one is free
    and building two over different stores is not a special case.
    """
    db = config.db
    mcp = FastMCP(
        PRODUCER_NAME, instructions=PRODUCER_INSTRUCTIONS, version=server_version()
    )
    mcp.add_middleware(ToolCallLogger(db))

    def _artifact_payload(art: store.Artifact) -> dict:
        return present.artifact_payload(db.conn(), art)

    @mcp.tool()
    def start_session(question: str, question_id: str | None = None) -> dict:
        """Start a new round of work, and learn what this store already holds.

        Call this once, first, for every new top-level question — before
        searching, fetching, or running anything. Reuse the returned
        session_id in every other tool call for this round.

        The response also reports `prior_work`: what this store already
        holds from earlier sessions, plus a few candidates that may already
        cover this question. Read it before you fetch anything. If a
        candidate fits, run_sql/run_python against its row_id instead of
        downloading and cleaning the same data again — that is the whole
        point of the store, and an agent that never looks will redo work
        that is already done.

        `related_questions` is the coordination board: the OTHER questions in
        this store that match what you asked — someone's work in flight (with
        a live lease, so you can see it is actually being touched), questions
        asked and waiting, and questions already answered, each with the row
        that answered it. Read it as a duplicate-work warning: an in_progress
        entry with a live lease means somebody is on this right now, so use
        their row or pick a different question rather than repeating the work.
        An answered entry with an artifact_row_id is the best outcome — that
        answer already exists, so reuse it instead of computing it again.

        Pass `question_id` (one this store reported, e.g. from a consumer's
        ask, or from related_questions) to CLAIM that question instead of
        starting a new one — you are working on existing work rather than
        opening a new line. The claim fails if another session currently
        holds it, with its last activity time, so you can either go and use
        the related work or wait for the lease to lapse. A claim needs no
        maintenance: it stays live while you keep calling tools, and goes
        stale on its own if you stop."""
        try:
            with db.write() as conn:
                # The abandon sweep first (WP-G1): a question nobody has
                # touched for DSOS_ABANDON_DAYS is finished, not in flight,
                # and a question_id claimed against one that is already
                # abandoned should fail as it would for any finished
                # question. It is one indexed UPDATE, and it never touches a
                # live claim (see spine.sweep_abandoned).
                spine.sweep_abandoned(conn)
                # The claim is checked BEFORE the session row is created, so
                # a refused claim leaves nothing behind — no empty session in
                # the GUI, and nothing for a later tool call to be logged
                # against. claim_question re-checks it, which is the
                # invariant every caller gets for free; the second
                # evaluation costs nothing because the write lock is held.
                if question_id is not None:
                    spine.check_claimable(conn, question_id)
                session_id = store.start_session(conn, question)
                if question_id is None:
                    # A question nobody else has asked: the session both
                    # asked it and holds the claim, so the board immediately
                    # says who is on it.
                    question_id = spine.create_question(
                        conn, question=question, status="in_progress",
                        asked_by=session_id, claimed_by=session_id,
                    )
                else:
                    spine.claim_question(conn, question_id, session_id=session_id)
                conn.execute(
                    "UPDATE sessions SET question_id = ? WHERE id = ?",
                    (question_id, session_id),
                )
                # Committed here rather than left to the caller: the write
                # lock is a process-wide Python lock, but the SQLite
                # transaction on this thread's connection is not released
                # when the tool returns it to the pool. An uncommitted write
                # here would keep a RESERVED lock on the file and lock out
                # every OTHER thread's write.
                conn.commit()
        except ValueError as exc:
            return {"error": str(exc), "artifact_row_ids": []}
        # Both reads are deliberately outside the write lock: they rank and
        # walk the question table, and the lock exists to order writers.
        return {
            "session_id": session_id,
            "question_id": question_id,
            **store.prior_work_signal(db.conn(), question),
            "related_questions": spine.related_questions(
                db.conn(), question, exclude_id=question_id
            ),
        }

    @mcp.tool()
    def close_question(
        session_id: str, question_id: str, status: str,
        artifact_row_id: str | None = None, note: str | None = None,
    ) -> dict:
        """Close the question you have been working on, and say what answered it.

        status is "answered" or "abandoned". "answered" REQUIRES
        artifact_row_id: the result row that answers the question, which the
        board then hands to every later session that asks the same thing. An
        exploratory row is refused — a question closed against a finding
        nobody claimed is a claim nobody made, and the board would repeat it
        to everyone. Claim the row first with mark(row_id=..., status="result")
        if it really is the answer. "abandoned" is for work you are stopping
        without an answer, and releases the question for someone else to
        claim.

        Closing clears the claim and records the time, so the question stops
        reading as in-flight. You do not have to hold the claim to close it:
        a finished question is a fact about the store, not a permission. A
        question that is already answered or abandoned cannot be closed
        again — its answer is kept; start a new question if it is wrong.

        `note` is returned in the response and recorded in your tool-call
        trace; it is not stored on the question, so put anything that must
        outlive this round into the answer row itself.
        """
        try:
            with db.write() as conn:
                result = spine.close_question(
                    conn, session_id=session_id, question_id=question_id, status=status,
                    artifact_row_id=artifact_row_id, note=note,
                )
        except ValueError as exc:
            return {"error": str(exc), "artifact_row_ids": []}
        return {**result, "artifact_row_ids": [result["artifact_row_id"]] if result.get(
            "artifact_row_id") else []}

    @mcp.tool()
    def record_decision(
        session_id: str, decision: str, rationale: str, evidence_row_ids: list[str],
        revisit_if: str | None = None, question_id: str | None = None,
    ) -> dict:
        """Record a call you made and why, linked to the evidence it rests on.

        Use this when you decided something — a method, a definition, a
        reading, a threshold — that a later question would otherwise redo or
        silently contradict. A decision is saved as an artifact, so search
        finds it and get_lineage answers "what was this based on":
        evidence_row_ids becomes its lineage.

        evidence_row_ids is REQUIRED and every row in it must exist. A
        decision is the one artifact whose whole value is that something
        else backs it, so a decision with no evidence is refused rather than
        stored.

        rationale is the reason, in a sentence or two — what the call rests
        on, not a restatement of the call. revisit_if is the condition that
        would reopen it ("if the season table is refetched with q4 in it");
        leave it out if nothing would.

        Pass question_id to also close that question as answered by this
        decision — the decision is written first, so the question can never
        point at a row that does not exist. The question must still be open
        or in progress; if it is already closed the call is refused before
        the decision is written — drop question_id to record it on its own.
        """
        try:
            with db.write() as conn:
                return spine.record_decision(
                    conn, session_id=session_id, decision=decision, rationale=rationale,
                    evidence_row_ids=evidence_row_ids, revisit_if=revisit_if,
                    question_id=question_id,
                )
        except ValueError as exc:
            return {"error": str(exc), "artifact_row_ids": []}

    @mcp.tool()
    def search_artifacts(
        query: str, session_id: str, top_k: int = 5, type: str | None = None,
        include_superseded: bool = False, include_exploratory: bool = False,
    ) -> dict:
        """Search every artifact saved so far, across every past session.

        This is the first call for any question, before fetching new data or
        rebuilding anything. An exact term — a title, a column name —
        reliably matches even with the fallback embedding model, and a vaguer
        query still finds something via semantic similarity.

        Each result carries `status` (see below), `created_at`, the current
        `validation` verdict and, on a superseded row, `superseded_by`. Read
        them before reusing a row: a superseded row is one something replaced,
        and a `contradicted` or `stale` validation is one a human or an
        expired input already called into question.

        Statuses: "exploratory" is the output of a run that nobody has
        claimed yet (what a fresh run of yours will be), "result" is a
        deliberate registration, and "superseded" is a row something
        replaced. Superseded rows are left out of this search unless you
        pass include_superseded=True — they are still readable with
        get_artifact, so nothing is lost, they are just not the answer to
        "what do I have". Exploratory rows are left out by default too: an
        unclaimed finding is a lead, not a result, so pass
        include_exploratory=True to search them. This is why a run of your
        own is invisible until you claim it with mark(status="result").

        Workflow skills and templates are not results and are left out
        unless you pass type="skill" or type="template" for one."""
        hits = store.search_artifacts(
            db.conn(), query, top_k=top_k, type=type,
            include_superseded=include_superseded,
            include_exploratory=include_exploratory,
        )
        conn = db.conn()
        results = [
            {
                "row_id": a.row_id, "type": a.type, "title": a.title,
                "description": a.description, "score": round(score, 3),
                "status": a.status, "created_at": a.created_at,
                "validation": store.validation_status(conn, a.row_id)["current"],
                "superseded_by": a.superseded_by,
            }
            for a, score in hits
        ]
        return {"results": results, "artifact_row_ids": [r["row_id"] for r in results]}

    @mcp.tool()
    def get_artifact(row_id: str, session_id: str) -> dict:
        """Fetch one artifact's full metadata and content, by row_id.

        Use the row_id that save_artifact / run_sql / run_python /
        search_artifacts gave you — that row_id is the only id you need;
        there's no separate "artifact_id" to look up.

        Tabular artifacts (dataset/query/transform) return row_count,
        columns, and a 10-row preview, not the full table — use
        run_sql/run_python against this row_id to compute over the full data.
        `uses` lists what this artifact was built from or embeds (e.g. a
        narrative's datasets).

        The response also carries the artifact's lifecycle: `status`
        ("exploratory" / "result" / "superseded"), its `caveats` and
        `confidence` claims, and its `validation` — the current verdict plus
        the full history with each verdict's basis. A superseded row is
        still returned in full, with `superseded_by` naming the row that
        replaced it, because a pinned row_id in a report or a citation has
        to stay readable and traceable after it stops being current.

        Note: run_sql/run_python already return this same preview inline in
        their own response — call get_artifact only to re-fetch something
        from an earlier tool call (e.g. a search_artifacts hit), not right
        after running it yourself.

        Example: get_artifact(row_id="a1b2c3...", session_id="s1")
        """
        art = store.get_artifact_by_row_id(db.conn(), row_id)
        if art is None:
            return {"error": f"no artifact with row_id {row_id!r}", "artifact_row_ids": []}
        payload = {**_artifact_payload(art), "artifact_row_ids": [art.row_id]}
        return with_inline_image(payload, art)

    @mcp.tool()
    def save_artifact(
        type: str,
        title: str,
        description: str,
        content_format: str,
        session_id: str,
        content_text: str | None = None,
        content_path: str | None = None,
        tags: list[str] | None = None,
        source: dict | None = None,
        parent_row_ids: list[str] | None = None,
        dedupe: bool = True,
        status: str = "result",
        caveats: list[str] | None = None,
        confidence: list[dict] | None = None,
    ) -> dict:
        """Register something as a real artifact, so future work can find and reuse it.

        A dataset you fetched isn't real to this system — invisible to search,
        lineage, and reuse for every future question — until you call this.

        Pass exactly one of:
        - content_text: inline text, for markdown/python/sql/json content.
          `content_format` must describe it (markdown, python, sql, json, ...).
        - content_path: a local file you already produced with your own tools,
          as an ABSOLUTE path — a relative one resolves against this server's
          working directory, not yours. For type="dataset" the format is read from the file extension —
          .csv/.tsv/.json/.parquet are all accepted and normalized to parquet
          internally; whatever you pass as content_format is ignored. For other
          types the file is read as text and content_format describes it.

        `description` must be a real 1-2 sentences (what this is, why it
        matters) — search_artifacts ranks on it, so a vague description makes
        this artifact unreachable to future questions.

        `status` is the lifecycle, and defaults to "result" because calling
        this tool at all is a claim: you are registering this as something a
        later question can rely on. Pass "exploratory" when you are parking a
        working artifact you have not checked yet. (run_sql/run_python
        default to "exploratory" instead — computed output is a finding until
        someone claims it. Use mark to promote one of those to a result.)

        `caveats`: up to 5 short strings (200 chars each) naming properties
        of THIS result a reader would otherwise get wrong — "excludes
        refunds", "one day of data only", "column renamed upstream". Anything
        longer than that is not a caveat, it is the analysis: write it as a
        narrative artifact instead of compressing it into one.

        `confidence`: a list of {claim, level, basis}. `level` is "high",
        "medium" or "low", and `basis` is REQUIRED and must be non-empty —
        say what the level rests on (the check you ran, the source, the
        sample size). A level with no basis is refused rather than stored:
        it is a label no later reader can check, and a store full of
        unsupported "high" is worth less than one with no field at all.

        For a narrative: write `{{artifact:<row_id>}}` in content_text for each
        dataset/chart/query it discusses. Those row_ids are automatically added
        to this artifact's lineage — no need to also pass parent_row_ids for
        them — and show up as `uses` when this narrative is fetched later.

        `source`: freeform, but for a fetched dataset prefer {"url": ...,
        "fetched_at": ... (an ISO timestamp for when), "method": ... (how it
        was fetched, e.g. "WebFetch"/"curl"/"Kaggle API"), "refresh_after":
        ... (how long this stays fresh, e.g. "7d"/"30d"/"static" — your
        call, not enforced)} plus whatever else identifies it. This is the
        only provenance a later session/report has to go on, and the only
        record of how/when this was retrieved: dsos can't see a fetch you ran
        with your own tools, only what you put here. `refresh_after` is read
        back: when a dataset's window has passed, anything built from it is
        reported as `stale` — computed on read from your `fetched_at`, never
        written, and never overriding a "contradicted" verdict. Use
        "static" (or leave it out) for data that does not expire.

        The response's `input_tables` (on run_sql/run_python) maps each input
        row_id to the name it was bound under — in_1, in_2, ... in the order
        you passed input_row_ids.

        Registering the same content twice is collapsed: if this exact content
        is already in the store, you get that artifact's existing row_id back
        (with deduplicated=true) instead of a near-duplicate that would then
        show up twice in every future search. Pass dedupe=False only when you
        genuinely want a second copy — same data deliberately re-registered
        under a new name or source.
        """
        # Checked before the dedupe lookup, not left to store.save_artifact:
        # a status="superseded" save whose content matches a current result
        # would otherwise come back as a successful dedupe instead of the
        # error saying superseded is mark's job.
        if status not in store.WRITABLE_STATUSES:
            return {
                "error": f"status must be one of {list(store.WRITABLE_STATUSES)}, got "
                         f"{status!r}. A row becomes superseded through "
                         f"mark(status='superseded', superseded_by=...).",
                "artifact_row_ids": [],
            }
        # Reading the file is local I/O, not a store operation, so it happens
        # before the write lock is taken.
        if content_path:
            try:
                content, content_format = ingest.ingest_path(content_path, type, content_format)
            except Exception as exc:
                return {"error": str(exc), "artifact_row_ids": []}
        elif content_text is not None:
            content = content_text
        else:
            return {"error": "must pass content_text or content_path", "artifact_row_ids": []}

        # The dedupe lookup and the insert are one critical section on
        # purpose. They are a read-then-write, which is the one shape
        # SQLite's busy_timeout cannot cover at all (a transaction holding a
        # read snapshot that then tries to write gets SQLITE_BUSY back
        # immediately) — the same hazard store.save_artifact's version read
        # has. One lock around both is what makes "two agents registering
        # the same content at once" collapse to one row instead of failing.
        with db.write() as conn:
            if dedupe:
                existing = store.find_by_content_hash(
                    conn, type, store._content_hash(content, content_format)
                )
                if existing is not None:
                    art = store.get_artifact_by_row_id(conn, existing)
                    return {
                        "row_id": existing,
                        "artifact_row_ids": [existing], "deduplicated": True,
                        "note": f"identical {type} already in the store as {art.title!r} "
                                f"(row {existing}); reused it instead of registering a duplicate. "
                                f"Search or run_sql against it directly. Pass dedupe=False to "
                                f"register a separate copy anyway.",
                    }

            try:
                row_id = store.save_artifact(
                    conn, type=type, title=title, description=description, content=content,
                    content_format=content_format, session_id=session_id, tags=tags, source=source,
                    parent_row_ids=parent_row_ids, status=status, caveats=caveats,
                    confidence=confidence,
                )
            except ValueError as exc:
                return {"error": str(exc), "artifact_row_ids": []}
        return {
            "row_id": row_id,
            "artifact_row_ids": [row_id],
        }

    @mcp.tool()
    def run_sql(
        code: str, session_id: str, title: str, description: str, input_row_ids: list[str],
        scratch: bool = False, status: str = "exploratory",
    ) -> dict:
        """Run SQL (DuckDB) against one or more artifacts, and save the result.

        Each input row_id is registered as a table named in_1, in_2, ... in
        the order you list them in input_row_ids — the response's
        `input_tables` says which row_id got which name. The names are
        positional, never derived from the title, so two inputs with the same
        title, a title starting with a digit, and an input you later
        re-titled all behave the same.

        The result is returned inline below (row_count, columns, a 10-row
        preview) — you do NOT need a second call to see it. It's also saved
        as a new `query` artifact, automatically lineage-linked to every
        input and recorded — no separate save_artifact call needed.

        `status` is the saved row's lifecycle: "exploratory" (the default) for
        a query that is a finding, "result" for one you are asserting as an
        answer. Computation is exploratory until someone claims it, so leave
        it alone unless the query IS the answer; to claim a row you saved
        earlier, call mark(row_id=..., status="result").

        On failure, status="error" and `error`/`stdout`/`stderr` below show
        why; a failed run produces no artifact, so that response carries an
        `execution_id` and no `row_id`. (That `status` is the RUN's outcome;
        the row's lifecycle state, when there is one, is `artifact_status`.)

        scratch=True: run and return the result inline but persist nothing —
        no artifact, no lineage, not searchable. For quick checks (row counts,
        schema pokes) where an artifact would be noise. The run still appears
        in the session's tool-call trace. A scratch run is not stored anywhere
        to promote from: if the result turns out to be worth keeping, re-run
        it with scratch=False.

        On failure, the error includes each registered input as
        `in_k "<title>" (columns)`, so a column/table typo is fixable from the
        error alone.

        Example:
          run_sql(
            code="SELECT team, score FROM in_1 WHERE score > 10",
            session_id="s1", title="High scorers",
            description="Teams scoring above 10.",
            input_row_ids=["<row_id of the toy_scores dataset artifact>"],
          )
        """
        # Checked before anything runs: a status the row cannot be saved in
        # used to run the query first and fail (or worse, save a superseded
        # row) afterwards.
        try:
            execution.check_run_status(status)
        except ValueError as exc:
            return {"error": str(exc), "artifact_row_ids": []}
        if scratch:
            # Scratch persists nothing — no write to serialise, so a quick
            # check never blocks another writer.
            return execution.run_sql(
                db.conn(), code=code, session_id=session_id, title=title, description=description,
                input_row_ids=input_row_ids, scratch=scratch,
            )
        # The query runs with no lock held; `persist=db.write` takes the
        # write lock only around the tail that saves the artifact and
        # records the execution, which is where the D13 read-then-write is.
        # Holding it for the whole run stalled every other client's calls
        # (the tool-call log takes the same lock) for as long as it ran.
        outcome = execution.run_sql(
            db.conn(), code=code, session_id=session_id, title=title, description=description,
            input_row_ids=input_row_ids, scratch=scratch, status=status, persist=db.write,
        )
        return execution_response(db.conn(), outcome, input_row_ids)

    @mcp.tool()
    def run_python(
        code: str, session_id: str, title: str, description: str, input_row_ids: list[str],
        output_type: str = "transform", scratch: bool = False,
        requirements: list[str] | None = None, code_paths: list[str] | None = None,
        style: str | None = "dsos", python_path: str | None = None,
        status: str = "exploratory",
    ) -> dict:
        """Run Python against one or more artifacts, and save the result.

        Each input row_id is bound to a variable named in_1, in_2, ... in the
        order you list them in input_row_ids (the response's `input_tables`
        says which row_id got which name), and every input is also reachable
        by id as `inputs["<row_id>"]`; `pd` (pandas) is available. The names
        are positional, never derived from the title, so two inputs with the
        same title, a title starting with a digit, and an input you later
        re-titled all behave the same.

        Your code MUST assign a `result` variable — a DataFrame, a dict/list
        (saved as JSON), a string, or for output_type="chart" a
        matplotlib Figure/Axes (whatever `plt.gcf()`/`plt.subplots()` gives
        you — it's rendered to PNG for you) or raw png bytes directly. On a
        persisted (non-scratch) call the result is returned inline below as
        an actual rendered image for a chart (not just a text placeholder) —
        you do NOT need a second call to see it. It's also saved as a new
        artifact (default type="transform"; pass output_type="chart" for a
        plot), lineage-linked to every input and recorded automatically.

        `status` is the saved row's lifecycle: "exploratory" (the default) for
        a transform that is a finding, "result" for one you are asserting as
        an answer. To claim a row you saved earlier, call
        mark(row_id=..., status="result").

        On failure, status="error" and `error`/`stdout`/`stderr` below show
        the traceback and anything printed before it failed; a failed run
        produces no artifact, so that response carries an `execution_id` and
        no `row_id`. (That `status` is the RUN's outcome; the row's lifecycle
        state, when there is one, is `artifact_status`.)

        scratch=True: run and return the result inline but persist nothing —
        no artifact, no lineage, not searchable (a chart still comes back as a
        text placeholder in scratch mode, not a rendered image — drop scratch
        once you're iterating on plot styling, to see it rendered). For rapid
        iteration (check a correlation, test an idea) where an artifact would
        be noise. The run still appears in the session's tool-call trace. A
        scratch run is not stored anywhere to promote from: once the idea
        works, re-run it with scratch=False so it is saved as a real artifact.

        Your code runs against python_path (below) — usually your own analysis
        Python, so whatever's already installed there just works. requirements=
        ["scikit-learn>=1.3", ...] fills gaps in that interpreter for THIS run
        only: uv resolves them into a throwaway environment layered on top of
        it, runs the code there, and discards it; python_path itself is never
        modified. Anything uv accepts works: PyPI specs, local package paths,
        wheels, git URLs (path-style entries need a filesystem shared with this
        server). First run pays the download, repeat runs ~1s (globally
        cached). Slower than the default path — only pass requirements when
        you actually need them.

        code_paths=["/abs/dir", ...]: directories holding your own unpackaged
        .py modules to import from — works with or without requirements. Use
        ABSOLUTE paths: a relative one resolves against this server's working
        directory, not yours.

        style: the chart style (the consistency layer) applied to rcParams
        BEFORE your code runs — "dsos" (the dark house style, the default),
        "report" (the same look sized for charts embedded in published reports
        — use it for charts a narrative will embed), "minimal" (bare light
        style), a custom chart-style template's row_id/artifact_id, or None for
        raw matplotlib defaults. Style never leaks between runs. An unknown
        name fails this one call, listing the built-ins and the custom
        chart-style templates in this store.

        python_path: overrides the server default for THIS call only — e.g.
        point it at a specific repo's .venv interpreter to run against that
        project's exact dependencies, instead of whatever DSOS_PYTHON_PATH (or
        this server's own interpreter, if that's unset) has installed. An
        ABSOLUTE path, for the same reason as code_paths.

        Example:
          run_python(
            code="result = in_1.groupby('team')['score'].mean().reset_index()",
            session_id="s1", title="Average score by team",
            description="Mean score per team.",
            input_row_ids=["<row_id of the toy_scores dataset artifact>"],
          )
        """
        # Before the subprocess, for the same reason as run_sql's check.
        try:
            execution.check_run_status(status)
        except ValueError as exc:
            return {"error": str(exc), "artifact_row_ids": []}
        if scratch:
            return execution.run_python(
                db.conn(), code=code, session_id=session_id, title=title, description=description,
                input_row_ids=input_row_ids, output_type=output_type, scratch=scratch,
                requirements=requirements, code_paths=code_paths, style=style,
                python_path=python_path or config.python_path,
            )
        # The subprocess — up to DSOS_SANDBOX_TIMEOUT — runs unlocked; only
        # the persist tail takes the write lock (see run_sql).
        outcome = execution.run_python(
            db.conn(), code=code, session_id=session_id, title=title, description=description,
            input_row_ids=input_row_ids, output_type=output_type, scratch=scratch,
            requirements=requirements, code_paths=code_paths, style=style,
            python_path=python_path or config.python_path, status=status, persist=db.write,
        )
        return execution_response(db.conn(), outcome, input_row_ids)

    @mcp.tool()
    def get_lineage(row_id: str, session_id: str, direction: str = "ancestors") -> dict:
        """See what an artifact was built from, or what was built from it.

        direction="ancestors" (the default) lists what this artifact was built
        from; direction="descendants" lists what has been built from it.
        """
        arts = store.get_lineage(db.conn(), row_id, direction=direction)
        results = [{"row_id": a.row_id, "type": a.type, "title": a.title} for a in arts]
        return {"results": results, "artifact_row_ids": [row_id, *(a.row_id for a in arts)]}

    @mcp.tool()
    def mark(
        row_id: str, session_id: str, status: str | None = None,
        verdict: str | None = None, basis: str | None = None,
        superseded_by: str | None = None,
    ) -> dict:
        """Move a row along its lifecycle, and/or record a verdict on it.

        Two halves of the same gesture, so they are one tool: this row is now
        a result (or is now dead), and here is what I know about it and why.
        Pass at least one of status or verdict; passing both is normal.

        status — the row's lifecycle state. Allowed transitions:
        exploratory -> result, exploratory -> superseded, result ->
        superseded. "superseded" is TERMINAL: a row something replaced does
        not come back, because two current answers to one question is the
        problem this state exists to prevent. To bring one back, save a new
        version instead. Registering with save_artifact is itself a claim, so
        a fresh save_artifact is already a "result" and a run_sql/run_python
        output starts "exploratory" — this is how you claim an existing row.

        superseded_by — required whenever status="superseded", and must be an
        existing row_id: the thing that replaced this one. A dead row with
        nothing said about what replaced it is a deletion with extra steps.
        A superseded row stays fully readable through get_artifact; it just
        stops being the answer search_artifacts gives.

        verdict — "confirmed", "contradicted", "stale" or "needs_review",
        recorded against the row and APPENDED to its validation history: the
        history is never rewritten, so a later opinion cannot erase what
        was believed earlier. `basis` is REQUIRED for a verdict, for the
        same reason a confidence level needs one — say what you checked and
        against what, because a verdict with no basis is a label no later
        reader can act on.

        The response reports the row's new status and its current validation
        verdict, which is DERIVED rather than stored: the latest human
        verdict if there is one (however late a model speaks afterwards),
        else the latest model verdict, else "unvalidated" — overridden to
        "stale" while any dataset it was built from is past its own
        `refresh_after`. A row already judged "contradicted" stays
        contradicted; the stale check never softens a known-bad result into
        a different kind of doubt.
        """
        try:
            with db.write() as conn:
                result = store.mark(
                    conn, row_id=row_id, session_id=session_id, status=status,
                    verdict=verdict, basis=basis, superseded_by=superseded_by,
                )
        except ValueError as exc:
            return {"error": str(exc), "artifact_row_ids": []}
        return {**result, "artifact_row_ids": [row_id]}

    # The consistency layer, back on the tool surface by the maintainer's U1
    # decision (WP-B2R): a shared chart style and a house report layout are the
    # reuse case the store exists for, and templating.py still held every
    # library function these two need. Only these two came back — the skill
    # library, publish_report and the import-time seeding stay cut.

    @mcp.tool()
    def list_templates(session_id: str, kind: str | None = None) -> dict:
        """List this store's chart styles and report templates, built-in and custom.

        Chart styles are run_python's style= (default "dsos", the dark house
        style); report templates are the layout the GUI's report page
        (/artifacts/<row_id>/report?template=...) renders a narrative with
        (default "report", the dark house layout). Custom templates made with
        save_template are referenced by row_id or artifact_id — the id is
        stable across version edits. kind="chart-style" or "report" to list
        one kind.
        """
        if kind is not None and kind not in templating.TEMPLATE_KINDS:
            return {
                "error": f"kind must be one of {sorted(templating.TEMPLATE_KINDS)}, got {kind!r}",
                "artifact_row_ids": [],
            }
        customs = store.list_artifacts(db.conn(), type="template")

        def _custom(kind_tag: str) -> list[dict]:
            return [
                {
                    "artifact_id": a.artifact_id, "row_id": a.row_id, "version": a.version,
                    "title": a.title, "description": a.description, "tags": a.tags,
                }
                for a in customs if kind_tag in a.tags
            ]

        result: dict = {"artifact_row_ids": [a.row_id for a in customs]}
        if kind in (None, templating.CHART_STYLE_KIND):
            result["chart_styles"] = {
                "builtins": [
                    {"name": n, "description": d}
                    for n, d in sorted(templating.CHART_STYLE_BUILTINS.items())
                ],
                "custom": _custom(templating.CHART_STYLE_KIND),
            }
        if kind in (None, templating.REPORT_KIND):
            result["report_templates"] = {
                "builtins": [
                    {"name": n, "description": d}
                    for n, d in sorted(templating.REPORT_TEMPLATE_BUILTINS.items())
                ],
                "custom": _custom(templating.REPORT_KIND),
            }
        return result

    @mcp.tool()
    def save_template(
        session_id: str, kind: str, content: str | None = None,
        artifact_id: str | None = None, title: str | None = None,
        description: str | None = None, tags: list[str] | None = None,
        base: str | None = None,
    ) -> dict:
        """Create or re-version a template: the look of this workstream, saved once.

        Every later session then gets it by reference instead of re-deriving
        the same chart style or report layout.

        kind: "chart-style" (matplotlib rcParams text, .mplstyle syntax — used
        via run_python's style=) or "report" (HTML with {{title}}/{{body}}/
        {{published_at}}/{{session_question}} tokens, where {{body}} is where
        the rendered narrative goes — the layout the GUI's report page renders
        a narrative with).

        base: an existing template (built-in name or row_id/artifact_id) to
        copy from — customize instead of rewriting from scratch. If you pass
        no content, the base's text becomes your starting point; the base is
        recorded in the new template's source either way.

        content: the template text (required unless base provides it).
        Validated at SAVE time, not first use — an unparsable .mplstyle or a
        report template without {{body}} fails here in one call.

        artifact_id: the stable id to re-version later — an edit becomes a new
        version with the same id, so references keep resolving. Omit to derive
        one from the title. title/description are what search ranks on — give
        them real ones.
        """
        # Everything up to the INSERT stays outside the write lock on
        # purpose: validate_chart_style_text shells out to python_path to
        # parse the style, which can take seconds, and there is no reason a
        # slow template check should block another agent's save.
        try:
            if kind not in templating.TEMPLATE_KINDS:
                raise ValueError(
                    f"kind must be one of {sorted(templating.TEMPLATE_KINDS)}, got {kind!r}"
                )
            if base is not None:
                base_text = templating.base_template_content(db.conn(), kind, base)
                if content is None:
                    content = base_text
            if not content or not content.strip():
                raise ValueError(
                    "content is required — pass content, or base to copy an existing template"
                )
            if kind == templating.CHART_STYLE_KIND:
                templating.validate_chart_style_text(content, config.python_path)
            else:
                templating.validate_report_template_text(content)

            if artifact_id is None:
                artifact_id = f"{kind}-" + store.safe_table_name(title or "custom")
            if title is None:
                title = f"Custom {kind} template" + (f" (based on {base})" if base else "")
            if description is None:
                description = (
                    f"Custom {kind} template for consistent styling"
                    + (f", customized from {base!r}." if base else ".")
                )
            final_tags = templating.template_tags(kind) + [
                t for t in (tags or []) if t not in ("template", kind)
            ]
            with db.write() as conn:
                row_id = store.save_artifact(
                    conn, artifact_id=artifact_id, type="template", title=title,
                    description=description, content=content,
                    content_format=(
                        "mplstyle" if kind == templating.CHART_STYLE_KIND else "html"
                    ),
                    tags=final_tags, session_id=session_id,
                    source={"base": base} if base else None,
                )
        except (ValueError, OSError) as exc:
            return {"error": str(exc), "artifact_row_ids": []}
        art = store.get_artifact_by_row_id(db.conn(), row_id, load_content=False)
        return {
            "row_id": row_id, "artifact_id": artifact_id, "version": art.version,
            "kind": kind, "artifact_row_ids": [row_id],
        }

    return mcp
