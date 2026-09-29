"""Reuse metrics for the benchmark's arms.

Every arm is declared once in benchmark/arms.json, which runner.mjs and
this module both read — a new arm is one registry entry, not a condition
name hardcoded in two languages.

Condition B (dsos) is measured automatically: the dsos store already
records every tool call and the session_id of every artifact it touches,
so cross-session reuse is a SQL join. Run against a copy of the store,
or read-only (all queries are SELECTs).

    python metrics.py dsos --db ~/.dsos/store.db --list-sessions
    python metrics.py dsos --db ~/.dsos/store.db --session <id> [--session <id2> ...]

The file arms have no store, so their transcripts are recorded as a
JSONL tool-call log (see benchmark/README.md for the event schema) and
parsed:

    python metrics.py file --transcript results/transcripts/titanic_file_R2.jsonl
    python metrics.py manifest --label chain_file_manifest --table diamonds.csv --round 2

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
                    (search_artifacts in dsos; ls/grep/find/glob in file, and
                    for the manifest arm, reading MANIFEST.md).
  - manifest use:  whether the file_manifest arm actually read the manifest
                    it was told to maintain, and whether it then loaded a file
                    an earlier round's manifest advertised — the only reuse
                    path the manifest is supposed to create.
"""

import argparse
import json
import pathlib
import re
import sqlite3
import sys

# Tools that count as locating prior work. `list_skills` used to sit here but
# the tool is gone from the product (WP-B2); a removed tool can never appear in
# a transcript, so keeping it was dead weight that misled the findability
# numbers. `list_templates` came back (maintainer decision U1) and stays.
FINDABILITY_TOOLS = {"search_artifacts", "list_templates"}
FINDABILITY_PATTERNS = ("ls", "dir", "grep", "rg", "find", "glob", "tree")
# A pull from the network, not a reuse. Several chain tables have public
# copies, so a file-less arm can bypass its own workspace entirely; the
# README calls this the known confound and this is the counter for it.
REMOTE_FETCH_RE = re.compile(r"\b(curl|wget|Invoke-WebRequest|urllib|urlopen|requests\.get|httpie|hf_hub_download|kaggle|git\s+clone)\b")
# The manifest arm's index file. It is a pointer to derived work, not the
# work itself, so reading it is findability rather than reuse.
MANIFEST_NAME = "MANIFEST.md"


def _normalize_tool(name: str) -> str:
    """Strip an MCP server prefix (dsos_bench_search_artifacts ->
    search_artifacts) so metrics work with or without namespacing."""
    for prefix in ("dsos_bench_", "dsos_dev_", "dsos_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE / "results"


# ------------------------------------------------------------ arm registry

def load_arms(path: pathlib.Path | None = None) -> dict:
    """The arm registry (benchmark/arms.json), keyed by condition name."""
    p = path or (HERE / "arms.json")
    return json.loads(p.read_text(encoding="utf-8"))["arms"]


def arm_of(label: str) -> str | None:
    """Which arm a run label names, or None.

    Labels carry the experiment, not just the arm: `chain_file`,
    `chain_file_manifest`, `file_keepraw`. Matching on a substring (as the
    report used to) makes `file_manifest` fall through to `file`, which
    would score the manifest arm's reuse as if it had no manifest at all —
    so this matches longest-suffix-then-prefix, and the caller gets None
    rather than a guess when nothing matches.
    """
    arms = load_arms()
    for arm in sorted(arms, key=len, reverse=True):
        if label == arm or label.endswith(f"_{arm}") or label.startswith(f"{arm}_"):
            return arm
    return None


def arm_spec(label: str) -> dict:
    return load_arms().get(arm_of(label) or "", {})


def is_file_arm(label: str) -> bool:
    """Transcript-scored arms: the workspace, not the store, is the record."""
    return arm_spec(label).get("scored_from") == "transcript"


def uses_manifest(label: str) -> bool:
    return arm_spec(label).get("manifest") is True


# ---------------------------------------------------------- MANIFEST.md

_MANIFEST_PATH_RE = re.compile(r"^##\s+(?P<path>\S.*?)\s*$")
_MANIFEST_FIELD_RE = re.compile(r"^-\s*(?P<field>question|computed|inputs):\s*(?P<value>.*)$")


def parse_manifest(text: str) -> list[dict]:
    """Entries of a MANIFEST.md, in file order.

    The format is the one the runner's system prompt asks for — a `## <path>`
    heading per derived file, then `- question:`, `- computed:` and
    `- inputs:` lines. Parsing it (rather than assuming the arm worked)
    is what lets the report say how often the manifest was actually used.
    """
    entries, cur = [], None
    for line in (text or "").splitlines():
        m = _MANIFEST_PATH_RE.match(line)
        if m:
            cur = {"path": m.group("path"), "question": "", "computed": "",
                   "inputs": []}
            entries.append(cur)
            continue
        if cur is None:
            continue
        m = _MANIFEST_FIELD_RE.match(line.strip())
        if not m:
            continue
        value = m.group("value").strip()
        if m.group("field") == "inputs":
            cur["inputs"] = [p.strip() for p in value.split(",")
                             if p.strip() and p.strip().lower() != "none"]
        else:
            cur[m.group("field")] = value
    return entries


def _as_posix(p: str) -> str:
    return p.replace("\\", "/").strip()


def _advertised_hit(path: str, advertised: list[str]) -> bool:
    """Does a path in a transcript name a file the manifest advertised?

    Suffix-matched, because a transcript records whatever form the agent
    typed (`clean.csv`, `out/clean.csv`, or the absolute path) while the
    manifest records the workspace-relative one.
    """
    p = _as_posix(path)
    for entry in advertised:
        e = _as_posix(entry)
        if p == e or p.endswith("/" + e):
            return True
    return False


def _read_events(transcript_path: pathlib.Path) -> list[dict]:
    if not transcript_path.exists():
        return []
    return [json.loads(line) for line in
            transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def manifest_metrics(label: str, table: str, rnd: int,
                     results: pathlib.Path | None = None) -> dict:
    """How much the file_manifest arm actually used its MANIFEST.md.

    Three separate questions, kept separate because "the prompt said to"
    is not "the agent did":
      - manifest_reads:  calls that touched MANIFEST.md (a write isn't a read).
      - advertised_paths: the entries the *previous* round left behind.
      - advertised_reuse: calls that then loaded one of those files. This is
        the arm's whole mechanism — a manifest nobody reads is a README.
    """
    root = results or RESULTS
    prev = root / "manifests" / label / f"{table}_R{rnd - 1}.md"
    advertised = ([e["path"] for e in
                   parse_manifest(prev.read_text(encoding="utf-8"))]
                  if prev.exists() else [])
    events = _read_events(root / "transcripts" / label / f"{table}_R{rnd}.jsonl")
    stats = {"manifest_snapshot": str(prev), "manifest_reads": 0,
             "manifest_writes": 0, "advertised_paths": advertised,
             "advertised_reuse": 0, "remote_fetches": 0}
    for ev in events:
        tool = _normalize_tool((ev.get("tool") or "").lower())
        names = {_as_posix(p) for p in (ev.get("paths") or [])}
        summary = ev.get("summary", "") or ""
        if any(_as_posix(p).rsplit("/", 1)[-1] == MANIFEST_NAME for p in names):
            if tool in {"write", "edit"}:
                stats["manifest_writes"] += 1
            else:
                stats["manifest_reads"] += 1
        if REMOTE_FETCH_RE.search(summary):
            stats["remote_fetches"] += 1
        if any(n != MANIFEST_NAME and _advertised_hit(n, advertised) for n in names):
            stats["advertised_reuse"] += 1
    return stats


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
             "derived_touches": 0, "manifest_touches": 0, "remote_fetches": 0,
             "unknown_table": None}
    touched_raw = set()
    for ev in events:
        tool = _normalize_tool((ev.get("tool") or "").lower())
        summary = ev.get("summary", "")
        if tool in FINDABILITY_TOOLS or tool in {"find", "ls", "grep", "tree"} \
                or summary.strip().lower().startswith(FINDABILITY_PATTERNS):
            stats["findability_calls"] += 1
        if REMOTE_FETCH_RE.search(summary):
            stats["remote_fetches"] += 1
        paths = {pathlib.Path(p).name for p in (ev.get("paths") or [])}
        if MANIFEST_NAME in paths:
            # The manifest is an index, not derived work: touching it is
            # findability, and never counts as reusing a prior round's file.
            stats["manifest_touches"] += 1
            paths.discard(MANIFEST_NAME)
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


# --------------------------------------------------------- consumer claims

# The consumer arm's output contract: one line per claim, `@claim[id:verdict:row]`.
# A row is a 32-char hex id (or `none`). Parsed here rather than by score.py
# because this is a verdict, not a numeric answer, and the gold is the fixture.
CLAIM_RE = re.compile(
    r"@claim\[\s*(\d+)\s*:\s*(supported|refuted)\s*:\s*([0-9a-fA-F]{6,}|none)\s*\]",
    re.IGNORECASE,
)


def claims_metrics(label: str, results: pathlib.Path | None = None,
                   claims_path: pathlib.Path | None = None) -> dict:
    """Score the consumer arm: did the PM back or refute each claim, and with
    which row?

    Three numbers, each answering a different question the spec asks:
      - verdict_correct: could the agent tell a backed claim from a refuted one?
      - cited_support:   did it cite the row that supports or refutes the claim?
      - bad_backing:     did it *support* a claim with a superseded, contradicted
                         or stale row — a citation used as evidence for a claim
                         that the row does not actually back. Target 0.

    A claim that is refuted *by* a repudiated row is the healthy case, not the
    error: "row X is contradicted, so I do not back this" cites X on purpose.
    So the target-0 count is restricted to citations used as support; the count
    of refutations that name a repudiated row is reported separately, because
    it is evidence the agent did the discrimination the arm exists to test.
    A definition chosen after seeing the numbers is not a measurement, so this
    is fixed here, before the run.

    The gold verdict and the rows live in the fixture (results/claims.json),
    built by build_claims.py. A citation is matched against the full row_id, so
    an agent that copies 8 characters instead of the whole id scores as
    unresolved rather than as correct-by-accident.
    """
    root = results or RESULTS
    fixture = json.loads((claims_path or root / "claims.json").read_text(encoding="utf-8"))
    answers = json.loads((root / f"answers_{label}.json").read_text(encoding="utf-8"))
    rec = answers.get("claims::R1", {})
    text = rec.get("answer_text_tagged") or rec.get("answer_text") or ""
    parsed = {int(m.group(1)): (m.group(2).lower(), m.group(3).lower())
              for m in CLAIM_RE.finditer(text)}

    verdict_correct = cited_support = bad_backing = refuted_by_bad = unresolved = 0
    per_claim = []
    for claim in fixture["claims"]:
        cid = claim["id"]
        got = parsed.get(cid)
        support = (claim.get("support_row_id") or "").lower()
        forbidden = {r.lower() for r in (claim.get("forbidden_row_ids") or [])}
        if got is None:
            unresolved += 1
            per_claim.append({"id": cid, "verdict": None, "cited": None,
                              "correct": False, "bad_backing": False})
            continue
        verdict, cited = got
        verdict_correct += verdict == claim["verdict"]
        cited_support += bool(support) and cited == support
        bad = cited != "none" and cited in forbidden and verdict == "supported"
        refuted_with_bad = cited != "none" and cited in forbidden and verdict == "refuted"
        bad_backing += bad
        refuted_by_bad += refuted_with_bad
        per_claim.append({"id": cid, "verdict": verdict, "cited": cited,
                          "correct": verdict == claim["verdict"],
                          "bad_backing": bad,
                          "refuted_by_bad_row": refuted_with_bad})
    return {
        "claims": len(fixture["claims"]),
        "verdict_correct": verdict_correct,
        "cited_support": cited_support,
        "bad_backing": bad_backing,
        "refuted_by_bad_row": refuted_by_bad,
        "unresolved": unresolved,
        "correct_fraction": (verdict_correct / len(fixture["claims"]))
        if fixture["claims"] else None,
        "tokens_in": rec.get("tokens_in"),
        "tokens_out": rec.get("tokens_out"),
        "tool_calls": rec.get("tool_calls"),
        "model": rec.get("model"),
        "per_claim": per_claim,
    }


# ---------------------------------------------------- exploratory noise

def exploratory_noise_metrics(db_path: pathlib.Path) -> dict:
    """How much of the store is exploratory, and would hiding it change what an
    agent sees?

    Doc II open question 1: does `status='exploratory'` survive contact with an
    agent, or does everything get marked `result` because that is the path of
    least resistance? This turns the assumption WP-G1's default flip rests on
    into a number. It is a finding about the design either way — a near-zero
    fraction is not a failed measurement.

    The store records each search_artifacts call's hits (`artifact_row_ids`),
    so the post-flip question is answerable without re-running the queries:
      - searches_returning_exploratory: a call whose hits included any
        exploratory row — the searches whose result set the flip shrinks.
      - searches_only_exploratory: a call whose every hit was exploratory —
        these return nothing at all once the default hides them.
    """
    total = _rows(db_path, "SELECT COUNT(*) FROM artifacts")[0][0]
    exploratory = _rows(db_path,
                        "SELECT COUNT(*) FROM artifacts WHERE status = 'exploratory'")[0][0]
    status_by_row = {r[0]: r[1] for r in
                     _rows(db_path, "SELECT row_id, status FROM artifacts")}
    sessions: dict[str, dict] = {}
    for sid, created in _rows(db_path, """
            SELECT session_id, COUNT(*) FROM artifacts GROUP BY session_id"""):
        sessions[sid] = {"artifacts": created, "exploratory": 0,
                         "searches": 0, "searches_returning_exploratory": 0,
                         "searches_only_exploratory": 0}
    for sid, cnt in _rows(db_path, """
            SELECT session_id, COUNT(*) FROM artifacts
            WHERE status = 'exploratory' GROUP BY session_id"""):
        sessions.setdefault(sid, {}).setdefault("artifacts", 0)
        sessions[sid]["exploratory"] = cnt
    for sid, row_ids_json in _rows(db_path, """
            SELECT session_id, artifact_row_ids FROM tool_calls
            WHERE tool_name LIKE '%search_artifacts%'"""):
        s = sessions.setdefault(sid, {"artifacts": 0, "exploratory": 0,
                                      "searches": 0,
                                      "searches_returning_exploratory": 0,
                                      "searches_only_exploratory": 0})
        hits = json.loads(row_ids_json or "[]")
        s["searches"] += 1
        flags = [status_by_row.get(r) for r in hits]
        if any(f == "exploratory" for f in flags):
            s["searches_returning_exploratory"] += 1
        if hits and all(f == "exploratory" for f in flags):
            s["searches_only_exploratory"] += 1
    for s in sessions.values():
        n = s.get("artifacts") or 0
        s["exploratory_fraction"] = (s.get("exploratory", 0) / n) if n else None
    return {
        "store": {
            "artifacts": total,
            "exploratory": exploratory,
            "exploratory_fraction": (exploratory / total) if total else None,
        },
        "sessions": sessions,
    }


# --------------------------------------------------------- parallel arm

_TITLE_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _norm_title(title: str) -> str:
    return " ".join(_TITLE_PUNCT.sub(" ", (title or "").lower()).split())


def duplicate_work_metrics(db_path: pathlib.Path, runs: list[dict]) -> dict:
    """Duplicate-work rate between two concurrent producers, per question pair.

    Definition, fixed before the numbers were seen: for a pair (A, B), a
    **duplicate** is an artifact registered by the second agent B whose
    `content_hash` equals that of an artifact A registered (both hashes
    non-empty), *or* whose normalised title equals a normalised title in A.
    Content hash catches the same output; title catches the same stated
    result written twice. The **duplicate-work rate** is B's duplicates over
    B's registrations — B is the agent that could have avoided the work.

    `runs` is the runner's per-agent index (results/runs_<label>.json) with
    `pair`, `agent` and `session_id`; artifacts are read back from the store
    by session. A pair with a missing session_id is reported as unmeasured
    rather than as a zero.
    """
    arts = _rows(db_path, "SELECT row_id, session_id, content_hash, title FROM artifacts")
    by_session: dict[str, list[dict]] = {}
    for row_id, sid, chash, title in arts:
        by_session.setdefault(sid, []).append(
            {"row_id": row_id, "content_hash": chash, "title": title})
    pairs: dict[str, dict] = {}
    for entry in runs:
        pairs.setdefault(entry.get("pair", "?"), {})[entry.get("agent")] = entry.get("session_id")
    out, tot_b, tot_dup = {}, 0, 0
    for pair, sides in sorted(pairs.items()):
        a_sid, b_sid = sides.get("a"), sides.get("b")
        if not a_sid or not b_sid:
            out[pair] = {"measured": False, "a_session": a_sid, "b_session": b_sid,
                         "a_registrations": None, "b_registrations": None,
                         "duplicates": None, "duplicate_work_rate": None}
            continue
        a_arts, b_arts = by_session.get(a_sid, []), by_session.get(b_sid, [])
        a_hashes = {a["content_hash"] for a in a_arts if a["content_hash"]}
        a_titles = {_norm_title(a["title"]) for a in a_arts}
        dup = 0
        for b in b_arts:
            by_hash = b["content_hash"] and b["content_hash"] in a_hashes
            by_title = _norm_title(b["title"]) in a_titles
            dup += bool(by_hash or by_title)
        tot_b += len(b_arts)
        tot_dup += dup
        out[pair] = {
            "measured": True, "a_session": a_sid, "b_session": b_sid,
            "a_registrations": len(a_arts), "b_registrations": len(b_arts),
            "duplicates": dup,
            "duplicate_work_rate": (dup / len(b_arts)) if b_arts else None,
        }
    return {
        "pairs": out,
        "totals": {"b_registrations": tot_b, "duplicates": tot_dup,
                   "duplicate_work_rate": (tot_dup / tot_b) if tot_b else None},
    }


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

    p3 = sub.add_parser("manifest", help="MANIFEST.md usage for a manifest-arm round")
    p3.add_argument("--label", required=True, help="run label, e.g. chain_file_manifest")
    p3.add_argument("--table", required=True)
    p3.add_argument("--round", type=int, required=True)
    p3.add_argument("--results", default=str(RESULTS))
    p3.add_argument("--out", default=None)

    args = ap.parse_args()
    if args.cmd == "dsos":
        db_path = pathlib.Path(args.db)
        if args.list_sessions or not args.session:
            list_sessions(db_path)
            if not args.session:
                return
        out = {sid: dsos_metrics(db_path, sid) for sid in args.session}
    elif args.cmd == "manifest":
        out = {"manifest": manifest_metrics(args.label, args.table, args.round,
                                            pathlib.Path(args.results))}
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
