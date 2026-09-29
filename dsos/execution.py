"""Execution runner: run_sql / run_python against dataset artifacts.

Every run is recorded (code, timing, status, output). A run that produced
something saves it as a new artifact, lineage-linked to its inputs, and
the execution points at that artifact — automatically, so there's no
separate `record_execution` call the agent could forget to make. A run
that failed records an execution with output_row_id NULL and produces no
artifact and no lineage at all: a crash is an execution, not a result,
and a dead content-less row in `artifacts` was never one. Both come back
as a RunOutcome, which names the artifact (row_id) on success and only
ever the execution on failure.

scratch=True (feedback #8) records nothing at all: the result comes back
inline, but nothing enters the store. The run still shows up in the
session's tool-call trace (the MCP middleware logs every call).

run_python always runs the code in a subprocess, against a configured
`python_path` (the caller's own analysis Python by default — see
mcp_server.py's DEFAULT_PYTHON_PATH) — never in this server's own process,
so the server never needs the analysis stack (matplotlib/scipy/sklearn/...)
installed at all. requirements=[...] additionally resolves packages that
interpreter is missing via uv into a throwaway environment layered on top
of it. Either way the result flows back through the same save/record/
lineage path — see dsos/sandbox.py. code_paths=[...] (unpackaged local
modules) is wired into the subprocess wrapper's sys.path.

The store's write lock covers only the persist tail, never the run. A
caller that serialises writers (the MCP tools, via Database.write) passes
`persist`, and the query or subprocess runs with no lock held — reads go
through the plain connection, and SELECTs open no transaction, so no read
snapshot is carried into the write either. The one read-then-write the
lock exists for (save_artifact's version read, D13) is inside
_record_run, so that is the only part that needs it. A run holding the
lock for its whole duration stalled every other client for up to
DSOS_SANDBOX_TIMEOUT, since the tool-call log takes the same lock.

A run_python `result` that's a matplotlib Figure/Axes (the natural shape of
a chart cell) is rendered to PNG bytes automatically (feedback #2, this
round) — see sandbox.py's wrapper template, which is the only place this
runs now.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager

import duckdb
import pandas as pd

from dsos import templating
from dsos.store import Artifact, get_artifact_by_row_id, save_artifact

_STDOUT_LIMIT = 2_000
_STDERR_LIMIT = 4_000  # tail-capped: the traceback's final frames are the useful ones

# The lifecycle states a run may save its row in. Not 'superseded': a row
# only gets there through store.mark, which demands the superseded_by that
# says what replaced it — a run has no such thing to name.
RUN_STATUSES = ("exploratory", "result")

# A zero-argument context manager factory yielding the connection to write
# through — `Database.write` is one.
Persist = Callable[[], ContextManager[sqlite3.Connection]]


@dataclass(frozen=True)
class RunOutcome:
    """What a persisted run left behind.

    `row_id` is the artifact the run produced, and is None for a run that
    failed — a failure has no artifact, so the caller is told which
    execution to look at instead. `execution_id` is set either way: the
    executions ledger is the record of the attempt, success or failure.
    """
    status: str
    row_id: str | None
    execution_id: str


def check_run_status(status: str) -> None:
    """Refuse a lifecycle status a run may not save its row in, naming the
    allowed ones. Called before any work, so a bad status costs the caller
    nothing — not a query, a subprocess, or a half-recorded run."""
    if status not in RUN_STATUSES:
        raise ValueError(
            f"status must be one of {list(RUN_STATUSES)} for a run, got {status!r}"
            + ("; a row becomes superseded only through mark(status='superseded', "
               "superseded_by=...)" if status == "superseded" else "")
        )


def _persist(conn: sqlite3.Connection, persist: Persist | None) -> ContextManager[sqlite3.Connection]:
    """The block the persist tail runs in: the caller's `persist` if it gave
    one, else just `conn` with no lock — the old direct-call shape, for a
    single-threaded caller with nothing to serialise against."""
    return persist() if persist is not None else contextlib.nullcontext(conn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_summary(result: Any) -> dict:
    if isinstance(result, pd.DataFrame):
        return {"rows": len(result), "columns": list(result.columns)}
    if isinstance(result, (list, dict)):
        return {"len": len(result)}
    return {}


def _alias(i: int) -> str:
    """The binding name for the i-th input of a call (1-based): in_1, in_2,
    ... Positional, never derived from the title — the single rule every
    run path and every response field follows (see input_aliases)."""
    return f"in_{i}"


def input_aliases(inputs: list) -> dict[str, str]:
    """{row_id: binding name} for a resolved input list, in list order. This
    is what the `input_tables` response field reports, and it is why the
    aliases are the same in the response, in DuckDB, and in the sandbox
    wrapper: one function, one rule.

    The names are positional rather than title-derived on purpose. A name
    from the title collided when two inputs shared one, needed a "t_"
    prefix to stay a legal identifier for a digit-leading title, and broke
    outright when the input was re-titled. A position can't do any of that.
    """
    return {art.row_id: _alias(i) for i, art in enumerate(inputs, start=1)}


def _resolve_inputs(conn: sqlite3.Connection, input_row_ids: list[str]) -> list[Artifact]:
    """Load every input artifact the caller listed, in the order they listed
    them, and raise a named ValueError for an unknown or repeated id.

    Both used to be silent-or-crash: get_artifact_by_row_id returns None for
    an unknown row_id, so the run died later with an AttributeError on None
    (or, for a repeat, quietly read the same artifact twice under one name).
    The message names the offending id, because these are 32-char hex ids a
    model mistypes."""
    inputs: list[Artifact] = []
    seen: set[str] = set()
    for row_id in input_row_ids:
        art = get_artifact_by_row_id(conn, row_id)
        if art is None:
            raise ValueError(f"unknown input_row_id {row_id!r}")
        if row_id in seen:
            raise ValueError(f"duplicate input_row_id {row_id!r}")
        seen.add(row_id)
        inputs.append(art)
    return inputs


def _schema_hint(inputs: list) -> str:
    """Alias, title and columns for every registered DataFrame input,
    appended to a run_sql failure (feedback #8) — so a column/table typo is
    fixable from the error alone, without a separate scratch DESCRIBE/schema
    poke. The title is here, not in the name: it's what the agent needs to
    recognize which in_k it meant."""
    tables = [
        f'{_alias(i)} "{a.title}" ({", ".join(a.content.columns)})'
        for i, a in enumerate(inputs, start=1) if isinstance(a.content, pd.DataFrame)
    ]
    return f" | available tables: {'; '.join(tables)}" if tables else ""


def _check_inputs(inputs: list) -> None:
    """A missing-blob input fails the run *before* code execution, with an
    error naming the broken input — not a cryptic FileNotFoundError from
    deep inside pandas/duckdb (feedback #4)."""
    for art in inputs:
        if art.content_error:
            raise ValueError(f"input {art.row_id} ({art.title!r}): {art.content_error}")


def _record_execution(
    conn: sqlite3.Connection, *, output_row_id: str | None, session_id: str,
    input_row_ids: list[str], kind: str, code: str,
    started_at: str, status: str, stdout: str, stderr: str, error: str | None,
    output_summary: dict,
) -> str:
    """Insert one executions row and return its id. `output_row_id` is None
    for a run that failed; `session_id` and `input_row_ids` are what make
    such a row findable at all, since with no artifact there is nothing to
    look it up by."""
    import json
    exec_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO executions (id, output_row_id, kind, code, started_at,
                                 ended_at, status, stdout, stderr, error,
                                 output_summary, session_id, input_row_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (exec_id, output_row_id, kind, code, started_at, _now(), status,
         stdout, stderr, error, json.dumps(output_summary), session_id,
         json.dumps(input_row_ids)),
    )
    conn.commit()
    return exec_id


def _scratch_payload(run: dict, inputs: list[Artifact]) -> dict:
    """The inline response for a scratch run: same shape as a persisted
    run's payload (status/stdout/result preview), minus anything that
    implies an artifact exists, plus scratch=True.

    A scratch run is not promotable: if a check turns out to be worth
    keeping, re-run it with scratch=False. There is deliberately no id and
    no in-process cache behind it (WP-B2) — a cached result that outlived
    the process was a second, invisible copy of the store's truth, and the
    input order it cached was the only correct in_1/in_2 mapping (the
    promotion path rebuilt it from lineage order instead, which need not
    match)."""
    from dsos import present

    input_row_ids = [a.row_id for a in inputs]
    input_tables = input_aliases(inputs)

    payload: dict[str, Any] = {
        "status": run["status"],
        "stdout": run["stdout"][:_STDOUT_LIMIT],
        "scratch": True,
        "input_tables": input_tables,
        "artifact_row_ids": list(input_row_ids),
    }
    if run["status"] == "ok":
        payload.update(present.result_payload(run["result"]))
    else:
        payload["error"] = run["error"]
        payload["stderr"] = run["stderr"][-_STDERR_LIMIT:]
    return payload


def _record_run(
    conn: sqlite3.Connection, *, kind: str, run: dict, input_row_ids: list[str],
    session_id: str, title: str, description: str, output_type: str, started_at: str,
    code: str, artifact_status: str = "exploratory",
) -> RunOutcome:
    """The shared tail of run_sql and run_python's persisted path: on
    success save the result as a new artifact, lineage-linked to its
    inputs, and record the execution against it; on failure record the
    execution alone.

    `artifact_status` is the LIFECYCLE state the saved row gets, and is a
    different thing from the `run["status"]` this function reads: that one
    is whether the code ran (ok/error, and the only value that ever
    reaches the executions table), this one is what the output is
    (D3). A run is exploratory until a caller claims otherwise — the
    default is on the tool, and this default exists only for a direct
    execution.run_sql caller, so that neither path can omit it.

    The two branches are deliberately not "save, then record": the failure
    branch touches neither `artifacts` nor `lineage`, so a crashed run
    leaves the store exactly as it found it apart from the one executions
    row that says what happened and to which inputs."""
    status, error, result = run["status"], run["error"], run["result"]

    if status != "ok":
        exec_id = _record_execution(
            conn, output_row_id=None, session_id=session_id,
            input_row_ids=input_row_ids, kind=kind, code=code,
            started_at=started_at, status=status, stdout=run["stdout"],
            stderr=run["stderr"], error=error, output_summary={},
        )
        return RunOutcome(status="error", row_id=None, execution_id=exec_id)

    output_format = _infer_format(result)
    output_row_id = save_artifact(
        conn, type=output_type, title=title, description=description,
        content=result, content_format=output_format, session_id=session_id,
        parent_row_ids=input_row_ids, status=artifact_status,
    )
    exec_id = _record_execution(
        conn, output_row_id=output_row_id, session_id=session_id,
        input_row_ids=input_row_ids, kind=kind, code=code, started_at=started_at,
        status=status, stdout=run["stdout"], stderr=run["stderr"], error=error,
        output_summary=_output_summary(result),
    )
    return RunOutcome(status="ok", row_id=output_row_id, execution_id=exec_id)


def run_sql(
    conn: sqlite3.Connection, *, code: str, session_id: str, title: str, description: str,
    input_row_ids: list[str], scratch: bool = False, status: str = "exploratory",
    persist: Persist | None = None,
) -> RunOutcome | dict:
    """Runs `code` in DuckDB with each input artifact registered as a table
    named `in_1`, `in_2`, ... in the order the caller listed them in
    `input_row_ids` — positional, never derived from the title, so two
    inputs sharing a title, a digit-leading title, and a re-titled input all
    behave the same. The result becomes a
    new `query` artifact, lineage-linked to every input — unless
    scratch=True, in which case the payload comes back inline and nothing is
    persisted. Returns a RunOutcome, or the scratch payload dict.

    `status` is the saved row's lifecycle state (D3): 'exploratory' (the
    default) for a query that is a finding, 'result' for one the caller is
    asserting as an answer. A scratch run persists nothing and takes no
    status. A status outside those two raises ValueError before the query
    runs.

    `persist`: see the module docstring. `conn` is used for the reads; the
    save/record tail runs inside `persist()` when given."""
    check_run_status(status)
    started_at = _now()
    stdout = io.StringIO()
    # run_status: whether the CODE ran, as distinct from the `status`
    # parameter, which is the lifecycle state of the row this run may save.
    run_status, error, stderr_text, result = "ok", None, "", None

    inputs: list[Artifact] = []
    duck = duckdb.connect(":memory:")

    try:
        inputs = _resolve_inputs(conn, input_row_ids)
        _check_inputs(inputs)
        for i, art in enumerate(inputs, start=1):
            duck.register(_alias(i), art.content)  # content: pandas.DataFrame
        with contextlib.redirect_stdout(stdout):
            result = duck.execute(code).fetchdf()
    except Exception as exc:
        # `error` is the concise, agent-facing message (no internal file/line
        # noise from dsos's own frames), with the registered tables' schema
        # appended (feedback #8) so a column/table typo is fixable from this
        # message alone; the full traceback goes in stderr, which is where a
        # real unhandled exception would print it anyway.
        run_status, error = "error", f"{type(exc).__name__}: {exc}{_schema_hint(inputs)}"
        stderr_text = traceback.format_exc()
        result = pd.DataFrame()
    finally:
        duck.close()

    run = {"status": run_status, "error": error, "stderr": stderr_text,
           "stdout": stdout.getvalue(), "result": result}
    if scratch:
        return _scratch_payload(run, inputs)

    with _persist(conn, persist) as wconn:
        return _record_run(
            wconn, kind="sql", run=run, input_row_ids=input_row_ids, session_id=session_id,
            title=title, description=description, output_type="query", started_at=started_at,
            code=code, artifact_status=status,
        )


def run_python(
    conn: sqlite3.Connection, *, code: str, session_id: str, title: str, description: str,
    input_row_ids: list[str], python_path: str, output_type: str = "transform",
    output_format: str = "parquet", scratch: bool = False,
    requirements: list[str] | None = None, code_paths: list[str] | None = None,
    style: str | None = "dsos", status: str = "exploratory",
    persist: Persist | None = None,
) -> RunOutcome | dict:
    """Runs `code` in a subprocess against `python_path`, with each input
    artifact bound to `in_1`, `in_2`, ... in the order the caller listed
    them in `input_row_ids` — positional, never derived from the title — plus
    `inputs`, a dict of the same objects keyed by row_id, and `pd` (pandas).
    The code must set a variable named
    `result`; that becomes the new artifact's content — unless scratch=True,
    in which case the payload comes back inline and nothing is persisted.
    Returns a RunOutcome, or the scratch payload dict.

    `python_path`: the already-resolved interpreter for this call (the
    caller — mcp_server.py — applies the `python_path or
    DEFAULT_PYTHON_PATH` fallback; this function never reads env vars).

    With `requirements` (non-empty), uv additionally resolves those packages
    on top of `python_path` into a throwaway environment for this run.
    Either way the result flows back through the same save/record/lineage
    path — see dsos/sandbox.py.

    `style` (the consistency layer — dsos/templating.py): the chart style
    applied to rcParams before the code runs. "dsos" is the dark house
    default; "report" the same look sized for published reports; "minimal"
    a bare light style; a custom chart-style template's row_id/artifact_id
    works too; None = raw matplotlib defaults. Resolved BEFORE the code
    runs, so a bad reference is a one-call error, not a matplotlib error
    mid-chart.

    `status` is the saved row's lifecycle state (D3), same as run_sql's:
    'exploratory' (the default) for a transform nobody has claimed yet,
    'result' for one this session is asserting; anything else raises
    ValueError before the subprocess starts.

    `persist`: as run_sql's — the subprocess runs with no lock held."""
    check_run_status(status)
    started_at = _now()
    inputs: list[Artifact] = []

    try:
        inputs = _resolve_inputs(conn, input_row_ids)
        _check_inputs(inputs)
        chart_style = templating.resolve_chart_style(conn, style)
        from dsos import sandbox
        run = sandbox.run_subprocess(
            code=code, requirements=list(requirements or []),
            code_paths=code_paths, chart_style=chart_style,
            # the alias/artifact pairs, so the wrapper binds exactly the
            # names this module reports in input_tables
            inputs=[(_alias(i), art) for i, art in enumerate(inputs, start=1)],
            python_path=python_path,
        )
    except Exception as exc:
        # dsos-side failures around the run (missing-blob inputs, bad
        # code_paths, a bad style reference, sandbox plumbing) — user-code
        # failures are already captured inside the run dicts above.
        run = {"status": "error", "error": f"{type(exc).__name__}: {exc}",
               "stdout": "", "stderr": traceback.format_exc(), "result": pd.DataFrame()}

    if scratch:
        return _scratch_payload(run, inputs)

    with _persist(conn, persist) as wconn:
        return _record_run(
            wconn, kind="python", run=run, input_row_ids=input_row_ids, session_id=session_id,
            title=title, description=description, output_type=output_type,
            started_at=started_at, code=code, artifact_status=status,
        )


def _infer_format(result: Any) -> str:
    if isinstance(result, pd.DataFrame):
        return "parquet"
    if isinstance(result, bytes):
        return "png"
    if isinstance(result, (list, dict)):
        return "json"
    return "markdown"
