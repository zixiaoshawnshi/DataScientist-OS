"""Seeds the one bootstrap artifact every fresh store needs: the discovery
skill. This is what replaces bespoke search_datasets/inspect_dataset/
fetch_dataset tools — see design doc, "Data discovery: no bespoke tool."
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
        source={"url": "<where it came from>", "fetched_at": "<timestamp>"},
        session_id=<current session>,
    )

No dataset is real to the rest of this system until it's registered this
way — an artifact with no save_artifact call is invisible to search,
lineage, and every future question that could have reused it.

Once registered, use run_sql / run_python against the artifact's row_id
for every query, transform, or chart you build from it, so lineage stays
connected.
"""


def seed(conn: sqlite3.Connection, *, session_id: str) -> str:
    """Idempotent: returns the existing skill's row_id if already seeded."""
    existing = get_artifact(conn, DISCOVERY_SKILL_ID, load_content=False)
    if existing:
        return existing.row_id
    return save_artifact(
        conn,
        artifact_id=DISCOVERY_SKILL_ID,
        type="skill",
        title="Start here: how to find and register data",
        description=(
            "Instructions for finding public datasets and registering them as "
            "dataset artifacts with provenance. Read this first for any "
            "question that needs data you don't already have."
        ),
        content=DISCOVERY_SKILL_MD,
        content_format="markdown",
        tags=["skill", "discovery", "bootstrap", "start-here"],
        session_id=session_id,
    )
