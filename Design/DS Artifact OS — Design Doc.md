# DS Artifact OS — Design Doc

Sep 25, 2026 · @Shawn Shi

This doc is split in two. **Part 1 is the hackathon MVP** — the only thing being built now. **Part 2 is the backlog**, built only if Part 1 is solid.

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

Search is metadata-driven but not keyword-only: `save_artifact` embeds `title + description + tags` and stores the vector alongside the row. `search_artifacts` embeds the query and ranks by cosine similarity (brute-force in Python — no vector DB needed at demo scale), with SQLite FTS5 as a keyword/tag fallback. This means the agent's description quality directly drives reuse quality, so `save_artifact`'s docstring requires a real 1–2 sentence description, not a filename.

**`sessions`**
```
id, question, started_at
```

**`artifacts`** — one row per *version*, not per logical artifact. `(artifact_id, version)` is unique; `get_artifact(id)` returns latest, `get_artifact(id, version=N)` pins one. `type` ∈ {dataset, query, transform, chart, narrative, **skill**} — skills are just artifacts (`content_format="markdown"`), no separate table or tools. One skill row (dataset-discovery instructions) is seeded at store-init, before the demo starts.
```
row_id (pk), artifact_id, version, type, title, description, tags,
content_ref, content_format, source,     -- source: provenance for fetched datasets (url, fetched_at)
embedding,                                -- vector, for semantic search
created_at, session_id, status
```

**`lineage`** — edges between specific versions, not logical artifacts, so `{{artifact:id@v2}}` embeds and staleness checks always resolve to a fixed row.
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
