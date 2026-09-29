"""Compute gold answers for the depth chains by running each chain's
reference computation in sequence, exactly as a correct agent would.

State carries between rounds on purpose: round N's reference consumes what
rounds 1..N-1 derived, so the gold answers are only reachable by following
the whole chain — which is the property the benchmark is trying to measure.

Writes results/manifest_chains.json in the same shape prepare.py emits, so
runner.mjs / score.py / report.py work unchanged.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from chains import CHAINS  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def r2(x):
    return f"{float(x):.2f}"


def r4(x):
    return f"{float(x):.4f}"


# --------------------------------------------------------------- diamonds

def diamonds_r1(d):
    clean = d[(d.x > 0) & (d.y > 0) & (d.z > 0)].copy()
    clean["price_per_carat"] = clean.price / clean.carat
    return {"clean": clean}, {
        "n_rows": str(len(clean)),
        "mean_price_per_carat": r2(clean.price_per_carat.mean()),
    }


def diamonds_r2(s):
    clean = s["clean"]
    g = clean.groupby("cut").price.agg(["median", "count"])
    return s, {f"median_price[{c}]": r2(v) for c, v in g["median"].items()} | \
              {f"count[{c}]": str(int(v)) for c, v in g["count"].items()}


CLARITY_ORDER = ["I1", "SI2", "SI1", "VS2", "VS1", "VVS2", "VVS1"]


def diamonds_r3(s):
    clean = s["clean"]
    sub = clean[clean.cut == "Ideal"]
    g = sub.groupby("clarity").agg(median_price=("price", "median"),
                                   mean_ppc=("price_per_carat", "mean"))
    return s, {f"median_price[{c}]": r2(g.loc[c, "median_price"]) for c in CLARITY_ORDER if c in g.index} | \
              {f"mean_ppc[{c}]": r2(g.loc[c, "mean_ppc"]) for c in CLARITY_ORDER if c in g.index}


def diamonds_r4(s):
    clean = s["clean"]
    thr = clean.price_per_carat.mean()
    prem = clean[clean.price_per_carat > thr]
    g = prem.groupby("cut").size().sort_values(ascending=False)
    s["premium"] = prem
    return s, {
        "premium_pct": r2(len(prem) / len(clean) * 100),
        "top_premium_cut": str(g.index[0]),
        "top_premium_count": str(int(g.iloc[0])),
    }


def diamonds_r5(s):
    sub = s["premium"]
    ideal_prem = sub[sub.cut == "Ideal"]
    s["ideal_premium"] = ideal_prem
    return s, {
        "median_carat": r2(ideal_prem.carat.median()),
        "subset_size": str(len(ideal_prem)),
        "median_ppc": r2(ideal_prem.price_per_carat.median()),
    }


def diamonds_r6(s):
    clean, prem = s["clean"], s["premium"]
    return s, {
        "corr_all": r4(clean[["carat", "price_per_carat"]].corr().iloc[0, 1]),
        "corr_premium": r4(prem[["carat", "price_per_carat"]].corr().iloc[0, 1]),
    }


# ---------------------------------------------------------------- vgsales

def vgsales_r1(v):
    v = v.copy()
    v["total_earlier"] = v.NA_Sales + v.EU_Sales + v.JP_Sales + v.Other_Sales
    v["decade"] = (v.Year // 10) * 10
    return {"v": v}, {
        "total_global": r2(v.Global_Sales.sum()),
        "missing_publisher": str(int(v.Publisher.isna().sum())),
    }


def vgsales_r2(s):
    v = s["v"]
    g = v.groupby("Genre").Global_Sales.sum().sort_values(ascending=False)
    total = v.Global_Sales.sum()
    top3 = g.head(3)
    s["genre_totals"] = g
    s["top_genre"] = str(top3.index[0])
    return s, {f"genre_share[{c}]": r2(val / total * 100) for c, val in top3.items()}


def vgsales_r3(s):
    v = s["v"]
    sub = v[v.Genre == s["top_genre"]]
    d = sub.groupby("decade").Global_Sales.sum().sort_values(ascending=False)
    s["top_decade"] = int(d.index[0])
    return s, {"top_decade": str(int(d.index[0])), "decade_sales": r2(d.iloc[0])}


def vgsales_r4(s):
    v = s["v"]
    sub = v[(v.Genre == s["top_genre"]) & (v.decade == s["top_decade"])]
    p = sub.groupby("Platform").Global_Sales.sum().sort_values(ascending=False)
    best = str(p.index[0])
    return s, {"top_platform": best, "platform_sales": r2(p.iloc[0]),
               "platform_titles": str(int(sub[sub.Platform == best].Name.nunique()))}


def vgsales_r5(s):
    v = s["v"]
    sub = v[v.Genre == s["top_genre"]]
    p = sub.groupby("Publisher").Global_Sales.sum().sort_values(ascending=False)
    return s, {"top_publisher": str(p.index[0]),
               "publisher_pct": r2(p.iloc[0] / sub.Global_Sales.sum() * 100)}


def vgsales_r6(s):
    v = s["v"]
    sub = v[v.Genre == s["top_genre"]]
    genre_total = sub.Global_Sales.sum()
    decade_total = sub[sub.decade == s["top_decade"]].Global_Sales.sum()
    pub_total = sub.groupby("Publisher").Global_Sales.sum().max()
    ds, ps = decade_total / genre_total * 100, pub_total / genre_total * 100
    return s, {"decade_share": r2(ds), "publisher_share": r2(ps),
               "larger": "decade" if ds > ps else "publisher"}


# ----------------------------------------------------------------- census

def census_r1(c):
    c = c.copy()
    c["capital_net"] = c["capital-gain"] - c["capital-loos"]
    c["is_long_hours"] = c["hour-per-week"] > 40
    return {"c": c}, {
        "missing_workclass": str(int((c.workclass == "?").sum())),
        "missing_occupation": str(int((c.occupation == "?").sum())),
        "total_capital_net": r2(c.capital_net.sum()),
    }


def census_r2(s):
    c = s["c"]
    pos = c[c.capital_net > 0]
    return s, {"n_capital_positive": str(len(pos)),
               "mean_education": r4(pos["education-num"].mean())}


def census_r3(s):
    c = s["c"]
    pos = c[c.capital_net > 0]
    g = pos.groupby("sex").agg(hours=("hour-per-week", "mean"),
                               edu=("education-num", "mean"))
    return s, {f"mean_hours[{k}]": r2(v) for k, v in g["hours"].items()} | \
              {f"mean_education[{k}]": r2(v) for k, v in g["edu"].items()}


def census_r4(s):
    c = s["c"]
    longh = c[c.is_long_hours]
    m = longh.groupby("marital-status").size().sort_values(ascending=False)
    return s, {
        "long_hours_pct": r2(len(longh) / len(c) * 100),
        "top_marital": str(m.index[0]),
        "top_marital_count": str(int(m.iloc[0])),
    }


def census_r5(s):
    c = s["c"]
    sub = c[c.is_long_hours & (c.capital_net > 0)]
    return s, {"n_remaining": str(len(sub)),
               "mean_capital_net": r2(sub.capital_net.mean())}


def census_r6(s):
    c = s["c"]
    longh = c[c.is_long_hours]
    return s, {
        "corr_all": r4(c[["hour-per-week", "education-num"]].corr().iloc[0, 1]),
        "corr_long_hours": r4(longh[["hour-per-week", "education-num"]].corr().iloc[0, 1]),
    }


REFERENCE = {name: fn for name, fn in list(globals().items())
             if name.endswith(("_r1", "_r2", "_r3", "_r4", "_r5", "_r6"))
             and callable(fn)}

LOADERS = {
    "diamonds.csv": lambda: pd.read_csv(HERE / "data" / "diamonds.csv"),
    "vgsales.csv": lambda: pd.read_csv(HERE / "data" / "vgsales.csv"),
    # Adult Census encodes missing values as the string '?' and pads several
    # categorical columns with leading spaces (" Female", " ?"). Both are
    # stripped here so gold tags don't inherit the padding — the questions
    # say to trim. Stripped by value rather than by dtype: these columns are
    # not reliably `object` across pandas versions/backends.
    "census.csv": lambda: pd.read_csv(HERE / "data" / "census.csv").apply(
        lambda col: col.map(lambda v: v.strip() if isinstance(v, str) else v)),
}


def main():
    tables = []
    for chain in CHAINS:
        name = chain["table"]
        print(f"\n=== {name}: {chain['title']}")
        state = LOADERS[name]()
        rounds = []
        for rnd in chain["rounds"]:
            state, answers = REFERENCE[rnd["reference"]](state)
            rounds.append({
                "round": rnd["round"], "question_id": f"{name}#{rnd['round']}",
                "question": rnd["question"], "constraints": "",
                "format": rnd["format"], "level": rnd["stage"],
                "concepts": [], "answers": answers,
                "depends_on": rnd["depends_on"], "stage": rnd["stage"],
            })
            print(f"  R{rnd['round']} {rnd['stage']:11s} depends_on={rnd['depends_on']} "
                  f"-> {len(answers)} gold tags")
            for k, v in answers.items():
                print(f"        {k} = {v}")
        tables.append({
            "file_name": name, "data_path": f"data/{name}",
            "title": chain["title"], "size_bytes": (HERE / "data" / name).stat().st_size,
            "rounds": rounds,
        })

    manifest = {
        "source": {
            "benchmark": "dsos depth chains (hand-authored over InfiAgent-DABench tables)",
            "url": "https://huggingface.co/datasets/infiagent/DABench",
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "method": "chains.py + build_chains.py; gold answers computed with pandas",
        },
        "design": "depth chains: each round consumes what earlier rounds derived",
        "rounds_per_table": len(tables[0]["rounds"]) if tables else 0,
        "tables": tables,
    }
    out = HERE / "results" / "manifest_chains.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(t["rounds"]) for t in tables)
    print(f"\nWrote {out} — {len(tables)} chains x {len(tables[0]['rounds'])} rounds "
          f"= {total} agent runs per arm")


if __name__ == "__main__":
    main()
