"""Layer-2c smoke test: the server is a package of factories, not one module
with a connection in it (WP-C1).

Before this WP, `dsos/mcp_server.py` was the whole of layer 2: it built a
`FastMCP`, opened a store connection at import time against whatever
`DSOS_DB_PATH` happened to say, and registered nine tools that all closed
over that one connection (ten since WP-E2 added `mark`, twelve since WP-E3
added close_question and record_decision). Two consequences,
both of which the next three WPs
(D1's daemon, F1's consumer, E2's lifecycle) had to build on:

- A process could not have two servers over two stores, because "the
  connection" was a module global. Everything that needs a second profile
  over the same store — a producer and a consumer in one daemon — has to be
  able to hold two handles at once.
- `import dsos.mcp_server` opened a database, and picked the path from an
  environment variable, at *import* time. Anything that imported it before
  the environment was set (or imported it only to ask for its version) was
  reading and creating a store as a side effect.

So the shape this pins down is: `build_producer(config)` /
`build_consumer(config)` return FastMCP servers, each tool is a closure over
one `ServerConfig`, and the only module that knows how to build a `Database`
or read `DSOS_DB_PATH` is the thin `dsos/mcp_server.py` shim.

Three things, all of them things a reviewer can otherwise only check by
reading:

1. Only `dsos/server/`, `dsos/mcp_server.py` and `dsos/daemon.py` may
   import `fastmcp` — the dependency that makes a module layer 2 rather
   than layer 1. A stray `from fastmcp import ...` in `store.py` would be
   invisible in review and fatal to the daemon.
2. No `connect(`/`Database(` call at module scope anywhere in
   `dsos/server/`. A module-scope call is the old bug in a new location: it
   would run on import, before any config existed, and bind every server
   built in the process to one store.
3. Two producers built on two temp stores in one process, with a write
   through one leaving the other empty. This is the test that would have
   caught the module-global connection, and it is the only one that
   exercises the factories as D1 will use them (built, not imported).

`dsos/daemon.py` in the allow-list does not exist yet (WP-D1). The scan
iterates the files that exist and matches the allow-list by name, so a
missing file is simply nothing to allow — not a missing path to open.

Run: .venv/Scripts/python.exe tests/layering_smoke_test.py
"""

from __future__ import annotations

import ast
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
DSOS_DIR = REPO_ROOT / "dsos"
SERVER_DIR = DSOS_DIR / "server"

# Where a `fastmcp` import is allowed to live, as repo-relative posix paths.
# A directory entry covers everything under it; a file entry covers itself.
FASTMCP_ALLOWED = ("dsos/server/", "dsos/mcp_server.py", "dsos/daemon.py")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_allowed(rel: str) -> bool:
    return any(rel == entry or rel.startswith(entry) for entry in FASTMCP_ALLOWED)


def _parse(path: Path) -> ast.Module:
    # encoding explicit: the locale codec on a Chinese-locale Windows
    # machine is GBK and these files are utf-8.
    return ast.parse(path.read_text(encoding="utf-8"), filename=_rel(path))


def _fastmcp_importers() -> list[str]:
    """Every module outside the allow-list that imports fastmcp."""
    hits: list[str] = []
    for path in sorted(DSOS_DIR.rglob("*.py")):
        rel = _rel(path)
        if _is_allowed(rel):
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] == "fastmcp" for alias in node.names
            ):
                hits.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(
                "."
            )[0] == "fastmcp":
                hits.append(f"{rel}:{node.lineno}")
    return hits


def _module_scope_conn_calls() -> list[str]:
    """connect(...) / Database(...) executed while a module is imported."""
    if not SERVER_DIR.is_dir():
        return [f"{_rel(SERVER_DIR)}/ does not exist — the server is still one module"]
    hits: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            # A class body's method is still deferred to call time; the
            # class body's own statements run at import.
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        def visit_Call(self, node: ast.Call) -> None:
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if self.depth == 0 and name in ("connect", "Database"):
                hits.append(f"{self.current}:{node.lineno} calls {name}()")
            self.generic_visit(node)

        current = ""

    for path in sorted(SERVER_DIR.rglob("*.py")):
        Visitor.current = _rel(path)
        Visitor().visit(_parse(path))
    return hits


async def _two_producers_two_stores() -> list[str]:
    """Build two producers over two temp stores and write through one."""
    from fastmcp import Client

    from dsos.db import Database
    from dsos.server import ServerConfig, build_consumer, build_producer

    problems: list[str] = []
    # mkdtemp + a tolerant rmtree rather than TemporaryDirectory: the
    # Database connections the tool threads opened are still open here, and
    # Windows refuses to unlink a file something else holds open.
    root = Path(tempfile.mkdtemp())
    try:
        # Two temp subdirectories, not two files in one: blobs live
        # beside the db file, and a shared blobs/ dir would make the
        # isolation this is testing an illusion.
        db_a = Database(root / "a" / "store.db")
        db_b = Database(root / "b" / "store.db")
        config_a = ServerConfig(db=db_a, python_path=sys.executable, base_url=None)
        config_b = ServerConfig(db=db_b, python_path=sys.executable, base_url=None)
        producer_a = build_producer(config_a)
        producer_b = build_producer(config_b)

        tools_a = await producer_a.list_tools()
        if len(tools_a) != 12:
            problems.append(f"producer A exposes {len(tools_a)} tools, not 12")
        consumer_tools = await build_consumer(config_a).list_tools()
        # Asserted here because a consumer that accidentally inherited the
        # producer's registrations would be invisible in every other test.
        # It asserted ZERO for the whole of the producer's life, which is a
        # count that had to be edited by whoever added the first consumer
        # tool; what the check is actually for is that the two surfaces do
        # not bleed into each other, so that is what it says now.
        leaked = sorted({t.name for t in consumer_tools} & {t.name for t in tools_a})
        if leaked:
            problems.append(f"the consumer exposes the producer's {leaked}")
        if not consumer_tools:
            problems.append("the consumer exposes no tools at all")

        async with Client(producer_a) as client:
            r = await client.call_tool("start_session", {"question": "isolation check"})
            session_id = r.data["session_id"]
            r = await client.call_tool("save_artifact", {
                "type": "dataset", "title": "Only In A",
                "description": "Written through producer A, to prove B's store is untouched.",
                "content_format": "csv", "content_text": "team,score\na,10\n",
                "session_id": session_id,
            })
            written = r.data.get("row_id")

        count_a = db_a.conn().execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
        count_b = db_b.conn().execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
        sessions_b = db_b.conn().execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        if count_a != 1:
            problems.append(f"producer A's store has {count_a} artifacts, expected 1 ({written})")
        if count_b != 0 or sessions_b != 0:
            problems.append(
                f"producer B's store saw A's write: {count_b} artifacts, {sessions_b} sessions"
            )
        # The tool-call log has to land in the store the tool was served
        # from, for the same reason: it is the reuse signal and the claim
        # lease, and a row in the wrong store is invisible rather than wrong.
        calls_b = [r["tool_name"] for r in db_b.conn().execute("SELECT tool_name FROM tool_calls")]
        calls_a = [r["tool_name"] for r in db_a.conn().execute("SELECT tool_name FROM tool_calls")]
        if calls_a != ["start_session", "save_artifact"] or calls_b:
            problems.append(f"tool_calls: A={calls_a}, B={calls_b} (expected both calls in A only)")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return problems


async def main() -> None:
    print("1. fastmcp is imported only by the server layer")
    offenders = _fastmcp_importers()
    check("no fastmcp import outside dsos/server/, mcp_server.py, daemon.py",
          not offenders,
          ", ".join(offenders) if offenders else
          f"allowed: {', '.join(FASTMCP_ALLOWED)} (dsos/daemon.py is WP-D1; absent is fine)")

    print("\n2. no store handle is built at import time inside dsos/server/")
    conn_calls = _module_scope_conn_calls()
    check("no connect()/Database() at module scope in dsos/server/",
          not conn_calls,
          "; ".join(conn_calls) if conn_calls else "clean")

    print("\n3. two producers, two temp stores, one process")
    try:
        problems = await _two_producers_two_stores()
    except ImportError as exc:
        problems = [f"dsos.server does not expose the factories yet: {exc}"]
    check("a write through one producer leaves the other store empty",
          not problems,
          "; ".join(problems) if problems else "isolated")

    if FAILURES:
        print(f"\nlayering smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nlayering smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
