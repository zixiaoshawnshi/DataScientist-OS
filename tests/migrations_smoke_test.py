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

Plus the spine schema itself (WP-E1's M3), which is where the constraints
that have to survive a file on disk are defined:

6. A v2 store's legacy `ready`/`ok` artifact rows become `result`, and its
   `error` rows are left exactly as they were.
7. `validations` is append-only in the schema, not in convention: UPDATE
   and DELETE both abort.
8. The verdict/by/question-status CHECK constraints reject bad values.
9. A store created fresh and a store migrated from v2 end up with the same
   schema — the property that makes "run the migrations" and "start empty"
   interchangeable, and that no other test in the suite would catch.

Plus the index the spine's hot paths lean on (M4, TTD U8), and the race
between two openers of the same file:

10. A fresh store and a migrated one both index tool_calls(session_id), and
    the abandon sweep's lookup actually uses it.
11. A v3 store upgrades to v4 and leaves a .bak-v3 of itself behind.
12. Two connections migrating the same store at once — the daemon and a
    standalone `python -m dsos.gui`, say — both succeed. Checked both
    deterministically (the other opener runs the whole migration between
    this one's version check and its first migration) and with real threads.
    Before the fix, the loser re-ran M3's ALTER TABLE ADD COLUMN and died on
    a duplicate column.
13. The same race against a NEWER dsos is still refused, not waved through
    as "nothing left to do".

Run: .venv/Scripts/python.exe tests/migrations_smoke_test.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DSOS_DB_PATH"] = "data/test-runs/migrations_smoke_test/store.db"
ROOT = Path(os.environ["DSOS_DB_PATH"]).parent
shutil.rmtree(ROOT, ignore_errors=True)

from dsos import db  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos import embeddings  # noqa: E402 — for a right-sized embedding blob

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


# --- WP-E1: the spine schema (M3) ------------------------------------------
#
# The v2 store below is built by running the real MIGRATIONS list truncated
# to 2, not by hard-coding a schema the way V03_SCHEMA above does. The
# hard-coded fixture is right for a v0.3 store — a store that predates
# dsos.db's SCHEMA entirely, which is exactly what that check is for — but
# check 9 compares a migrated store against a fresh one character by
# character, so a hand-written v2 fixture would drift the moment a word of
# dsos/db.py changed and every difference would read as a migration bug. A
# v2 store produced by the real code is what a user who ran an earlier
# release actually has on disk.

V2_STATUSES = [("r-ready", "ready"), ("r-ok", "ok"), ("r-error", "error")]


def build_v2_store(path: Path) -> None:
    """A store at user_version 2 holding one artifact row per legacy
    status, so M3's status mapping can be checked against all three."""
    with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:2]):
        conn = db.connect(path)
    try:
        conn.execute("INSERT INTO sessions VALUES ('s0', 'a v2 question', '2026-01-01')")
        for row_id, status in V2_STATUSES:
            conn.execute(
                "INSERT INTO artifacts (row_id, artifact_id, version, type, title,"
                " description, tags, content_ref, content_format, created_at,"
                " embedding, session_id, status) VALUES (?, ?, 1, 'query', ?, ?,"
                " '[]', 'x.sql', 'sql', '2026-01-01', ?, 's0', ?)",
                (row_id, row_id, row_id, f"a {status} row", bytes(4 * embeddings.DIM), status),
            )
        conn.commit()
    finally:
        conn.close()
    check("the fixture really is a v2 store", user_version(path) == 2, f"v{user_version(path)}")


def schema_of(path: Path) -> dict[str, str]:
    """Every declared object in a store, as {name: normalised SQL}, with
    fts5's shadow tables left out — they are an implementation detail of
    the virtual table, and what this compares is what dsos declares."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        con.close()
    out = {}
    for name, sql in rows:
        if name.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
            continue
        out[name] = " ".join(sql.split())
    return out


def test_legacy_statuses_become_results() -> None:
    spine = ROOT / "spine" / "store.db"
    build_v2_store(spine)
    conn = db.connect(spine)
    try:
        rows = dict(conn.execute("SELECT row_id, status FROM artifacts"))
        check("M3 maps a legacy 'ready' row to 'result'", rows.get("r-ready") == "result",
              str(rows.get("r-ready")))
        check("M3 maps a legacy 'ok' row to 'result'", rows.get("r-ok") == "result",
              str(rows.get("r-ok")))
        check("M3 leaves a legacy 'error' row alone", rows.get("r-error") == "error",
              str(rows.get("r-error")))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(artifacts)")}
        check("M3 adds confidence, caveats and superseded_by to artifacts",
              {"confidence", "caveats", "superseded_by"} <= cols, str(sorted(cols)))
        sess_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        check("M3 adds kind, question_id and client to sessions",
              {"kind", "question_id", "client"} <= sess_cols, str(sorted(sess_cols)))
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM sessions")}
        check("an existing session gets kind='producer'", kinds == {"producer"}, str(kinds))
    finally:
        conn.close()


def test_validations_are_append_only() -> None:
    spine = ROOT / "spine" / "store.db"
    conn = db.connect(spine)
    try:
        conn.execute(
            "INSERT INTO validations (id, row_id, verdict, by, session_id, at, basis)"
            " VALUES ('v1', 'r-ready', 'confirmed', 'model', 's0', '2026-01-02', 'recomputed')"
        )
        conn.execute(
            "INSERT INTO validations (id, row_id, verdict, by, session_id, at, basis)"
            " VALUES ('v2', 'r-ready', 'contradicted', 'human', 's0', '2026-01-03', 'checked')"
        )
        conn.commit()
        check("a validations row can be appended",
              conn.execute("SELECT COUNT(*) FROM validations").fetchone()[0] == 2)

        messages = []
        for statement in ("UPDATE validations SET verdict = 'stale' WHERE id = 'v1'",
                          "DELETE FROM validations WHERE id = 'v1'"):
            try:
                conn.execute(statement)
                conn.commit()
                messages.append("")
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                messages.append(str(exc))
        check("UPDATE on validations aborts", "validations are append-only" in messages[0],
              messages[0] or "the UPDATE was allowed")
        check("DELETE on validations aborts", "validations are append-only" in messages[1],
              messages[1] or "the DELETE was allowed")
        check("an aborted write leaves the rows alone",
              conn.execute("SELECT COUNT(*) FROM validations").fetchone()[0] == 2)
    finally:
        conn.close()


def test_check_constraints() -> None:
    spine = ROOT / "spine" / "store.db"
    conn = db.connect(spine)
    try:
        rejected = []
        for bad in ("probably", "a robot"):
            column, value = ("verdict", bad) if bad == "probably" else ("by", bad)
            try:
                conn.execute(
                    "INSERT INTO validations (id, row_id, verdict, by, at, basis)"
                    " VALUES ('bad', 'r-ok', ?, ?, 'now', 'because')",
                    (value, value) if column == "verdict" else ("confirmed", value),
                )
                conn.commit()
                rejected.append("")
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                rejected.append(str(exc))
        check("a verdict outside the four allowed values is rejected",
              "CHECK" in rejected[0].upper(), rejected[0] or "it was accepted")
        check("a 'by' that is neither model, human nor derived:* is rejected",
              "CHECK" in rejected[1].upper(), rejected[1] or "it was accepted")

        # derived:<how> is the escape hatch, so it has to work or every
        # computed check would have to be recorded as a human/model claim.
        conn.execute(
            "INSERT INTO validations (id, row_id, verdict, by, at, basis)"
            " VALUES ('vd', 'r-ok', 'confirmed', 'derived:schema', 'now', 'same query, same data')"
        )
        conn.commit()
        check("by='derived:...' is accepted", True)

        status_error = ""
        try:
            conn.execute(
                "INSERT INTO questions (id, question, status, created_at)"
                " VALUES ('q-bad', 'is it so?', 'maybe', 'now')"
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            status_error = str(exc)
        check("a question status outside the four allowed values is rejected",
              "CHECK" in status_error.upper(), status_error or "it was accepted")

        conn.execute(
            "INSERT INTO questions (id, question, hypothesis, status, asked_by, created_at)"
            " VALUES ('q1', 'is it so?', 'yes', 'in_progress', 's0', 'now')"
        )
        # The sync from questions into questions_fts is the code's job
        # (WP-E3); what is checked here is that the virtual table this
        # migration created is searchable and joins back to its row.
        conn.execute(
            "INSERT INTO questions_fts (id, question, hypothesis)"
            " VALUES ('q1', 'is it so?', 'yes')"
        )
        conn.commit()
        check("a valid question can be created", True)
        hit = conn.execute(
            "SELECT q.id FROM questions_fts JOIN questions q ON q.id = questions_fts.id"
            " WHERE questions_fts MATCH 'so' AND q.id = 'q1'"
        ).fetchall()
        check("questions_fts indexes the question text and resolves back to the row",
              bool(hit), "" if hit else "no fts hit")
    finally:
        conn.close()


def test_fresh_and_migrated_agree() -> None:
    fresh = ROOT / "compare-fresh" / "store.db"
    legacy = ROOT / "compare-legacy" / "store.db"
    db.connect(fresh).close()
    build_v2_store(legacy)
    db.connect(legacy).close()

    a, b = schema_of(fresh), schema_of(legacy)
    only_fresh = sorted(set(a) - set(b))
    only_legacy = sorted(set(b) - set(a))
    differing = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    check("a fresh store and a migrated store declare the same objects",
          not only_fresh and not only_legacy,
          f"only fresh: {only_fresh}, only migrated: {only_legacy}")
    check("a fresh store and a migrated store declare the same SQL", not differing,
          f"{len(a)} objects compared" if not differing else "\n" + "\n".join(
              f"      {k}\n        fresh:    {a[k]}\n        migrated: {b[k]}" for k in differing))
    ready_default = [schema.get("artifacts", "").count("DEFAULT 'ready'") for schema in (a, b)]
    check("the status column keeps its 'ready' default in both", ready_default == [1, 1],
          "" if ready_default == [1, 1] else f"{ready_default} occurrences of the default")
    indexed = ["questions(status)" in schema.get("idx_questions_status", "") for schema in (a, b)]
    check("both stores index questions(status)", indexed == [True, True],
          "" if indexed == [True, True] else str(indexed))
    check("both stores carry the two append-only triggers",
          {"validations_no_update", "validations_no_delete"} <= set(a)
          and {"validations_no_update", "validations_no_delete"} <= set(b),
          str(sorted(set(a) & set(b))))
    by_session = ["tool_calls(session_id)" in schema.get("idx_tool_calls_session_id", "")
                  for schema in (a, b)]
    check("both stores index tool_calls(session_id)", by_session == [True, True],
          "" if by_session == [True, True] else str(by_session))


# --- M4 and the two-openers race --------------------------------------------


def build_store_at(path: Path, version: int) -> None:
    """A store at user_version `version`, made by the real MIGRATIONS list
    truncated to it (see build_v2_store for why not a hard-coded schema),
    holding one session and one tool call so a backup has rows to keep."""
    with mock.patch.object(db, "MIGRATIONS", db.MIGRATIONS[:version]):
        conn = db.connect(path)
    try:
        conn.execute("INSERT INTO sessions (id, question, started_at)"
                     " VALUES ('s0', 'a question', '2026-01-01')")
        conn.execute("INSERT INTO tool_calls VALUES"
                     " ('t0', 's0', '2026-01-01', 'search', '{}', '', '[]')")
        conn.commit()
    finally:
        conn.close()
    check(f"the fixture really is a v{version} store", user_version(path) == version,
          f"v{user_version(path)}")


def test_session_index_is_used() -> None:
    fresh = ROOT / "m4-fresh" / "store.db"
    conn = db.connect(fresh)
    try:
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_tool_calls_session_id'"
        ).fetchone()
        check("a fresh store indexes tool_calls(session_id)",
              index is not None and "tool_calls(session_id)" in index[0], str(index and index[0]))
        # The shape of spine.sweep_abandoned's and lease_state's activity
        # read. The index is only worth a migration if SQLite picks it.
        plan = " ".join(r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(ts) FROM tool_calls WHERE session_id = ?", ("s",)))
        check("the per-session activity lookup uses the index",
              "idx_tool_calls_session_id" in plan, plan)
    finally:
        conn.close()


def test_v3_store_upgrades_to_v4() -> None:
    v3 = ROOT / "v3" / "store.db"
    build_store_at(v3, 3)
    conn = db.connect(v3)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        check("a v3 store migrates to the latest version", version == len(db.MIGRATIONS),
              f"user_version={version}")
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        check("M4 adds idx_tool_calls_session_id to a v3 store",
              "idx_tool_calls_session_id" in names)
        kept = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        check("the v3 store's tool calls survive", kept == 1, f"{kept} rows")
    finally:
        conn.close()
    backup = v3.parent / "store.db.bak-v3"
    check("migrating a v3 store writes a .bak-v3 copy", backup.exists(), str(backup))
    if backup.exists():
        con = sqlite3.connect(backup)
        try:
            bversion = con.execute("PRAGMA user_version").fetchone()[0]
            bindex = con.execute("SELECT COUNT(*) FROM sqlite_master"
                                 " WHERE name = 'idx_tool_calls_session_id'").fetchone()[0]
        finally:
            con.close()
        check("the .bak-v3 is the store as it was before M4",
              bversion == 3 and bindex == 0, f"user_version={bversion}, index present: {bool(bindex)}")


def _migrate_with_interloper(path: Path, interloper) -> tuple[int | None, str]:
    """Open `path`, and migrate it — but run `interloper(path)` from a
    second connection at the worst possible moment: after this connection has
    read user_version and decided what is pending, before it applies any of
    it. That window is where two processes opening one store actually
    collide, and a thread race only lands in it some of the time.

    Returns (the version migrate() reported, any error text)."""
    conn = db._open(path)
    real_apply = db._apply
    fired = []

    def racing_apply(*args, **kwargs):
        if not fired:
            fired.append(True)
            interloper(path)
        return real_apply(*args, **kwargs)

    try:
        with mock.patch.object(db, "_apply", racing_apply):
            try:
                return db.migrate(conn, path), ""
            except db.MigrationError as exc:
                return None, str(exc)
    finally:
        conn.close()


def _other_opener_migrates(path: Path) -> None:
    db.connect(path).close()


def test_racing_openers_deterministic() -> None:
    for label, build in (("a v0.3 store", build_v03_store),
                         ("an empty store", lambda p: p.parent.mkdir(parents=True, exist_ok=True))):
        target = ROOT / "race-fixed" / label.replace(" ", "-").replace(".", "") / "store.db"
        build(target)
        version, error = _migrate_with_interloper(target, _other_opener_migrates)
        check(f"{label}: the second opener's migrate() does not fail", not error, error)
        check(f"{label}: it reports the latest version", version == len(db.MIGRATIONS),
              f"reported {version}")
        check(f"{label}: the store ends at the latest version",
              user_version(target) == len(db.MIGRATIONS), f"v{user_version(target)}")
    legacy = ROOT / "race-fixed" / "a-v03-store" / "store.db"
    con = sqlite3.connect(legacy)
    try:
        rows = con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    finally:
        con.close()
    check("the raced legacy store keeps its rows", rows == len(V03_ARTIFACTS), f"{rows} rows")


def test_racing_openers_threaded() -> None:
    """The same race with no hook in it: two threads, two connections, one
    barrier, several rounds. Nondeterministic by nature, which is why the
    deterministic version above is the one that pins the fix down; this is
    the check that nothing else about two real openers collides (the
    backup, the lock wait) either."""
    errors: list[str] = []
    for round_number in range(10):
        target = ROOT / "race-threads" / f"round-{round_number}" / "store.db"
        if round_number % 2:
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            build_v03_store(target)
        barrier = threading.Barrier(2)

        def opener(name: str) -> None:
            # The barrier is before _open, not after it: opening is part of
            # the race (the first opener of a legacy file converts it to WAL),
            # and a thread that failed there must not leave the other one
            # waiting at a barrier forever — hence the timeout as well.
            conn = None
            try:
                barrier.wait(timeout=30)
                conn = db._open(target)
                db.migrate(conn, target)
            except Exception as exc:  # noqa: BLE001 — collected and reported
                errors.append(f"round {round_number} {name}: {type(exc).__name__}: {exc}")
            finally:
                if conn is not None:
                    conn.close()

        threads = [threading.Thread(target=opener, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if user_version(target) != len(db.MIGRATIONS):
            errors.append(f"round {round_number}: ended at v{user_version(target)}")
        backups = sorted(p.name for p in target.parent.glob("store.db.bak-*"))
        expected = [] if round_number % 2 else ["store.db.bak-v0"]
        if backups != expected:
            errors.append(f"round {round_number}: backups {backups}, expected {expected}")
    check("two threads racing migrate() on one store both succeed, every round",
          not errors, "; ".join(errors[:3]))


def test_racing_newer_store_is_refused() -> None:
    target = ROOT / "race-newer" / "store.db"
    build_store_at(target, 2)
    newer = len(db.MIGRATIONS) + 1

    def newer_dsos_migrates(path: Path) -> None:
        con = sqlite3.connect(path)
        try:
            con.execute(f"PRAGMA user_version = {newer}")
            con.commit()
        finally:
            con.close()

    version, error = _migrate_with_interloper(target, newer_dsos_migrates)
    check("a store a newer dsos migrated mid-open is still refused",
          error == f"store was created by a newer dsos (schema v{newer}); upgrade dsos.",
          error or f"migrate() returned {version}")
    check("and is left at the newer dsos's version", user_version(target) == newer,
          f"v{user_version(target)}")


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
    case("a v2 store's legacy statuses become results", test_legacy_statuses_become_results)
    case("validations are append-only in the schema", test_validations_are_append_only)
    case("the spine CHECK constraints reject bad values", test_check_constraints)
    case("a fresh store and a migrated store agree", test_fresh_and_migrated_agree)
    case("the tool_calls(session_id) index is there and used", test_session_index_is_used)
    case("a v3 store upgrades to v4", test_v3_store_upgrades_to_v4)
    case("two openers racing migrate() both succeed", test_racing_openers_deterministic)
    case("two threads racing migrate() both succeed", test_racing_openers_threaded)
    case("a newer store is refused even mid-race", test_racing_newer_store_is_refused)

    shutil.rmtree(ROOT, ignore_errors=True)
    if FAILURES:
        print(f"\nmigrations smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\nmigrations smoke test passed.")


if __name__ == "__main__":
    main()
