"""Subprocess sandbox: the only place run_python code actually executes.

The server process itself never execs agent code — every run_python call
runs against a configured `python_path` (the user's own analysis Python by
default, or whatever a specific call overrides it to; see mcp_server.py's
DEFAULT_PYTHON_PATH and execution.run_python). That interpreter usually
already has whatever the code needs; `requirements=[...]` is only for
filling gaps in it:

    1. serialize input artifacts to temp files (parquet/json/bin/text)
    2. write a wrapper script that re-binds them as variables and execs the
       agent's code at module level
    3. run the wrapper — directly via `python_path` if `requirements` is
       empty, or via `uv run --quiet --no-project --python <python_path>
       --with <req>... wrapper.py` if not (wheels are cached globally by
       uv, so repeat runs cost ~a second, first runs pay the download)
    4. the wrapper serializes `result` back (parquet/png/json/text) and
       touches an _ok marker; the parent loads it and hands it to the
       normal save/record/lineage path.

`requirements` entries are a verbatim passthrough to uv's `--with`, so
anything uv accepts works: PyPI specs ("scikit-learn>=1.3"), local package
directories, wheels, git URLs. Path-style requirements need the agent and
the server to share a filesystem — mcp_server's docstring says so.

Everything here runs in the *target* interpreter's environment, never the
server's own — the server's process and venv are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from dsos.store import Artifact, safe_table_name  # shared binding-name rule for in-process and subprocess runs

DEFAULT_TIMEOUT_S = 300
_OK_MARKER = "_dsos_ok"

# The wrapper template. Tokens are replaced in a fixed order, with the
# agent's code LAST — replacement values are never re-scanned, so user code
# containing token-like text can never corrupt the substitutions above it.
_WRAPPER_TEMPLATE = '''\
import json
import sys
import traceback
from pathlib import Path

for _p in #__DSOS_CODE_PATHS__#:
    sys.path.insert(0, _p)

import pandas as pd

try:
    # Same reasoning as execution.py's module-level matplotlib.use("Agg"):
    # force the headless backend before the agent's code can import pyplot
    # and pick the platform default, which assumes a GUI main-loop thread
    # this subprocess doesn't have.
    import matplotlib
    matplotlib.use("Agg", force=True)
except ImportError:
    pass

#__DSOS_CHART_STYLE__#

#__DSOS_INPUT_BINDINGS__#

#__DSOS_USER_CODE__#

# --- coerce a matplotlib Figure/Axes `result` to png bytes (feedback #2) —
# this always runs in a separate process from the server, so the coercion
# has to happen here rather than wherever `result` gets set ---
_r = result
try:
    from matplotlib.axes import Axes as _Axes
    from matplotlib.figure import Figure as _Figure
    if isinstance(_r, _Axes):
        _r = _r.get_figure()
    if isinstance(_r, _Figure):
        import io as _io
        _buf = _io.BytesIO()
        _r.savefig(_buf, format="png", bbox_inches="tight")
        _r = _buf.getvalue()
except ImportError:
    pass

# --- serialize `result` back for the parent process ---
if isinstance(_r, pd.DataFrame):
    _r.to_parquet(r#__DSOS_RESULT_PARQUET__#)
elif isinstance(_r, bytes):
    Path(r#__DSOS_RESULT_PNG__#).write_bytes(_r)
elif isinstance(_r, (dict, list)):
    Path(r#__DSOS_RESULT_JSON__#).write_text(
        json.dumps(_r, default=str, indent=2), encoding="utf-8"
    )
elif isinstance(_r, str):
    Path(r#__DSOS_RESULT_TEXT__#).write_text(_r, encoding="utf-8")
else:
    Path(r#__DSOS_RESULT_TEXT__#).write_text(str(_r), encoding="utf-8")
Path(r#__DSOS_OK_MARKER__#).touch()
'''


def find_uv() -> str | None:
    """Locate the uv binary: pip-installed uv sits next to the interpreter
    (which may not be on PATH); fall back to PATH."""
    exe = "uv.exe" if os.name == "nt" else "uv"
    beside = Path(sys.executable).parent / exe
    if beside.is_file():
        return str(beside)
    return shutil.which("uv")


def _write_inputs(workdir: Path, inputs: list[Artifact]) -> list[str]:
    """Serialize each input artifact to a file and emit the wrapper lines
    that bind it back to a variable named after its title — mirroring the
    in-process namespace exactly (same safe_table_name)."""
    bindings: list[str] = []
    for art in inputs:
        name = safe_table_name(art.title)
        content = art.content
        path = workdir / f"input_{name}"
        if isinstance(content, pd.DataFrame):
            file = path.with_suffix(".parquet")
            content.to_parquet(file)
            bindings.append(f"{name} = pd.read_parquet({str(file)!r})")
        elif isinstance(content, bytes):
            file = path.with_suffix(".bin")
            file.write_bytes(content)
            bindings.append(f"{name} = Path({str(file)!r}).read_bytes()")
        elif isinstance(content, (dict, list)):
            file = path.with_suffix(".json")
            file.write_text(json.dumps(content, default=str, indent=2), encoding="utf-8")
            bindings.append(
                f"{name} = json.loads(Path({str(file)!r}).read_text(encoding='utf-8'))"
            )
        else:
            file = path.with_suffix(".txt")
            file.write_text(str(content), encoding="utf-8")
            bindings.append(f"{name} = Path({str(file)!r}).read_text(encoding='utf-8')")
    return bindings


def _load_result(workdir: Path) -> Any:
    if (workdir / "result.parquet").exists():
        return pd.read_parquet(workdir / "result.parquet")
    if (workdir / "result.png").exists():
        return (workdir / "result.png").read_bytes()
    if (workdir / "result.json").exists():
        return json.loads((workdir / "result.json").read_text(encoding="utf-8"))
    return (workdir / "result.txt").read_text(encoding="utf-8")


def _timeout_s() -> int:
    try:
        return int(os.environ.get("DSOS_SANDBOX_TIMEOUT", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _build_wrapper(
    *, code: str, code_paths: list[str] | None, chart_style: str | None,
    bindings: list[str], workdir: Path,
) -> str:
    """Assemble the wrapper script: token substitution in a fixed order,
    with the agent's code LAST — replacement values are never re-scanned,
    so user code containing token-like text can never corrupt the
    substitutions above it. Split out of run_subprocess so the wiring
    (chart style lines, input bindings, result paths) is checkable without
    paying a uv run."""
    from dsos import templating

    return (_WRAPPER_TEMPLATE
            .replace("#__DSOS_CODE_PATHS__#", repr(list(code_paths or [])))
            .replace("#__DSOS_CHART_STYLE__#", templating.sandbox_style_block(chart_style))
            .replace("#__DSOS_INPUT_BINDINGS__#", "\n".join(bindings))
            .replace("#__DSOS_RESULT_PARQUET__#", repr(str(workdir / "result.parquet")))
            .replace("#__DSOS_RESULT_PNG__#", repr(str(workdir / "result.png")))
            .replace("#__DSOS_RESULT_JSON__#", repr(str(workdir / "result.json")))
            .replace("#__DSOS_RESULT_TEXT__#", repr(str(workdir / "result.txt")))
            .replace("#__DSOS_OK_MARKER__#", repr(str(workdir / _OK_MARKER)))
            .replace("#__DSOS_USER_CODE__#", code))  # user code last


def run_interpreter(
    python_path: str, args: list[str], *, cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run `python_path args...`, capturing output — the plumbing shared by
    run_subprocess's no-`requirements` path and templating's chart-style
    validation. Both just need "run something in this interpreter, get
    stdout/stderr back"; callers own their own error shape (a run dict here,
    a ValueError there)."""
    return subprocess.run(
        [python_path, *args], capture_output=True, cwd=cwd,
        # encoding explicit: on a GBK-locale Windows machine the default
        # would mangle/replace the subprocess's Unicode output — same bug
        # class as the blob-storage fix.
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def run_subprocess(
    *, code: str, requirements: list[str], code_paths: list[str] | None,
    inputs: list[Artifact], python_path: str, chart_style: str | None = None,
) -> dict:
    """Run the agent's code against `python_path` — directly if
    `requirements` is empty (the common case: that interpreter already has
    what the code needs), or via a uv-resolved ephemeral environment built
    on top of it if not (uv's job is filling gaps, not resolving the whole
    stack every time). Returns status/error/stdout/stderr/result — the run
    dict execution.py's save/record/lineage tail expects.

    chart_style: an already-resolved style path (see execution.run_python)
    applied in the wrapper before the user code. (No reset needed for None
    here: each subprocess starts from raw matplotlib defaults.)"""
    if not Path(python_path).is_absolute():
        return _error_run(
            f"python_path {python_path!r} must be an absolute path — a relative "
            "one resolves against this server's launch cwd, not the caller's"
        )
    if not Path(python_path).is_file():
        return _error_run(
            f"python_path {python_path!r} is not a file — resolve the "
            "interpreter's own executable path (not a directory, not "
            "bare `python`/`python3` off PATH)"
        )

    for p in code_paths or []:
        if not Path(p).is_dir():
            return _error_run(f"code_path {p!r} is not a directory")

    uv = None
    if requirements:
        uv = find_uv()
        if uv is None:
            return _error_run(
                "uv is not installed — install it (e.g. `pip install uv`) to use "
                f"requirements={requirements!r}. Meanwhile, run the code with your "
                "own bash tools and register the result via save_artifact."
            )

    with tempfile.TemporaryDirectory(prefix="dsos_sandbox_") as tmp:
        workdir = Path(tmp)
        bindings = _write_inputs(workdir, inputs)

        wrapper = _build_wrapper(
            code=code, code_paths=code_paths, chart_style=chart_style,
            bindings=bindings, workdir=workdir,
        )
        wrapper_path = workdir / "wrapper.py"
        wrapper_path.write_text(wrapper, encoding="utf-8")

        if uv is not None:
            run_cmd, args = uv, ["run", "--quiet", "--no-project", "--python", python_path]
            for req in requirements:
                args += ["--with", req]
            args.append(str(wrapper_path))
        else:
            run_cmd, args = python_path, [str(wrapper_path)]

        try:
            proc = run_interpreter(run_cmd, args, cwd=workdir, timeout=_timeout_s())
        except subprocess.TimeoutExpired:
            return _error_run(
                f"sandbox run exceeded {_timeout_s()}s (DSOS_SANDBOX_TIMEOUT) — "
                "uv resolution can be slow on first use; retry, or cache the "
                "work with your own bash tools."
            )

        if proc.returncode != 0 or not (workdir / _OK_MARKER).exists():
            return {
                **_error_run(_concise_error(proc)),
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
            }
        return {
            "status": "ok", "error": None, "stdout": proc.stdout or "",
            "stderr": proc.stderr or "", "result": _load_result(workdir),
        }


def _concise_error(proc: subprocess.CompletedProcess) -> str:
    """One agent-facing line from uv/user stderr — uv's resolution failures
    end with `error: <why>`, user tracebacks end with the exception line. A
    missing-module failure additionally gets a requirements= hint appended:
    the target interpreter is arbitrary and external, so (unlike the old
    in-process sandbox) there's no cheap way to also list what it already
    has installed — just point at the escape hatch."""
    lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
    if not lines:
        return f"sandbox run failed with exit code {proc.returncode}"
    last = lines[-1].strip()
    stderr = proc.stderr or ""
    if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
        return (
            f"{last} — pass requirements=[...] to run_python (uv resolves "
            "missing packages into a throwaway environment on top of "
            "python_path for this one call), or compute it with your own "
            "tools (bash) and register the result via save_artifact."
        )
    return last


def _error_run(error: str) -> dict:
    return {"status": "error", "error": error, "stdout": "", "stderr": "", "result": pd.DataFrame()}
