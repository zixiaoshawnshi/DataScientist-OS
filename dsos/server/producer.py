"""The producer profile: the nine tools an analysis agent calls.

`build_producer(config)` is a factory, not a module. Every tool is a closure
over one `ServerConfig`, so two producers over two stores can exist in one
process — which is what the daemon (WP-D1) needs, and what the
module-global connection this replaced made impossible. Nothing here opens a
store: reads go through `config.db.conn()`, writes take
`config.db.write()` first, and both are the same `Database` the caller
passed in.

The tool bodies are unchanged from `dsos/mcp_server.py`, which is the point
of this WP: same names, same parameters, same responses, same docstrings
(the docstrings are most of the product — an agent decides what to call from
them). What changed is where the connection comes from.
"""

from __future__ import annotations

from fastmcp import FastMCP

from dsos import execution, ingest, present, store, templating
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
    def start_session(question: str) -> dict:
        """Start a new round of work. Call this once, first, for every new
        top-level question — before searching, fetching, or running anything.
        Reuse the returned session_id in every other tool call for this round.

        The response also reports `prior_work`: what this store already holds
        from earlier sessions, plus a few candidates that may already cover
        this question. Read it before you fetch anything. If a candidate fits,
        run_sql/run_python against its row_id instead of downloading and
        cleaning the same data again — that is the whole point of the store,
        and an agent that never looks will redo work that is already done."""
        with db.write() as conn:
            session_id = store.start_session(conn, question)
        # The search is deliberately outside the write lock: it embeds and
        # ranks, which is slow, and the lock exists to order writers.
        return {
            "session_id": session_id,
            **store.prior_work_signal(db.conn(), question),
        }

    @mcp.tool()
    def search_artifacts(
        query: str, session_id: str, top_k: int = 5, type: str | None = None
    ) -> dict:
        """Search over everything saved so far, across every past session, not
        just this one — an exact term (a title, a column name) reliably matches
        even with the fallback embedding model; a vaguer query still finds
        something via semantic similarity. Call this before fetching new data
        or rebuilding anything — if a past artifact already covers part of the
        question (this session's or an earlier one's), reuse it via
        get_artifact/run_sql/run_python instead of redoing the work.

        Workflow skills and templates are not results and are left out unless
        you pass type="skill" or type="template" for one."""
        hits = store.search_artifacts(
            db.conn(), query, top_k=top_k, type=type
        )
        results = [
            {
                "row_id": a.row_id, "type": a.type, "title": a.title,
                "description": a.description, "score": round(score, 3),
            }
            for a, score in hits
        ]
        return {"results": results, "artifact_row_ids": [r["row_id"] for r in results]}

    @mcp.tool()
    def get_artifact(row_id: str, session_id: str) -> dict:
        """Fetch an artifact's full metadata and content, by the row_id that
        save_artifact/run_sql/run_python/search_artifacts gave you — that row_id
        is the only id you need; there's no separate "artifact_id" to look up.
        Tabular artifacts (dataset/query/transform) return row_count, columns,
        and a 10-row preview, not the full table — use run_sql/run_python
        against this row_id to compute over the full data. `uses` lists what
        this artifact was built from or embeds (e.g. a narrative's datasets).

        Note: run_sql/run_python already return this same preview inline in
        their own response — call get_artifact only to re-fetch something from
        an earlier tool call (e.g. a search_artifacts hit), not right after
        running it yourself.

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
    ) -> dict:
        """Register something as a real artifact. A dataset you fetched isn't
        real to this system — invisible to search, lineage, and reuse for every
        future question — until you call this.

        Pass exactly one of:
        - content_text: inline text, for markdown/python/sql/json content.
          `content_format` must describe it (markdown, python, sql, json, ...).
        - content_path: a local file you already produced with your own tools.
          For type="dataset" the format is read from the file extension —
          .csv/.tsv/.json/.parquet are all accepted and normalized to parquet
          internally; whatever you pass as content_format is ignored. For other
          types the file is read as text and content_format describes it.

        `description` must be a real 1-2 sentences (what this is, why it
        matters) — search_artifacts ranks on it, so a vague description makes
        this artifact unreachable to future questions.

        For a narrative: write `{{artifact:<row_id>}}` in content_text for each
        dataset/chart/query it discusses. Those row_ids are automatically added
        to this artifact's lineage — no need to also pass parent_row_ids for
        them — and show up as `uses` when this narrative is fetched later.

        `source`: freeform, but for a fetched dataset prefer {"url": ...,
        "fetched_at": ... (ISO-ish timestamp/description of when), "method": ...
        (how it was fetched, e.g. "WebFetch"/"curl"/"Kaggle API"), "refresh_after":
        ... (how long this stays fresh, e.g. "7d"/"30d"/"static" — your call, not
        enforced)} plus whatever else identifies it (e.g. "survey": "Stack
        Overflow 2024") — this is the only provenance a later session/report has
        to go on, and the only record of how/when this was retrieved: dsos can't
        see a fetch you ran with your own tools, only what you put here. Nothing
        computes staleness automatically — before reusing a dataset, check
        `fetched_at`/`refresh_after` yourself and decide if it's worth refetching.

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
                    parent_row_ids=parent_row_ids,
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
        scratch: bool = False,
    ) -> dict:
        """Run SQL (DuckDB) against one or more artifacts. Each input row_id is
        registered as a table named in_1, in_2, ... in the order you list them
        in input_row_ids — the response's `input_tables` says which row_id got
        which name. The names are positional, never derived from the title, so
        two inputs with the same title, a title starting with a digit, and an
        input you later re-titled all behave the same.
        The result is returned inline below (row_count,
        columns, a 10-row preview) — you do NOT need a second call to see it.
        It's also saved as a new `query` artifact, automatically lineage-linked
        to every input and recorded — no separate save_artifact call needed.
        On failure, status="error" and `error`/`stdout`/`stderr` below show why;
        a failed run produces no artifact, so that response carries an
        `execution_id` and no `row_id`.

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
        if scratch:
            # Scratch persists nothing — no write to serialise, so a quick
            # check never blocks another writer.
            return execution.run_sql(
                db.conn(), code=code, session_id=session_id, title=title, description=description,
                input_row_ids=input_row_ids, scratch=scratch,
            )
        # The lock spans the run, not just its write: execution.py owns the
        # tail that saves the artifact and records the execution, so from
        # here there is no way to take the lock for less than the whole
        # call. That costs a concurrent writer the duration of a long run;
        # it buys the D13 guarantee on the read-then-write inside it.
        with db.write() as conn:
            outcome = execution.run_sql(
                conn, code=code, session_id=session_id, title=title, description=description,
                input_row_ids=input_row_ids, scratch=scratch,
            )
            return execution_response(conn, outcome, input_row_ids)

    @mcp.tool()
    def run_python(
        code: str, session_id: str, title: str, description: str, input_row_ids: list[str],
        output_type: str = "transform", scratch: bool = False,
        requirements: list[str] | None = None, code_paths: list[str] | None = None,
        style: str | None = "dsos", python_path: str | None = None,
    ) -> dict:
        """Run Python against one or more artifacts. Each input row_id is bound
        to a variable named in_1, in_2, ... in the order you list them in
        input_row_ids (the response's `input_tables` says which row_id got which
        name), and every input is also reachable by id as
        `inputs["<row_id>"]`; `pd` (pandas) is available. The names are
        positional, never derived from the title, so two inputs with the same
        title, a title starting with a digit, and an input you later re-titled
        all behave the same. Your
        code MUST assign a `result` variable — a DataFrame, a dict/list (saved
        as JSON), a string, or for output_type="chart" a matplotlib Figure/Axes
        (e.g. whatever `plt.gcf()`/`plt.subplots()` gives you — it's rendered to
        PNG for you) or raw png bytes directly. On a persisted (non-scratch)
        call the result is returned inline below as an actual rendered image
        for a chart (not just a text placeholder) — you do NOT need a second
        call to see it. It's also saved as a new artifact (default
        type="transform"; pass output_type="chart" for a plot), lineage-linked
        to every input and recorded automatically. On failure, status="error"
        and `error`/`stdout`/`stderr` below show the traceback and anything
        printed before it failed; a failed run produces no artifact, so that
        response carries an `execution_id` and no `row_id`.

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
        .py modules to import from — works with or without requirements.

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
        this server's own interpreter, if that's unset) has installed.

        Example:
          run_python(
            code="result = in_1.groupby('team')['score'].mean().reset_index()",
            session_id="s1", title="Average score by team",
            description="Mean score per team.",
            input_row_ids=["<row_id of the toy_scores dataset artifact>"],
          )
        """
        if scratch:
            return execution.run_python(
                db.conn(), code=code, session_id=session_id, title=title, description=description,
                input_row_ids=input_row_ids, output_type=output_type, scratch=scratch,
                requirements=requirements, code_paths=code_paths, style=style,
                python_path=python_path or config.python_path,
            )
        with db.write() as conn:
            outcome = execution.run_python(
                conn, code=code, session_id=session_id, title=title, description=description,
                input_row_ids=input_row_ids, output_type=output_type, scratch=scratch,
                requirements=requirements, code_paths=code_paths, style=style,
                python_path=python_path or config.python_path,
            )
            return execution_response(conn, outcome, input_row_ids)

    @mcp.tool()
    def get_lineage(row_id: str, session_id: str, direction: str = "ancestors") -> dict:
        """See what an artifact was built from (direction="ancestors", the
        default) or what has been built from it (direction="descendants")."""
        arts = store.get_lineage(db.conn(), row_id, direction=direction)
        results = [{"row_id": a.row_id, "type": a.type, "title": a.title} for a in arts]
        return {"results": results, "artifact_row_ids": [row_id, *(a.row_id for a in arts)]}

    # The consistency layer, back on the tool surface by the maintainer's U1
    # decision (WP-B2R): a shared chart style and a house report layout are the
    # reuse case the store exists for, and templating.py still held every
    # library function these two need. Only these two came back — the skill
    # library, publish_report and the import-time seeding stay cut.

    @mcp.tool()
    def list_templates(session_id: str, kind: str | None = None) -> dict:
        """Chart styles and report templates — the consistency layer, built-ins
        and custom. Chart styles are run_python's style= (default "dsos", the
        dark house style); report templates are the layout the GUI's report page
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
        """Create or re-version a template — the customizable half of the
        consistency layer. Save the look of this workstream once here, and every
        later session gets it by reference.

        kind: "chart-style" (matplotlib rcParams text, .mplstyle syntax — used
        via run_python's style=) or "report" (HTML with {{title}}/{{body}}/
        {{published_at}}/{{session_question}} tokens, where {{body}} is where
        the rendered narrative goes — the layout the GUI's report page renders a
        narrative with).

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
