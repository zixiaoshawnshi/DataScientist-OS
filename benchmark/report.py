"""Aggregate the benchmark results into one comparison report.

Reads:
  results/answers_<cond>.json   (per-round answer + efficiency fields)
  results/scores.json           (from score.py, per-question pass/fail)
  results/runs_<cond>.json      (the runner's per-round index: pairs a round
                                 with its store session for the dsos arm)
  results/transcripts/<cond>/   (tool-call JSONL, file arms)
  results/manifests/<cond>/     (MANIFEST.md as each manifest-arm round left it)
  store.db                      (dsos arm, via metrics.py's queries)

Writes results/report.json + results/report.md: per-round, per-table and
three-way comparisons of accuracy, tokens, tool calls, duration, and the
reuse metrics (findability, re-fetch, cross-round reuse, manifest use).

Arms come from benchmark/arms.json, so a new condition is one registry
entry and a run label like `chain_file_manifest` resolves to its arm
rather than to whichever arm its name happens to contain.

Usage: python report.py
"""

import json
import pathlib
import re
import sqlite3
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE / "results"
sys.path.insert(0, str(HERE))
from metrics import (MANIFEST_NAME, REMOTE_FETCH_RE, arm_of, arm_spec,  # noqa: E402
                     claims_metrics, duplicate_work_metrics,
                     exploratory_noise_metrics, is_file_arm,
                     load_arms, manifest_metrics, uses_manifest)


def discover_conditions():
    """Every run label with answers, in registry order.

    Discovered rather than hardcoded so a new experiment (a --label, or a
    new arm) needs no code change here. The example template ships as
    answers.example.json (dot, not underscore) and must not be mistaken
    for an arm.
    """
    found = {p.stem[len("answers_"):] for p in RESULTS.glob("answers_*.json")
             if "example" not in p.stem}
    return sorted(found, key=lambda c: (arm_spec(c).get("order", 99), c))


# `list_skills` is gone from the product (WP-B2); `list_templates` remains.
FINDABILITY_TOOLS = {"search_artifacts", "list_templates",
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

    The reuse term is what makes the arms comparable. dsos earns
    reuse_calls for touching an artifact owned by an earlier session; a
    file arm's equivalent is reading back a file an earlier round wrote
    (a cleaned CSV, a saved result). Without crediting that, the file arms
    could only ever be debited — reusing a derived file and redoing the
    work from scratch would score identically.
    """
    tpath = RESULTS / "transcripts" / label / f"{table}_R{rnd}.jsonl"
    if not tpath.exists():
        return None
    raw_name = table  # the CSV is copied into the workspace under its own name
    carry = carry_for(label, table, rnd)
    carried = {pathlib.Path(p).name for p in carry.get("carried", [])
               if pathlib.Path(p).name != MANIFEST_NAME}
    find = raw_touch = derived_touch = reuse_touch = calls = 0
    manifest_touch = remote = 0
    for line in tpath.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        calls += 1
        tool = norm_tool((ev.get("tool") or "").lower())
        summary = ev.get("summary", "")
        if REMOTE_FETCH_RE.search(summary):
            # Several chain tables have public copies, so a workspace with no
            # raw CSV in it can still pull one down and bypass its own history.
            remote += 1
        if tool in FINDABILITY_TOOLS or tool in {"find", "ls", "grep", "tree"} \
                or summary.strip().lower().startswith(FINDABILITY_PATTERNS):
            find += 1
        names = {pathlib.Path(p).name for p in (ev.get("paths") or [])}
        if MANIFEST_NAME in names:
            # The manifest is an index of derived work, not derived work.
            # Reading it is findability, so it earns no reuse credit — the
            # credit belongs to whichever file it points at.
            manifest_touch += 1
            names.discard(MANIFEST_NAME)
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
    out = {"findability_calls": find, "raw_data_touches": raw_touch,
           "derived_touches": derived_touch, "reuse_calls": reuse_touch,
           "tool_calls_logged": calls, "carried_files": sorted(carried),
           "carry_recorded": bool(carry), "manifest_touches": manifest_touch,
           "remote_fetches": remote}
    if uses_manifest(label):
        # Whether the manifest was read, and whether it then led the agent to
        # a file an earlier round advertised. A prompt nobody follows and a
        # manifest nobody reads have to look different in the report.
        out.update(manifest_metrics(label, table, rnd, RESULTS))
    return out


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


def build_three_way(answers, scores, report, conditions):
    """One row per arm: the four headline axes, N arms wide.

    Accuracy and tokens cover every round; findability, re-fetch and reuse
    cover R2+ only, because R1 is the seed round — there is nothing to find
    or reuse before an arm has produced anything. Reuse is summed only over
    the rounds where it was actually measured, with the unmeasured count kept
    beside it, so a missing carry record can never read as a measured zero.
    """
    out = {}
    for cond in conditions:
        rounds_seen = sorted({r for recs in answers[cond].values() for r in recs})
        scored = [scores[cond]["results"][f"{t}::R{r}"]["passed"]
                  for t, recs in answers[cond].items() for r in recs
                  if f"{t}::R{r}" in scores.get(cond, {}).get("results", {})]
        toks = [sum((recs[r].get("tokens_in") or 0) + (recs[r].get("tokens_out") or 0)
                    for recs in answers[cond].values() for r in recs)]
        row = {
            "arm": cond,
            "arm_label": arm_spec(cond).get("label") or cond,
            "registry_arm": arm_of(cond),
            "tables": sorted(answers[cond]),
            "rounds": rounds_seen,
            "n_runs": sum(len(recs) for recs in answers[cond].values()),
            "scored_rounds": len(scored),
            "accuracy": (sum(scored) / len(scored)) if scored else None,
            "tokens_mean": round(statistics.mean(toks), 1) if toks else None,
            "tokens_total": sum(toks),
            "tool_calls": sum(r.get("tool_calls") or 0
                              for recs in answers[cond].values() for r in recs.values()),
            "duration_s": sum(r.get("duration_s") or 0
                              for recs in answers[cond].values() for r in recs.values()),
            "refetch": 0, "findability": 0, "reuse": 0,
            "reuse_rounds_measured": 0, "reuse_rounds_unmeasured": 0,
            "remote_fetches": 0, "manifest": None,
        }
        manifest = {"rounds_with_a_manifest": 0, "manifest_reads": 0,
                    "manifest_writes": 0, "advertised_reuse": 0}
        for table in row["tables"]:
            for rnd, m in sorted(report["per_table"].get(table, {})
                                 .get("reuse", {}).get(cond, {}).items()):
                row["refetch"] += m.get("raw_data_touches",
                                        m.get("new_dataset_registrations", 0)) or 0
                row["findability"] += m.get("findability_calls", 0) or 0
                row["remote_fetches"] += m.get("remote_fetches", 0) or 0
                if is_file_arm(cond) and m.get("carry_recorded") is False:
                    row["reuse_rounds_unmeasured"] += 1
                else:
                    row["reuse"] += m.get("reuse_calls", 0) or 0
                    row["reuse_rounds_measured"] += 1
                if uses_manifest(cond):
                    manifest["rounds_with_a_manifest"] += 1 if m.get("advertised_paths") else 0
                    manifest["manifest_reads"] += m.get("manifest_reads", 0) or 0
                    manifest["manifest_writes"] += m.get("manifest_writes", 0) or 0
                    manifest["advertised_reuse"] += m.get("advertised_reuse", 0) or 0
        if uses_manifest(cond):
            row["manifest"] = manifest
        out[cond] = row
    return out


def fmt_pct(v):
    return "n/a" if v is None else f"{v:.0%}"


def fmt_num(v):
    return "n/a" if v is None else f"{v:,.0f}"


def three_way_lines(three_way, conditions):
    """The three-way table as text rows. Shared by the console and report.md,
    so the two can never disagree."""
    lines = [(f"{'arm':16s} {'accuracy':>9s} {'tok/round':>10s} {'tokens':>10s} "
              f"{'re-fetch':>9s} {'findability':>12s} {'reuse':>6s} {'manifest reads':>15s}")]
    for cond in conditions:
        r = three_way[cond]
        man = r.get("manifest") or {}
        lines.append(
            f"{cond:16s} {fmt_pct(r['accuracy']):>9s} {fmt_num(r['tokens_mean']):>10s} "
            f"{fmt_num(r['tokens_total']):>10s} {r['refetch']:>9d} "
            f"{r['findability']:>12d} {r['reuse']:>6d} "
            f"{(str(man.get('manifest_reads', 0)) if man else '-'):>15s}")
    return lines


def write_report_md(report, conditions):
    """The console tables in results/report.md, so a run's numbers can be read
    without re-running anything. Every number here comes from report.json."""
    lines = ["# Benchmark report", "",
             "Generated by `python report.py` from `results/` — do not edit by hand.", "",
             "## Three-way comparison", "",
             "Accuracy and tokens cover every round. Re-fetch, findability and reuse "
             "cover R2+ only: R1 is the seed round, before any arm has produced "
             "anything to find or reuse.", "", "```"]
    lines += three_way_lines(report["three_way"], conditions)
    lines += ["```", "", "## Per round", "",
              "| round | arm | accuracy | mean tokens | tool calls | seconds |",
              "|---|---|---|---|---|---|"]
    for rnd, row in report["per_round"].items():
        for cond in conditions:
            r = row[cond]
            if not r["n"]:
                continue
            lines.append(f"| {rnd} | {cond} | {fmt_pct(r['accuracy'])} | "
                         f"{fmt_num(r['tokens_total_mean'])} | {r['tool_calls_total']} | "
                         f"{r['duration_s_total']} |")
    lines += ["", "## Reuse in R2+", "",
              "| table | arm | round | findability | re-fetch | reuse calls | "
              "manifest reads | advertised reuse |",
              "|---|---|---|---|---|---|---|---|"]
    for table, row in report["per_table"].items():
        for cond in conditions:
            for rnd, m in sorted(row["reuse"].get(cond, {}).items()):
                unmeasured = is_file_arm(cond) and m.get("carry_recorded") is False
                lines.append(
                    f"| {table} | {cond} | {rnd} | {m.get('findability_calls', 0)} | "
                    f"{m.get('raw_data_touches', m.get('new_dataset_registrations', 0))} | "
                    f"{'n/a' if unmeasured else m.get('reuse_calls', 0)} | "
                    f"{m.get('manifest_reads', '-')} | {m.get('advertised_reuse', '-')} |")
    lines += ["", "`n/a` = the run predates carry recording, so reuse was not measured "
                  "for that round. `re-fetch` is calls touching the raw CSV (file arms) "
                  "or new dataset registrations (dsos).", "",
              "## Models", ""]
    for cond in conditions:
        lines.append(f"- `{cond}`: {', '.join(report['models'][cond]) or '(not recorded)'}")
    if report.get("consumer"):
        lines += ["", "## Consumer arm (claims)", "",
                  "| arm | claims | verdicts correct | cited the right row | "
                  "bad backing (target 0) | refuted by a repudiated row | tokens |",
                  "|---|---|---|---|---|---|---|"]
        for cond, m in report["consumer"].items():
            toks = (m.get("tokens_in") or 0) + (m.get("tokens_out") or 0)
            lines.append(
                f"| {cond} | {m['claims']} | {m['verdict_correct']} | "
                f"{m['cited_support']} | {m['bad_backing']} | "
                f"{m['refuted_by_bad_row']} | {toks:,} |")
        lines += ["", "`bad backing` counts a claim marked *supported* with a "
                      "superseded, contradicted or stale row — the number the "
                      "consumer arm is built to drive to zero. `refuted by a "
                      "repudiated row` is the healthy case: the agent named the "
                      "bad row as the reason to reject the claim. The files ceiling "
                      "has no rows to cite, so both are `none` by construction."]
    if report.get("parallel"):
        lines += ["", "## Parallel arm (duplicate work)", "",
                  "| arm | pair | A regs | B regs | B duplicates | rate |",
                  "|---|---|---|---|---|---|"]
        for cond, data in report["parallel"].items():
            for pair, p in data["pairs"].items():
                if not p["measured"]:
                    lines.append(f"| {cond} | {pair} | n/a | n/a | n/a | n/a |")
                    continue
                rate = p["duplicate_work_rate"]
                lines.append(
                    f"| {cond} | {pair} | {p['a_registrations']} | {p['b_registrations']} | "
                    f"{p['duplicates']} | {'n/a' if rate is None else format(rate, '.0%')} |")
            t = data["totals"]
            rate = t["duplicate_work_rate"]
            lines.append(
                f"| {cond} | **all** | | {t['b_registrations']} | {t['duplicates']} | "
                f"{'n/a' if rate is None else format(rate, '.0%')} |")
        lines += ["", "`duplicates` = registrations by the second agent B whose "
                      "`content_hash` or normalised title matches one of A's. "
                      "`rate` = duplicates / B's registrations. The board-on and "
                      "board-off conditions differ in nothing but "
                      "`DSOS_DISABLE_BOARD` on the daemon."]
    if report.get("exploratory_noise"):
        noise = report["exploratory_noise"]["store"]
        lines += ["", "## Exploratory noise", "",
                  f"Store-wide: {noise['exploratory']} of {noise['artifacts']} "
                  f"artifacts are exploratory "
                  f"({'n/a' if noise['exploratory_fraction'] is None else format(noise['exploratory_fraction'], '.1%')}).",
                  "",
                  "| session | artifacts | exploratory | fraction | searches | "
                  "searches returning exploratory | searches only exploratory |",
                  "|---|---|---|---|---|---|---|"]
        for sid, s in report["exploratory_noise"]["sessions"].items():
            frac = s.get("exploratory_fraction")
            lines.append(
                f"| {sid[:12]} | {s.get('artifacts', 0)} | {s.get('exploratory', 0)} | "
                f"{'n/a' if frac is None else format(frac, '.1%')} | {s.get('searches', 0)} | "
                f"{s.get('searches_returning_exploratory', 0)} | "
                f"{s.get('searches_only_exploratory', 0)} |")
        lines += ["", "`searches only exploratory` are calls whose every hit was "
                      "exploratory — these return nothing once WP-G1 hides the "
                      "default. A near-zero exploratory fraction is a finding about "
                      "the design, not a failed measurement."]
    lines.append("")
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    discovered = discover_conditions()
    # The claims arms answer a claims fixture, not the chain tables, so they are
    # scored on their own axis (verdicts, citations) and kept out of the
    # chain-derived three-way table — putting a verdict count next to an
    # accuracy would be comparing two different tasks.
    claims_arms = [c for c in discovered if arm_spec(c).get("mode") == "claims"]
    parallel_arms = [c for c in discovered if arm_spec(c).get("mode") == "parallel"]
    conditions = [c for c in discovered
                  if c not in claims_arms and c not in parallel_arms]
    answers = {c: rounds_of(load_json(RESULTS / f"answers_{c}.json")) for c in conditions}
    # score.py keys conditions by the answers filename stem: "answers_file"/"answers_dsos".
    raw_scores = load_json(RESULTS / "scores.json")
    scores = {c: raw_scores.get(f"answers_{c}", {"results": {}}) for c in conditions}
    sessions, arts = dsos_store_metrics() if (HERE / "store.db").exists() else ({}, {})
    store_m = dsos_metrics(sessions, arts) if sessions else {}

    # dsos store sessions map to runs via the runner's own run index, appended
    # as each round completes. Parsing the console log was fragile: it broke for
    # any --label, and a truncated log silently mis-paired rounds with sessions,
    # which would quietly corrupt every reuse number.
    dsos_round_metrics = {}
    for label in conditions:
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
    report = {"per_round": {}, "per_table": {}, "models": {}}
    for cond in conditions:
        models = set()
        for rounds in answers[cond].values():
            for rec in rounds.values():
                if rec.get("model"):
                    models.add(rec["model"])
        report["models"][cond] = sorted(models)
    # Round numbers come from the data, not a fixed 1..3: the depth chains
    # run to R6, and a report that stops at R3 hides the reuse-heavy tail.
    for rnd in sorted({r for c in conditions for a in answers[c].values() for r in a}):
        row = {}
        for cond in conditions:
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

    for table in sorted({t for cond in conditions for t in answers[cond]}):
        row = {}
        for cond in conditions:
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
        for cond in conditions:
            per = {}
            for rnd in sorted(answers[cond].get(table, {})):
                if rnd == 1:
                    continue  # R1 is the seed; there is nothing to reuse yet
                # Which scorer applies comes from the registry, not from the
                # label's spelling: a file arm has a transcript, a dsos arm has
                # the store. The old `"file" in cond` test also swallowed
                # chain_file_manifest, scoring the manifest arm as if the
                # manifest did not exist.
                m = (file_transcript_metrics(cond, table, rnd)
                     if is_file_arm(cond) else None)
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

    report["three_way"] = build_three_way(answers, scores, report, conditions)

    # The consumer arm: did the PM tell a backed claim from a refuted one, and
    # did it cite a row the store has since repudiated?
    report["consumer"] = {}
    for cond in claims_arms:
        p = RESULTS / f"answers_{cond}.json"
        if p.exists() and (RESULTS / "claims.json").exists():
            report["consumer"][cond] = claims_metrics(cond, RESULTS)

    # Exploratory noise: how much of the dsos store is a finding nobody claimed,
    # and would hiding it change what a search returns.
    report["exploratory_noise"] = (
        exploratory_noise_metrics(HERE / "store.db") if (HERE / "store.db").exists()
        else {})

    # Parallel arm: two producers, one daemon, duplicate work with the board on
    # vs the same pair with DSOS_DISABLE_BOARD=1.
    report["parallel"] = {}
    for cond in parallel_arms:
        idx = RESULTS / f"runs_{cond}.json"
        store = HERE / "parallel" / arm_spec(cond).get("board", "on") / "store.db"
        if idx.exists() and store.exists():
            runs = json.loads(idx.read_text(encoding="utf-8"))
            report["parallel"][cond] = duplicate_work_metrics(store, runs)

    (RESULTS / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")

    # Console summary
    print("== Per round (accuracy, mean tokens per run) ==")
    printed = False
    for rnd, row in report["per_round"].items():
        for cond in conditions:
            r = row[cond]
            if not r["n"]:
                continue
            printed = True
            print(f"{rnd:4s} {cond:16s} {fmt_pct(r['accuracy']):>7s} "
                  f"{fmt_num(r['tokens_total_mean']):>9s} tok  "
                  f"{r['tool_calls_total']:>4d} calls  {r['duration_s_total']:>5d}s")
    if not printed:
        print("  (no answers found in results/)")

    print("\n== Three-way comparison (accuracy / tokens / re-fetch / findability) ==")
    print("  re-fetch, findability and reuse are R2+ only; R1 is the seed round.")
    for line in three_way_lines(report["three_way"], conditions):
        print("  " + line)
    for cond in conditions:
        r = report["three_way"][cond]
        print(f"  {cond:16s} n={r['n_runs']} runs, {len(r['rounds'])} rounds, "
              f"scored {r['scored_rounds']}, reuse measured on {r['reuse_rounds_measured']} "
              f"round(s)"
              + (f", unmeasured {r['reuse_rounds_unmeasured']}"
                 if r["reuse_rounds_unmeasured"] else "")
              + (f", remote fetches {r['remote_fetches']}" if r["remote_fetches"] else ""))
    if any((report["three_way"][c].get("manifest") or {}).get("advertised_reuse")
           for c in conditions):
        print("  manifest reads = calls touching MANIFEST.md; advertised reuse = calls "
              "that then loaded a file an earlier round's manifest listed.")

    print("\n== Reuse in R2+ (the claim under test) ==")
    print(f"{'table':22s} {'cond':16s} {'rnd':4s} {'findability':>12s} "
          f"{'raw/re-registered':>18s} {'reuse calls':>12s}")
    for table, row in report["per_table"].items():
        for cond in conditions:
            for rnd, m in sorted(row["reuse"].get(cond, {}).items()):
                # A file-arm round with no carry record predates that metric,
                # so its reuse count is unmeasured, not zero. Printing a bare
                # 0 would let missing data read as a measured result.
                if is_file_arm(cond) and m.get("carry_recorded") is False:
                    reuse = "n/a"
                else:
                    reuse = str(m.get("reuse_calls", 0))
                extra = ""
                if uses_manifest(cond) and m.get("manifest_reads"):
                    extra = (f"  (manifest read: {m['manifest_reads']}, advertised "
                             f"reuse: {m.get('advertised_reuse', 0)})")
                print(f"{table:22s} {cond:16s} {rnd:4s} "
                      f"{m.get('findability_calls', 0):>12d} "
                      f"{m.get('raw_data_touches', m.get('new_dataset_registrations', 0)):>18d} "
                      f"{reuse:>12s}{extra}")
    print("  (n/a = run predates carry recording; reuse was not measured for that round)")
    print("\n== models seen per arm ==")
    for cond in conditions:
        print(f"  {cond:16s} {report['models'][cond] or '(not recorded — run predates model capture)'}")
    if report.get("consumer"):
        print("\n== Consumer arm (claims) ==")
        print(f"{'arm':16s} {'claims':>6s} {'correct':>8s} {'cited':>6s} {'bad backing':>12s} {'tokens':>8s}")
        for cond, m in report["consumer"].items():
            toks = (m.get("tokens_in") or 0) + (m.get("tokens_out") or 0)
            print(f"{cond:16s} {m['claims']:>6d} {m['verdict_correct']:>8d} "
                  f"{m['cited_support']:>6d} {m['bad_backing']:>10d} {toks:>8,d}")
    if report.get("exploratory_noise"):
        noise = report["exploratory_noise"]["store"]
        frac = noise["exploratory_fraction"]
        print("\n== Exploratory noise ==")
        print(f"  {noise['exploratory']} of {noise['artifacts']} artifacts exploratory "
              f"({'n/a' if frac is None else format(frac, '.1%')})")
        for sid, s in report["exploratory_noise"]["sessions"].items():
            if s.get("searches"):
                print(f"  {sid[:12]}  searches={s['searches']} "
                      f"returning-exploratory={s['searches_returning_exploratory']} "
                      f"only-exploratory={s['searches_only_exploratory']}")
    if report.get("parallel"):
        print("\n== Parallel arm (duplicate work) ==")
        print(f"{'arm':20s} {'pair':18s} {'A regs':>6s} {'B regs':>6s} {'dups':>5s} {'rate':>6s}")
        for cond, data in report["parallel"].items():
            for pair, p in data["pairs"].items():
                rate = p.get("duplicate_work_rate")
                print(f"{cond:20s} {pair:18s} {str(p['a_registrations']):>6s} "
                      f"{str(p['b_registrations']):>6s} {str(p['duplicates']):>5s} "
                      f"{'n/a' if rate is None else format(rate, '.0%'):>6s}")
            t = data["totals"]
            rate = t["duplicate_work_rate"]
            print(f"{cond:20s} {'ALL':18s} {'':>6s} {t['b_registrations']:>6d} "
                  f"{t['duplicates']:>5d} {'n/a' if rate is None else format(rate, '.0%'):>6s}")
    write_report_md(report, conditions)
    print("\nWrote results/report.json and results/report.md")




if __name__ == "__main__":
    main()
