# DS Artifact OS — Design Doc

Sep 25, 2026 · @Shawn Shi

This doc is in three parts. **Part 1 is the hackathon MVP.** **Part 2 is the backlog** — built only if Part 1 is solid. **Part 3 is release/GUI/publish** — Part 1's core (layers 1+2: artifact store, execution runner, MCP server) is built, passing two independent smoke-test suites, and live-tested against a real agent session (Pi); ahead of schedule, so this scopes the next slice deliberately rather than improvising it.

## Problem

Data scientists spend more time re-creating and re-finding past work than doing new analysis. Ad hoc queries, transforms, charts, and models pile up across notebooks and scratch files with no shared structure, so nothing gets reused across questions.

Two forces make this worse right now:

- Agents can produce artifacts far faster than humans, which multiplies the clutter problem instead of solving it — more outputs, still no place to put them
- Notebook- and dashboard-centric tools treat the container (the notebook, the dashboard) as the unit of value, so artifacts live and die inside one context instead of being addressable and reusable across contexts

The actual lever: make the artifact — not the notebook or the dashboard — the first-class, versioned, addressable unit. A report becomes a composition over artifacts rather than a fresh build each time.

## Philosophy

1. **One uniform envelope.** Every artifact type shares the same schema (id, version, lineage, metadata). A new type costs nothing structural to add.
2. **Composition over bespoke containers.** A report is a narrative artifact that embeds references to other artifacts. One rendering path.
3. **MCP-first.** The product *is* an MCP server. Any MCP client (Claude Code, Claude Desktop) is the agent — we don't build one. A GUI, if any, is a read-only view over the same store.
4. **Transparent by default.** Every execution and every tool call is recorded automatically. Nothing depends on the agent remembering to log.
5. **Working layer, not governance layer.** This lives *before* a catalog/warehouse-governance layer (dbt, Atlan, Unity Catalog) — it's where a DS's own queries, transforms, and drafts get organized before anything is fit for anyone else to see. No multi-user permissions, approval workflows, or org-wide catalog. `publish_report` is the seam: a finished HTML artifact is what crosses out of this layer into whatever governs it downstream.

---

# Part 1 — Hackathon MVP

## Demo

Live, MCP-driven, starting from an empty store. Dataset domain: Devpost hackathon projects — **discovered by the agent live**, not pre-loaded.

1. **Round 1 — a vague question** (e.g. *"What makes a hackathon project win?"*). The agent searches for public data, inspects it, fetches it, explores, and publishes a short report. Every step becomes an artifact.
2. **Round 2 — a follow-up question** that goes deeper. The agent calls `search_artifacts` first, and visibly reuses round-1 artifacts instead of re-fetching and re-cleaning.
3. **Payoff:** show the lineage (round 2 hanging off round 1) and the numbers — artifacts reused, tool calls saved.

Rules for the build:

- The discovery skill is dataset-agnostic. Sanity-check it against a *different* dataset before the demo, so the Devpost discovery on stage stays genuine, not memorized.
- Pre-run a backup store and record a backup video.

## Features

**Must-have**

- **Data discovery: no bespoke tool.** A seeded `skill` artifact (found via `search_artifacts` like anything else) tells the agent to use its own web tools to find and inspect public data, then register what it fetches via `run_python` + `save_artifact(type=dataset, source=..., ...)`. Provenance capture lives in the skill's instructions, not in code.
- **Execution:** run SQL and Python against dataset artifacts; each run is recorded (code, inputs, timing, status, output summary) and its output saved as a new artifact with lineage
- **Artifact registry:** save, get, search, lineage — versioned
- **Tool-call log:** the MCP server logs every tool call it receives, per session — the agent trace comes for free, no custom agent
- **Narrative + publish:** markdown with artifact embeds, published as one self-contained HTML file

**Stretch — only after the demo runs end-to-end**

- **GUI (read-only):** live page (tool-call feed + artifact cards appearing, "reused" badge in round 2) and a report page
- Lineage graph view

## MCP tools

| Group | Tools |
| --- | --- |
| Session | `start_session(question)` — marks the reuse boundary; see Sessions below |
| Execution | `run_sql`, `run_python` |
| Artifacts | `save_artifact`, `get_artifact`, `search_artifacts`, `get_lineage` |
| Publish | `publish_report` |

~8 tools. No discovery tools — the agent uses its own web/bash tools, guided by a seeded skill artifact (see Data model). Execution tools save outputs and record executions themselves, so there is no `record_execution` tool the agent could forget to call.

### Sessions

A session exists for one reason: to give the reuse demo a boundary that isn't a guess. `tool_calls.artifact_row_ids` (below) only proves reuse if you can tell "an artifact from *this* question" from "an artifact from a *previous* question" — and neither timing gaps nor MCP connection boundaries give a clean split during a live demo. So each round starts with an explicit `start_session(question)` call, and reuse is just: does this tool call touch a row whose `session_id` isn't the current session? One tool, one row per round, `started_at` only — no `ended_at`, nothing else depends on it.

## Architecture

- **Core library** (plain Python, no protocol awareness): artifact store, execution runner. SQLite for metadata, filesystem for blobs, DuckDB as the query engine.
- **MCP server** (FastMCP): thin wrapper over the core, plus middleware that logs every tool call.
- **GUI (stretch):** FastAPI + Jinja, read-only over the same SQLite.

## Data model

Search is keyword-first, semantic-fallback: `search_artifacts` matches `query` against title/description/tags via SQLite FTS5 first (an exact term reliably wins), then fills any remaining slots via embedding cosine similarity (brute-force in Python — no vector DB needed at demo scale) for queries with no literal term overlap. `save_artifact` embeds `title + description + tags` and stores the vector alongside the row for that fallback path. Either way, the agent's description quality directly drives reuse quality, so `save_artifact`'s docstring requires a real 1–2 sentence description, not a filename.

**`sessions`**
```
id, question, started_at
```

**`artifacts`** — one row per *version*, not per logical artifact. `(artifact_id, version)` is unique internally, but the MCP surface only ever deals in `row_id` — every tool that returns or accepts an artifact reference uses it, so there's one id to track, not two. `type` ∈ {dataset, query, transform, chart, narrative, **skill**} — skills are just artifacts (`content_format="markdown"`), no separate table or tools. One skill row (dataset-discovery instructions) is seeded at store-init, before the demo starts.
```
row_id (pk), artifact_id, version, type, title, description, tags,
content_ref, content_format, source,     -- source: provenance for fetched datasets (url, fetched_at)
embedding,                                -- vector, for semantic search
created_at, session_id, status
```

**`lineage`** — edges between specific `row_id`s, not logical artifacts, so a `{{artifact:<row_id>}}` embed always resolves to a fixed row (row_id already pins a version — no `@version` suffix needed). A narrative's `{{artifact:...}}` embeds are auto-added to its lineage on save, and surfaced back as `uses` when the narrative is fetched — see `save_artifact`'s docstring.
```
child_row_id, parent_row_id
```

**`executions`** — inputs are just the parent rows in `lineage` for the output row; no separate input field.
```
id, output_row_id,     -- the artifact row this execution produced
kind,                    -- sql | python
code, started_at, ended_at, status,
stdout, stderr, error, output_summary   -- JSON: row count, shape, etc.
```

**`tool_calls`** — the free agent trace; `artifact_row_ids` drives the reuse badge and metric directly (any row touched whose `session_id` differs from the current session = a genuine reuse, no manual bookkeeping).
```
id, session_id, ts, tool_name, args_json, result_summary, artifact_row_ids
```

---

# Part 2 — Backlog (not now)

Only if Part 1 is solid.

- Custom agent loop (direct tool-calling against the model API) for a controlled, scripted demo
- Model artifacts beyond metrics (fitted models, versioned)
- Staleness flagging + `heal_artifact` (re-run against updated parents)
- Session context store (`get_session_context`, `update_session_context`)
- Multiple/curated skills (beyond the one seeded discovery skill), skill editing based on what worked
- Markdown publish with sibling image files
- Interactive GUI (htmx, drag-to-reorder, live re-render)
- Content-hash deduplication
- Cross-session / cross-user search

## Competitive landscape

No direct competitor treats the artifact itself, rather than the notebook or dashboard, as the first-class unit — but every adjacent category has real, well-funded players worth knowing before pitching this as novel.

| Category | Players | What they do | Gap vs. this project |
| --- | --- | --- | --- |
| Agentic notebooks | Hex Notebook Agent, Zerve, marimo + marimo pair | Agent works inside a notebook/canvas; marimo pair adds structured working memory for agents, shipped as an agent skill | Notebook/canvas is still the primitive; artifacts aren't independently addressable outside it |
| Execution sandboxes | E2B, Daytona, Modal, Cloudflare Sandbox, Vercel Sandbox | Isolated code execution for agents, with state persistence and framework integrations | Solved infrastructure problem — a dependency to plug into, not a competitor |
| Semantic/metric layers | dbt Semantic Layer (MetricFlow, Apache 2.0, OSI-aligned), Cube | Governed metric definitions served across BI tools and warehouses | Solves the query artifact's serving problem only, not artifact management broadly |
| Context/skill tooling | Anthropic Agent Skills (SKILL.md), MCP, Packmind | Standardizing how agents discover and reuse instructions/context across tools | Emerging standard this project should adopt, not compete with |
| Experiment/artifact tracking | MLflow, Weights & Biases, DVC | Mature ML experiment and dataset versioning | Built for training runs, not lightweight, auto-narrated versioning of everyday analysis artifacts |

All five rows above operate one layer up from this project: they govern, serve, or track things *after* someone has decided they're worth governing. This project is the working layer underneath — individual, no-permissions, disposable-until-published — that feeds into any of them. `publish_report` is the only export seam; nothing about this project competes with what a catalog or governance tool does above that seam.

---

# Part 3 — Release, GUI, Publish

Three independent-ish workstreams, scoped concretely enough to hand to an agent directly. Each answers a fork that was deliberately left open rather than guessed at:

| Fork | Decision |
| --- | --- |
| Release mechanism | Git tag + GitHub Release (not PyPI — no external account to set up; not a bare `uvx`-from-git ref — a Release gives a citable, documented checkpoint) |
| GUI scope | Read-only (browse/search/lineage) — no delete/retag/edit; those need core-library functions that don't exist yet |
| Publish target | Local self-contained HTML only — no external service (Notion/Google Docs were considered; out of scope for now) |

## Release

**Real gap, not just process:** `pyproject.toml` has no `[build-system]` table. `pip install -e .` only works today via pip's legacy fallback — undocumented behavior that likely breaks under a real build (`pip install git+...`, `uv`/`uvx`). Fix first, regardless of tag:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

Verify by installing into a **throwaway** venv, not the dev one — editable installs hide "missing file in the sdist" bugs that only surface on a real build.

Steps:
1. Fix `[build-system]`, verify with a clean-venv install.
2. Tag `v0.1.0`. This freezes the current MCP tool contract (`start_session`, `search_artifacts`, `get_artifact`, `save_artifact`, `run_sql`, `run_python`, `get_lineage`) as the first versioned checkpoint — worth a short changelog in the Release notes, since these shapes changed several times getting here (row_id unification, inline results, keyword search, auto-lineage).
3. GitHub Release with those notes.
4. README: add a "pin a version" install line — `pip install "git+https://github.com/zixiaoshawnshi/DataScientist-OS.git@v0.1.0"`.

## GUI (read-only)

Same three-layer architecture already committed: a separate process, reading `dsos.store` directly off the same SQLite file — not through the MCP server. FastAPI + Jinja2 + htmx, no new frontend framework.

**Prerequisite refactor:** `_artifact_payload` currently lives inside `dsos/mcp_server.py`. Move it into the core library (e.g. a new `dsos/present.py`) so the GUI and the MCP server call the same function instead of two views that can silently drift apart.

**Concurrency risk, fix now not on demo day:** the GUI reads the same SQLite file the MCP server is actively writing to during a live agent run. Default SQLite journal mode can throw `database is locked` under exactly that read/write overlap. Fix: `PRAGMA journal_mode=WAL` in `dsos/db.py`'s `connect()`, so readers and a writer coexist without blocking.

Routes:

| Route | Shows |
| --- | --- |
| `GET /` | Sessions list — question, artifact count, reuse count (`reused_artifact_row_ids`) |
| `GET /sessions/{id}` | That session's tool-call feed (htmx-polled partial — no websockets needed) + artifacts it created |
| `GET /artifacts?type=&session_id=&q=` | Gallery; `q` calls `search_artifacts` directly, so gallery search and agent search share one index |
| `GET /artifacts/{row_id}` | Detail: metadata, content preview, `uses`/used-by (both lineage directions), execution trace, other versions of the same logical artifact |
| `GET /artifacts/{row_id}/lineage` | Small neighborhood graph, rendered via Mermaid.js off server-generated graph text — no new Python dependency |

Explicitly not in scope here: the fancier interactive GUI (drag-to-reorder, live re-render) stays in Part 2's backlog. This is browse-and-understand, not edit.

## Publish (local HTML only)

New MCP tool: `publish_report(row_id, session_id) -> {"path": ..., "artifact_row_ids": [...]}`.

Reuses the `{{artifact:row_id}}` regex already built for auto-lineage (`dsos/store.py`'s `_EMBED_RE`) — but now to *substitute*, not just detect:

| Referenced artifact's type | Rendered as |
| --- | --- |
| dataset / query / transform | an HTML table (`pandas.DataFrame.to_html()`) |
| chart | `<img>` with the PNG bytes inlined as a base64 data URI — required for the file to stay self-contained |
| narrative markdown body | converted to HTML via a small markdown library (`markdown`, the more common of the two candidates over `mistune`) — the one new dependency this adds |

Output is a single `.html` file with everything inlined (images included) — no sibling image files, per the Part 1 scope cut. It returns a file path and does **not** create its own artifact row: a rendered export isn't itself a versioned analysis artifact, it's an exit from this layer (see Philosophy #5). Easy to revisit if published reports should themselves be searchable/reusable later.
