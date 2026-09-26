"""Execution runner: run_sql / run_python against dataset artifacts.

Every run is recorded (code, timing, status, output) and its output is
saved as a new artifact, lineage-linked to its inputs — automatically, so
there's no separate `record_execution` call the agent could forget to make.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb
import pandas as pd

from dsos.store import get_artifact_by_row_id, save_artifact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_summary(result: Any) -> dict:
    if isinstance(result, pd.DataFrame):
        return {"rows": len(result), "columns": list(result.columns)}
    if isinstance(result, (list, dict)):
        return {"len": len(result)}
    return {}


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


def run_sql(
    conn: sqlite3.Connection, *, code: str, session_id: str, title: str, description: str,
    input_row_ids: list[str],
) -> str:
    """Runs `code` in DuckDB with each input artifact registered as a table
    named after its title (lowercased, non-alnum -> `_`). Result becomes a
    new `query` artifact, lineage-linked to every input.
    """
    started_at = _now()
    stdout = io.StringIO()
    status, error, result = "ok", None, None

    inputs = [get_artifact_by_row_id(conn, rid) for rid in input_row_ids]
    duck = duckdb.connect(":memory:")
    for art in inputs:
        table_name = _safe_table_name(art.title)
        duck.register(table_name, art.content)  # content: pandas.DataFrame

    try:
        with contextlib.redirect_stdout(stdout):
            result = duck.execute(code).fetchdf()
    except Exception:
        status, error = "error", traceback.format_exc()
        result = pd.DataFrame()
    finally:
        duck.close()

    output_row_id = save_artifact(
        conn, type="query", title=title, description=description,
        content=result, content_format="parquet", session_id=session_id,
        parent_row_ids=input_row_ids, status=status,
    )
    _record_execution(
        conn, output_row_id=output_row_id, kind="sql", code=code, started_at=started_at,
        status=status, stdout=stdout.getvalue(), stderr="", error=error,
        output_summary=_output_summary(result),
    )
    return output_row_id


def run_python(
    conn: sqlite3.Connection, *, code: str, session_id: str, title: str, description: str,
    input_row_ids: list[str], output_type: str = "transform", output_format: str = "parquet",
) -> str:
    """Runs `code` with each input artifact bound to a variable named after
    its title (lowercased, non-alnum -> `_`), plus `pd`. The code must set a
    variable named `result`; that becomes the new artifact's content.
    """
    started_at = _now()
    stdout, stderr = io.StringIO(), io.StringIO()
    status, error, result = "ok", None, None

    inputs = [get_artifact_by_row_id(conn, rid) for rid in input_row_ids]
    namespace: dict[str, Any] = {"pd": pd}
    for art in inputs:
        namespace[_safe_table_name(art.title)] = art.content

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(code, namespace)  # noqa: S102 — intentional: this is the product
        if "result" not in namespace:
            raise ValueError("run_python code must assign a `result` variable")
        result = namespace["result"]
    except Exception:
        status, error = "error", traceback.format_exc()
        result = pd.DataFrame()
        output_format = "parquet"

    output_format = _infer_format(result) if status == "ok" else output_format
    output_row_id = save_artifact(
        conn, type=output_type, title=title, description=description,
        content=result, content_format=output_format, session_id=session_id,
        parent_row_ids=input_row_ids, status=status,
    )
    _record_execution(
        conn, output_row_id=output_row_id, kind="python", code=code, started_at=started_at,
        status=status, stdout=stdout.getvalue(), stderr=stderr.getvalue(), error=error,
        output_summary=_output_summary(result),
    )
    return output_row_id


def _safe_table_name(title: str) -> str:
    import re
    name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return name or "t"


def _infer_format(result: Any) -> str:
    if isinstance(result, pd.DataFrame):
        return "parquet"
    if isinstance(result, bytes):
        return "png"
    if isinstance(result, (list, dict)):
        return "json"
    return "markdown"
