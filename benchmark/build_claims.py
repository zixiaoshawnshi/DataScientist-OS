"""Build the consumer arm's claim fixture: a store of chain results, some of
them deliberately unreliable, plus the claims a PM agent has to adjudicate.

The consumer arm asks the one question the producer arms cannot: given a claim
a colleague wrote down, can an agent that has no data access decide whether to
back it, and cite the right row — or does it quote a row that was superseded,
contradicted, or has gone stale? The producer arms measure what it costs to
reuse work; this one measures whether a reader can tell good work from bad.

The claim values are the depth chains' gold answers
(`results/manifest_chains.json`), so every claim is a statement about a result
an earlier chain round actually produced. The store is assembled here rather
than taken from a producer run on purpose: the three lifecycle hazards (a
superseded first pass, a contradicted recomputation, a dataset past its
`refresh_after`) have to sit on exactly the rows the claims are about, and a
model's producer transcript puts them wherever it happens to. A deterministic
fixture is what lets the same claims be scored the same way across arms.

Two arms read this fixture:

  consumer        — a PM agent on /mcp/consumer, the store its only record.
  consumer_files  — the ceiling: the same PM with the raw CSVs and a
                    MANIFEST.md, no store. Files can hold the numbers but not
                    the lifecycle, which is the whole asymmetry under test.

Writes:
  benchmark/results/claims.json          the claims, their gold verdict, and
                                         the rows that support or must never
                                         be cited
  benchmark/consumer_store.db            the prepared store (gitignored)
  benchmark/results/consumer_workspace/  the files ceiling's working directory

Run: .venv/Scripts/python.exe build_claims.py
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
RESULTS = HERE / "results"
# In its own directory, which is tidier than it is necessary: the daemon's
# manifest is per store (`<store>.daemon.json`), so two stores could share
# `benchmark/` now, but they share `daemon.token` there.
STORE_PATH = HERE / "consumer" / "store.db"
WORKSPACE = RESULTS / "consumer_workspace"

sys.path.insert(0, str(HERE))
# The shared venv has dsos installed editable against the MAIN checkout; a
# worktree's own copy has to win, so put the repo root ahead of it.
sys.path.insert(0, str(REPO))

# dsos is imported only after DSOS_DB_PATH is set below: the library reads the
# path lazily, but Database() opens the file at construction, so the env has to
# be in place before anything touches it.
from dsos import store  # noqa: E402


def gold_answers() -> dict:
    """{chain file_name: {round: {tag: value}}} from the depth chains."""
    manifest = json.loads(
        (RESULTS / "manifest_chains.json").read_text(encoding="utf-8")
    )
    return {
        t["file_name"]: {r["round"]: r["answers"] for r in t["rounds"]}
        for t in manifest["tables"]
    }


def build_store() -> dict:
    """Register the chain results and plant the lifecycle hazards.

    Returns the row_ids of the rows the claims refer to, so claims.json can
    name exactly which row backs a claim and which rows must never be cited.
    """
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(STORE_PATH) + suffix)
        if p.exists():
            p.unlink()
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if (STORE_PATH.parent / "blobs").exists():
        shutil.rmtree(STORE_PATH.parent / "blobs")

    os.environ["DSOS_DB_PATH"] = str(STORE_PATH)
    from dsos.db import Database

    db = Database(STORE_PATH)
    conn = db.conn()
    sid = store.start_session(conn, "consumer-arm claim fixture")
    old_fetch = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=3)).isoformat()

    def save(**kw):
        return store.save_artifact(conn, session_id=sid, **kw)

    rid = {}
    # --- diamonds -----------------------------------------------------
    rid["diamonds_ds"] = save(
        type="dataset", title="diamonds.csv (raw)",
        description="The raw diamonds table, one row per stone.",
        content={"file": "diamonds.csv"}, content_format="json",
        source={"url": "local", "refresh_after": "static"},
    )
    d = gold_answers()["diamonds.csv"]
    rid["diamonds_r1"] = save(
        type="query", title="diamonds R1 — cleaned rows and mean price per carat",
        description="Rows surviving the x/y/z=0 drop, and the mean price_per_carat over them.",
        content=dict(d[1]), content_format="json",
        parent_row_ids=[rid["diamonds_ds"]], tags=["diamonds", "r1"],
    )
    rid["diamonds_r2"] = save(
        type="query", title="diamonds R2 — median price and count by cut",
        description="Median price and row count for each of the five cut levels.",
        content=dict(d[2]), content_format="json",
        parent_row_ids=[rid["diamonds_r1"]], tags=["diamonds", "r2"],
    )
    rid["diamonds_r4"] = save(
        type="query", title="diamonds R4 — premium share and top cut",
        description="Share of cleaned rows above the mean price per carat, and the cut with the most premium stones.",
        content=dict(d[4]), content_format="json",
        parent_row_ids=[rid["diamonds_r1"]], tags=["diamonds", "r4"],
    )
    # A first pass that got the premium share wrong, then was replaced. The
    # claim quoting its wrong number must not be backed by it.
    rid["diamonds_r4_old"] = save(
        type="query", title="diamonds R4 — premium share (first pass)",
        description="An early cut at the premium share; superseded by the corrected R4 result.",
        content={**dict(d[4]), "premium_pct": "55.10"}, content_format="json",
        parent_row_ids=[rid["diamonds_r1"]], tags=["diamonds", "r4"],
    )
    store.mark(conn, row_id=rid["diamonds_r4_old"], session_id=sid,
               status="superseded", superseded_by=rid["diamonds_r4"])
    # A recomputation that was itself found wrong. Quoting it is the exact
    # error this arm is built to catch.
    rid["diamonds_r4_bad"] = save(
        type="query", title="diamonds R4 — premium share (recomputed)",
        description="A recomputed premium share, later contradicted against the cleaned data.",
        content={**dict(d[4]), "premium_pct": "41.20"}, content_format="json",
        parent_row_ids=[rid["diamonds_r1"]], tags=["diamonds", "r4"],
    )
    store.mark(conn, row_id=rid["diamonds_r4_bad"], session_id=sid,
               verdict="contradicted",
               basis="recomputed against the cleaned data: the premium share is 40.38%, not 41.20%")

    # --- vgsales ------------------------------------------------------
    rid["vgsales_ds"] = save(
        type="dataset", title="vgsales.csv (raw)",
        description="The raw video-game sales table.",
        content={"file": "vgsales.csv"}, content_format="json",
        source={"url": "local", "refresh_after": "static"},
    )
    v = gold_answers()["vgsales.csv"]
    rid["vgsales_r2"] = save(
        type="query", title="vgsales R2 — genre shares",
        description="Global-sales share of the top three genres.",
        content=dict(v[2]), content_format="json",
        parent_row_ids=[rid["vgsales_ds"]], tags=["vgsales", "r2"],
    )

    # --- census -------------------------------------------------------
    # A dataset whose refresh window has already elapsed, so any result built
    # from it derives 'stale' on read (D11) without anything writing that.
    rid["census_ds"] = save(
        type="dataset", title="census.csv (raw)",
        description="The raw census table, fetched from a source with a short refresh window.",
        content={"file": "census.csv"}, content_format="json",
        source={"url": "local", "refresh_after": "1h", "fetched_at": old_fetch},
    )
    c = gold_answers()["census.csv"]
    rid["census_r1"] = save(
        type="query", title="census R1 — capital net totals",
        description="Missing-value counts and total net capital across the census respondents.",
        content=dict(c[1]), content_format="json",
        parent_row_ids=[rid["census_ds"]], tags=["census", "r1"],
    )
    return rid


def claims_for(rid: dict) -> list[dict]:
    """The claims a PM agent adjudicates, with their gold verdict and rows.

    `support_row_id` is the row that backs the correct verdict — for a
    supported claim the row that states it, for a refuted one the row that
    refutes it. `forbidden_row_ids` are the rows that must never be cited as
    *backing* for a claim: they are superseded, contradicted, or stale, so an
    agent that supports a claim with one of them could not tell a good row
    from a bad one. Citing one to *refute* a claim is the healthy case.
    """
    d = gold_answers()["diamonds.csv"]
    c = gold_answers()["census.csv"]
    return [
        {
            "id": 1, "verdict": "supported",
            "claim": f"The cleaned diamonds dataset has {d[1]['n_rows']} rows.",
            "support_row_id": rid["diamonds_r1"], "forbidden_row_ids": [],
        },
        {
            "id": 2, "verdict": "supported",
            "claim": f"The mean price per carat across the cleaned diamonds is {d[1]['mean_price_per_carat']}.",
            "support_row_id": rid["diamonds_r1"], "forbidden_row_ids": [],
        },
        {
            "id": 3, "verdict": "supported",
            "claim": f"Fair-cut diamonds have a median price of {d[2]['median_price[Fair]']}.",
            "support_row_id": rid["diamonds_r2"], "forbidden_row_ids": [],
        },
        {
            "id": 4, "verdict": "supported",
            "claim": f"Premium diamonds make up {d[4]['premium_pct']}% of the cleaned data.",
            "support_row_id": rid["diamonds_r4"], "forbidden_row_ids": [],
        },
        {
            "id": 5, "verdict": "refuted",
            "claim": "Premium diamonds make up 55.10% of the cleaned data.",
            "support_row_id": rid["diamonds_r4"],
            "forbidden_row_ids": [rid["diamonds_r4_old"]],
        },
        {
            "id": 6, "verdict": "refuted",
            "claim": "Most premium diamonds are Fair cut.",
            "support_row_id": rid["diamonds_r4"], "forbidden_row_ids": [],
        },
        {
            "id": 7, "verdict": "refuted",
            "claim": "The premium share, recomputed, is 41.20% of the cleaned data.",
            "support_row_id": rid["diamonds_r4"],
            "forbidden_row_ids": [rid["diamonds_r4_bad"]],
        },
        {
            "id": 8, "verdict": "refuted",
            "claim": f"Total net capital across census respondents is {c[1]['total_capital_net']}.",
            "support_row_id": None, "forbidden_row_ids": [rid["census_r1"]],
        },
        {
            "id": 9, "verdict": "supported",
            "claim": "The top genre by global sales is Action.",
            "support_row_id": rid["vgsales_r2"], "forbidden_row_ids": [],
        },
        {
            "id": 10, "verdict": "refuted",
            "claim": "The top genre by global sales is Sports.",
            "support_row_id": rid["vgsales_r2"], "forbidden_row_ids": [],
        },
    ]


def write_files_ceiling() -> None:
    """The ceiling arm's working directory: the raw CSVs plus a manifest.

    This is the file_manifest arm's affordance handed to a PM instead of a
    producer — the strongest thing files can do for "should I believe this
    number". It can hold the data; it cannot hold the lifecycle.
    """
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)
    entries = []
    for name in ("diamonds.csv", "vgsales.csv", "census.csv"):
        src = HERE / "data" / name
        if src.exists():
            shutil.copyfile(src, WORKSPACE / name)
            entries.append(name)
    lines = ["# MANIFEST.md", "",
             "Index of the data files in this working directory. One entry per file:", ""]
    for name in entries:
        lines += [f"## {name}", f"- question: raw source table for {name.split('.')[0]} claims",
                  "- computed: fetched, not derived", "- inputs: none", ""]
    (WORKSPACE / "MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    rid = build_store()
    claims = claims_for(rid)
    (RESULTS / "claims.json").write_text(
        json.dumps({"store": str(STORE_PATH), "claims": claims}, indent=2) + "\n",
        encoding="utf-8")
    write_files_ceiling()
    print(f"built {STORE_PATH}")
    print(f"wrote {RESULTS / 'claims.json'} ({len(claims)} claims)")
    print(f"seeded {WORKSPACE}")


if __name__ == "__main__":
    main()
