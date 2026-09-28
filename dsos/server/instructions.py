"""The instruction strings each MCP profile is introduced with.

Surfaced to the client at connect time via the MCP `initialize` response
(protocol-level InitializeResult.instructions, not a tool docstring) — this
is what should make the agent reach for this server unprompted, in any
repo, with no CLAUDE.md/AGENTS.md to copy in. Keep it short: it's paid on
every connection. Whether a given client actually injects it into the
model's context (vs. just displaying it) is client-dependent — verify
with a live session before trusting it for a demo.

Both strings live here, apart from the tools they describe, so that the
producer's text can be read (and diffed) without reading nine tool
docstrings, and so the consumer profile has somewhere to put its own.
"""

from __future__ import annotations

from dsos import update_check

# _UPDATE_NOTICE (if any) goes first, so it isn't lost if a client truncates
# a long instructions string — only matters to someone on an installed
# release (pip install git+...@vX.Y.Z); silently absent for a dev checkout.
_UPDATE_NOTICE = update_check.check_for_update()

PRODUCER_INSTRUCTIONS = (f"{_UPDATE_NOTICE}\n\n" if _UPDATE_NOTICE else "") + """\
Use this server for any question that needs real data: trends, comparisons, \
correlations, "what predicts X", "how many/which", etc. Do not answer such \
questions from general or prior knowledge — every answer must be backed by \
a number this server actually computed.

Workflow, in order:
1. start_session(question) — first call, for every new question.
2. search_artifacts — check for reusable prior work before fetching anything new.
3. If nothing reusable: find and download real public data with your own \
tools, then save_artifact to register it (type="dataset", with source). An \
unregistered dataset is invisible to search, lineage, and every future question.
4. run_sql / run_python against the registered artifacts to compute the \
actual answer — never eyeball or summarize the raw data yourself. Your \
inputs are bound positionally: the first row_id in input_row_ids is \
`in_1`, the second `in_2`, and so on (in Python, `inputs["<row_id>"]` \
reaches one by id). Each response echoes that mapping in `input_tables`. \
Their result is returned inline in the same response (a preview, row_count, \
columns) — do not call get_artifact right after just to see what you \
produced; it's already there. Pass scratch=True for a quick check (a row \
count, a schema poke) that should cost nothing: the result comes back \
inline, and no artifact, lineage or searchable row is written. Charts: \
output_type="chart" — styled with the dark house style by default, or \
pick another with style= (list_templates for the built-ins and this store's \
custom ones).
5. Answer using the computed output, citing the row_ids you used. If a \
check you ran in scratch mode turns out to be worth keeping, re-run it \
with scratch=False.
6. Once you know how this workstream should look, save it once: \
save_template(kind="chart-style" | "report") with the content, or base= an \
existing template to edit a copy of it. list_templates lists the built-ins \
plus whatever this store has customized, and a custom template is \
referenced by the row_id/artifact_id it returns — for charts that is \
run_python's style=, for reports it is the layout the GUI's report page \
renders for a narrative.

Every tool that returns an artifact — save_artifact, run_sql, run_python, \
search_artifacts — gives you a row_id. That row_id is the only id you need \
for get_artifact/run_sql/run_python's input_row_ids; there is no separate \
"artifact_id" to track.
"""

# The consumer profile is the other direction through the same store: it
# never computes anything, it only asks what has already been computed and
# cites what it is told. It is built here with the right shape and no tools
# at all, because the four evidence tools are WP-F1's — and F1 owns this
# string too, so what is here says what is true today rather than
# describing tools an agent would then fail to call.
CONSUMER_INSTRUCTIONS = (f"{_UPDATE_NOTICE}\n\n" if _UPDATE_NOTICE else "") + """\
This server is a read-only window onto a DS Artifact OS store: a working \
layer where earlier analysis sessions registered the datasets, queries, \
charts and write-ups they computed, each with the question it answered and \
the rows it was built from. It never fetches, computes or saves anything.

It currently exposes no tools: the evidence tools that read this store \
(find_evidence, get_claim, cite, ask) arrive with WP-F1, and until they do \
there is nothing here to call. Use the producer server over the same store \
if you need to compute something.
"""
