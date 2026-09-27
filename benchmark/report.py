"""Aggregate the pilot results into one comparison report.

Reads:
  results/answers_<cond>.json   (per-round answer + efficiency fields)
  results/scores.json           (from score.py, per-question pass/fail)
  results/transcripts/<cond>/   (tool-call JSONL for the file condition)
  store.db                      (dsos condition, via metrics.py's queries)

Writes results/report.md + results/report.json: per-round and per-table
comparisons of accuracy, tokens, tool calls, duration, and the reuse
metrics (findability, raw-data touches, cross-session reuse).

Usage: python report.py
"""

import json
import pathlib
import re
import sqlite3
import statistics

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE / "results"
# Arms are discovered from results/answers_*.json so a new experiment (e.g.
# the depth chains under a --label) needs no code change here. The example
# template ships as answers.example.json (dot, not underscore) and must not
# be mistaken for an arm.
CONDITIONS = sorted(p.stem[len("answers_"):] for p in RESULTS.glob("answers_*.json")
                    if "example" not in p.stem)

FINDABILITY_TOOLS = {"search_artifacts", "list_skills", "list_templates",
                     "find", "ls", "grep", "tree"}
FINDABILITY_PATTERNS = ("ls", "dir", "grep", "rg", "find", "glob", "tree")
# A path that names a file rather than a directory, so directory navigation
# isn't miscounted as touching derived work.
_FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def norm_tool(name: str) -> str:
    for p in ("dsos_bench_", "dsos_dev_", "dsos_"):
        if name.startswith(p):
            return name[len(p):]
    return name


def load_json(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def rounds_of(answers):
    """{table: {round: rec}} from the answers file."""
    out = {}
    for key, rec in answers.items():
        table, r = key.split("::R")
        out.setdefault(table, {})[int(r)] = rec
    return out


def pass_map(scores):
    """{cond: {table: {round: bool}}}"""
    return {cond: {k: {} for k in scores[cond]["results"]} for cond in scores}


def carry_for(label, table, rnd):
    """What a round inherited from earlier rounds, recorded by the runner at
    round start. Empty dict if the run predates carry recording."""
    p = RESULTS / "carry" / label / f"{table}_R{rnd}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def file_transcript_metrics(label, table, rnd):
    """Replay a file-arm transcript: findability + raw-data touches +
    derived-artifact reuse.

    The reuse term is what makes the two arms comparable. dsos earns
    reuse_calls for touching an artifact owned by an earlier session; the
    file arm's equivalent is reading back a file an earlier round wrote
    (a cleaned CSV, a saved result). Without crediting that, the file arm
    could only ever be debited — reusing a derived file and redoing the
    work from scratch would score identically.
    """
    tpath = RESULTS / "transcripts" / label / f"{table}_R{rnd}.jsonl"
    if not tpath.exists():
        return None
    raw_name = table  # the CSV is copied into the workspace under its own name
    carry = carry_for(label, table, rnd)
    carried = {pathlib.Path(p).name for p in carry.get("carried", [])}
    find = raw_touch = derived_touch = reuse_touch = calls = 0
    for line in tpath.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        calls += 1
        tool = norm_tool((ev.get("tool") or "").lower())
        summary = ev.get("summary", "")
        if tool in FINDABILITY_TOOLS or tool in {"find", "ls", "grep", "tree"} \
                or summary.strip().lower().startswith(FINDABILITY_PATTERNS):
            find += 1
        names = {pathlib.Path(p).name for p in (ev.get("paths") or [])}
        if raw_name in names:
            raw_touch += 1
        elif names & carried:
            # Reused work an earlier round produced — the file arm's credit.
            reuse_touch += 1
        elif any(_FILE_LIKE.search(n) for n in names):
            # A real file, made this round. A bare directory name (an `ls` of
            # a folder) is navigation, already counted as findability — not
            # a touch on derived work.
            derived_touch += 1
    return {"findability_calls": find, "raw_data_touches": raw_touch,
            "derived_touches": derived_touch, "reuse_calls": reuse_touch,
            "tool_calls_logged": calls, "carried_files": sorted(carried),
            "carry_recorded": bool(carry)}


def dsos_store_metrics():
    """Per-session store metrics, keyed by session_id."""
    db = HERE / "store.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        sessions = {}
        for sid, question in con.execute("SELECT id, question FROM sessions"):
            sessions[sid] = {"question": question}
        arts = {}
        for row_id, sid, typ, title in con.execute(
                "SELECT row_id, session_id, type, title FROM artifacts"):
            arts[row_id] = {"session_id": sid, "type": typ, "title": title}
        for sid, tool_name, row_ids_json in con.execute(
                "SELECT session_id, tool_name, artifact_row_ids FROM tool_calls"):
            s = sessions.setdefault(sid, {})
            s.setdefault("calls", []).append((tool_name, json.loads(row_ids_json or "[]")))
        return sessions, arts
    finally:
        con.close()


def dsos_metrics(sessions, arts):
    """Compute per-session reuse metrics from the store."""
    out = {}
    for sid, s in sessions.items():
        if s.get("question", "").startswith("bootstrap"):
            continue
        reuse_calls = find = new_datasets = 0
        for tool_name, row_ids in s.get("calls", []):
            if norm_tool(tool_name) in FINDABILITY_TOOLS:
                find += 1
            touched = [arts[r] for r in row_ids if r in arts]
            if any(a["session_id"] != sid for a in touched):
                reuse_calls += 1
        new_datasets = sum(1 for a in arts.values()
                           if a["session_id"] == sid and a["type"] == "dataset")
        out[sid] = {"reuse_calls": reuse_calls, "findability_calls": find,
                    "new_dataset_registrations": new_datasets}
    return out


def main():
    answers = {c: rounds_of(load_json(RESULTS / f"answers_{c}.json")) for c in CONDITIONS}
    # score.py keys conditions by the answers filename stem: "answers_file"/"answers_dsos".
    raw_scores = load_json(RESULTS / "scores.json")
    scores = {c: raw_scores.get(f"answers_{c}", {"results": {}}) for c in CONDITIONS}
    sessions, arts = dsos_store_metrics() if (HERE / "store.db").exists() else ({}, {})
    store_m = dsos_metrics(sessions, arts) if sessions else {}

    # dsos store sessions map to runs via the runner's own run index, appended
    # as each round completes. Parsing the console log was fragile: it broke for
    # any --label, and a truncated log silently mis-paired rounds with sessions,
    # which would quietly corrupt every reuse number.
    dsos_round_metrics = {}
    for label in CONDITIONS:
        index_path = RESULTS / f"runs_{label}.json"
        if not index_path.exists():
            continue
        try:
            run_index = json.loads(index_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for entry in run_index:
            sid = entry.get("session_id")
            # Keyed by arm as well as (table, round): two arms run the same
            # table and rounds, and keying without the label let the file arm
            # silently fall through to the dsos arm's store numbers.
            if sid and sid in store_m:
                dsos_round_metrics[(label, entry["table"], entry["round"])] = store_m[sid]

    # Build the report
    report = {"per_round": {}, "per_table": {}, "reuse": {}, "models": {}}
    for cond in CONDITIONS:
        models = set()
        for rounds in answers[cond].values():
            for rec in rounds.values():
                if rec.get("model"):
                    models.add(rec["model"])
        report["models"][cond] = sorted(models)
    for rnd in (1, 2, 3):
        row = {}
        for cond in CONDITIONS:
            recs = [a[rnd] for a in answers[cond].values() if rnd in a]
            scored = []
            for table, a in answers[cond].items():
                key = f"{table}::R{rnd}"
                if key in scores.get(cond, {}).get("results", {}):
                    scored.append(scores[cond]["results"][key]["passed"])
            row[cond] = {
                "n": len(recs),
                "accuracy": (sum(scored) / len(scored)) if scored else None,
                "tokens_in_total": sum(r.get("tokens_in") or 0 for r in recs),
                "tokens_out_total": sum(r.get("tokens_out") or 0 for r in recs),
                "tokens_total_mean": statistics.mean(
                    [(r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in recs]) if recs else None,
                "tool_calls_total": sum(r.get("tool_calls") or 0 for r in recs),
                "duration_s_total": sum(r.get("duration_s") or 0 for r in recs),
            }
        report["per_round"][f"R{rnd}"] = row

    for table in sorted({t for cond in CONDITIONS for t in answers[cond]}):
        row = {}
        for cond in CONDITIONS:
            recs = answers[cond].get(table, {})
            scored = []
            for rnd in recs:
                key = f"{table}::R{rnd}"
                if key in scores.get(cond, {}).get("results", {}):
                    scored.append(scores[cond]["results"][key]["passed"])
            row[cond] = {
                "accuracy": (sum(scored) / len(scored)) if scored else None,
                "tokens_total": sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in recs.values()),
                "tool_calls": sum(r.get("tool_calls") or 0 for r in recs.values()),
            }
        # reuse side
        reuse = {}
        for cond in CONDITIONS:
            per = {}
            for rnd in sorted(answers[cond].get(table, {})):
                if rnd == 1:
                    continue  # R1 is the seed; there is nothing to reuse yet
                # A file arm is scored from its transcript, whichever label it
                # carries ("file", "chain_file", "file_keepraw" ...). Matching
                # on the substring keeps prefixed labels working; a bare
                # startswith("file") silently dropped every labelled arm.
                m = (file_transcript_metrics(cond, table, rnd)
                     if "file" in cond else None)
                if m:
                    per[f"R{rnd}"] = m
                else:
                    dm = dsos_round_metrics.get((cond, table, rnd))
                    if dm:
                        per[f"R{rnd}"] = dm
            if per:
                reuse[cond] = per
        row["reuse"] = reuse
        report["per_table"][table] = row

    (RESULTS / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")

    # Console summary
    print("== Per round ==")
    hdr = f"{'':4s} {'acc file':>9s} {'acc dsos':>9s} {'tok file':>9s} {'tok dsos':>9s} {'tools file':>11s} {'tools dsos':>11s} {'sec file':>9s} {'sec dsos':>9s}"
    print(hdr)
    for rnd, row in report["per_round"].items():
        f, d = row["file"], row["dsos"]
        print(f"{rnd:4s} {f['accuracy']:>8.0%} {d['accuracy']:>9.0%} "
              f"{f['tokens_total_mean']:>9.0f} {d['tokens_total_mean']:>9.0f} "
              f"{f['tool_calls_total']:>11d} {d['tool_calls_total']:>11d} "
              f"{f['duration_s_total']:>9d} {d['duration_s_total']:>9d}")

    print("\n== Reuse in R2/R3 (the claim under test) ==")
    print(f"{'table':22s} {'cond':5s} {'rnd':4s} {'findability':>12s} {'raw/re-registered':>18s} {'reuse calls':>12s}")
    for table, row in report["per_table"].items():
        for cond in CONDITIONS:
            for rnd, m in sorted(row["reuse"].get(cond, {}).items()):
                # A file-arm round with no carry record predates that metric,
                # so its reuse count is unmeasured, not zero. Printing a bare
                # 0 would let missing data read as a measured result.
                if "file" in cond and m.get("carry_recorded") is False:
                    reuse = "n/a"
                else:
                    reuse = str(m.get("reuse_calls", 0))
                print(f"{table:22s} {cond:5s} {rnd:4s} "
                      f"{m.get('findability_calls', 0):>12d} "
                      f"{m.get('raw_data_touches', m.get('new_dataset_registrations', 0)):>18d} "
                      f"{reuse:>12s}")
    print("  (n/a = run predates carry recording; reuse was not measured for that round)")
    print("\n== models seen per arm ==")
    for cond in CONDITIONS:
        print(f"  {cond:6s} {report['models'][cond] or '(not recorded — run predates model capture)'}")
    print("\nWrote results/report.json")


if __name__ == "__main__":
    main()
