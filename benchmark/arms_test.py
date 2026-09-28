"""Smoke test for the benchmark's three arms and its three-way reporting.

Stands in for ``tests/*_test.py``: a standalone script with a ``check``
helper that exits 1 on any failure. It lives in ``benchmark/`` because Lane H
owns that directory and nothing else — ``tests/run_all.py`` does not scan it
yet, so run this directly::

    .venv/Scripts/python benchmark/arms_test.py

Covers the pieces WP-H1 adds: the arm registry shared by runner.mjs and the
Python side, the MANIFEST.md parser and the manifest metrics it feeds, and
report.py's three-way table over file / file_manifest / dsos.
"""

import io
import json
import pathlib
import sqlite3
import sys
import contextlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import metrics  # noqa: E402
import report  # noqa: E402
import score  # noqa: E402

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


ARMS = ("file", "file_manifest", "dsos", "consumer", "consumer_files")
# The run labels the three-way fixture writes results under: an experiment
# name in front, the arm behind it.
FIXTURE_LABELS = ("chain_file", "chain_file_manifest", "chain_dsos")
# The claims arms answer the fixture, not the chain tables.
CLAIMS_ARMS = ("consumer", "consumer_files")


# ------------------------------------------------------- 1. the arm registry

def test_registry():
    print("== arm registry ==")
    arms = metrics.load_arms()
    check("registry has the three arms", set(arms) == set(ARMS),
          f"got {sorted(arms)}")
    fm = arms.get("file_manifest", {})
    check("file_manifest requires a manifest", fm.get("manifest") is True)
    check("file_manifest removes the raw CSV after R1, like dsos",
          fm.get("raw_csv_policy") == "remove_after_r1"
          and arms["dsos"].get("raw_csv_policy") == "remove_after_r1",
          f"file_manifest={fm.get('raw_csv_policy')!r} dsos={arms['dsos'].get('raw_csv_policy')!r}")
    check("the plain file arm keeps the raw CSV",
          arms["file"].get("raw_csv_policy") == "keep")
    check("only file_manifest is scored from a manifest",
          [a for a, s in arms.items() if s.get("manifest")] == ["file_manifest"])
    check("the two claims arms carry mode=claims",
          [a for a, s in arms.items() if s.get("mode") == "claims"] == list(CLAIMS_ARMS),
          f"got {[a for a, s in arms.items() if s.get('mode') == 'claims']}")
    check("the consumer arm is store-only (consumer tools)",
          arms["consumer"].get("tools") == "consumer")
    check("the files ceiling gets builtin tools, not consumer tools",
          arms["consumer_files"].get("tools") == "builtin")
    check("the claims arms sit after the chain arms in the table order",
          arms["consumer"].get("order", 0) > arms["dsos"].get("order", 0)
          and arms["consumer_files"].get("order", 0) > arms["consumer"].get("order", 0))


def test_findability_tools():
    print("== FINDABILITY_TOOLS (U2) ==")
    # list_skills is gone from the product; counting it is dead weight that would
    # mislead anyone reading the findability numbers. list_templates came back.
    check("metrics no longer counts the removed list_skills tool",
          "list_skills" not in metrics.FINDABILITY_TOOLS,
          f"got {sorted(metrics.FINDABILITY_TOOLS)}")
    check("metrics still counts list_templates", "list_templates" in metrics.FINDABILITY_TOOLS)
    check("report no longer counts list_skills",
          "list_skills" not in report.FINDABILITY_TOOLS,
          f"got {sorted(report.FINDABILITY_TOOLS)}")


def test_arm_of():
    print("== label -> arm ==")
    for label, want in [("file", "file"), ("dsos", "dsos"),
                        ("file_manifest", "file_manifest"),
                        ("chain_file", "file"),
                        ("chain_dsos", "dsos"),
                        ("chain_file_manifest", "file_manifest"),
                        ("file_keepraw", "file"),
                        ("claims_consumer", "consumer"),
                        ("claims_consumer_files", "consumer_files")]:
        check(f"arm_of({label!r}) == {want!r}", metrics.arm_of(label) == want,
              f"got {metrics.arm_of(label)!r}")
    check("an unknown label resolves to no arm", metrics.arm_of("nonsense") is None)
    check("is_file_arm(chain_file_manifest)", metrics.is_file_arm("chain_file_manifest"))
    check("not is_file_arm(chain_dsos)", not metrics.is_file_arm("chain_dsos"))
    check("uses_manifest(chain_file_manifest)", metrics.uses_manifest("chain_file_manifest"))
    check("not uses_manifest(chain_file)", not metrics.uses_manifest("chain_file"))


def test_runner_reads_registry():
    print("== runner.mjs ==")
    src = (HERE / "runner.mjs").read_text(encoding="utf-8")
    check("runner.mjs loads the shared registry", "arms.json" in src)
    check("runner.mjs accepts the file_manifest condition", "file_manifest" in src)
    check("runner.mjs writes the MANIFEST.md system prompt",
          "appendSystemPrompt" in src and "MANIFEST.md" in src)


# ------------------------------------------------------------- 2. the manifest

MANIFEST_TEXT = """# MANIFEST.md

Index of the derived files in this working directory.

## clean.csv
- question: drop the impossible rows and add price_per_carat
- computed: pandas: read diamonds.csv, drop x/y/z == 0, price/carat, to_csv
- inputs: diamonds.csv

## by_cut.csv
- question: median price per cut
- computed: clean.groupby('cut').price.median()
- inputs: clean.csv, diamonds.csv
"""


def test_parse_manifest():
    print("== MANIFEST.md parsing ==")
    entries = metrics.parse_manifest(MANIFEST_TEXT)
    check("two entries parsed", len(entries) == 2, f"got {len(entries)}")
    if len(entries) != 2:
        return
    e = entries[0]
    check("entry carries path/question/computed/inputs",
          e["path"] == "clean.csv" and e["question"].startswith("drop the impossible")
          and e["computed"].startswith("pandas") and e["inputs"] == ["diamonds.csv"],
          json.dumps(e, ensure_ascii=False))
    check("a second entry parses too", entries[1]["path"] == "by_cut.csv"
          and entries[1]["inputs"] == ["clean.csv", "diamonds.csv"])
    check("an empty manifest yields no entries", metrics.parse_manifest("# MANIFEST.md\n") == [])


# ------------------------------------------------------- 3. manifest metrics

def test_manifest_metrics():
    print("== manifest metrics ==")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "manifests" / "chain_file_manifest").mkdir(parents=True)
        (root / "manifests" / "chain_file_manifest" / "diamonds.csv_R1.md").write_text(
            MANIFEST_TEXT, encoding="utf-8")
        (root / "transcripts" / "chain_file_manifest").mkdir(parents=True)
        events = [
            {"ts": "1", "tool": "read", "summary": "MANIFEST.md", "paths": ["MANIFEST.md"]},
            {"ts": "2", "tool": "bash", "summary": "python -c 'import pandas'",
             "paths": ["clean.csv"]},
            {"ts": "3", "tool": "bash", "summary": "python -c 'import pandas'",
             "paths": ["by_cut.csv"]},
            {"ts": "4", "tool": "bash", "summary": "curl -O diamonds.csv", "paths": []},
        ]
        (root / "transcripts" / "chain_file_manifest" / "diamonds.csv_R2.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        m = metrics.manifest_metrics("chain_file_manifest", "diamonds.csv", 2, root)
    check("the manifest was read at round start", m["manifest_reads"] == 1, json.dumps(m))
    check("reuse is credited only for a file the manifest advertised",
          m["advertised_reuse"] == 2, json.dumps(m))
    check("remote fetch attempts are surfaced (raw-CSV confound)", m["remote_fetches"] == 1,
          json.dumps(m))
    check("the manifest's own entries are reported", m["advertised_paths"] == ["clean.csv", "by_cut.csv"],
          json.dumps(m))
    check("an arm with no manifest snapshots has no advertised paths",
          metrics.manifest_metrics("chain_file", "diamonds.csv", 2, root)["advertised_paths"] == [])


# ------------------------------------------------------- 4. the three-way table

def _write_transcript(root, label, table, rnd, events):
    d = root / "transcripts" / label
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{table}_R{rnd}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _make_fixture(root):
    """Three arms of the same two-round chain, enough for one three-way row."""
    table = "d.csv"
    for label, tools, in (("chain_file", 3), ("chain_file_manifest", 4), ("chain_dsos", 5)):
        answers = {}
        for rnd, toks in ((1, 1000), (2, 2000)):
            answers[f"{table}::R{rnd}"] = {
                "answer_text": "@n_rows[10]", "tokens_in": toks, "tokens_out": toks // 2,
                "duration_s": 5, "tool_calls": tools, "model": "fake/model",
            }
        (root / f"answers_{label}.json").write_text(
            json.dumps(answers, indent=2) + "\n", encoding="utf-8")
        _write_transcript(root, label, table, 2, [
            {"ts": "1", "tool": "read", "summary": "MANIFEST.md", "paths": ["MANIFEST.md"]},
            {"ts": "2", "tool": "bash", "summary": "python x.py", "paths": ["clean.csv"]},
        ] * (label != "chain_dsos"))
        d = root / "carry" / label
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{table}_R2.json").write_text(json.dumps({
            "raw_file": table, "carried": ["MANIFEST.md", "clean.csv"],
            "created": ["clean.csv"],
        }) + "\n", encoding="utf-8")
    (root / "manifests" / "chain_file_manifest").mkdir(parents=True, exist_ok=True)
    (root / "manifests" / "chain_file_manifest" / f"{table}_R1.md").write_text(
        MANIFEST_TEXT, encoding="utf-8")
    # score.py output, keyed the way it keys it: by the answers-file stem.
    scores = {}
    for label in ("chain_file", "chain_file_manifest", "chain_dsos"):
        scores[f"answers_{label}"] = {"results": {
            f"{table}::R1": {"passed": True}, f"{table}::R2": {"passed": label != "chain_dsos"}}}
    (root / "scores.json").write_text(json.dumps(scores, indent=2) + "\n", encoding="utf-8")
    # A minimal dsos store: R1's session owning clean.csv, R2 touching it (reuse)
    # and registering the raw CSV again (re-fetch).
    con = sqlite3.connect(root / "store.db")
    con.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, question TEXT, started_at TEXT);
        CREATE TABLE artifacts (row_id TEXT PRIMARY KEY, session_id TEXT, type TEXT, title TEXT, status TEXT);
        CREATE TABLE tool_calls (session_id TEXT, tool_name TEXT, artifact_row_ids TEXT);
    """)
    con.execute("INSERT INTO sessions VALUES ('s1','q1','2026-01-01')")
    con.execute("INSERT INTO sessions VALUES ('s2','q2','2026-01-02')")
    con.execute("INSERT INTO artifacts VALUES ('a1','s1','transform','clean','result')")
    con.execute("INSERT INTO artifacts VALUES ('a2','s2','dataset','d.csv','exploratory')")
    con.execute("INSERT INTO tool_calls VALUES ('s2','search_artifacts','[]')")
    con.execute("INSERT INTO tool_calls VALUES ('s2','get_artifact','[\"a1\", \"a2\"]')")
    con.commit()
    con.close()
    (root / "runs_chain_dsos.json").write_text(json.dumps([
        {"table": table, "round": 2, "condition": "dsos", "session_id": "s2"},
    ]) + "\n", encoding="utf-8")


def test_three_way_report():
    print("== three-way report ==")
    import tempfile
    saved_results, saved_here = report.RESULTS, report.HERE
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "bench"
        root.mkdir()
        (root / "results").mkdir()
        _make_fixture(root / "results")
        report.RESULTS = report.HERE = root / "results"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                report.main()
        finally:
            report.RESULTS, report.HERE = saved_results, saved_here
        out = buf.getvalue()
        rep = json.loads((root / "results" / "report.json").read_text(encoding="utf-8"))
        md = root / "results" / "report.md"
        md_exists = md.exists()
        md_text = md.read_text(encoding="utf-8") if md_exists else ""

    tw = rep.get("three_way", {})
    check("the three-way table has one row per run label", set(tw) == set(FIXTURE_LABELS),
          f"got {sorted(tw)}")
    check("each row names the arm its label resolves to",
          {c: tw[c].get("registry_arm") for c in tw} == {"chain_file": "file",
                                                         "chain_file_manifest": "file_manifest",
                                                         "chain_dsos": "dsos"},
          json.dumps({c: tw[c].get("registry_arm") for c in tw}))
    if set(tw) == set(FIXTURE_LABELS):
        f, fm, d = tw["chain_file"], tw["chain_file_manifest"], tw["chain_dsos"]
        for arm in (f, fm, d):
            for field in ("accuracy", "tokens_mean", "refetch", "findability", "reuse"):
                check(f"{arm.get('arm')} has {field}", field in arm, json.dumps(arm))
        check("file arm accuracy 2/2", f["accuracy"] == 1.0, json.dumps(f))
        check("dsos arm accuracy 1/2", d["accuracy"] == 0.5, json.dumps(d))
        check("dsos re-fetch is the new dataset registration", d["refetch"] == 1, json.dumps(d))
        check("dsos findability is one search_artifacts", d["findability"] == 1, json.dumps(d))
        check("dsos reuse is the cross-session artifact touch", d["reuse"] == 1, json.dumps(d))
        check("file_manifest reuse is credited from the manifest + carry",
              fm["reuse"] >= 1, json.dumps(fm))
        check("file_manifest reports advertised reuse",
              fm.get("manifest", {}).get("advertised_reuse", 0) >= 1, json.dumps(fm))
        check("a non-manifest arm reports no manifest block", f["manifest"] is None,
              json.dumps(f))
    check("a per-round table covering rounds beyond R3 is produced",
          "R2" in rep.get("per_round", {}) and len(rep["per_round"]) >= 2,
          f"rounds {sorted(rep.get('per_round', {}))}")
    check("report.md is written", md_exists)
    if md_exists:
        check("report.md carries the three-way table",
              all(a in md_text for a in FIXTURE_LABELS) and "findability" in md_text.lower())
    check("the console summary prints all three arms",
          all(a in out for a in FIXTURE_LABELS) and "findability" in out.lower())


# --------------------------------------------------------- 5. answer extraction

def test_scoring():
    print("== answer extraction ==")
    import score
    gold = {"n_rows": "53920"}
    tagged = score.score_round(
        {"answer_text": "Done: summary of the work, no tags here.",
         "answer_text_tagged": "Rows remaining: @n_rows[53920]"},
        gold, 0.02, 0.01)
    check("a turn that ends with a summary still scores its tagged answer",
          tagged["passed"] and tagged["tags"]["n_rows"]["got"] == "53920",
          json.dumps(tagged))
    # What the runner records when the final message corrects an earlier one:
    # the last *tagged* text is the final message, so the correction wins.
    trailing = score.score_round(
        {"answer_text": "Correcting myself: @n_rows[12345]",
         "answer_text_tagged": "Correcting myself: @n_rows[12345]"},
        gold, 0.02, 0.01)
    check("when the last message disagrees with the tagged one, the last wins",
          not trailing["passed"], json.dumps(trailing))
    legacy = score.score_round({"answer_text": "@n_rows[53920]"}, gold, 0.02, 0.01)
    check("an answers file with only answer_text scores as before", legacy["passed"],
          json.dumps(legacy))


def test_claims_metrics():
    print("== consumer claims scoring ==")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        r1, r2, r3, bad, bad2 = "a" * 8, "b" * 8, "d" * 8, "c" * 8, "e" * 8
        fixture = {"claims": [
            {"id": 1, "verdict": "supported", "claim": "x",
             "support_row_id": r1, "forbidden_row_ids": []},
            {"id": 2, "verdict": "refuted", "claim": "y",
             "support_row_id": r2, "forbidden_row_ids": [bad]},
            {"id": 3, "verdict": "supported", "claim": "z",
             "support_row_id": r3, "forbidden_row_ids": [bad2]},
        ]}
        (root / "claims.json").write_text(json.dumps(fixture), encoding="utf-8")
        # A refutation that names the repudiated row (healthy) and a support
        # that leans on a repudiated row (the target-0 error).
        answer = (f"@claim[1:supported:{r1}]\n"
                  f"@claim[2:refuted:{bad}]\n"
                  f"@claim[3:supported:{bad2}]\n")
        (root / "answers_consumer.json").write_text(json.dumps({
            "claims::R1": {"answer_text": answer, "tokens_in": 100, "tokens_out": 20,
                            "tool_calls": 3, "model": "fake/model"}}), encoding="utf-8")
        m = metrics.claims_metrics("consumer", root)
    check("every claim is scored", m["claims"] == 3, json.dumps(m))
    check("all three verdicts are correct", m["verdict_correct"] == 3, json.dumps(m))
    check("the correct support row is credited", m["cited_support"] == 1, json.dumps(m))
    check("supporting a claim with a repudiated row is bad backing (target 0)",
          m["bad_backing"] == 1, json.dumps(m))
    check("a refutation that names the repudiated row is not a bad citation",
          m["refuted_by_bad_row"] == 1, json.dumps(m))
    check("tokens are carried through", m["tokens_in"] == 100, json.dumps(m))
    # A missing verdict line is unresolved, not silently correct.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "claims.json").write_text(json.dumps(fixture), encoding="utf-8")
        (root / "answers_consumer.json").write_text(json.dumps({
            "claims::R1": {"answer_text": "@claim[1:supported:none]"}}), encoding="utf-8")
        partial = metrics.claims_metrics("consumer", root)
    check("a claim with no verdict line is unresolved, not silently correct",
          partial["unresolved"] == 2 and partial["per_claim"][1]["correct"] is False,
          json.dumps(partial))


def test_exploratory_noise():
    print("== exploratory noise ==")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = pathlib.Path(td) / "store.db"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE artifacts (row_id TEXT PRIMARY KEY, session_id TEXT,
                                    type TEXT, title TEXT, status TEXT);
            CREATE TABLE tool_calls (session_id TEXT, tool_name TEXT,
                                     artifact_row_ids TEXT);
        """)
        for rid, status in (("e1", "exploratory"), ("e2", "exploratory"),
                            ("r1", "result"), ("r2", "result")):
            con.execute("INSERT INTO artifacts VALUES (?,?,?,?,?)",
                        (rid, "s1", "query", rid, status))
        # One search that saw an exploratory row among results, one that only
        # saw exploratory rows (returns nothing post-flip), one that saw none.
        con.execute("INSERT INTO tool_calls VALUES ('s1','search_artifacts','[\"e1\",\"r1\"]')")
        con.execute("INSERT INTO tool_calls VALUES ('s1','search_artifacts','[\"e2\"]')")
        con.execute("INSERT INTO tool_calls VALUES ('s1','search_artifacts','[\"r1\"]')")
        con.commit()
        con.close()
        n = metrics.exploratory_noise_metrics(db)
    check("the exploratory fraction is measured",
          n["store"]["exploratory_fraction"] == 0.5, json.dumps(n["store"]))
    s = n["sessions"]["s1"]
    check("searches returning an exploratory row are counted",
          s["searches_returning_exploratory"] == 2, json.dumps(s))
    check("searches whose only hits were exploratory would return nothing post-flip",
          s["searches_only_exploratory"] == 1, json.dumps(s))


def main():
    for t in (test_registry, test_findability_tools, test_arm_of, test_runner_reads_registry,
              test_parse_manifest, test_manifest_metrics, test_three_way_report,
              test_claims_metrics, test_exploratory_noise, test_scoring):
        print()
        t()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
