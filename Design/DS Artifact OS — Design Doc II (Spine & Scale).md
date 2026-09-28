# DS Artifact OS — Design Doc II: Spine & Scale

Sep 27, 2026 · @Shawn Shi

**Relationship to Doc I.** [DS Artifact OS — Design Doc](./DS%20Artifact%20OS%20%E2%80%94%20Design%20Doc.md) built the working layer: the store, execution, MCP surface, skills/templates, GUI, publish. That code exists and passes 8 smoke suites. This doc does not replace it — it argues that the next slice is not more features but a **different organising axis**, and that getting there is a refactor, not an increment. Where this doc changes the contract, the change is called out explicitly.

Two things landed since Doc I and are folded in here as done, not proposed: **content-hash dedupe** (Doc I Part 2 backlog item) and the **prior-work signal** in `start_session`. Both were driven by measurements rather than taste, which is the point of the next section.

**Revision (Sep 27, after review of the production pilot).** The review asked whether a structured file system plus a manifest would do the job with less machinery. For one analysis agent on one machine, it probably would. It stops being enough once the store has to serve agents that are *not* the analysis agent. That is where this is headed: a PM agent backing a claim, remote and local agents working together, and parallel agents discovering together. So this revision keeps the service but moves it to a **single daemon with two tool profiles** (§Architecture). It also cuts producer-side surface that the pilot showed to be a source of bugs, not value (§MVP scope). The staging in §Refactor plan is rewritten to match.

---

## Why now: what the benchmark actually found

A benchmark was built to test Doc I's founding claim — *data scientists spend more time re-creating and re-finding past work than doing new analysis* — by running the same agent under two conditions: plain files, and dsos. It is described in `benchmark/README.md`. The results that should drive this design:

**1. The effect only exists above a threshold, and the original test was below it.** Five small tables × one independent question each had **no headroom**: rebuilding a 58KB CSV costs one `read_csv`, so a store can never win. Redesigned as 3 datasets × 6 causally dependent rounds, the mechanism became visible — across five reuse rounds the dsos arm re-touched raw data **zero** times where the file arm did so 19 times, using half the findability calls.

**2. But reuse bought no accuracy, and cost 25% more.** The 6-round diamonds chain:

| | total input tokens | accuracy |
|---|---|---|
| files (+ manifest discipline) | 38.4k | 4/6 |
| dsos | 48.1k | 4/6 |

Identical failures in identical rounds (R2 answered median `price_per_carat` when asked for median `price`; R6 got the same wrong correlation). Reuse makes the *plumbing* cheaper. It does not make the agent think better, and the R1 registration premium (2.1× the file arm) has to be repaid before any of it pays.

**3. The baseline that matters was never tested.** "Files" is not the competitor. **Files plus a manifest** is: a competent file-based workflow writes `MANIFEST.md`, and R2 becomes trivial at zero registration cost. Every "dsos loses" number to date is against *disorganised* files.

**4. The store records artifacts, not intent.** Round 4 of the diamonds chain tests a question round 1 implicitly raised (is price driven by carat or cut?). Nothing in the store records that. The artifacts are all there; the reasoning that connects them is not.

The honest summary: **the plumbing works and the premise is unproven.** What follows is an attempt to make the premise measurable and the store worth its overhead — not a claim that it already is.

---

## The problem, restated

Doc I frames the problem as clutter: artifacts pile up, nothing gets reused. Measurement suggests the sharper framing is **reconstruction cost**:

> The value of the store is the gap between *reconstructing* something and *recording* it once.

Everything else follows. Artifact count is a symptom, not the disease — at 8 artifacts `ls` suffices; at 800, neither `ls` nor search helps, because you cannot remember *which question* any of them answered. The bottleneck at scale is not retrieval, it is **meaning**.

So the organising axis has to be the **spine**, not the container.

There is a second framing, and it matters more for the product than the first. The benchmark measured reuse by the *same kind of agent* that did the original work, and there a disciplined file workflow is a strong competitor. The claim files cannot compete on is reuse **across kinds of agent**: an agent that never ran the analysis, has no access to the data, and needs a result it can cite. That consumer needs the spine even more than the producer does, because the spine is the only thing that turns a pile of outputs into citable answers.

---

## The model: a spine with a recorded middle

```
                    ┌────────── exploration ──────────┐
                    │  scratch queries, dead ends,   │
question ──> analysis artifacts ──> narrative ──> decision
  │             (the middle)              │            │
  └──── hypothesis? / status ─────────────┴────────────┘
                 validation · confidence · caveats  (metadata, on any row)
```

**The spine** — `question → narrative → decision` — is what makes a store navigable. **The middle is not optional filler and not required to be summarised.**

### Exploration is recorded, and it does not owe you a narrative

This is the correction that matters most, and it inverts a reasonable-looking instinct.

`run_sql`/`run_python` already auto-register their output with lineage, so exploratory work *is* captured today. The gap is that captured work has **no way to be exploratory** — every row is `status='ready'`, equal weight, and the only path to significance is `promote_scratch`, which is a *promotion* metaphor: exploration starts outside the record and is grudgingly let in.

That is backwards, and it produces the landfill Doc I warned about. A session that spikes eight queries and keeps one has recorded eight artifacts of indistinguishable weight and no record of which mattered.

**Proposal: exploration is a first-class lifecycle state, not a demotion.**

| state | meaning |
|---|---|
| `exploratory` | recorded, linked, not claimed as a result |
| `result` | something the analysis stands behind |
| `superseded` | kept, never deleted, but not current |

Three things make this work:

- **Lineage already carries the story.** Eight exploratory queries hanging off one question, with the kept one marked `result`, *is* the record of the search. No separate "exploration log" is needed — the graph is the log.
- **Nothing requires promotion.** A question can be closed by a narrative, by a decision, or explicitly as `abandoned`. A dead end that killed a hypothesis is a legitimate, complete outcome.
- **The store stops being a landfill**, because exploratory rows are visibly exploratory and can be pruned from a search by default.

This also makes Doc I's Part 2 `heal_artifact` coherent: staleness propagates through lineage from a changed parent, whether that parent was a result or an exploratory intermediate.

### `question` and `decision` as types; `hypothesis` folded in

A hypothesis without a question is meaningless, and every additional type is a decision the agent must make at the worst possible moment. So:

- **`question`** — `{ question, hypothesis?, status: open|in_progress|answered|abandoned, answered_by? }`
- **`decision`** — `{ decision, rationale, revisit_if, status: active|superseded }`

`revisit_if` is the field that makes a decision valuable a year later ("unless the 2027 cohort data contradicts it"), and it is the one most likely to be skipped in practice. Worth measuring rather than assuming.

**The question is cheap and auto-created.** `start_session` already receives the question text on every call. Promoting it to a first-class row makes the spine free — but it also creates questions for throwaway sessions ("fix the typo"). Proposed compromise: auto-create, and let the agent close it in one call; unanswered questions older than N sessions are marked `abandoned` rather than accumulating. Surfacing *open* questions at session start (next to the prior-work signal already built) is the reuse of understanding, which the benchmark does not currently measure at all.

### Confidence, caveats, validation as metadata

The governing rule:

> **Metadata is a property of a thing. An artifact is a thing that can be cited, linked, argued with, and revisited.**

- **`confidence`** — on the artifact. Proposed as a *per-claim* assertion rather than a row-level enum: asked for one `high|medium|low` per artifact, a model will answer `high` every time, and the signal is worth nothing.
- **`caveats`** — a short list of strings, with an explicit threshold. `"assumes 2014 data"` is a property of the result; three paragraphs of caveats is a note that wants to be its own artifact. Without a stated boundary this field becomes an essay slot.
- **`validation`** — **append-only, not a mutable flag.** This is the one I'd push back on hardest in the earlier discussion. If a model marks its own result `confirmed` and a reviewer later contradicts it, a single column overwrites the first assertion and the fact that anyone ever doubted the result is lost. Disagreement is information about the result, not noise to be squashed. Store `(verdict, by, when, basis)`; derive the current status. This also lets `stale` be *computed* from `source.refresh_after` rather than asserted by whoever remembers to check.

---

## Data model changes

Additive to Doc I's model. `row_id` remains the only id on the MCP surface.

**`artifacts`** — four columns added to Doc I's row:

```sql
status        TEXT DEFAULT 'result'   -- exploratory | result | superseded
confidence    TEXT,                   -- JSON: [{claim, level, basis}]
caveats       TEXT                    -- JSON: [string, ...]  (short only)
```

**`validations`** — new table, append-only, never updated:

```sql
row_id, verdict,    -- confirmed | contradicted | stale | needs_review
by,                 -- 'model' | 'human' | 'derived:staleness'
at, basis           -- what the verdict rests on
```

**`questions`** — new table, *not* an artifacts row. The question is cheap, numerous, and lives in a lifecycle; giving it a blob + embedding per session would tax the hot path for a row that is mostly text. It still gets full-text search, and `artifact_row_id` points at the narrative or decision that closed it. It also serves as the coordination board for parallel agents (§Architecture), which adds a claim lease:

```sql
id, question, hypothesis,
status,             -- open | in_progress | answered | abandoned
asked_by,           -- session or consumer that raised it
claimed_by, claimed_at,   -- lease, not a lock; expires without activity
artifact_row_id     -- what closed it
```

**`decisions`** — a real `artifacts` row (`type='decision'`), because a decision outlives its evidence, can be cited, and can be contradicted later.

**Migrations.** Doc I has no migration story; `connect()` runs `CREATE TABLE IF NOT EXISTS`, and the `content_hash` column needed an ad-hoc guarded `ALTER` (`db.py:_add_missing_columns`) that must run *before* the schema script. Adding two tables and four columns makes that pattern untenable. Propose a numbered, idempotent migration list driven by `PRAGMA user_version`, each verified against a copy of a pre-existing store.

---

## Architecture: one daemon, two audiences

### Why a service, not files + manifest

A structured file system plus a manifest serves one analysis agent that has local disk access. It cannot serve these three cases, and all three are where the product is going:

- **Non-analysis consumers.** A PM agent writing a roadmap note needs to back up "activation dropped after the onboarding change." It needs the number, how that number was computed, and whether anyone has contradicted it since. It does not have the analysis agent's file system, does not want a dataset, and should not need to write Python. What it needs is a **citable result**.
- **Remote and local agents together.** A cloud agent cannot reach local files, and it cannot reach a stdio process started by some other client.
- **Parallel agents discovering together.** Two agents working the same question need to see each other's *in-progress* work, not only finished artifacts. Otherwise they duplicate it, which is the exact failure dsos exists to prevent.

### One daemon owns the store

**Today:** every MCP client launches its own `python -m dsos.mcp_server` over stdio. N agents means N processes writing one SQLite file, coordinated only by SQLite's file lock. That is adequate for one agent, but it is not a concurrency design. A CLI opening the same file would have exactly the same guarantees, so MCP is currently adding none.

**Proposed, one machine first:**

- **A single long-running daemon owns `store.db` and the blob directory.** All writes serialise through that one process. Readers, including the GUI, use WAL.
- **Transport:** MCP over streamable HTTP on localhost (FastMCP already supports it), plus a thin stdio shim for clients that only speak stdio. The shim forwards to the daemon; it never opens the DB itself.
- **Auth:** a bearer token from day one, because any local process can reach a localhost port.
- **No shared file system assumed.** Content leaves the daemon as inline previews and summaries, or as a daemon-served URL for the full bytes. No tool assumes the caller can open a path.
- **Storage stays as it is:** SQLite rows plus content-addressed blobs, with the DB as the source of truth. A distributed deployment (Postgres plus object storage) is a later swap behind the same daemon, not a redesign.

### Two tool profiles

Every tool definition costs tokens on every turn, for every agent that loads it, and that is part of the 25% premium measured above. A PM agent should never pay for `run_python`. The daemon exposes two profiles on two endpoints:

**producer** — called by analysis agents. Generated from the live FastMCP registry by `python -m dsos.server.contract --write`; do not hand-edit between the markers.

<!-- contract:producer -->
| tool | params | summary |
|---|---|---|
| `close_question` | `session_id*`: string, `question_id*`: string, `status*`: string, `artifact_row_id`: string \| null, `note`: string \| null | Close the question you have been working on, and say what answered it. |
| `get_artifact` | `row_id*`: string, `session_id*`: string | Fetch one artifact's full metadata and content, by row_id. |
| `get_lineage` | `row_id*`: string, `session_id*`: string, `direction`: string | See what an artifact was built from, or what was built from it. |
| `list_templates` | `session_id*`: string, `kind`: string \| null | List this store's chart styles and report templates, built-in and custom. |
| `mark` | `row_id*`: string, `session_id*`: string, `status`: string \| null, `verdict`: string \| null, `basis`: string \| null, `superseded_by`: string \| null | Move a row along its lifecycle, and/or record a verdict on it. |
| `record_decision` | `session_id*`: string, `decision*`: string, `rationale*`: string, `evidence_row_ids*`: list[string], `revisit_if`: string \| null, `question_id`: string \| null | Record a call you made and why, linked to the evidence it rests on. |
| `run_python` | `code*`: string, `session_id*`: string, `title*`: string, `description*`: string, `input_row_ids*`: list[string], `output_type`: string, `scratch`: boolean, `requirements`: list[string] \| null, `code_paths`: list[string] \| null, `style`: string \| null, `python_path`: string \| null, `status`: string | Run Python against one or more artifacts, and save the result. |
| `run_sql` | `code*`: string, `session_id*`: string, `title*`: string, `description*`: string, `input_row_ids*`: list[string], `scratch`: boolean, `status`: string | Run SQL (DuckDB) against one or more artifacts, and save the result. |
| `save_artifact` | `type*`: string, `title*`: string, `description*`: string, `content_format*`: string, `session_id*`: string, `content_text`: string \| null, `content_path`: string \| null, `tags`: list[string] \| null, `source`: object \| null, `parent_row_ids`: list[string] \| null, `dedupe`: boolean, `status`: string, `caveats`: list[string] \| null, `confidence`: list[object] \| null | Register something as a real artifact, so future work can find and reuse it. |
| `save_template` | `session_id*`: string, `kind*`: string, `content`: string \| null, `artifact_id`: string \| null, `title`: string \| null, `description`: string \| null, `tags`: list[string] \| null, `base`: string \| null | Create or re-version a template: the look of this workstream, saved once. |
| `search_artifacts` | `query*`: string, `session_id*`: string, `top_k`: integer, `type`: string \| null, `include_superseded`: boolean, `include_exploratory`: boolean | Search every artifact saved so far, across every past session. |
| `start_session` | `question*`: string, `question_id`: string \| null | Start a new round of work, and learn what this store already holds. |
<!-- /contract:producer -->

**consumer** — called by the PM agent and other non-analysis agents. The table is empty until the consumer's tools are registered; the registry is the source of truth, so it fills in on the next regeneration rather than by an edit here.

<!-- contract:consumer -->
| tool | params | summary |
|---|---|---|
| `ask` | `question*`: string, `context`: string \| null | Put a question this store cannot answer to an analysis agent, and return its id. |
| `cite` | `row_id*`: string | Turn a result into a reference you can paste into a document, with its standing attached. |
| `find_evidence` | `claim*`: string, `top_k`: integer | Find the stated results in this store that speak to a claim, with the numbers behind them. |
| `get_claim` | `row_id*`: string | Get one result in full: what it says, how well it is backed, and how it was computed. |
<!-- /contract:consumer -->

The consumer tools are designed against the PM agent:

- **`find_evidence(claim)`** searches `result` and `decision` rows only (never `exploratory`). Each hit returns the finding in one line, the number or numbers behind it, its current validation status (confirmed / contradicted / stale), and the question it answered.
- **`get_claim(row_id)`** returns the result, its caveats, its lineage flattened to *source → transforms → result*, and the code that produced it. That is enough for the PM agent to judge the claim without re-running anything.
- **`cite(row_id)`** returns a stable reference that the PM agent can paste into a document. It resolves to the GUI's artifact page.
- **`ask(question)`** is the consumer's only write. When no evidence exists, the PM agent opens a question for a producer to pick up instead of computing the answer itself.

### Questions as the coordination board

The `questions` table takes on a second role. `start_session` surfaces **open and in-progress** questions that match the new one, so a parallel agent can join or skip the work instead of duplicating it. Questions a consumer raised with `ask` show up in the same place, and that is how the PM agent's request reaches an analysis agent.

A claim is a **lease, not a lock**: `claimed_by` / `claimed_at`, expiring after N minutes without activity, so a crashed agent cannot hold a question forever. On one machine this is a row and a timestamp inside the daemon, not a distributed locking system.

---

## MVP scope: producer-side cuts

The pilot's bugs cluster where dsos owns things the agent could already do itself: the execution environment, identifier naming, and storage formats. Two were patched in `a8030aa`: digit-leading names now get a `t_` prefix, and error rows are hidden from search and lineage. Both are patches over a root cause. The MVP removes the causes:

- **Bind inputs by position, not by title.** Names derived from titles break on leading digits (patched), collide when two inputs share a title, and silently change when a title changes. Instead, both SQL and Python get fixed aliases (`in_1`, `in_2`, …, in `input_row_ids` order), echoed back in the response. Python also gets `inputs["<row_id>"]`.
- **A failed run is an execution, not an artifact.** It is recorded in `executions` only, with no `artifacts` row and no lineage edge. The error comes back in the response under the execution id. This replaces the current "error row that is hidden everywhere" design.
- **`run_python` runs in the user's interpreter and records its outputs.** This has been the direction since `d6f2e10`; finish it, so there is no sandbox-specific environment to maintain. `requirements=` resolved via `uv` stays as a gap-filler.
- **`promote_scratch` is removed.** The `exploratory` status replaces it.
- **Skills and templates are out of the MVP.** `list_skills`, `save_skill`, `list_templates`, `save_template` and `publish_report` leave the MVP tool surface; the code stays. They come back later as **shareable, distributable bundles across users**. That needs things the current in-store design lacks: author, version and origin fields, and a portable format (SKILL.md-compatible for skills, so an exported skill also works as a native agent skill). For sharing in the MVP, the consumer's `cite` covers it by linking to the GUI page.

---

## Scale: which system, and when

The three options are not competitors at the same size. They fail at different ones:

| | Fits | Fails when |
|---|---|---|
| **Notebook** | one question, one sitting, linear exploration | you need to reference *step 4* from a different question. Its unit of reuse is the whole notebook, not the analysis |
| **Files + manifest** | one project, one author, <~50 artifacts | provenance is manual and rots; composing across artifacts means rewriting glue; unreachable by an agent without that file system |
| **dsos** | many artifacts, many sessions, many questions | the store itself becomes a landfill — and the spine is what prevents that |

**Crossover, honestly:** around **50–100 artifacts across 10+ sessions**, or when derivation depth passes ~5 steps. The 6-round chain is still at or below it, which is why it lost on tokens. Do not tune the product to win below the crossover — the correct response there is to be cheaper, not more capable.

That crossover applies to **single-agent reuse only**. For the consumer case (§Architecture) there is no crossover: at any artifact count, files cannot serve an agent that does not share the file system. That makes the consumer path the claim to prove first, and the producer path's job is to stay cheap enough not to lose below the crossover.

**The fourth option, which is probably the right one:** notebooks are for *process*, stores are for *products*. The notebook stays disposable and linear; what escapes it — a cleaned dataset, a computed metric, a question, a decision — gets registered. dsos should not try to replace notebooks and should not try to store them. It should capture **what notebooks throw away**, which is the trail of decisions and the reusable results.

The "don't add a notebook type" position follows from this: the scratch work genuinely is disposable. What is not disposable is the *decision trail*, and `status='exploratory'` + lineage already captures it without a new container.

---

## Refactor plan

**The claim, grounded.** 3,489 lines across 13 modules. `mcp_server.py` is 758 of them (22%) and currently owns: the MCP tool surface, the tool docstrings and server `INSTRUCTIONS`, CSV/JSON ingestion (`_ingest_path`), response shaping (`_artifact_payload`, `_execution_result_payload`, `_with_inline_image`), the logging middleware, and store seeding. Doc I's GUI section already called out `_artifact_payload` as something that "lives inside `dsos/mcp_server.py`" and should move to the core; that extraction was partial — `present.py` exists (99 lines) and the server still defines three payload builders of its own.

**The contract has already drifted.** Doc I Part 3 froze a 7-tool contract at `v0.1.0`. The surface is now 13 tools. This is what happens when a module owns both the contract and the reasoning behind it: there is nowhere to put a design change that isn't also a code change, so the doc and the tool list diverge quietly.

Staged, in dependency order (revised Sep 27). The work-package breakdown, with files, specs and tests, is in [the TTD](./DS%20Artifact%20OS%20%E2%80%94%20TTD%20(Spine%20%26%20Scale).md).

| Stage | Work | Why here |
|---|---|---|
| **0. Migrations** | numbered `user_version` migrations; backfill and verify against a copied legacy store | Everything below adds schema, including stage 1: a failed run can only leave `artifacts` once `executions.output_row_id` is nullable |
| **1. Producer cuts** | bind inputs as `in_1..in_N`; failed runs go to `executions` only; remove `promote_scratch`; take skills/templates/`publish_report` off the MVP surface (§MVP scope) | Cheap, removes whole bug classes, and every later stage has less surface to carry |
| **2. Core / transport split** | a core with no MCP imports (store, lineage, execution, search), `tools/producer.py`, `tools/consumer.py`, `ingest.py` (path → content), `payloads.py` (response shaping) | The daemon and both profiles need a core that does not know about MCP. Also makes the contract reviewable as code |
| **3. Daemon** | one process owns the store; streamable-HTTP MCP on localhost with a bearer token; stdio shim that forwards to it | The coordination board and consumer tools assume a single writer and no shared file system |
| **4. Contract as an artifact** | generate each profile's tool table in this doc from the registry; a smoke test asserts doc == registry | The 7-vs-13 drift should be impossible, not merely noted |
| **5. Lifecycle and spine** | `status`/`exploratory`, `validations`, `questions` (auto-registered in `start_session`, with claim leases), `decision` rows | The substantive change. Needs 1–4 to be reviewable |
| **6. Consumer profile** | `find_evidence`, `get_claim`, `cite`, `ask` | Reads only `result`/`decision` rows plus validation status, so it needs 5 |
| **7. Cutover** | `exploratory` rows hidden by default in search and GUI; `abandoned` sweep; lease expiry | Changes what agents see; do last |

**What should not be refactored:** `store.py` (689 lines, 27 functions) is doing a coherent persistence job and is the layer the GUI already reads; it becomes the heart of the core in stage 2. `sandbox.py` changes only in how it binds inputs. `templating.py` is independent and fine; it is just off the MVP surface. This is a targeted refactor of one oversized module, a schema foundation and a transport change, not a rewrite. The value of the existing design is that the store is a real, working, tested thing. The refactor's job is to let the *next* slice be cheap, not to make the current one prettier.

---

## What the benchmark must measure next

The current benchmark cannot judge any of this, and that is deliberate to state plainly:

- **It never ran the files+manifest arm.** Until it does, no claim about dsos beating files is meaningful.
- **It runs one model.** "The agent never searched" conflates a weak affordance with a weak model. Two models minimum before generalising.
- **It measures reuse of work, not reuse of understanding.** A spine makes a second, distinct claim: that a later session surfaces *prior questions*, not just prior artifacts. That needs its own metric — questions reopened, decisions revisited, work repeated across questions.
- **n=1 chain.** Directional, not conclusive.
- **It never tests the claim files cannot compete on.** Add a **consumer arm**: a PM-role agent with no data access has to back a set of claims from prior analysis sessions. Measure claims backed by a correct number with a traceable source, contradicted or stale results cited (target: zero), and tokens. Also run a files+manifest version with a shared file system as the ceiling; it is the best case files can reach.
- **It never runs agents in parallel.** Add two producers working overlapping questions at once, measuring the duplicate-work rate with and without the coordination board.

## Deliberately not doing

- **A project type.** Sessions are flat and time-ordered; real work belongs to something recognisable. But this is more likely a *query* (`--project hackathons`) than an artifact type, and it can wait for evidence that flatness actually hurts.
- **A metric type.** Metrics are results. Promoting them makes every query look like a headline number.
- **A notebook/exploration type.** Exploration is `status` plus lineage, not a container.
- **Multi-user, permissions, governance.** Doc I Philosophy #5 — this is the working layer *beneath* that, and nothing here changes it. One caveat for later: once skills and templates are shared across users, they will need author and origin fields. That is provenance, not permissions.
- **Distributed deployment.** One machine first. The daemon boundary is what makes the later move to Postgres plus object storage a swap rather than a redesign.
- **Skills and templates in the MVP.** Deferred, not dropped (§MVP scope).

## Open questions

1. **Does `status='exploratory'` survive contact with an agent?** The risk is that everything gets marked `result` because that is the path of least resistance — the same failure mode as row-level `confidence`. Worth a cheap guard: search defaults to hiding exploratory rows, so over-claiming costs the agent visibility.
2. **Is the spine worth its cost if the questions are short-lived?** If most sessions are throwaway, auto-registering questions is pollution. The `abandoned` sweep is the safety valve, but it needs a real abandonment rate before we trust it.
3. **Should `question` really not be an artifact?** The table-plus-FTS route is cheaper, but an artifact would inherit lineage, versioning, and citations for free. Worth revisiting if questions ever need to be cited *from* an analysis.
4. **What is the honest crossover?** §Scale says 50–100 artifacts; the chain says we are below it. A cheap synthetic study — one dataset, N rounds, artifact count as the only variable — would put a number on it that is currently a guess.
5. **How does a remote agent reach a localhost daemon?** "One machine first" and "remote plus local agents" collide here. The options are a tunnel with the bearer token, or treating remote access as the trigger for the distributed deployment. This is the first thing that will break the one-machine assumption.
6. **Is `ask` the right size for the consumer's only write?** The alternative is that consumers are strictly read-only, and a missing answer is reported back to a human. `ask` is more useful, but it lets any consumer create work for the producers.
7. **How long is a claim lease?** Too short, and a slow analysis loses its claim mid-run. Too long, and a crashed agent blocks the question. Activity-based renewal (any tool call in the claiming session renews it) is the likely answer.
