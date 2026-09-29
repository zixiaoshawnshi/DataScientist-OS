"""Run every tests/*_test.py as its own subprocess and summarise the result.

Each smoke test is a standalone script that exits non-zero on failure, and
several of them share process-wide state (an env var, a store under
data/test-runs/, a seeded model cache). Running them in one process would let
an early failure or a leaked env var decide the outcome of every script after
it, so each gets its own interpreter — that is the only way the "one script,
one verdict" model the tests are written against survives a red suite.

What this runner is *for* is the merge-time legibility the rest of the suite
does not have: a single pass/fail line per script, in a stable order, so two
runs are diffable, and the full output of every failing script printed at the
end, so a red suite says what broke rather than just how much.

Usage:
    .venv/Scripts/python tests/run_all.py
    .venv/Scripts/python tests/run_all.py --only lifecycle   # substring filter

Exits 1 if any script failed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


def resolve_python() -> str:
    """Pick the interpreter the child scripts run under.

    The tests need pandas/pyarrow/duckdb, so they must run under the venv that
    has them, not under whatever `python` happens to be on PATH. Order:

    1. ``DSOS_PYTHON`` — explicit override.
    2. ``sys.executable`` — the normal case, and correct precisely because
       this script is itself started with the venv's python. It is also the
       only thing that works in a git worktree, where the venv lives in the
       main checkout and no ``.venv`` exists next to these tests.
    3. ``python`` off PATH — last resort, so the runner still says something
       useful rather than failing on a missing path.

    There is deliberately no auto-detection of a sibling ``.venv``: guessing
    that the caller's interpreter is "wrong" would silently ignore the one
    they chose, which is the same class of surprise as a stray test store.
    Set ``DSOS_PYTHON`` instead.
    """
    override = os.environ.get("DSOS_PYTHON")
    if override:
        # Checked up front rather than per script: a mistyped override would
        # otherwise only surface as a FileNotFoundError after every earlier
        # script had already run.
        if not Path(override).exists():
            raise SystemExit(f"DSOS_PYTHON points at nothing: {override}")
        return override
    if sys.executable:
        return sys.executable
    return shutil.which("python") or "python"


def discover(only: str | None) -> list[Path]:
    """Every *_test.py, sorted by name so the output is diffable run to run."""
    scripts = sorted(TESTS_DIR.glob("*_test.py"), key=lambda p: p.name)
    if only:
        scripts = [p for p in scripts if only in p.name]
    return scripts


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every tests/*_test.py in its own subprocess.")
    parser.add_argument("--only", help="substring filter on the script filename")
    args = parser.parse_args(argv)

    # A failing child's output is decoded as utf-8, so it can contain
    # characters this terminal's own codepage cannot encode; replace rather
    # than let the summary itself die halfway through a report.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    python = resolve_python()
    scripts = discover(args.only)
    if not scripts:
        print("no test scripts found" + (f" matching {args.only!r}" if args.only else ""))
        return 0

    print(f"python: {python}")
    print(f"running {len(scripts)} test script(s) from {TESTS_DIR.name}/\n")

    failures: list[tuple[Path, float, str]] = []
    started = time.perf_counter()
    for script in scripts:
        began = time.perf_counter()
        # cwd is the repo root because the tests' DSOS_DB_PATH values are
        # repo-relative (data/test-runs/<name>/store.db); run them from
        # anywhere and they'd scatter stores into the caller's directory.
        completed = subprocess.run(
            [python, str(script)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            # encoding explicit: on a GBK-locale Windows machine a child
            # writing its own stdout in the console codepage emits bytes that
            # are not utf-8, and a strict decode in subprocess' reader thread
            # would turn a test failure into a runner crash.
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - began
        ok = completed.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}  {script.name:<40} {format_duration(elapsed):>8}")
        if not ok:
            failures.append((script, elapsed, (completed.stdout or "") + (completed.stderr or "")))

    total = time.perf_counter() - started
    print(
        f"\n{len(scripts) - len(failures)}/{len(scripts)} passed"
        f" in {format_duration(total)}"
    )

    if failures:
        for script, elapsed, output in failures:
            rule = "=" * 70
            print(f"\n{rule}\nFAILED: {script.name} ({format_duration(elapsed)})\n{rule}")
            sys.stdout.write(output)
            if output and not output.endswith("\n"):
                sys.stdout.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
