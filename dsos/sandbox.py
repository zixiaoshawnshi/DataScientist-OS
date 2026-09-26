"""Subprocess sandbox for run_python `requirements` (feedback #1, Option B).

When an agent asks for packages this sandbox doesn't have, we don't install
into the server's own environment (that would permanently mutate shared
state and risk breaking the server). Instead uv resolves the requirements
into a *throwaway* environment, runs the code there, and discards it:

    1. serialize input artifacts to temp files (parquet/json/bin/text)
    2. write a wrapper script that re-binds them as variables and execs the
       agent's code at module level
    3. `uv run --quiet --no-project --python <server's interpreter>
       --with <req>... wrapper.py` — wheels are cached globally by uv, so
       repeat runs cost ~a second, first runs pay the download
    4. the wrapper serializes `result` back (parquet/png/json/text) and
       touches an _ok marker; the parent loads it and hands it to the
       normal save/record/lineage path — identical to an in-process run.

`requirements` entries are a verbatim passthrough to uv's `--with`, so
anything uv accepts works: PyPI specs ("scikit-learn>=1.3"), local package
directories, wheels, git URLs. Path-style requirements need the agent and
the server to share a filesystem — mcp_server's docstring says so.

Everything here runs in the *agent's* per-call environment; the server's
process and venv are untouched.
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

#__DSOS_INPUT_BINDINGS__#

#__DSOS_USER_CODE__#

# --- coerce a matplotlib Figure/Axes `result` to png bytes (feedback #2) —
# mirrors execution._coerce_chart_result, duplicated here since this code
# runs in a separate uv-resolved process, not this one ---
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


def run_subprocess(
    *, code: str, requirements: list[str], code_paths: list[str] | None,
    inputs: list[Artifact],
) -> dict:
    """Run the agent's code in a uv-resolved ephemeral environment. Returns
    the same run dict shape as the in-process path: status/error/stdout/
    stderr/result — so execution.py's save/record/lineage tail is identical
    either way."""
    uv = find_uv()
    if uv is None:
        return _error_run(
            "uv is not installed — install it (e.g. `pip install uv`) to use "
            f"requirements={requirements!r}. Meanwhile, run the code with your "
            "own bash tools and register the result via save_artifact."
        )

    for p in code_paths or []:
        if not Path(p).is_dir():
            return _error_run(f"code_path {p!r} is not a directory")

    with tempfile.TemporaryDirectory(prefix="dsos_sandbox_") as tmp:
        workdir = Path(tmp)
        bindings = _write_inputs(workdir, inputs)

        wrapper = (_WRAPPER_TEMPLATE
                   .replace("#__DSOS_CODE_PATHS__#", repr(list(code_paths or [])))
                   .replace("#__DSOS_INPUT_BINDINGS__#", "\n".join(bindings))
                   .replace("#__DSOS_RESULT_PARQUET__#", repr(str(workdir / "result.parquet")))
                   .replace("#__DSOS_RESULT_PNG__#", repr(str(workdir / "result.png")))
                   .replace("#__DSOS_RESULT_JSON__#", repr(str(workdir / "result.json")))
                   .replace("#__DSOS_RESULT_TEXT__#", repr(str(workdir / "result.txt")))
                   .replace("#__DSOS_OK_MARKER__#", repr(str(workdir / _OK_MARKER)))
                   .replace("#__DSOS_USER_CODE__#", code))  # user code last
        wrapper_path = workdir / "wrapper.py"
        wrapper_path.write_text(wrapper, encoding="utf-8")

        cmd = [uv, "run", "--quiet", "--no-project", "--python", sys.executable]
        for req in requirements:
            cmd += ["--with", req]
        cmd.append(str(wrapper_path))

        try:
            # encoding explicit: on a GBK-locale Windows machine the default
            # would mangle/replace the subprocess's Unicode output — same bug
            # class as the blob-storage fix.
            proc = subprocess.run(
                cmd, capture_output=True, cwd=workdir,
                encoding="utf-8", errors="replace", timeout=_timeout_s(),
            )
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
    end with `error: <why>`, user tracebacks end with the exception line."""
    lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
    if not lines:
        return f"sandbox run failed with exit code {proc.returncode}"
    return lines[-1].strip()


def _error_run(error: str) -> dict:
    return {"status": "error", "error": error, "stdout": "", "stderr": "", "result": pd.DataFrame()}
