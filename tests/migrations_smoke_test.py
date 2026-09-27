"""Layer-2a smoke test: numbered migrations (tests/../dsos/db.py MIGRATIONS).

The store is a file on the user's disk that outlives any given dsos release,
so opening it has to be able to move it forward (and, just as importantly, to
refuse a store it doesn't understand). Covered here:

1. A fresh store ends at the latest schema version.
2. A v0.3 store — the schema as it was before content_hash, hard-coded below
   rather than imported, so this test keeps passing when dsos.db's SCHEMA moves
   on — upgrades in place, keeps its rows, gains content_hash, and leaves a
   .bak-v0 copy behind to migrate back from.
3. Reopening a current store changes nothing and writes no second backup.
4. A migration that raises part-way through rolls back: no half-applied
   schema, no bumped user_version, and an error naming which migration failed.
5. A store from a newer dsos is refused rather than opened.

Run: .venv/Scripts/python.exe tests/migrations_smoke_test.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DSOS_DB_PATH"] = "data/test-runs/migrations_smoke_test/store.db"
ROOT = Path(os.environ["DSOS_DB_PATH"]).parent
shutil.rmtree(ROOT, ignore_errors=True)

from dsos import db  # noqa: E402 — import after DSOS_DB_PATH is set

# The v0.3 schema, verbatim: the current SCHEMA minus the content_hash column
# and minus the index over it, since a store without the column could not have
# had the index either. Hard-coded so this test is a check against dsos.db's
# SCHEMA, not a restatement of it.
V03_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE artifacts (
    row_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    content_format TEXT NOT NULL,
    source TEXT,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL DEFAULT 'ready',
    UNIQUE (artifact_id, version)
);

CREATE INDEX idx_artifacts_artifact_id ON artifacts(artifact_id);
CREATE INDEX idx_artifacts_session_id ON artifacts(session_id);

CREATE VIRTUAL TABLE artifacts_fts USING fts5(
    row_id UNINDEXED,
    title,
    description,
    tags
);

CREATE TABLE lineage (
    child_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    parent_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    PRIMARY KEY (child_row_id, parent_row_id)
);

CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    output_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    error TEXT,
    output_summary TEXT NOT NULL
);

CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ts TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    artifact_row_ids TEXT NOT NULL
);
"""

V03_ARTIFACTS = [
    ("r1", "a1", 1, "dataset", "Passengers", "who was aboard", "[]", "p.csv", "csv", "2026-01-01"),
    ("r2", "a2", 1, "query", "Fares by class", "fare spread", "[]", "f.sql", "sql", "2026-01-02"),
    ("r3", "a3", 1, "chart", "Survival", "survival rate", "[]", "s.png", "png", "2026-01-03"),
]

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def case(label: str, fn) -> None:
    """Run one scenario, reporting a crash as a failed check rather than
    letting it hide every check after it."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — a broken scenario is a failure
        check(label, False, f"{type(exc).__name__}: {exc}")


def user_version(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def build_v03_store(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(V03_SCHEMA)
    con.execute("INSERT INTO sessions VALUES ('s0', 'how did fares relate to class?', '2026-01-01')")
    con.executemany(
        "INSERT INTO artifacts (row_id, artifact_id, version, type, title, description,"
        " tags, content_ref, content_format, created_at, embedding, session_id, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, X'00', 's0', 'ready')",
        V03_ARTIFACTS,
    )
    con.commit()
    con.close()


def test_fresh_store() -> None:
    fresh = ROOT / "fresh" / "store.db"
    conn = db.connect(fresh)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        check("a fresh store ends at the latest schema version",
              version == len(db.MIGRATIONS), f"user_version={version}, latest={len(db.MIGRATIONS)}")
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table')")}
        check("a fresh store has the baseline tables", {"artifacts", "sessions", "executions"} <= tables,
              str(sorted(tables)))
    finally:
        conn.close()
    check("a store that had nothing to migrate is not backed up",
          not (fresh.parent / "store.db.bak-v0").exists())


def test_legacy_store_upgrades() -> None:
    legacy = ROOT / "legacy" / "store.db"
    build_v03_store(legacy)
    check("the v0.3 store starts at user_version 0", user_version(legacy) == 0)

    conn = db.connect(legacy)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        check("a v0.3 store migrates to the latest version", version == len(db.MIGRATIONS),
              f"user_version={version}")
        rows = conn.execute("SELECT row_id, title FROM artifacts ORDER BY row_id").fetchall()
        check("the legacy rows survive the migration",
              [r["title"] for r in rows] == [a[4] for a in V03_ARTIFACTS], str([r["title"] for r in rows]))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(artifacts)")}
        check("a store that predates content_hash gains the column", "content_hash" in cols)
        nulls = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE content_hash IS NOT NULL").fetchone()[0]
        check("the added column is empty, not invented", nulls == 0, f"{nulls} backfilled")
    finally:
        conn.close()

    backup = legacy.parent / "store.db.bak-v0"
    check("migrating an existing store writes a .bak-v0 copy", backup.exists(), str(backup))
    if backup.exists():
        con = sqlite3.connect(backup)
        try:
            kept = con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            bcols = {r[1] for r in con.execute("PRAGMA table_info(artifacts)")}
        finally:
            con.close()
        check("the backup is the pre-migration store, not a copy of the result",
              kept == 3 and "content_hash" not in bcols, f"{kept} rows, content_hash in backup: "
              f"{'content_hash' in bcols}")


def test_reopen_is_a_no_op() -> None:
    legacy = ROOT / "legacy" / "store.db"
    before = sorted(p.name for p in legacy.parent.iterdir())
    conn = db.connect(legacy)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    finally:
        conn.close()
    after = sorted(p.name for p in legacy.parent.iterdir())
    check("reopening a current store is a no-op", version == len(db.MIGRATIONS) and rows == 3,
          f"user_version={version}, {rows} rows")
    check("reopening a current store writes no second backup", before == after,
          f"{before} -> {after}")


def test_failing_migration_rolls_back() -> None:
    target = ROOT / "failing" / "store.db"
    conn = db.connect(target)
    conn.close()
    start_version = user_version(target)
    next_number = len(db.MIGRATIONS) + 1

    def _boom(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_applied (x TEXT)")
        raise RuntimeError("deliberate migration failure")

    message = None
    with mock.patch.object(db, "MIGRATIONS", list(db.MIGRATIONS) + [_boom]):
        try:
            db.connect(target)
        except db.MigrationError as exc:
            message = str(exc)
    check("a failing migration raises MigrationError", message is not None, str(message))
    if message is not None:
        check("the error names the migration that failed", f"migration {next_number}" in message, message)
        check("the error carries the underlying cause", "deliberate migration failure" in message, message)
    check("a failed migration does not bump user_version", user_version(target) == start_version,
          f"user_version={user_version(target)}, was {start_version}")
    con = sqlite3.connect(target)
    try:
        leaked = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'half_applied'").fetchone()[0]
    finally:
        con.close()
    check("a failed migration rolls its own work back", leaked == 0)


def test_newer_store_is_refused() -> None:
    newer = ROOT / "newer" / "store.db"
    newer.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(newer)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()

    message = None
    try:
        db.connect(newer)
    except db.MigrationError as exc:
        message = str(exc)
    check("a store from a newer dsos is refused", message is not None, str(message))
    if message is not None:
        check("the refusal says which version and what to do",
              message == "store was created by a newer dsos (schema v99); upgrade dsos.", message)
    check("a refused store is left at its own version", user_version(newer) == 99)


def main() -> None:
    case("a fresh store migrates to the latest version", test_fresh_store)
    case("a v0.3 store upgrades in place", test_legacy_store_upgrades)
    case("reopening a current store changes nothing", test_reopen_is_a_no_op)
    case("a failing migration rolls back", test_failing_migration_rolls_back)
    case("a newer store is refused", test_newer_store_is_refused)

    shutil.rmtree(ROOT, ignore_errors=True)
    if FAILURES:
        print(f"\nmigrations smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nmigrations smoke test passed.")


if __name__ == "__main__":
    main()
