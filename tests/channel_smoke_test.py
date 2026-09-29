"""Channel smoke test: a released dsos and a source-checkout dsos keep apart.

Both run on one machine — prod serving the store real work lives in, dev
serving test stores — and before channels they shared one default port, so
whichever daemon started second found it taken. And the daemon only found
that out AFTER opening (and migrating) its store, then exited with nothing
but a uvicorn warning to show for it.

Covered here:

1. channel(): a source checkout is `dev`, a package with no pyproject beside
   it is `prod`, and DSOS_CHANNEL overrides either.
2. port_in_use() sees a listener on the loopback address AND one bound to
   every interface — the Windows case where a daemon could otherwise bind
   127.0.0.1:<port> alongside a `python -m http.server <port>`.
3. choose_port(): an explicit port is used exactly or refused, naming the
   holder; with no port the first free one in the range is taken.
4. End to end: a daemon given a port another dsos daemon holds exits with
   code 4, names that daemon's channel and store, and never creates or
   migrates its own store. A daemon given no port lands in its channel's
   range and reports the channel in /healthz and its manifest.

Run: .venv/Scripts/python.exe tests/channel_smoke_test.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DB_DIR = REPO_ROOT / "data" / "test-runs" / "channel_smoke_test"
shutil.rmtree(DB_DIR, ignore_errors=True)
DB_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DSOS_DB_PATH"] = str(DB_DIR / "unused.db")

from dsos import channel as channel_mod  # noqa: E402
from dsos.daemon import PORT_RANGES, PortUnavailable, choose_port, port_in_use  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listener(host: str = "127.0.0.1", port: int = 0) -> socket.socket:
    s = socket.socket()
    s.bind((host, port))
    s.listen()
    return s


def check_channel() -> None:
    saved = os.environ.pop(channel_mod.CHANNEL_ENV, None)
    try:
        check("a source checkout is the dev channel", channel_mod.channel() == "dev",
              channel_mod.channel())
        os.environ[channel_mod.CHANNEL_ENV] = "prod"
        check("DSOS_CHANNEL=prod overrides detection", channel_mod.channel() == "prod")
        os.environ[channel_mod.CHANNEL_ENV] = "banana"
        check("an unknown DSOS_CHANNEL falls back to detection",
              channel_mod.channel() == "dev")
        os.environ.pop(channel_mod.CHANNEL_ENV)

        # A release: the same module file in a package with no pyproject.toml
        # beside it, which is what a wheel in site-packages looks like.
        site = DB_DIR / "site-packages" / "dsos"
        site.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / "dsos" / "channel.py", site / "channel.py")
        spec = importlib.util.spec_from_file_location("released_channel", site / "channel.py")
        released = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(released)
        check("an installed package with no pyproject beside it is the prod channel",
              released.channel() == "prod", released.channel())
    finally:
        os.environ.pop(channel_mod.CHANNEL_ENV, None)
        if saved is not None:
            os.environ[channel_mod.CHANNEL_ENV] = saved


def check_ports() -> None:
    check("the prod and dev ranges do not overlap",
          not set(PORT_RANGES["prod"]) & set(PORT_RANGES["dev"]))

    held = listener()
    port = held.getsockname()[1]
    check("a loopback listener makes its port in use", port_in_use("127.0.0.1", port))
    held.close()
    check("a closed port is free again", not port_in_use("127.0.0.1", port))

    wildcard = listener("0.0.0.0")
    wport = wildcard.getsockname()[1]
    check("a listener on every interface makes the loopback port in use too",
          port_in_use("127.0.0.1", wport))

    try:
        choose_port("127.0.0.1", wport, "dev")
        check("an explicit busy port is refused", False, "no PortUnavailable")
    except PortUnavailable as exc:
        check("an explicit busy port is refused, naming a non-dsos holder",
              "not a dsos daemon" in str(exc) and str(wport) in str(exc), str(exc))

    free = free_port()
    check("an explicit free port is used exactly",
          choose_port("127.0.0.1", free, "dev") == free)

    busy_first = listener()
    first = busy_first.getsockname()[1]
    # A two-port candidate range whose first port is taken: the second is
    # chosen. Built around real ports so the test cannot collide with a
    # daemon the developer is running in the real dev range.
    second = free_port()
    candidates = range(first, first + 1)
    try:
        choose_port("127.0.0.1", None, "dev", candidates=candidates)
        check("a range with no free port is refused", False, "no PortUnavailable")
    except PortUnavailable as exc:
        check("a range with no free port is refused", "taken" in str(exc), str(exc))
    if second == first + 1:
        check("the first free port in the range is taken",
              choose_port("127.0.0.1", None, "dev", candidates=range(first, second + 1))
              == second)
    busy_first.close()
    wildcard.close()


def launch(name: str, args: list[str], **env: str) -> subprocess.Popen:
    full_env = {**os.environ, **env}
    full_env.pop("DSOS_PORT", None)
    log = open(DB_DIR / f"{name}.log", "w", encoding="utf-8")
    return subprocess.Popen([sys.executable, "-m", "dsos.daemon", *args], cwd=str(REPO_ROOT),
                            env=full_env, stdout=log, stderr=subprocess.STDOUT)


def read_log(name: str) -> str:
    return (DB_DIR / f"{name}.log").read_text(encoding="utf-8", errors="replace")


def healthz(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def wait_healthz(url: str, proc: subprocess.Popen, seconds: float = 120) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline and proc.poll() is None:
        answer = healthz(url)
        if answer:
            return answer
        time.sleep(0.3)
    return None


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def check_daemons() -> None:
    first_db = DB_DIR / "first" / "store.db"
    port = free_port()
    first = launch("first", ["--port", str(port)], DSOS_DB_PATH=str(first_db),
                   DSOS_CHANNEL="dev")
    try:
        answer = wait_healthz(f"http://127.0.0.1:{port}", first)
        check("a daemon on an explicit port reports its channel in /healthz",
              bool(answer) and answer.get("channel") == "dev", str(answer))
        manifest = json.loads((first_db.parent / "store.db.daemon.json").read_text(
            encoding="utf-8")) if (first_db.parent / "store.db.daemon.json").exists() else {}
        check("...and in its manifest", manifest.get("channel") == "dev", str(manifest))

        second_db = DB_DIR / "second" / "store.db"
        second = launch("second", ["--port", str(port)], DSOS_DB_PATH=str(second_db))
        try:
            second.wait(timeout=60)
        except subprocess.TimeoutExpired:
            second.kill()
        log = read_log("second")
        check("a daemon given a port another daemon holds exits with code 4",
              second.returncode == 4, f"exit {second.returncode}: {log[-300:]}")
        check("...naming the holder's channel and store",
              "dsos dev daemon" in log and str(first_db) in log, log[-400:])
        check("...without creating or migrating its own store", not second_db.exists(),
              str(second_db))
        check("the first daemon is undisturbed",
              healthz(f"http://127.0.0.1:{port}") is not None)
    finally:
        stop(first)

    auto_db = DB_DIR / "auto" / "store.db"
    auto = launch("auto", [], DSOS_DB_PATH=str(auto_db), DSOS_CHANNEL="dev")
    try:
        deadline = time.time() + 120
        manifest_file = auto_db.parent / "store.db.daemon.json"
        while time.time() < deadline and not manifest_file.exists() and auto.poll() is None:
            time.sleep(0.3)
        record = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
        chosen = record.get("port")
        check("a dev daemon with no port takes one from the dev range",
              chosen in PORT_RANGES["dev"], f"{record} / {read_log('auto')[-300:]}")
        answer = wait_healthz(record.get("base_url", ""), auto) if chosen else None
        check("...and serves there", bool(answer) and answer.get("channel") == "dev", str(answer))
    finally:
        stop(auto)


def main() -> int:
    check_channel()
    check_ports()
    check_daemons()
    shutil.rmtree(DB_DIR, ignore_errors=True)
    if FAILURES:
        print(f"\nchannel smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\nchannel smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
