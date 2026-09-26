"""Execution runner: run_sql / run_python against dataset artifacts.

Every persisted run is recorded (code, timing, status, output) and its
output is saved as a new artifact, lineage-linked to its inputs —
automatically, so there's no separate `record_execution` call the agent
could forget to make.

scratch=True (feedback #8) skips both the artifact and the execution
record: the result comes back inline, but nothing enters search or lineage.
The run still shows up in the session's tool-call trace (the MCP middleware
logs every call), and the executions ledger is skipped because its
output_row_id column is NOT NULL — there's no artifact to point at.

requirements=[...] (feedback #1, Option B) runs the code in a subprocess
sandbox instead: uv resolves the packages into a throwaway environment, and
the result flows back through the same save/record/lineage path — see
dsos/sandbox.py. code_paths=[...] (unpackaged local modules) works on both
paths: sys.path injection in-process, --sys.path wiring in the wrapper.

A scratch run's result is cached in-process under a scratch_id
(_SCRATCH_CACHE); promote_scratch() persists it later through the exact
same save/record/lineage tail (_persist_run) a non-scratch call would have
used, without re-executing anything (feedback #4, this round).

A run_python `result` that's a matplotlib Figure/Axes (the natural shape of
a chart cell) is rendered to PNG bytes automatically (feedback #2, this
round) — see _coerce_chart_result and sandbox.py's wrapper-template
equivalent for the subprocess path.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

try:
    # Force the non-interactive Agg backend before any run_python code gets
    # a chance to `import matplotlib.pyplot` and pick the platform default
    # (e.g. TkAgg) instead. That default assumes a single GUI main-loop
    # thread; FastMCP runs tools in a thread pool, so a Tk-backed Figure
    # created off the main thread throws on GC ("main thread is not in main
    # loop") — a real failure mode this round's chart auto-render surfaced,
    # not just cosmetic noise. Agg is also what feedback #2's coercion needs
    # anyway: headless PNG rendering, no display.
    import matplotlib
    matplotlib.use("Agg", force=True)
except ImportError:
    pass

from dsos.store import Artifact, get_artifact_by_row_id, safe_table_name, save_artifact

_STDOUT_LIMIT = 2_000
_STDERR_LIMIT = 4_000  # tail-capped: the traceback's final frames are the useful ones

# scratch=True (feedback #8) never persists — by design. But that meant
# promoting a scratch result that turned out useful required re-running the
# code (feedback #4, this round). This in-memory, per-process cache lets a
# scratch run be persisted from its scratch_id, without re-executing
# anything. Capped and LRU-evicted since it's process memory, not storage:
# lost on restart, and never grows unbounded across a long session.
_SCRATCH_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_SCRATCH_CACHE_MAX = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_summary(result: Any) -> dict:
    if isinstance(result, pd.DataFrame):
        return {"rows": len(result), "columns": list(result.columns)}
    if isinstance(result, (list, dict)):
        return {"len": len(result)}
    return {}


def _installed_packages() -> list[str]:
    """Names of every distribution in this sandbox — the ImportError hint's
    payload (feedback #7), so the agent stops guessing and re-probing."""
    from importlib.metadata import distributions

    names = sorted({
        (d.metadata["Name"] or "").lower() for d in distributions() if d.metadata["Name"]
    })
    return names


def _missing_package_hint() -> str:
    packages = _installed_packages()
    shown = ", ".join(packages[:80]) + (", ..." if len(packages) > 80 else "")
    return (
        f"Package not installed in this sandbox. Available here: {shown}. "
        "For anything missing, pass requirements=[...] to run_python (uv "
        "resolves it in a throwaway environment), or compute it with your "
        "own tools (bash) and register the result via save_artifact."
    )


def _coerce_chart_result(result: Any) -> Any:
    """A run_python chart cell naturally ends with `plt.gcf()` or the Figure/
    Axes that `plt.subplots()` returns, assigned as `result` — but only raw
    png bytes were ever treated as chart content, so that object landed in
    storage stringified as "Figure(1400x800)" instead of rendering (feedback
    #2). Render it to PNG bytes here instead, so a natural chart cell just
    works without the agent having to know to call fig.savefig() itself."""
    try:
        from matplotlib.axes import Axes
        from matplotlib.figure import Figure
    except ImportError:
        return result
    if isinstance(result, Axes):
        fig = result.get_figure()
    elif isinstance(result, Figure):
        fig = result
    else:
        return result
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return buf.getvalue()


def _schema_hint(inputs: list) -> str:
    """Table/column names for every registered DataFrame input, appended to
    a run_sql failure (feedback #8) — so a column/table typo is fixable from
    the error alone, without a separate scratch DESCRIBE/schema poke."""
    tables = [
        f"{safe_table_name(a.title)}({', '.join(a.content.columns)})"
        for a in inputs if isinstance(a.content, pd.DataFrame)
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
    conn: sqlite3.Connection, *, output_row_id: str, kind: str, code: str,
    started_at: str, status: str, stdout: str, stderr: str, error: str | None,
    output_summary: dict,
) -> str:
    import json
    exec_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO executions (id, output_row_id, kind, code, started_at,
                                 ended_at, status, stdout, stderr, error, output_summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (exec_id, output_row_id, kind, code, started_at, _now(), status,
         stdout, stderr, error, json.dumps(output_summary)),
    )
    conn.commit()
    return exec_id


def _scratch_payload(
    run: dict, inputs: list[Artifact], *, kind: str, code: str, title: str, description: str,
    output_type: str = "transform",
) -> dict:
    """The inline response for a scratch run: same shape as a persisted
    run's payload (status/stdout/result preview), minus anything that
    implies an artifact exists, plus scratch=True.

    Also caches the run (feedback #4) under a scratch_id, so a later
    promote_scratch call can persist it as a real artifact without
    re-running the code."""
    from dsos import present

    input_row_ids = [a.row_id for a in inputs]
    input_tables = {a.row_id: safe_table_name(a.title) for a in inputs}
    scratch_id = uuid.uuid4().hex
    _SCRATCH_CACHE[scratch_id] = {
        "kind": kind, "code": code, "title": title, "description": description,
        "output_type": output_type, "input_row_ids": input_row_ids, "run": run,
    }
    _SCRATCH_CACHE.move_to_end(scratch_id)
    while len(_SCRATCH_CACHE) > _SCRATCH_CACHE_MAX:
        _SCRATCH_CACHE.popitem(last=False)

    payload: dict[str, Any] = {
        "status": run["status"],
        "stdout": run["stdout"][:_STDOUT_LIMIT],
        "scratch": True,
        "scratch_id": scratch_id,
        "input_tables": input_tables,
        "artifact_row_ids": list(input_row_ids),
    }
    if run["status"] == "ok":
        payload.update(present.result_payload(run["result"]))
    else:
        payload["error"] = run["error"]
        payload["stderr"] = run["stderr"][-_STDERR_LIMIT:]
    return payload


def _persist_run(
    conn: sqlite3.Connection, *, kind: str, run: dict, input_row_ids: list[str],
    session_id: str, title: str, description: str, output_type: str, started_at: str,
    code: str,
) -> str:
    """Save a run's result as a new artifact, lineage-linked to its inputs,
    and record the execution — the shared tail of run_sql/run_python's
    persisted path and promote_scratch (feedback #4), so promoting a cached
    scratch run goes through the exact same save/record path a fresh
    non-scratch run would."""
    status, error, result = run["status"], run["error"], run["result"]
    output_format = _infer_format(result) if status == "ok" else "parquet"
    output_row_id = save_artifact(
        conn, type=output_type, title=title, description=description,
        content=result, content_format=output_format, session_id=session_id,
        parent_row_ids=input_row_ids, status=status,
    )
    _record_execution(
        conn, output_row_id=output_row_id, kind=kind, code=code, started_at=started_at,
        status=status, stdout=run["stdout"], stderr=run["stderr"], error=error,
        output_summary=_output_summary(result),
    )
    return output_row_id


def promote_scratch(
    conn: sqlite3.Connection, *, scratch_id: str, session_id: str,
    title: str, description: str,
) -> str:
    """Persist a previously-run scratch=True result as a real artifact,
    without re-running its code (feedback #4) — the "spike, then keep it"
    shortcut. `title`/`description` may differ from the original scratch
    call's (the agent gets to name it properly now that it's staying).

    Raises ValueError if scratch_id is unknown — either it never existed,
    or it aged out of the cache (capped at the most recent
    _SCRATCH_CACHE_MAX scratch runs, per server process)."""
    entry = _SCRATCH_CACHE.pop(scratch_id, None)
    if entry is None:
        raise ValueError(
            f"no cached scratch run with scratch_id {scratch_id!r} — it may have "
            "aged out (this cache is per-process and capped) or already been "
            "promoted. Re-run with scratch=False instead."
        )
    return _persist_run(
        conn, kind=entry["kind"], run=entry["run"], input_row_ids=entry["input_row_ids"],
        session_id=session_id, title=title, description=description,
        output_type=entry["output_type"], started_at=_now(), code=entry["code"],
    )


def run_sql(
    conn: sqlite3.Connection, *, code: str, session_id: str, title: str, description: str,
    input_row_ids: list[str], scratch: bool = False,
) -> str | dict:
    """Runs `code` in DuckDB with each input artifact registered as a table
    named after its title (lowercased, non-alnum -> `_`). The result becomes a
    new `query` artifact, lineage-linked to every input — unless
    scratch=True, in which case the payload comes back inline and nothing is
    persisted."""
    started_at = _now()
    stdout = io.StringIO()
    status, error, stderr_text, result = "ok", None, "", None

    inputs = [get_artifact_by_row_id(conn, rid) for rid in input_row_ids]
    duck = duckdb.connect(":memory:")

    try:
        _check_inputs(inputs)
        for art in inputs:
            duck.register(safe_table_name(art.title), art.content)  # content: pandas.DataFrame
        with contextlib.redirect_stdout(stdout):
            result = duck.execute(code).fetchdf()
    except Exception as exc:
        # `error` is the concise, agent-facing message (no internal file/line
        # noise from dsos's own frames), with the registered tables' schema
        # appended (feedback #8) so a column/table typo is fixable from this
        # message alone; the full traceback goes in stderr, which is where a
        # real unhandled exception would print it anyway.
        status, error = "error", f"{type(exc).__name__}: {exc}{_schema_hint(inputs)}"
        stderr_text = traceback.format_exc()
        result = pd.DataFrame()
    finally:
        duck.close()

    run = {"status": status, "error": error, "stderr": stderr_text,
           "stdout": stdout.getvalue(), "result": result}
    if scratch:
        return _scratch_payload(run, inputs, kind="sql", code=code, title=title,
                                 description=description, output_type="query")

    return _persist_run(
        conn, kind="sql", run=run, input_row_ids=input_row_ids, session_id=session_id,
        title=title, description=description, output_type="query", started_at=started_at,
        code=code,
    )


def _run_python_inprocess(*, code: str, inputs: list, code_paths: list[str] | None) -> dict:
    """The default fast path: exec in this process. Same run-dict shape as
    the sandbox path, so everything downstream is identical."""
    stdout, stderr = io.StringIO(), io.StringIO()
    namespace: dict[str, Any] = {"pd": pd}
    for art in inputs:
        namespace[safe_table_name(art.title)] = art.content

    added_paths: list[str] = []
    try:
        if code_paths:
            # Temporary sys.path additions, always restored — the server's
            # process must not leak one run's import paths into the next.
            for p in code_paths:
                if not Path(p).is_dir():
                    raise ValueError(f"code_path {p!r} is not a directory")
                sys.path.insert(0, p)
                added_paths.append(p)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(code, namespace)  # noqa: S102 — intentional: this is the product
        if "result" not in namespace:
            raise ValueError("run_python code must assign a `result` variable")
        status, error, result = "ok", None, namespace["result"]
    except ImportError as exc:
        # Feedback #7: a missing package used to cost a full guess-and-fail
        # cycle. Name what *is* available instead — and how to get more.
        status, error = "error", f"{type(exc).__name__}: {exc}. {_missing_package_hint()}"
        stderr.write(traceback.format_exc())
        result = pd.DataFrame()
    except Exception as exc:
        # `error` is the concise, agent-facing message; the full traceback is
        # appended to stderr, same as a real unhandled exception would do.
        status, error = "error", f"{type(exc).__name__}: {exc}"
        stderr.write(traceback.format_exc())
        result = pd.DataFrame()
    finally:
        for p in added_paths:
            sys.path.remove(p)
    return {"status": status, "error": error, "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(), "result": result}


def run_python(
    conn: sqlite3.Connection, *, code: str, session_id: str, title: str, description: str,
    input_row_ids: list[str], output_type: str = "transform", output_format: str = "parquet",
    scratch: bool = False, requirements: list[str] | None = None,
    code_paths: list[str] | None = None,
) -> str | dict:
    """Runs `code` with each input artifact bound to a variable named after
    its title (lowercased, non-alnum -> `_`), plus `pd`. The code must set a
    variable named `result`; that becomes the new artifact's content — unless
    scratch=True, in which case the payload comes back inline and nothing is
    persisted.

    With `requirements` (non-empty), the code instead runs in a uv-resolved
    throwaway subprocess environment; the result flows back through the same
    save/record/lineage path."""
    started_at = _now()
    inputs = [get_artifact_by_row_id(conn, rid) for rid in input_row_ids]

    try:
        _check_inputs(inputs)
        if requirements:
            from dsos import sandbox
            run = sandbox.run_subprocess(
                code=code, requirements=list(requirements),
                code_paths=code_paths, inputs=inputs,
            )
        else:
            run = _run_python_inprocess(code=code, inputs=inputs, code_paths=code_paths)
    except Exception as exc:
        # dsos-side failures around the run (missing-blob inputs, bad
        # code_paths, sandbox plumbing) — user-code failures are already
        # captured inside the run dicts above.
        run = {"status": "error", "error": f"{type(exc).__name__}: {exc}",
               "stdout": "", "stderr": traceback.format_exc(), "result": pd.DataFrame()}

    if run["status"] == "ok":
        run["result"] = _coerce_chart_result(run["result"])

    if scratch:
        return _scratch_payload(run, inputs, kind="python", code=code, title=title,
                                 description=description, output_type=output_type)

    return _persist_run(
        conn, kind="python", run=run, input_row_ids=input_row_ids, session_id=session_id,
        title=title, description=description, output_type=output_type, started_at=started_at,
        code=code,
    )


def _infer_format(result: Any) -> str:
    if isinstance(result, pd.DataFrame):
        return "parquet"
    if isinstance(result, bytes):
        return "png"
    if isinstance(result, (list, dict)):
        return "json"
    return "markdown"
