"""Reuse metrics for the two benchmark conditions.

Condition B (dsos) is measured automatically: the dsos store already
records every tool call and the session_id of every artifact it touches,
so cross-session reuse is a SQL join. Run against a copy of the store,
or read-only (all queries are SELECTs).

    python metrics.py dsos --db ~/.dsos/store.db --list-sessions
    python metrics.py dsos --db ~/.dsos/store.db --session <id> [--session <id2> ...]

Condition A (plain files) has no store, so its transcript is recorded as
a JSONL tool-call log (see benchmark/README.md for the event schema) and
parsed:

    python metrics.py file --transcript results/transcripts/titanic_file_R2.jsonl

Definitions (see benchmark/README.md for the full protocol):
  - reuse call:     a tool call touching an artifact created in a previous
                    session (dsos), or reading a file produced in a previous
                    round (file).
  - re-fetch/re-clean proxy:
       files: a call touching the original table CSV. In R2+ ideally zero.
       dsos:  a NEW dataset-type registration created this session — the
              re-fetch signal. Touching the *existing* dataset artifact is
              fine (any new query needs it as input); that's reuse.
  - findability:   calls spent locating prior work before analysis starts
                    (search_artifacts in dsos; ls/grep/find/glob in file).
"""

import argparse
import json
import pathlib
import sqlite3
import sys

FINDABILITY_TOOLS = {"search_artifacts", "list_skills", "list_templates"}
FINDABILITY_PATTERNS = ("ls", "dir", "grep", "rg", "find", "glob", "tree")


def _normalize_tool(name: str) -> str:
    """Strip an MCP server prefix (dsos_bench_search_artifacts ->
    search_artifacts) so metrics work with or without namespacing."""
    for prefix in ("dsos_bench_", "dsos_dev_", "dsos_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name

HERE = pathlib.Path(__file__).resolve().parent


# ---------------------------------------------------------------- dsos store

def _rows(db_path: pathlib.Path, sql: str, params=()):
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def list_sessions(db_path: pathlib.Path) -> None:
    rows = _rows(db_path, """
        SELECT s.id, s.started_at, s.question,
               (SELECT COUNT(*) FROM tool_calls tc WHERE tc.session_id = s.id),
               (SELECT COUNT(DISTINCT json_each.value)
                  FROM tool_calls tc, json_each(tc.artifact_row_ids)
                 WHERE tc.session_id = s.id)
        FROM sessions s ORDER BY s.started_at DESC
    """)
    print(f"{'session':38s} {'calls':>5s} {'arts':>4s}  started  question")
    for sid, started, question, calls, arts in rows:
        print(f"{sid[:36]:38s} {calls:5d} {arts:4d}  {started[:16]}  {question[:60]}")


def dsos_metrics(db_path: pathlib.Path, session_id: str) -> dict:
    """Reuse metrics for one dsos session (one benchmark round run)."""
    session = _rows(db_path, "SELECT question, started_at FROM sessions WHERE id = ?",
                    (session_id,))
    if not session:
        raise SystemExit(f"session {session_id!r} not found in {db_path}")

    calls = _rows(db_path, """
        SELECT tool_name, artifact_row_ids FROM tool_calls
        WHERE session_id = ? ORDER BY ts
    """, (session_id,))

    stats = {"session_id": session_id, "question": session[0][0][:80],
             "tool_calls": len(calls), "search_artifacts_calls": 0,
             "reuse_calls": 0, "dataset_artifact_touches": 0,
             "derived_touches": 0, "reused_artifacts": [],
             "new_dataset_registrations": []}
    # Defense in depth: the server rejects calls with an unknown session_id,
    # but if an orphan ever lands it vanishes from every per-session reuse
    # count, so surface it instead of silently undercounting reuse.
    orphan = _rows(db_path, """
        SELECT COUNT(*) FROM tool_calls
        WHERE session_id NOT IN (SELECT id FROM sessions)
    """)[0][0]
    if orphan:
        stats["orphan_tool_calls_in_store"] = orphan
    if not calls:
        return stats
    all_row_ids = sorted({r for _, row_ids_json in calls
                          for r in json.loads(row_ids_json or "[]")})
    # Re-registration = creating a NEW dataset-type artifact this session.
    # In R2+ that's the re-fetch/re-clean signal (touching the *existing*
    # dataset artifact is fine — any new query needs it as input).
    stats["new_dataset_registrations"] = [
        {"title": r[0]} for r in _rows(db_path, """
            SELECT title FROM artifacts
            WHERE session_id = ? AND type = 'dataset' AND version = 1
        """, (session_id,))
    ]
    placeholders = ",".join("?" * len(all_row_ids))
    rows = _rows(db_path, f"""
        SELECT row_id, session_id, type, title FROM artifacts
        WHERE row_id IN ({placeholders})
    """, all_row_ids)
    art = {r[0]: {"session_id": r[1], "type": r[2], "title": r[3]} for r in rows}

    seen_reused = set()
    for tool_name, row_ids_json in calls:
        row_ids = json.loads(row_ids_json or "[]")
        if _normalize_tool(tool_name) in FINDABILITY_TOOLS:
            stats["search_artifacts_calls"] += 1
        touched = [art[r] for r in row_ids if r in art]
        if any(a["session_id"] != session_id for a in touched):
            stats["reuse_calls"] += 1
        for a in touched:
            if a["session_id"] != session_id and a["title"] not in seen_reused:
                seen_reused.add(a["title"])
                stats["reused_artifacts"].append(
                    {"title": a["title"], "type": a["type"]})
        if any(a["type"] == "dataset" for a in touched):
            stats["dataset_artifact_touches"] += 1
        elif touched:
            stats["derived_touches"] += 1
    stats["reused_artifacts"] = sorted(stats["reused_artifacts"],
                                       key=lambda d: d["type"])
    return stats


# ------------------------------------------------------------- file condition

def file_metrics(transcript_path: pathlib.Path, manifest_path: pathlib.Path) -> dict:
    """Reuse metrics for one plain-file round run, from a JSONL tool-call log.

    Event schema (one JSON object per line):
        {"ts": "...", "tool": "bash", "summary": "python clean.py",
         "paths": ["data/titanic.csv", "outputs/cleaned.csv"]}
    `paths` (optional) are the files the call read or wrote.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Match by basename: the runner copies each table into its own workspace,
    # so the CSV's directory varies — its filename doesn't.
    raw_files = {pathlib.Path(t["data_path"]).name for t in manifest["tables"]}
    # Which table is this run about? Anything the transcript touches.
    events = [json.loads(line) for line in
              transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    stats = {"transcript": str(transcript_path), "tool_calls": len(events),
             "findability_calls": 0, "raw_data_touches": 0,
             "derived_touches": 0, "unknown_table": None}
    touched_raw = set()
    for ev in events:
        tool = _normalize_tool((ev.get("tool") or "").lower())
        summary = ev.get("summary", "")
        if tool in FINDABILITY_TOOLS or tool in {"find", "ls", "grep", "tree"} \
                or summary.strip().lower().startswith(FINDABILITY_PATTERNS):
            stats["findability_calls"] += 1
        paths = {pathlib.Path(p).name for p in (ev.get("paths") or [])}
        raw_hits = paths & raw_files
        touched_raw |= raw_hits
        if raw_hits:
            stats["raw_data_touches"] += 1
        elif paths:
            stats["derived_touches"] += 1
    stats["tables_touched_raw"] = sorted(touched_raw)
    if not touched_raw and len(events) > 0:
        stats["unknown_table"] = "(no raw table touched in this run)"
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("dsos", help="metrics from a dsos store")
    p1.add_argument("--db", required=True, help="path to the dsos store.db")
    p1.add_argument("--session", action="append", default=[],
                    help="session id (repeatable)")
    p1.add_argument("--list-sessions", action="store_true")
    p1.add_argument("--out", default=None)

    p2 = sub.add_parser("file", help="metrics from a JSONL tool-call log")
    p2.add_argument("--transcript", required=True)
    p2.add_argument("--manifest", default=str(HERE / "manifest.json"))
    p2.add_argument("--out", default=None)

    args = ap.parse_args()
    if args.cmd == "dsos":
        db_path = pathlib.Path(args.db)
        if args.list_sessions or not args.session:
            list_sessions(db_path)
            if not args.session:
                return
        out = {sid: dsos_metrics(db_path, sid) for sid in args.session}
    else:
        out = {"file": file_metrics(pathlib.Path(args.transcript),
                                    pathlib.Path(args.manifest))}

    print(json.dumps(out, indent=2, ensure_ascii=False))
    if args.out:
        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
