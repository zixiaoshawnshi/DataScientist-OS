"""Starting the daemon for a store when a client needs it and none is running.

Before this, installing dsos meant two long-lived things to set up: the MCP
registration, and a `python -m dsos.daemon` somebody had to start by hand
and keep alive. Forget the second and every tool call failed with "daemon
not running" — a regression from the stdio server it replaced, where the
registration was the whole install. This module puts that back: the stdio
shim calls `start_daemon` when no daemon is serving its store, so the
registration is the whole install again, and starting a daemon by hand is
only for people who want to manage it themselves (`DSOS_NO_AUTOSTART=1`).

What the started daemon is:

- **Detached from the client.** It outlives the shim that started it — a
  store's daemon serves every agent and the GUI, so it must not die when the
  first client closes. Its own session/process group on POSIX; on Windows
  DETACHED_PROCESS, and CREATE_BREAKAWAY_FROM_JOB where the client's job
  object allows it (an MCP client that puts its servers in a kill-on-close
  job would otherwise take the daemon down with it).
- **Configured by the shim's environment**, which is the registration's:
  `DSOS_DB_PATH`, `DSOS_PYTHON_PATH`, `DSOS_PORT`, `DSOS_CHANNEL`. A daemon
  that is already running keeps its own; this only decides how a new one
  starts.
- **Running the same dsos as the shim.** Started with this interpreter and
  `-P`, with the directory the shim imported dsos from put first on
  PYTHONPATH — so a released install never picks up a checkout from the
  working directory, and a checkout never picks up a released copy.
- **Writing its output to `<store>.daemon.log`**, never to the shim's
  stdout: that is the client's MCP channel, and one stray line on it breaks
  the protocol.
- **Run from the store's directory**, not the client's: a long-lived
  process holding a project directory as its cwd keeps it from being
  deleted on Windows (a git worktree, a temp checkout).

Two clients starting at once is the ordinary case (an agent with a producer
and a consumer registration starts both shims together), so starting is
guarded by `<store>.daemon.starting`, created exclusively: one shim starts
the daemon, the other waits for the manifest. The daemon's own
one-per-store check is the second line if the lock is ever bypassed.

Import-light on purpose — no fastmcp, no store — because the shim imports it
on every client start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

DISABLE_ENV = "DSOS_NO_AUTOSTART"
TIMEOUT_ENV = "DSOS_AUTOSTART_TIMEOUT"
# A first start migrates the store and builds the server, and a cold machine
# importing pandas, duckdb and fastmcp from a spinning disk is not quick.
DEFAULT_TIMEOUT_SECONDS = 90.0

# dsos.daemon's exit codes that mean "another daemon has this store" (3) —
# the race this module expects and simply waits out — as opposed to a real
# failure worth reporting.
_EXIT_OTHER_DAEMON = 3

# Windows process-creation flags (subprocess exposes only some by name).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class AutostartError(RuntimeError):
    """A daemon could not be started, or did not come up in time. The
    message names the log to read."""


def enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def timeout_seconds() -> float:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def log_path(db_path: Path) -> Path:
    return db_path.parent / f"{db_path.name}.daemon.log"


def lock_path(db_path: Path) -> Path:
    return db_path.parent / f"{db_path.name}.daemon.starting"


def start_daemon(db_path: Path, live_base_url: Callable[[], str | None]) -> str:
    """Make sure a daemon is serving `db_path`, and return its base URL.

    `live_base_url` is the caller's own "is a daemon serving THIS store
    right now" check (manifest, then /healthz naming this store); it is
    passed in rather than reimplemented so the shim and this module cannot
    disagree about what "running" means.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    timeout = timeout_seconds()
    deadline = time.monotonic() + timeout
    lock = lock_path(db_path)
    proc: subprocess.Popen | None = None
    holding = _take_lock(lock, stale_after=timeout)
    try:
        while True:
            url = live_base_url()
            if url:
                return url
            if proc is None and not holding and not lock.exists():
                # Whoever was starting it gave up (or crashed without
                # starting anything); take over rather than wait forever.
                holding = _take_lock(lock, stale_after=timeout)
            if proc is None and holding:
                proc = _spawn(db_path)
            if proc is not None and proc.poll() is not None \
                    and proc.returncode not in (0, _EXIT_OTHER_DAEMON):
                raise AutostartError(
                    f"the dsos daemon for {db_path} exited with code {proc.returncode} "
                    f"while starting. Last lines of {log_path(db_path)}:\n{_tail(db_path)}"
                )
            if time.monotonic() > deadline:
                raise AutostartError(
                    f"no dsos daemon answered for {db_path} within {timeout:.0f}s "
                    f"({TIMEOUT_ENV} to allow longer). Last lines of "
                    f"{log_path(db_path)}:\n{_tail(db_path)}"
                )
            time.sleep(0.3)
    finally:
        if proc is not None and hasattr(proc, "cleanup"):
            proc.cleanup()
        if holding:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def _take_lock(lock: Path, stale_after: float) -> bool:
    """Create the start lock exclusively; True if this process now holds it.
    A lock older than the start timeout belongs to a shim that died
    mid-start, and is taken over rather than obeyed for ever."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age <= stale_after:
                return False
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def _daemon_env(db_path: Path) -> dict[str, str]:
    import dsos

    package_root = str(Path(dsos.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["DSOS_DB_PATH"] = str(db_path)
    env.pop("DSOS_URL", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _spawn(db_path: Path):
    """Start `python -P -m dsos.daemon` for this store, detached. Returns
    something with `.poll()` and `.returncode`, like a Popen."""
    env = _daemon_env(db_path)
    log = open(log_path(db_path), "a", encoding="utf-8")  # noqa: SIM115 — handed to the child
    log.write(f"\n--- {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"started by a dsos shim (pid {os.getpid()}) ---\n")
    log.flush()
    kwargs: dict = dict(
        cwd=str(db_path.parent), env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT, close_fds=True,
    )
    command = [sys.executable, "-P", "-m", "dsos.daemon"]
    try:
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, **kwargs)
        # Windows: WMI first. A process started from here sits inside
        # whatever job objects this one is in, and there can be several: the
        # MCP client's (the MCP Python SDK's is kill-on-close and forbids
        # breakaway) and the venv launcher's (a venv's python.exe is a
        # redirector that runs the real interpreter in its own kill-on-close
        # job). Breakaway handles only the first, and only when it is allowed;
        # a WMI-created process is in none of them.
        try:
            return _spawn_outside_job(db_path, env)
        except AutostartError as exc:
            log.write(f"WMI start failed, starting directly instead: {exc}\n")
            log.flush()
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(
                command, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            # Breakaway refused: start inside the job. The daemon then lives
            # only as long as the client, which still beats not starting.
            return subprocess.Popen(command, creationflags=flags, **kwargs)
    finally:
        log.close()  # the child holds its own handle


# Run by the WMI-created process: WMI starts it with the user's default
# environment and no redirection, so the environment, sys.path, working
# directory and log all travel in a spec file and are restored here before
# dsos is imported at all.
_BOOTSTRAP = "; ".join([
    # One line on purpose: a newline inside the -c argument does not survive
    # the trip through Win32_Process.Create's CommandLine.
    "import json, os, runpy, sys",
    "spec = json.load(open(sys.argv[1], encoding='utf-8'))",
    "os.remove(sys.argv[1])",
    "os.environ.clear()",
    "os.environ.update(spec['env'])",
    "sys.path[:0] = spec['path']",
    "os.chdir(spec['cwd'])",
    "log = open(spec['log'], 'a', encoding='utf-8', buffering=1)",
    "os.dup2(log.fileno(), 1)",
    "os.dup2(log.fileno(), 2)",
    "sys.stdout = sys.stderr = log",
    "sys.argv = ['dsos.daemon']",
    "runpy.run_module('dsos.daemon', run_name='__main__', alter_sys=True)",
])


def _spawn_outside_job(db_path: Path, env: dict[str, str]):
    """Windows only: create the daemon through WMI (Win32_Process.Create).

    The process is created by the WMI service, not by this one, so it is in
    none of this process's job objects — the one reliable way out of a
    kill-on-close job that forbids breakaway. pythonw.exe keeps a console
    window from opening; SW_HIDE is the second belt.
    """
    import json
    import tempfile

    log_offset = log_path(db_path).stat().st_size if log_path(db_path).exists() else 0
    fd, name = tempfile.mkstemp(prefix="dsos-daemon-", suffix=".json")
    os.close(fd)  # written by path below; an open handle would lock it on Windows
    spec_file = Path(name)
    spec_file.write_text(json.dumps({
        "env": env,
        "path": [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p],
        "cwd": str(db_path.parent),
        "log": str(log_path(db_path)),
    }), encoding="utf-8")
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    command = subprocess.list2cmdline([
        str(windowless if windowless.exists() else interpreter), "-P", "-c", _BOOTSTRAP,
        str(spec_file),
    ])
    script = (
        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
        "-Property @{ShowWindow=[uint16]0}; "
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        "@{CommandLine=$env:DSOS_SPAWN_COMMAND; CurrentDirectory=$env:DSOS_SPAWN_CWD; "
        "ProcessStartupInformation=$si}; "
        "if ($r.ReturnValue -ne 0) { exit 100 + $r.ReturnValue }; $r.ProcessId"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "DSOS_SPAWN_COMMAND": command, "DSOS_SPAWN_CWD": str(db_path.parent)},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=60,
    )
    if done.returncode != 0 or not done.stdout.strip().isdigit():
        spec_file.unlink(missing_ok=True)
        raise AutostartError(
            f"could not start a dsos daemon outside this client's job object via WMI "
            f"(exit {done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}"
        )
    return _WindowsProcess(int(done.stdout.strip()), spec_file, log_path(db_path), log_offset)


class _WindowsProcess:
    """The slice of Popen start_daemon uses, for a daemon started through WMI.

    The pid WMI hands back is not the daemon's: a venv's python(w).exe is a
    launcher that runs the real interpreter as a child and may exit on its
    own schedule, so its exit code says nothing about the daemon. What does
    is the log this start is writing to, read from where it stood before the
    start: a traceback, or one of the daemon's own refusals, is an exit.
    """

    def __init__(self, pid: int, spec_file: Path, log: Path, log_offset: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        # The bootstrap deletes this as soon as it has read it; this is the
        # backstop for a bootstrap that never got that far, because the file
        # holds a copy of the whole environment.
        self.spec_file = spec_file
        self._log = log
        self._offset = log_offset

    def cleanup(self) -> None:
        self.spec_file.unlink(missing_ok=True)

    def poll(self) -> int | None:
        try:
            with open(self._log, encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                written = handle.read()
        except OSError:
            return self.returncode
        if "dsos: a dsos daemon is already serving" in written:
            self.returncode = _EXIT_OTHER_DAEMON
        elif "Traceback (most recent call last)" in written or any(
                marker in written for marker in
                ("dsos: port ", "dsos: every port", "dsos: could not bind",
                 "dsos: refusing to bind", "is not a port number")):
            self.returncode = 1
        return self.returncode


def _tail(db_path: Path, lines: int = 15) -> str:
    try:
        text = log_path(db_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log written)"
    return "\n".join(text.strip().splitlines()[-lines:]) or "(log is empty)"
