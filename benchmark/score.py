"""Score recorded answers against the manifest's gold answers.

DABench questions require answers in a tagged format, e.g.
``@mean_fare[34.65]`` — so scoring is deterministic: extract tags from
the final answer text, compare each against the gold value with
numeric tolerance (no LLM judge).

Answers file format (JSON, one entry per round run):

    {
      "<table>::R<round>": {
        "answer_text": "The mean fare was @mean_fare[34.66].",
        "answer_text_tagged": "...same, when the turn ended with a summary...",
        "tokens_in": 52310,        # optional
        "tokens_out": 3120,        # optional
        "duration_s": 184,        # optional
        "tool_calls": 22,         # optional
        "notes": "..."            # optional
      }
    }

Usage:
    python score.py results/answers_dsos.json --out results/scores_dsos.json
    python score.py results/answers_file.json results/answers_dsos.json  # both conditions

Optional token/cost fields, when present, are carried through into the
per-round summary so efficiency numbers live in the same artifact.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent

TAG_RE = re.compile(r"@([A-Za-z0-9_.\-]+)\[([^\[\]]*)\]")

# DABench's flat convention is @tag[value]. Per-category questions need the
# keyed form @tag[key:value] (e.g. @median_price[Fair:3282.00]) to match a
# gold key like "median_price[Fair]". Keyed and flat tags coexist: an inner
# "key:value" is keyed unless the left side is purely numeric (so a value
# that happens to contain a colon isn't mistaken for a key).
_KEYED_RE = re.compile(r"^([^:]{1,60}):\s*(.+)$")


def _split_tag(inner: str) -> tuple[str | None, str]:
    m = _KEYED_RE.match(inner.strip())
    if m and not _is_number(m.group(1)):
        return m.group(1).strip(), m.group(2).strip()
    return None, inner.strip()


def _is_number(s: str) -> bool:
    try:
        float(s.replace(",", "").strip())
        return True
    except ValueError:
        return False


def extract_tags(answer_text: str) -> dict:
    """Return {(tag, key): value} for every @tag[...] in the answer text.
    key is None for flat tags. First occurrence of a given (tag, key) wins,
    so an agent that repeats a tag for different categories is scored on each
    category rather than silently keeping only the first."""
    tags = {}
    for name, inner in TAG_RE.findall(answer_text or ""):
        key, value = _split_tag(inner)
        k = (name, key)
        if k not in tags:
            tags[k] = value
    return tags


def gold_keys(gold: dict) -> dict:
    """Gold keys are written "tag" or "tag[key]" -> the same (tag, key) shape."""
    out = {}
    for k, v in gold.items():
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[([^\]]*)\])?$", k)
        if m:
            out[(m.group(1), m.group(2))] = v
    return out


def _as_float(s):
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def match(got: str, expected: str, abs_tol: float, rel_tol: float) -> bool:
    g, e = _as_float(got), _as_float(expected)
    if g is not None and e is not None:
        return abs(g - e) <= max(abs_tol, rel_tol * abs(e))
    return str(got).strip().lower() == str(expected).strip().lower()


def score_round(rec: dict, gold: dict, abs_tol: float, rel_tol: float) -> dict:
    # answer_text is whatever the agent said last; answer_text_tagged is the
    # last thing it said that carried the tags (the runner records both). A
    # turn that ends with a closing summary after already stating its answer
    # would otherwise be scored as "missing" — a property of the harness's
    # choice of message, not of the arm. Older answer files have only the
    # former, so they score exactly as before.
    text = rec.get("answer_text_tagged") or rec.get("answer_text", "")
    tags = extract_tags(text)
    gk = gold_keys(gold)
    details = {}
    for key, expected in gk.items():
        if key not in tags:
            details[f"{key[0]}[{key[1]}]" if key[1] else key[0]] = {
                "status": "missing", "expected": expected}
        else:
            got = tags[key]
            ok = match(got, expected, abs_tol, rel_tol)
            name = f"{key[0]}[{key[1]}]" if key[1] else key[0]
            details[name] = {"status": "ok" if ok else "wrong",
                             "got": got, "expected": expected}
    passed = bool(gk) and all(d["status"] == "ok" for d in details.values())
    return {"passed": passed, "tags": details}


def load_manifest(path: pathlib.Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    index = {}
    for table in manifest["tables"]:
        for rnd in table["rounds"]:
            index[f"{table['file_name']}::R{rnd['round']}"] = (table, rnd)
    return manifest, index


def score_condition(answers_path: pathlib.Path, index: dict,
                     abs_tol: float, rel_tol: float) -> dict:
    raw = json.loads(answers_path.read_text(encoding="utf-8"))
    results, missing = {}, []
    for key, (table, rnd) in index.items():
        if key not in raw:
            missing.append(key)
            continue
        rec = raw[key]
        details = score_round(rec, rnd["answers"], abs_tol, rel_tol)
        details.update({
            "question_id": rnd["question_id"], "level": rnd["level"],
            "tokens_in": rec.get("tokens_in"), "tokens_out": rec.get("tokens_out"),
            "duration_s": rec.get("duration_s"), "tool_calls": rec.get("tool_calls"),
        })
        results[key] = details

    by_round = {}
    # Rounds come from the answers, not a fixed 1..3: the depth chains run to
    # R6, and a summary that stops at R3 hides the reuse-heavy tail of a chain.
    for r in sorted({int(k.rsplit("R", 1)[1]) for k in results}):
        rows = [d for k, d in results.items() if k.endswith(f"R{r}")]
        if rows:
            by_round[f"R{r}"] = {
                "n": len(rows),
                "accuracy": sum(d["passed"] for d in rows) / len(rows),
                "tokens_in_total": sum(d["tokens_in"] or 0 for d in rows),
                "tokens_out_total": sum(d["tokens_out"] or 0 for d in rows),
            }
    return {"answers_file": str(answers_path), "missing": missing,
            "results": results, "by_round": by_round,
            "overall_accuracy": (sum(d["passed"] for d in results.values()) / len(results)
                                 if results else None)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("answers", nargs="+", help="answers JSON file(s), one per condition")
    ap.add_argument("--manifest", default=str(HERE / "manifest.json"))
    ap.add_argument("--out", default=None, help="write combined scores JSON here")
    ap.add_argument("--abs-tol", type=float, default=0.02,
                    help="absolute numeric tolerance (default 0.02)")
    ap.add_argument("--rel-tol", type=float, default=0.01,
                    help="relative numeric tolerance (default 1%%)")
    args = ap.parse_args()

    manifest, index = load_manifest(pathlib.Path(args.manifest))
    combined = {}
    for path in args.answers:
        cond = pathlib.Path(path).stem  # answers_dsos.json -> "answers_dsos"
        combined[cond] = score_condition(pathlib.Path(path), index,
                                         args.abs_tol, args.rel_tol)

    # Console summary
    for cond, sc in combined.items():
        print(f"\n== {cond} ==")
        for key, d in sorted(sc["results"].items()):
            flag = "PASS" if d["passed"] else "FAIL"
            bad = [f"{n}: got {t.get('got', '(missing)')!r}, expected {t['expected']!r}"
                   for n, t in d["tags"].items() if t["status"] != "ok"]
            print(f"  [{flag}] {key} (id={d['question_id']}, {d['level']})"
                  + (f"  -- {'; '.join(bad)}" if bad else ""))
        for r, s in sc["by_round"].items():
            print(f"  {r}: accuracy {s['accuracy']:.0%} ({s['n']} runs)")
        if sc["missing"]:
            print(f"  MISSING: {', '.join(sc['missing'])}")
        print(f"  overall: {sc['overall_accuracy']:.0%}"
              if sc["overall_accuracy"] is not None else "  overall: n/a")

    out = pathlib.Path(args.out) if args.out else HERE / "results" / "scores.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(combined, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
