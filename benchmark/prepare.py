"""Build the pilot manifest for the DABench-based benchmark.

Downloads the InfiAgent-DABench dev questions/labels and the raw CSV for
each pilot table, then writes benchmark/manifest.json describing the
round structure:

    R1 = easiest question on the table (ingest + analysis)
    R2 = a medium question (reuse expected)
    R3 = a hard question (reuse expected)

All outputs stay inside benchmark/: data/<table>.csv and manifest.json.

Usage:
    python prepare.py                 # default 5-table pilot subset
    python prepare.py --tables a.csv b.csv   # a different subset
"""

import argparse
import datetime
import json
import pathlib
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
HF_BASE = "https://huggingface.co/datasets/infiagent/DABench/resolve/main"

# The pilot subset: >=3 questions, small files, diverse domains/levels.
# Sizes verified 2024-xx via HF HEAD requests; see benchmark/README.md.
PILOT_TABLES = [
    "titanic.csv",    # 24 questions, 58KB
    "auto-mpg.csv",   # 8 questions, 13KB
    "insurance.csv",  # 6 questions, 53KB
    "hotel_data.csv",  # 6 questions, 81KB
    "microsoft.csv",  # 5 questions, 11KB
]
ROUNDS_PER_TABLE = 3
LEVEL_RANK = {"easy": 0, "medium": 1, "hard": 2}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dsos-benchmark/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_jsonl(name: str) -> list:
    raw = fetch(f"{HF_BASE}/{name}")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def download_table(file_name: str, out_dir: pathlib.Path) -> int:
    encoded = urllib.parse.quote(file_name)
    raw = fetch(f"{HF_BASE}/da-dev-tables/{encoded}")
    path = out_dir / file_name
    path.write_bytes(raw)
    return len(raw)


def select_rounds(questions: list, n: int = ROUNDS_PER_TABLE) -> list:
    """Pick n questions: one easy, one medium, one hard if available
    (by ascending question id within each level); fill from whatever
    remains, in level order. R1 (the ingest round) is always the easiest."""
    by_level = {rank: sorted(qs, key=lambda q: q["id"])
                for rank, qs in _group_by_level(questions).items()}
    picked = []
    for rank in (0, 1, 2):  # easy, medium, hard
        if len(picked) < n and by_level.get(rank):
            picked.append(by_level[rank].pop(0))
    rest = sorted((q for qs in by_level.values() for q in qs), key=lambda q: LEVEL_RANK[q["level"]])
    for q in rest:
        if len(picked) < n:
            picked.append(q)
    return picked


def _group_by_level(questions: list) -> dict:
    groups = {}
    for q in questions:
        groups.setdefault(LEVEL_RANK[q["level"]], []).append(q)
    return groups


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", nargs="*", default=PILOT_TABLES,
                    help="table file names to include (default: the pilot 5)")
    ap.add_argument("--out", default=str(HERE / "manifest.json"))
    args = ap.parse_args()

    print("Downloading DABench questions and labels ...")
    questions = fetch_jsonl("da-dev-questions.jsonl")
    labels = {rec["id"]: dict(rec["common_answers"]) for rec in fetch_jsonl("da-dev-labels.jsonl")}

    data_dir = HERE / "data"
    data_dir.mkdir(exist_ok=True)

    tables = []
    for file_name in args.tables:
        table_qs = [q for q in questions if q["file_name"] == file_name]
        if not table_qs:
            raise SystemExit(f"No questions found for table {file_name!r} in DABench dev")
        rounds = select_rounds(table_qs)
        size = download_table(file_name, data_dir)
        print(f"  {file_name}: {size // 1024}KB, {len(table_qs)} questions, "
              f"using ids {[q['id'] for q in rounds]}")
        tables.append({
            "file_name": file_name,
            "data_path": f"data/{file_name}",
            "size_bytes": size,
            "questions_available": len(table_qs),
            "rounds": [
                {
                    "round": i + 1,
                    "question_id": q["id"],
                    "question": q["question"],
                    "constraints": q.get("constraints", ""),
                    "format": q.get("format", ""),
                    "level": q["level"],
                    "concepts": q.get("concepts", []),
                    "answers": {k: v for k, v in labels.get(q["id"], {}).items()},
                }
                for i, q in enumerate(rounds)
            ],
        })

    manifest = {
        "source": {
            "benchmark": "InfiAgent-DABench (dev split)",
            "url": "https://huggingface.co/datasets/infiagent/DABench",
            "paper": "https://arxiv.org/abs/2401.05507",
            "code": "https://github.com/InfiAgent/InfiAgent (Apache-2.0)",
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "method": "downloaded via benchmark/prepare.py",
        },
        "rounds_per_table": ROUNDS_PER_TABLE,
        "tables": tables,
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({len(tables)} tables x {ROUNDS_PER_TABLE} rounds = "
          f"{len(tables) * ROUNDS_PER_TABLE} agent runs per condition)")


if __name__ == "__main__":
    main()
