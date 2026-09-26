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
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from dsos.store import get_artifact_by_row_id, safe_table_name, save_artifact

_STDOUT_LIMIT = 2_000
_STDERR_LIMIT = 4_000  # tail-capped: the traceback's final frames are the useful ones


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


def _scratch_payload(run: dict, input_row_ids: list[str]) -> dict:
    """The inline response for a scratch run: same shape as a persisted
    run's payload (status/stdout/result preview), minus anything that
    implies an artifact exists, plus scratch=True."""
    from dsos import present

    payload: dict[str, Any] = {
        "status": run["status"],
        "stdout": run["stdout"][:_STDOUT_LIMIT],
        "scratch": True,
        "artifact_row_ids": list(input_row_ids),
    }
    if run["status"] == "ok":
        payload.update(present.result_payload(run["result"]))
    else:
        payload["error"] = run["error"]
        payload["stderr"] = run["stderr"][-_STDERR_LIMIT:]
    return payload


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
        # noise from dsos's own frames); the full traceback goes in stderr,
        # which is where a real unhandled exception would print it anyway.
        status, error = "error", f"{type(exc).__name__}: {exc}"
        stderr_text = traceback.format_exc()
        result = pd.DataFrame()
    finally:
        duck.close()

    run = {"status": status, "error": error, "stderr": stderr_text,
           "stdout": stdout.getvalue(), "result": result}
    if scratch:
        return _scratch_payload(run, input_row_ids)

    output_row_id = save_artifact(
        conn, type="query", title=title, description=description,
        content=result, content_format="parquet", session_id=session_id,
        parent_row_ids=input_row_ids, status=status,
    )
    _record_execution(
        conn, output_row_id=output_row_id, kind="sql", code=code, started_at=started_at,
        status=status, stdout=run["stdout"], stderr=stderr_text, error=error,
        output_summary=_output_summary(result),
    )
    return output_row_id


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

    if scratch:
        return _scratch_payload(run, input_row_ids)

    status, error, result = run["status"], run["error"], run["result"]
    output_format = _infer_format(result) if status == "ok" else "parquet"
    output_row_id = save_artifact(
        conn, type=output_type, title=title, description=description,
        content=result, content_format=output_format, session_id=session_id,
        parent_row_ids=input_row_ids, status=status,
    )
    _record_execution(
        conn, output_row_id=output_row_id, kind="python", code=code, started_at=started_at,
        status=status, stdout=run["stdout"], stderr=run["stderr"], error=error,
        output_summary=_output_summary(result),
    )
    return output_row_id


def _infer_format(result: Any) -> str:
    if isinstance(result, pd.DataFrame):
        return "parquet"
    if isinstance(result, bytes):
        return "png"
    if isinstance(result, (list, dict)):
        return "json"
    return "markdown"
