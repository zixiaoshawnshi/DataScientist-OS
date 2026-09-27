"""Seeds the skill library every fresh store needs: common analysis
workflow skills as `skill` artifacts, found and customized like anything
else in the store (design doc, "Data discovery: no bespoke tool").

Deliberate scope (this round): skills teach WORKFLOW — how to drive the
tools so work stays addressable, reusable, and consistently styled.
Prescriptive methodology (which test when, which chart for which data)
comes later; the aim right now is making artifact management and the DS
workflow easier, not encoding taste.

Skills are plain markdown instructions, not code. Seeding is idempotent BY
artifact_id: a skill that already exists — including one the agent has
re-saved with save_skill — is never re-seeded, so customization survives
server restarts and store re-inits.
"""

from __future__ import annotations

import sqlite3

from dsos.store import get_artifact, save_artifact

DISCOVERY_SKILL_ID = "skill-dataset-discovery"

DISCOVERY_SKILL_MD = """\
# Start here: how to find and register data

You have no dataset tools. Use your own web/bash tools (search, fetch, curl)
to find public data relevant to the question, the same way you would for
any other research task.

Before committing to a dataset: check its size and inspect its schema/columns
— don't fetch something huge you'll only use a slice of.

After fetching to a local file (csv/tsv/json/parquet), you MUST register it:

    save_artifact(
        type="dataset",
        title="...",
        description="1-2 sentences: what this contains and why you fetched it",
        content_path="<local path to the file you fetched>",
        content_format="parquet",   # datasets are normalized to parquet on ingest
        source={
            "url": "<where it came from>",
            "fetched_at": "<timestamp you fetched it>",
            "method": "<how you fetched it, e.g. 'WebFetch', 'curl', 'Kaggle API'>",
            "refresh_after": "<how long this stays fresh, e.g. '7d', '30d', or "
                              "'static' for data that won't change — your call>",
        },
        session_id=<current session>,
    )

No dataset is real to the rest of this system until it's registered this
way — an artifact with no save_artifact call is invisible to search,
lineage, and every future question that could have reused it.

This is also the only record of *how* the data was retrieved — dsos can't
see a fetch you ran with your own tools, only what you register here.
`fetched_at` + `refresh_after` are provenance, not an automatic staleness
check: nothing here computes "stale" for you. Before reusing an existing
dataset artifact for a new question, look at both and decide for yourself
whether it's still good enough or worth refetching.

Once registered, use run_sql / run_python against the artifact's row_id
for every query, transform, or chart you build from it, so lineage stays
connected.

How the inputs reach your code: the first row_id in input_row_ids is
`in_1`, the second is `in_2`, and so on — in SQL as a table, in Python
as a variable (plus `inputs["<row_id>"]` to reach one by id). Every
response echoes the mapping in `input_tables`. Never name a table or
variable after a title yourself: titles are for humans and change, these
names don't.
"""

_EDA_MD = """\
# Skill: exploratory analysis workflow

Purpose: get from "raw dataset artifact" to "understood, cleaned, reusable
artifacts" — leaving every step addressable for the next question.

1. **Peek with scratch runs.** run_sql/run_python with scratch=True for row
   counts, SELECT * FROM in_1 LIMIT 10, value counts on the columns that
   matter. Scratch peeks never become artifacts — use them freely,
   register nothing.
2. **Compute summaries, don't eyeball previews.** A 10-row preview is not
   evidence. Make the summary the `result` (shape, dtypes, missingness per
   column, duplicates) so it's a number you can cite, not a print you
   squinted at.
3. **Persist the cleaning once.** The cleaned/filtered/typed frame is a
   real run_python transform artifact, lineage-linked to the raw dataset,
   described well enough that the next question reuses it instead of
   re-cleaning.
4. **Persist the useful views.** Any filtered/aggregated slice that answers
   something becomes a query artifact — those are what search finds next
   session.
5. **Describe for the next session.** Titles and descriptions are the
   index: write them as if the reader is an agent that has never seen this
   data.

A scratch result that turned out to matter can be promoted with
promote_scratch — don't re-run it by hand. Charts follow the charting
skill; writeups follow the reporting skill.
"""

_CHARTING_MD = """\
# Skill: charting workflow

How to produce, style, and reference charts.

- run_python with output_type="chart", and assign `result` the Figure or
  Axes (`plt.gcf()`, or what `plt.subplots()` returns) — it's rendered to
  PNG and saved as a chart artifact, lineage-linked to its inputs.
- **style=** picks the chart template, applied before your code runs:
  the default "dsos" is the dark house style; "report" is the same look
  sized for embedding in published reports; "minimal" is a bare light
  style. Custom templates are referenced by row_id/artifact_id — see what
  exists with list_templates(kind="chart-style") and make your own with
  save_template(kind="chart-style"). Pass style=None for raw matplotlib
  defaults.
- Label axes with units and title charts with the point they make — a
  chart the reader has to decode is a failed chart.
- A chart you'll reference in a writeup must be a persisted (non-scratch)
  run, so it has a row_id to embed as {{artifact:<row_id>}} in a narrative.
- Iterate on styling in scratch mode if you like, but publish the final
  version non-scratch — scratch charts are invisible to search and can't
  be embedded.
"""

_STATS_MD = """\
# Skill: statistical analysis workflow

How to run statistics so every number is an addressable artifact.

1. **State the comparison before computing** — which groups or values,
   answering what question. It goes in the artifact title so a later
   session knows what the numbers are about without re-deriving it.
2. **Compute with run_python.** Your code runs in your own local Python —
   whatever's already installed there (scipy, statsmodels, ...) is
   available; use requirements=[...] for anything missing. The result is
   returned inline. Never assert a statistic from memory or from a
   preview — if it isn't computed against a row_id, it isn't a number.
3. **Persist the test output as a table artifact**: statistic, p-value,
   effect size, n per group — with the test named in the title, so a later
   session can tell which test produced which numbers.
4. **Report uncertainty with any estimate** (confidence interval, sample
   size). A point estimate with no n is unverifiable.
5. **Record caveats in the artifact description** (assumptions, sample
   limits, what this does NOT answer) — the next session inherits them
   through search instead of rediscovering them.
"""

_MODELING_MD = """\
# Skill: modeling workflow

How to keep model work reproducible as artifacts.

1. **Every trained model + its metrics is an artifact.** One run_python
   that fits and returns a metrics table (model, parameters, score) per
   model — or one comparison table across models.
2. **Keep the model-building code as a real artifact**, not scratch: the
   next question rebuilds the model from its row_id (get_artifact shows
   the code; run_python against it rebuilds) instead of from memory.
3. **Let metrics accumulate across sessions.** Model scores saved as
   artifacts mean "which models were tried" is answered by
   search_artifacts, not by recall.
4. **Build from the cleaned transform, not raw data**, so get_lineage
   shows exactly what fed the model — the raw dataset, the cleaning, the
   model, in one chain.
"""

_REPORTING_MD = """\
# Skill: reporting workflow

How to write and publish a report whose exhibits are the artifacts
themselves.

1. **Answer first.** The first sentence carries the number and its
   direction; process comes later.
2. **Evidence is embedded, not pasted.** Every claim's supporting artifact
   appears as {{artifact:<row_id>}} — datasets/queries/transforms render
   as tables, charts as images. The report renders the artifacts
   themselves, so numbers can't drift from data.
3. **One line of method, then caveats** (sample size, assumptions, what
   this does not answer). Caveats are part of the report, not a weakness
   in it.
4. **save_artifact(type="narrative") first, then publish_report.**
   `template=` picks the layout: "report" is the dark house layout (the
   default), "default" the plain original look, "minimal" bare HTML — or
   a custom report template's row_id/artifact_id (list_templates(kind=
   "report"); make your own with save_template(kind="report")).
   Use dry_run=true first to catch broken embeds before spending a render.
5. Charts destined for the report should use style="report" so type sizes
   and resolution match the report page (see the charting skill).
"""

# (artifact_id, title, description, tags, markdown) — one per workflow
# skill. The stable artifact_id is what makes both seeding idempotent and
# save_skill's version-editing possible.
SKILLS: list[tuple[str, str, str, list[str], str]] = [
    (
        DISCOVERY_SKILL_ID,
        "Start here: how to find and register data",
        (
            "Instructions for finding public datasets and registering them as "
            "dataset artifacts with provenance. Read this first for any "
            "question that needs new data you don't already have."
        ),
        ["skill", "discovery", "bootstrap", "start-here"],
        DISCOVERY_SKILL_MD,
    ),
    (
        "skill-exploratory-analysis",
        "How to explore a dataset (EDA workflow)",
        (
            "Workflow for exploratory analysis: scratch peeks, computed "
            "summaries, a persisted cleaning transform, and reusable views — "
            "so the next question starts from your work instead of raw data."
        ),
        ["skill", "eda", "workflow", "exploration"],
        _EDA_MD,
    ),
    (
        "skill-charting",
        "How to make charts",
        (
            "Workflow for charting: output_type=chart with style= templates "
            "for consistent styling, labels and units, when a chart must be "
            "published vs scratch."
        ),
        ["skill", "charts", "workflow", "visualization", "style"],
        _CHARTING_MD,
    ),
    (
        "skill-statistical-analysis",
        "How to run statistics",
        (
            "Workflow for statistical analysis: state the comparison, "
            "compute with run_python, persist test outputs as table "
            "artifacts with uncertainty and caveats."
        ),
        ["skill", "statistics", "workflow", "testing"],
        _STATS_MD,
    ),
    (
        "skill-modeling",
        "How to build models",
        (
            "Workflow for modeling: models and metrics as artifacts, "
            "model-building code kept real and reusable, lineage from raw "
            "data through cleaning to model."
        ),
        ["skill", "modeling", "workflow", "ml", "evaluation"],
        _MODELING_MD,
    ),
    (
        "skill-reporting",
        "How to write and publish a report",
        (
            "Workflow for reporting: answer first, embed evidence as "
            "artifact references, include caveats, publish with a report "
            "template for consistent styling."
        ),
        ["skill", "reporting", "workflow", "narrative", "publish"],
        _REPORTING_MD,
    ),
]

SKILL_IDS = [s[0] for s in SKILLS]


def seed_library(conn: sqlite3.Connection, *, session_id: str) -> list[str]:
    """Idempotent: seeds every skill that doesn't exist yet (by
    artifact_id) and returns the row_ids actually seeded. Existing skills —
    including ones the agent has since edited via save_skill — are left
    untouched, so customization survives restarts and re-inits."""
    seeded: list[str] = []
    for artifact_id, title, description, tags, md in SKILLS:
        if get_artifact(conn, artifact_id, load_content=False) is not None:
            continue
        seeded.append(
            save_artifact(
                conn, artifact_id=artifact_id, type="skill", title=title,
                description=description, content=md, content_format="markdown",
                tags=tags, session_id=session_id,
            )
        )
    return seeded


def seed(conn: sqlite3.Connection, *, session_id: str) -> str:
    """Back-compat single-skill entry point (tests/smoke_test.py): seeds the
    discovery skill (idempotently, with the rest of the library) and
    returns its row_id. New code calls seed_library."""
    existing = get_artifact(conn, DISCOVERY_SKILL_ID, load_content=False)
    if existing:
        return existing.row_id
    seed_library(conn, session_id=session_id)
    return get_artifact(conn, DISCOVERY_SKILL_ID, load_content=False).row_id
