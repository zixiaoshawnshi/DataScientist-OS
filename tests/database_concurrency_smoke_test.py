"""Layer-1 smoke test: one Database per process, one connection per thread,
one process-wide write lock (dsos/db.py, WP-A2 / decision D8).

Why this is a test and not a comment. `connect()` opens a single connection
with check_same_thread=False, and its own docstring used to say outright that
this was "not a claim of real concurrency safety" — fine while one call is in
flight at a time, which is what the stdio server does, and not fine the moment
a second thread writes. What the second thread actually gets depends on which
write it is doing, which is worth being precise about, because it decides what
this test can and cannot prove:

- A write transaction that starts with an INSERT simply queues behind whoever
  holds the write lock, and busy_timeout=5000 absorbs it. Measured on this
  machine, 8 unlocked writer threads doing 400 saves raised *zero* errors.
- A transaction that has already taken a read snapshot and then tries to write
  gets SQLITE_BUSY_SNAPSHOT, returned immediately with no busy-handler
  consultation, because waiting cannot help. That is store.save_artifact's
  versioning path (it SELECTs the current version before INSERTing).

So a test whose only assertion is "no 'database is locked' was raised" would
pass on a completely unlocked store — it does, and this file did until the
non-overlap assertions below were added. What Database.write() actually
guarantees is *serialisation*, and that is what is asserted here: the writers'
critical sections are recorded with time.perf_counter() and must not overlap,
and the workload's wall clock must be spent inside the lock rather than
queueing for it. Both fail without the lock; the error count is reported
alongside as the symptom, not as the proof.

Covered here:

1. 8 threads x 50 save_artifact calls, each on its own db.conn() inside
   db.write(): 400 artifacts, 400 FTS rows, no error.
2. The writers' critical sections provably do not overlap, and the wall clock
   goes on being inside the lock rather than waiting for it. These are the
   assertions that matter — see the module docstring for why "no error was
   raised" is not one of them.
3. A reader thread looping on search_artifacts throughout the writes raises
   nothing, and keeps reading (WAL: readers and the one writer coexist), and
   every one of the concurrent writes is findable by the query it was looping
   on.
4. db.conn() hands back the same object within a thread and a different object
   in every other thread.

And the transaction boundary write() owns (TTD U6), which the lock alone did
not give it:

5. An exception raised inside `with db.write() as conn:` after an INSERT
   rolls that INSERT back: the connection leaves the block with no open
   transaction, the row is not in the store, and another thread's write goes
   through at once instead of waiting out busy_timeout for a RESERVED lock
   nobody is ever going to release.
6. A block that writes and forgets to commit is committed on the way out, so
   another thread sees the row and can write straight after it.
7. A nested write() block does not end the outer block's transaction: only
   the outermost block commits, so the outer's work stays one unit.

Run: .venv/Scripts/python.exe tests/database_concurrency_smoke_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DSOS_DB_PATH"] = "data/test-runs/database_concurrency_smoke_test/store.db"
ROOT = Path(os.environ["DSOS_DB_PATH"]).parent
shutil.rmtree(ROOT, ignore_errors=True)

from dsos import store  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.db import Database  # noqa: E402

FAILURES: list[str] = []

WRITERS = 8
ITERATIONS = 50
EXPECTED = WRITERS * ITERATIONS


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def count(db: Database, table: str) -> int:
    # The main thread's own connection: sqlite3.Connection objects are not
    # safe to share across threads even with check_same_thread=False, and this
    # is exactly the mistake the handle exists to prevent.
    return db.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def writer_thread(
    index: int, session_id: str, db: Database, out: dict, start: threading.Event
) -> None:
    start.wait()
    for i in range(ITERATIONS):
        try:
            with db.write():
                entered = time.perf_counter()
                store.save_artifact(
                    db.conn(),
                    type="narrative",
                    title=f"concurrency probe {index}-{i}",
                    description="a note written by a writer thread to contend on the store",
                    content={"thread": index, "i": i},
                    content_format="json",
                    session_id=session_id,
                )
                exited = time.perf_counter()
        except Exception as exc:  # noqa: BLE001 — collected and reported
            with out["lock"]:
                out["errors"].append(f"thread {index} iteration {i}: {exc!r}")
            continue
        with out["lock"]:
            out["intervals"].append((entered, exited))


def reader_thread(db: Database, out: dict, stop: threading.Event) -> None:
    """Search in a loop for as long as the writers run. Read-only: it never
    takes db.write(), because under WAL a reader does not block the writer."""
    while not stop.is_set():
        try:
            out["hits"] += len(store.search_artifacts(db.conn(), "concurrency probe"))
        except Exception as exc:  # noqa: BLE001 — collected and reported
            with out["lock"]:
                out["errors"].append(f"reader: {exc!r}")
            return
        out["reads"] += 1


class _Deliberate(RuntimeError):
    """The failure a store function hits half-way through its writes."""


def _insert_session(conn, session_id: str) -> None:
    conn.execute(
        "INSERT INTO sessions (id, question, started_at) VALUES (?, ?, '2026-01-01')",
        (session_id, f"transaction probe {session_id}"),
    )


def _session_count(db: Database, session_id: str) -> int:
    return db.conn().execute(
        "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]


def _write_from_another_thread(db: Database, session_id: str) -> tuple[float, str]:
    """(seconds taken, error text) for one committed write on a fresh thread
    — and so on a connection of its own, which is the one that a transaction
    left open on this thread's connection would lock out."""
    out = {"elapsed": 0.0, "error": ""}

    def run() -> None:
        began = time.perf_counter()
        try:
            with db.write() as conn:
                _insert_session(conn, session_id)
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — reported by the caller
            out["error"] = repr(exc)
        out["elapsed"] = time.perf_counter() - began

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return out["elapsed"], out["error"]


# busy_timeout is 5s, so a write that had to wait for a leaked RESERVED lock
# takes all five and then fails. Anything under one second did not wait.
PROMPT = 1.0


def check_raise_rolls_back(db: Database) -> None:
    raised = False
    try:
        with db.write() as conn:
            _insert_session(conn, "tx-raised")
            raise _Deliberate("store function failed after its first INSERT")
    except _Deliberate:
        raised = True
    check("the exception still reaches the caller", raised)
    check("the connection leaves the block with no open transaction",
          not db.conn().in_transaction, "a transaction is still open on it")
    check("the INSERT before the raise was rolled back",
          _session_count(db, "tx-raised") == 0, "the half-written row is in the store")
    elapsed, error = _write_from_another_thread(db, "tx-after-raise")
    check("another thread can write straight after the failed block",
          not error and elapsed < PROMPT, error or f"{elapsed:.2f}s")


def check_forgotten_commit(db: Database) -> None:
    with db.write() as conn:
        _insert_session(conn, "tx-forgot")  # and no commit(), which is the bug
    check("a block that wrote without committing ends with no open transaction",
          not db.conn().in_transaction, "the write is still pending")
    visible = {}

    def look() -> None:
        visible["n"] = _session_count(db, "tx-forgot")

    t = threading.Thread(target=look)
    t.start()
    t.join()
    check("its write is committed, so another connection sees it",
          visible.get("n") == 1, str(visible.get("n")))
    elapsed, error = _write_from_another_thread(db, "tx-after-forgot")
    check("another thread can write straight after it",
          not error and elapsed < PROMPT, error or f"{elapsed:.2f}s")


def check_nested_write(db: Database) -> None:
    with db.write() as outer:
        _insert_session(outer, "tx-outer")
        with db.write() as inner:
            check("a nested block hands back the same connection", inner is outer)
            _insert_session(inner, "tx-inner")
        check("the inner block's exit leaves the outer transaction open",
              outer.in_transaction, "the inner exit committed the outer's work")
    check("the outer block's exit commits both writes",
          not db.conn().in_transaction
          and _session_count(db, "tx-outer") == 1 and _session_count(db, "tx-inner") == 1)


def main() -> None:
    db = Database(os.environ["DSOS_DB_PATH"])

    # (4) one connection per thread, one per thread only.
    check("db.conn() is the same object twice in one thread",
          db.conn() is db.conn(), repr(db.conn()))
    seen: list[int] = []
    probe_barrier = threading.Barrier(WRITERS + 1)

    def conn_identity(index: int) -> None:
        probe_barrier.wait()
        seen.append(id(db.conn()))

    probes = [threading.Thread(target=conn_identity, args=(i,)) for i in range(WRITERS)]
    for t in probes:
        t.start()
    probe_barrier.wait()
    for t in probes:
        t.join()
    check("every other thread gets its own connection",
          len(set(seen)) == WRITERS, f"{len(set(seen))} distinct of {WRITERS}")
    check("the main thread's connection is not one of theirs",
          id(db.conn()) not in seen, "")

    # (1) + (2) + (3): concurrent writers under the lock, concurrent reader.
    # Force the embedding model to load FIRST. Whichever thread calls embed()
    # first pays ~10s for it, and if that thread is the reader it spends the
    # entire writer window loading a model instead of reading — a measurement
    # artefact, not a concurrency finding. (Seen: the reader got through one
    # search while 400 writes completed around it.)
    store.search_artifacts(db.conn(), "warmup")

    # One session per writer thread, created up front on the main thread, so
    # the threads themselves only do the contended work.
    sessions = [store.start_session(db.conn(), f"concurrency probe {i}") for i in range(WRITERS)]

    out = {"lock": threading.Lock(), "errors": [], "intervals": [],
           "reads": 0, "hits": 0}
    stop = threading.Event()
    start = threading.Event()
    writers = [
        threading.Thread(target=writer_thread, args=(i, sessions[i], db, out, start))
        for i in range(WRITERS)
    ]
    reader = threading.Thread(target=reader_thread, args=(db, out, stop))
    for t in writers:
        t.start()
    reader.start()
    start.set()

    began = time.perf_counter()
    for t in writers:
        t.join()
    elapsed = time.perf_counter() - began
    stop.set()
    reader.join()

    check(f"{EXPECTED} save_artifact calls completed without an error",
          not out["errors"], "; ".join(out["errors"][:3]))
    check("no writer saw 'database is locked'",
          not any("locked" in e for e in out["errors"]), "; ".join(out["errors"][:3]))
    check(f"{EXPECTED} artifacts rows", count(db, "artifacts") == EXPECTED,
          str(count(db, "artifacts")))
    check(f"{EXPECTED} FTS rows", count(db, "artifacts_fts") == EXPECTED,
          str(count(db, "artifacts_fts")))

    # The assertion that makes this a concurrency test: sort the recorded
    # critical sections by start time and require each to begin no earlier than
    # the previous one ended. Overlap anywhere means the write lock was not held
    # for the whole block, whether or not SQLite happened to complain.
    intervals = sorted(out["intervals"])
    overlaps = [(prev, cur) for prev, cur in zip(intervals, intervals[1:])
                if cur[0] < prev[1]]
    check("writer critical sections never overlap",
          not overlaps and len(intervals) == EXPECTED,
          f"{len(intervals)} sections, {len(overlaps)} overlapping"
          + (f", first overlap at t={overlaps[0][1][0]:.4f}s" if overlaps else ""))
    # Quantitative form of the same claim: if the writers really were
    # serialised, essentially all of the workload's wall clock is spent *inside*
    # the critical section, not spent waiting to enter it. Unlocked, 8 threads
    # run in parallel and the sum of their section times dwarfs the wall clock.
    inside = sum(e - s for s, e in out["intervals"])
    duty = inside / elapsed if elapsed else 0.0
    check("the writers spent the workload inside the lock, not queued for it",
          duty > 0.8,
          f"{inside:.2f}s of critical section in {elapsed:.2f}s wall clock ({duty:.0%})")

    check("the reader threaded through the writes without raising",
          not any(e.startswith("reader:") for e in out["errors"]),
          "; ".join([e for e in out["errors"] if e.startswith("reader:")][:3]))
    # Iterations, not hits: a search that runs before the first commit commits
    # legitimately returns nothing, and how many of them land mid-write is a
    # scheduling detail. "The reader did not raise, and it did read, many
    # times" is the claim; the hits are detail.
    check("the reader actually read while the writers ran",
          out["reads"] >= 10, f"{out['reads']} searches, {out['hits']} hits")
    # What the reader was chasing, checked deterministically on the main thread
    # afterwards: every one of the 400 concurrent writes is findable by the
    # query the reader was looping on.
    found = store.search_artifacts(db.conn(), "concurrency probe", top_k=EXPECTED)
    check(f"all {EXPECTED} concurrently written artifacts are searchable",
          len(found) == EXPECTED, str(len(found)))

    # (5)-(7) last, and each one cleans up after itself: a failure here means
    # this thread's connection may be holding the write lock on the file, which
    # would turn every check after it into a five-second busy_timeout.
    for label, fn in (
        ("a raise inside write() rolls the block back", check_raise_rolls_back),
        ("a write() block that forgot to commit is committed", check_forgotten_commit),
        ("a nested write() leaves the outer transaction open", check_nested_write),
    ):
        try:
            fn(db)
        except Exception as exc:  # noqa: BLE001 — a broken scenario is a failure
            check(label, False, f"{type(exc).__name__}: {exc}")
        finally:
            if db.conn().in_transaction:
                db.conn().rollback()

    shutil.rmtree(ROOT, ignore_errors=True)
    if FAILURES:
        print(f"\ndatabase concurrency smoke test FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("\ndatabase concurrency smoke test passed.")


if __name__ == "__main__":
    main()
