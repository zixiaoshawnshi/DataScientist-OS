"""SQLite schema and connection helper for the DS Artifact OS store.

Layer 1 (core library): no MCP, no HTTP — just the data model from the
design doc (Design/DS Artifact OS — Design Doc.md, "Data model" section),
plus the numbered migrations that move an existing store file forward.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    started_at TEXT NOT NULL
);

-- One row per *version*, not per logical artifact.
-- (artifact_id, version) is the pin used by lineage and narrative embeds.
CREATE TABLE IF NOT EXISTS artifacts (
    row_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    type TEXT NOT NULL,              -- dataset | query | transform | chart | narrative | skill
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,              -- JSON list
    content_ref TEXT NOT NULL,       -- path under data/blobs/
    content_format TEXT NOT NULL,    -- csv | parquet | python | sql | markdown | json | png
    content_hash TEXT,               -- fingerprint of the stored content; identical
                                      -- content is registered once, not per session
    source TEXT,                     -- JSON: {url, fetched_at} for fetched datasets, else NULL
    embedding BLOB NOT NULL,         -- float32 vector, see dsos/embeddings.py
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL DEFAULT 'ready',
    UNIQUE (artifact_id, version)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_artifact_id ON artifacts(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_session_id ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_content_hash ON artifacts(type, content_hash);

-- Keyword/tag fallback alongside embedding search. Plain (non-external-content)
-- FTS5 table, kept in sync manually in store.save_artifact — artifacts.row_id
-- is a TEXT uuid, which external-content FTS5 (rowid-aliased) doesn't support.
CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    row_id UNINDEXED,
    title,
    description,
    tags
);

-- Edges between specific versions (row_ids), not logical artifacts, so
-- {{artifact:id@v2}} embeds and future staleness checks always resolve
-- to a fixed row.
CREATE TABLE IF NOT EXISTS lineage (
    child_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    parent_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    PRIMARY KEY (child_row_id, parent_row_id)
);

-- The transparency layer. output_row_id is the artifact this execution
-- produced; inputs are just that row's parents in `lineage` — no separate
-- input field.
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    output_row_id TEXT NOT NULL REFERENCES artifacts(row_id),
    kind TEXT NOT NULL,              -- sql | python
    code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok | error
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    error TEXT,
    output_summary TEXT NOT NULL     -- JSON: row count, shape, etc.
);

-- The free agent trace. artifact_row_ids drives the reuse badge/metric:
-- any row touched whose session_id differs from the current session is
-- a genuine reuse, no manual bookkeeping.
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ts TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_summary TEXT NOT NULL,
    artifact_row_ids TEXT NOT NULL   -- JSON list
);
"""


class MigrationError(RuntimeError):
    """A migration could not be applied. The store is left at the version it
    was at before the migration started — nothing is half-applied."""


# A migration moves the store from PRAGMA user_version n-1 to n: it is given
# the connection, already inside its own transaction, and either finishes or
# leaves the store exactly as it found it. Append new migrations to
# MIGRATIONS; never edit one that has shipped, because a store that has run
# it only ever runs the entries after it.
Migration = Callable[[sqlite3.Connection], None]


def _split_statements(script: str) -> list[str]:
    """Split a DDL script into individual statements.

    `executescript()` can't be used inside a migration: it issues an implicit
    COMMIT before running, which would end the transaction the migration
    owns. So the script is split by hand, text kept verbatim, because SQLite
    stores the statement's own text in sqlite_master — a store created with
    the comments stripped out of SCHEMA would not match one that wasn't.
    Splitting on ';' alone would cut a statement in half at the ';' inside
    SCHEMA's content_hash comment, so a line comment runs on to the end of its
    line instead of terminating anything. String literals and block comments
    are not handled; SCHEMA has neither.
    """
    statements: list[str] = []
    current: list[str] = []
    in_comment = False
    index = 0
    while index < len(script):
        char = script[index]
        if char == "\n":
            in_comment = False
        elif not in_comment and script.startswith("--", index):
            in_comment = True
            current.append("--")
            index += 2
            continue
        elif char == ";" and not in_comment:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


def _m1_baseline(conn: sqlite3.Connection) -> None:
    """v0 -> v1: the schema above, plus the content_hash column that stores
    created before v0.4 predate."""
    # The guarded ALTER runs BEFORE the schema script, because
    # idx_artifacts_content_hash indexes a column a pre-v0.4 store doesn't
    # have yet. On a fresh store the table doesn't exist, the guard skips,
    # and the script creates the table with the column.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(artifacts)")}
    if columns and "content_hash" not in columns:
        conn.execute("ALTER TABLE artifacts ADD COLUMN content_hash TEXT")
    for statement in _split_statements(SCHEMA):
        conn.execute(statement)


MIGRATIONS: list[Migration] = [_m1_baseline]


def _is_empty(conn: sqlite3.Connection) -> bool:
    """True of a store file that has no tables yet. Backing one of those up
    would leave every fresh store littered with a .bak-v0 of nothing."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'").fetchone()
    return not row[0]


def _backup(path: Path, from_version: int) -> None:
    """Copy the store to `<db path>.bak-v<from_version>` before it is changed,
    so a bad migration can be backed out by hand. Uses the backup API rather
    than a file copy, because the store is in WAL mode and a plain copy of the
    main file would miss whatever is still in the -wal."""
    source = sqlite3.connect(path)
    try:
        target = sqlite3.connect(f"{path}.bak-v{from_version}")
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _apply(conn: sqlite3.Connection, number: int, migration: Migration) -> None:
    # BEGIN IMMEDIATE takes the write lock up front rather than discovering at
    # COMMIT time that someone else got there first, and every statement in the
    # migration — including PRAGMA user_version, which is a header write and so
    # rolls back with everything else — lands in one transaction.
    conn.execute("BEGIN IMMEDIATE")
    try:
        migration(conn)
        conn.execute(f"PRAGMA user_version = {number}")
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 — re-raised as MigrationError
        with suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise MigrationError(f"migration {number} failed: {exc}") from exc


def migrate(conn: sqlite3.Connection, path: str | Path) -> int:
    """Bring the store up to the latest schema version, returning its new
    user_version. A store that is already current is left alone, backup and
    all; a store from a newer dsos is refused."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise MigrationError(
            f"store was created by a newer dsos (schema v{version}); upgrade dsos.")
    if version == len(MIGRATIONS):
        return version
    if not _is_empty(conn):
        _backup(Path(path), version)
    # isolation_level=None is what makes BEGIN IMMEDIATE/COMMIT mean what they
    # say: in the default mode sqlite3 opens a transaction of its own around
    # DML and would end the migration's transaction early. Restored on the way
    # out, so callers keep the implicit-transaction behaviour they had before.
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        for number in range(version + 1, len(MIGRATIONS) + 1):
            _apply(conn, number, MIGRATIONS[number - 1])
    finally:
        conn.isolation_level = previous
    return len(MIGRATIONS)


class _Connection(sqlite3.Connection):
    """Plain sqlite3.Connection has no __dict__ (C extension type), so it
    can't hold a stashed db_path attribute directly — subclassing it is the
    standard way to get one. Used only so store.py can find a connection's
    blob dir without every call site (execution.py, seed.py, ...) having to
    thread db_path through separately."""

    _dsos_db_path: str


def _open(db_path: str | Path) -> sqlite3.Connection:
    """Open the store's SQLite file and put the connection in the mode every
    caller expects (row factory, WAL, busy timeout, db path), without
    touching the schema. `connect()` and `Database.conn()` both start here so
    the two cannot drift apart."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False is required, not a shortcut: a sqlite3
    # connection belongs to the thread that opened it, and FastMCP runs tools
    # in a thread pool, so the thread that opens a connection is rarely the
    # one that later uses it. What makes it safe is that each thread has its
    # OWN connection (Database.conn()), never that one is shared.
    conn = sqlite3.connect(path, factory=_Connection, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL: the read-only GUI opens its own connection to this same file while
    # the MCP server is actively writing to it during a live agent run.
    # Default (rollback-journal) mode throws "database is locked" under that
    # read/write overlap; WAL lets readers and a writer coexist.
    conn.execute("PRAGMA journal_mode=WAL")
    # A migration's BEGIN IMMEDIATE, and every ordinary write transaction,
    # waits for whoever else holds the write lock instead of failing
    # immediately — the read-only GUI keeps its own connection open on this
    # file, including across a restart.
    #
    # This is a safety net, not the concurrency story. It does not fire at all
    # for the case that actually bites: a transaction that has already taken a
    # read snapshot and then tries to upgrade to a write gets SQLITE_BUSY
    # (SQLITE_BUSY_SNAPSHOT) returned immediately, with no busy-handler
    # consultation, because waiting cannot help. That is Database.write()'s job.
    conn.execute("PRAGMA busy_timeout=5000")
    conn._dsos_db_path = str(path)
    return conn


def connect(db_path: str | Path = "data/store.db") -> sqlite3.Connection:
    """Open (and if needed, migrate) the store's SQLite database.

    Kept for the callers that want one connection and no concurrency story:
    the GUI, the seeding path, the single-threaded tests, and dsos.mcp_server
    until WP-C1 moves it onto a Database. A connection from here is not safe
    to use from two threads at once; use Database for that.
    """
    conn = _open(db_path)
    migrate(conn, conn._dsos_db_path)
    return conn


# One write lock per PROCESS, not per Database instance. Two handles onto the
# same store file in one process (a test that opens its own, a GUI and a
# server sharing a path) would each serialise against only themselves and so
# would not serialise against each other, which is the entire point. SQLite's
# own file lock is deliberately not a substitute: it is the thing that is
# already failing, and it can only say "wait", never "nobody else is writing".
_WRITE_LOCK = threading.RLock()


class Database:
    """The store handle: one per process, one connection per thread, and one
    process-wide write lock (decision D8).

    Migrations run once, in __init__, not per connection: eight threads each
    running the migration list would be eight writers racing to take the same
    schema version, and the loser would fail on a lock or, worse, on a
    user_version another thread had already bumped.

        db = Database(path)
        with db.write():
            store.save_artifact(db.conn(), ...)

    `write()` is re-entrant (RLock), so a tool that takes it and then calls a
    helper that also takes it does not deadlock; what it costs is that the
    outer block holds the lock for the helper's duration too, which is the
    right answer anyway — the outer block was going to hold it regardless.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        # A throwaway connection for the migration run, so the handle holds
        # no connection of its own and the first conn() in whichever thread
        # asks is the first connection that thread opens.
        bootstrap = _open(self.path)
        try:
            migrate(bootstrap, self.path)
        finally:
            bootstrap.close()
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use and kept for the
        life of the thread.

        Thread-local rather than one shared connection because a single
        sqlite3 connection is not safe to use from two threads at once even
        with check_same_thread=False: its cursor state, its transaction and its
        statement cache are all per-connection, so two threads interleave
        inside one BEGIN and commit each other's half-written work.

        Thread-local also means a short-lived thread's connection is dropped
        with the thread, instead of a thread pool accumulating a connection
        per thread that ever ran a tool. A thread that never calls conn()
        never opens one.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = _open(self.path)
        return conn

    @contextmanager
    def write(self):
        """Serialise writers in this process for the duration of the block,
        yielding the calling thread's connection so a write reads as
        `with db.write() as conn:` and cannot accidentally reach for another
        thread's.
        """
        # The lock is taken for the whole block, and released only on the way
        # out of it — including on an exception, which is the point: a tool
        # that raises mid-write must not leave the store locked for the next
        # caller. Commit stays where store.py put it (in each store function):
        # the lock serialises writers, it does not own the transaction, and a
        # store function that is called outside a write() block still commits
        # exactly as it did before.
        with _WRITE_LOCK:
            yield self.conn()


def blob_dir_for(conn: sqlite3.Connection) -> Path:
    """Filesystem root for this connection's artifact blob content, sibling
    to its db file. Requires the connection to have come from `connect()`."""
    db_path = getattr(conn, "_dsos_db_path", None)
    if db_path is None:
        raise ValueError("connection was not opened via dsos.db.connect()")
    d = Path(db_path).parent / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return d
