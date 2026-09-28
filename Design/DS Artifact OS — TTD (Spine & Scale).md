# DS Artifact OS — Technical Task Document: Spine & Scale

Sep 27, 2026 · @Shawn Shi

**What this is.** The implementation breakdown of [Design Doc II](./DS%20Artifact%20OS%20%E2%80%94%20Design%20Doc%20II%20(Spine%20%26%20Scale).md), written for coding agents that will each pick up **one work package (WP)**. Doc II says *why*; this says *what to change, in which files, and how we know it's done*. If the two disagree, this doc wins on implementation detail. A disagreement about intent goes back to the maintainer.

---

## Rules for every agent

1. **Read first:** Doc II §Architecture, §MVP scope and §Data model changes, then your WP. Also read every file in your WP's *Owns* list before editing.
2. **Tests first.** Write the WP's new smoke test before the implementation. Run it, and confirm it fails *for the reason the WP describes*. Paste that failing output into the PR description, then implement until it passes.
3. **Stay in your files.** Modify only the files listed under *Owns*. If you need to change anything else, stop and report back; don't widen the WP.
4. **Tests you own must be green; the full suite is run at merge time.** This is the one rule the TTD's own parallelism breaks, so it is split:
   - **During the WP**, you must pass every test in your *Owns* list, plus your new test. You do **not** have to pass the whole suite, because WPs run concurrently in separate worktrees and a sibling's half-finished refactor will turn a suite red for reasons you are forbidden by rule 3 to fix.
   - **At merge**, the integrator runs the full `tests/run_all.py` and resolves what broke. Report honestly in the PR which tests you ran and which you left red, and why.
   - If you *do* run the full suite and it is green, say so; if it is red, paste the failing test names and say whether the cause is in your files.
   - Update existing tests *only* where your WP changes the behaviour they assert, and say which ones in the PR.
5. **Test style.** Match `tests/execution_hygiene_smoke_test.py`: a standalone script, a `check(label, ok, detail)` helper, `DSOS_DB_PATH` set to `data/test-runs/<test_name>/store.db` (wiped at start) **before** importing dsos, exit code 1 on any failure. MCP-level tests use the in-process `fastmcp.Client`.
6. **Conventions.** Pass `encoding="utf-8"` on every text read or write. Use `.venv/Scripts/python`, never a bare `python`. Don't bump the package version. Match the surrounding comment density.
7. **Branch / PR.** Name the branch `wp/<id>-<slug>`, with one PR per WP. The PR body lists the tests added and any existing tests changed.
8. **Data.** Don't fetch or inspect the Devpost demo dataset; it's reserved for the live demo.

---

## Decisions fixed here

These resolve ambiguities in Doc II so that agents don't each resolve them differently.

| # | Decision | Why |
|---|---|---|
| D1 | **Migrations come before the producer cuts.** | Moving failed runs out of `artifacts` needs `executions.output_row_id` to be nullable, which is a table rebuild. |
| D2 | **The lifecycle reuses `artifacts.status`.** A migration maps `ready`/`ok` → `result`. Legacy `error` rows are left in place and stay hidden. Code always writes `status` explicitly and never relies on the column default. | There is already a `status` column (`ready`/`ok`/`error`), and a second status column would be ambiguous. |
| D3 | **Default status:** `run_sql`/`run_python` output → `exploratory`; `save_artifact` → `result`; decisions → `result`. | Computation is exploratory until claimed. A deliberate registration is a claim. |
| D4 | **Inputs are bound as `in_1..in_N`** (in `input_row_ids` order) in both SQL and Python. Python also gets `inputs: dict[row_id, object]`. Names derived from titles are gone. | Removes the digit-leading, title-collision and title-rename bug classes. |
| D5 | **`scratch=True` stays**, but without the cache, `scratch_id` or promotion. | It's still useful for quick checks. Promotion is replaced by `status`. |
| D6 | **Producer tools (11):** `start_session`, `search_artifacts`, `get_artifact`, `save_artifact`, `run_sql`, `run_python`, `get_lineage`, `mark`, `close_question`, `record_decision`, `save_template`. **Plus `list_templates` for discovery (12).** **Consumer tools (4):** `find_evidence`, `get_claim`, `cite`, `ask`. | `mark` covers both the status change and appending a validation, so there's one tool rather than two. `save_template`/`list_templates` were cut in WP-B2 and restored by the maintainer's decision in U1: a shared chart style and a house report layout are the reuse case the store exists for. `publish_report` did **not** come back — the GUI report route already consumes report templates. |
| D7 | **`superseded` rows are hidden from search once WP-E2 lands.** `exploratory` rows are hidden only at cutover (WP-G1). | A superseded row was explicitly marked, so hiding it is safe. Hiding exploratory rows changes what agents see, so it goes last. |
| D8 | **Concurrency:** one `Database` per process, one SQLite connection per thread, and a process-wide write lock. **One daemon per store**, enforced by `daemon.json`. | A single `sqlite3` connection shared across FastMCP's thread pool is not safe once there are concurrent clients. |
| D9 | **The daemon serves the GUI.** The GUI stays read-only and needs no auth on loopback. The MCP endpoints require a bearer token. | `cite` URLs have to open in a browser. GUI auth is deferred. |
| D10 | **A claim lease is activity-based:** it's live while the claiming session made any tool call within `DSOS_LEASE_MINUTES` (default 30). It's computed on read and never swept. | Renewal comes free from `tool_calls`, so there's no heartbeat tool. |
| D11 | **`stale` is computed on read** from ancestor datasets' `source.refresh_after`, and never written. | That's what Doc II means by "computed, not asserted". |
| D12 | **Consumer calls are logged** against an auto-created session with `kind='consumer'`, one per MCP client session. | Consumer reuse is the metric that matters, and `tool_calls.session_id` is `NOT NULL`. |
| D13 | **A concurrency test must prove serialisation, not the absence of errors.** "No `database is locked` under 8 writers" passes even with the write lock removed, because `busy_timeout=5000` absorbs the contention. The assertions that actually discriminate are **non-overlap of the critical sections** and a **duty-cycle** figure. | Added after WP-A2, which ran the same workload with the lock neutered: 400 rows, **0 errors**, 399 overlapping sections, 786% duty. The case `busy_timeout` genuinely cannot cover is `SQLITE_BUSY_SNAPSHOT` — a read-then-write transaction, which is `save_artifact`'s versioning path — where SQLite returns BUSY immediately without consulting the busy handler. |
| D14 | **Any WP that adds, removes, or changes a tool's signature regenerates Doc II's marker region in its own commit** (`python -m dsos.server.contract --write`). No escalation, no permission needed. | Added after WP-E2 escalated, correctly, because it added `mark` and three parameters and therefore invalidated a block it did not own. The block is a generated artifact, so regenerating it is a mechanical rewrite, not a hand edit — and the review value is in the tool definitions, not in the rendered table. Deferring it to the integrator guarantees every subsequent signature change escalates the same question, and guarantees a branch shipping with a knowingly-red `contract_smoke_test.py`. **Verify the diff stays inside the markers**; a line-ending change to Doc II hides a real edit behind a whole-file diff. |

---

## Dependency graph and waves

```
WP-00 test runner
   │
   ├── A1 migrations ──┬── B3 failed runs → executions ──┐
   │                   └── E1 spine schema ──────────────┼──────────────┐
   ├── A2 Database handle ───────────────────────────────┤              │
   └── B1 input aliases ── B2 surface trim ──────────────┴── C1 server split
                                                              │
                         ┌──────────────┬─────────────────────┼──────────────┐
                         C2 contract    D1 daemon ── D2 shim  E2 lifecycle ── E3 questions
                                                                              │
                                                                         F1 consumer ── G1 cutover

H1 files+manifest arm: independent, start now      H2 consumer arm: after F1      H3 parallel arm: after E3 + D1
```

The wave table below is a **schedule, not a claim that every cell is safe to parallelise.** Two cells were found to conflict on file ownership during a review of the ownership lists, and are marked. The real constraint is not the dependency graph, it is that 6 files are each owned by 4 or 5 WPs:

| file | WPs that own it |
|---|---|
| `dsos/store.py` | B2, B3, E2, F1, G1 |
| `dsos/mcp_server.py` | B1, B2, B3, C1, D2 |
| `dsos/db.py` | A1, A2, B3, E1 |
| `dsos/execution.py` | B1, B2, B3, E2 |
| `dsos/gui.py` | B3, D1, E2, G1 |
| `dsos/server/producer.py` | C1, E2, E3, G1 |

**Rule: two WPs may run concurrently only if their *Owns* lists are disjoint.** Waves 2 and 4 below violate this as originally written; the fixes are in the cells.

| Wave | WPs that can run in parallel | Notes |
|---|---|---|
| 1 | WP-00 · A1 · B1 · H1 | All four *Owns* lists are disjoint. **A1 and B1 need no predecessor** and are the only two WPs in the whole graph that can start on `main` as-is. H1 is the longest job: start it first, leave it unattended. |
| 2 | A2 · B2 | A2 owns `db.py`; B2 owns `mcp_server.py`/`execution.py`/`store.py` search filters. Disjoint. |
| 2b | B3 · E1 | **Amended — originally "B3 · E1, disjoint".** They are not: B3 appends M2 to `dsos/db.py` and E1 appends M3 to the same `MIGRATIONS` list. One agent does **B3's M2 first, then E1's M3**, in that order, as a single sequential lane. |
| 3 | C1 | Big move; everything touching the server waits for it. Runs alone. |
| 4 | C2 · D1 · E2 | **Amended — originally "C2 · D1 · E2, disjoint files".** They are not: D1 owns `dsos/gui.py` (refactoring it into `create_gui_router(db)`) and E2 owns `dsos/gui.py` + `templates/artifact_detail.html`. **D1 and E2 must be sequential, in that order** (D1 creates the router seam E2 then adds display code to). C2 is genuinely disjoint from both and runs alongside whichever is going. |
| 5 | D2 · E3 | Disjoint (`mcp_server.py __main__` + `AGENTS.md` + `README.md` vs `spine.py` + `producer.py`). |
| 6 | F1 | |
| 7 | G1 · H2 · H3 | G1 owns `gui.py`; H2/H3 are `benchmark/`-only. Genuinely disjoint. |

**Measured against the cutover.** WP-G1 flips the exploratory default. WP-H2 and WP-H3 therefore run *after* cutover, against a store whose `search_artifacts` hides exploratory rows and whose `questions` sweep has run. This is intentional: the parallel-arm duplicate-work measurement (H3) is only meaningful if it sees what a producer actually sees. If either arm must run earlier, record the mismatch explicitly in `benchmark/README.md` rather than presenting a pre-cutover number as a post-cutover one.

---

## WP-00 — Test runner

**Owns:** `tests/run_all.py` (new), `README.md` (test section only)

**Spec:** runs every `tests/*_test.py` as a separate subprocess with `.venv`'s Python, in a stable order. Prints one line per script with pass/fail and duration, prints the failing scripts' output at the end, and exits 1 if any script failed. `--only <substring>` filters scripts.

**Done:** running it on current `main` passes, and the README tells people to run this instead of the individual scripts.

---

## Lane A — storage foundation (`db.py`)

### WP-A1 — Numbered migrations

**Owns:** `dsos/db.py`, `tests/migrations_smoke_test.py` (new)

**Spec**
- `MIGRATIONS: list[Migration]`, where each entry is a function `(conn) -> None`. Migration *i* moves the store from `PRAGMA user_version` *i−1* to *i*.
- **M1 = baseline.** The current `SCHEMA` (all tables, indexes and FTS), plus the guarded `content_hash` `ALTER` for stores created before v0.4. A legacy store has `user_version = 0` and an existing `artifacts` table.
- **Each migration runs in its own explicit transaction.** Set `isolation_level = None`, run `BEGIN IMMEDIATE`, then the statements via `conn.execute` (**not** `executescript`, which commits implicitly), then `PRAGMA user_version = i`, then `COMMIT`. On an exception: `ROLLBACK`, and raise `MigrationError` naming the migration's number and the underlying error.
- **Backup before migrating.** Before applying any migration to an existing, non-empty store, copy it to `store.db.bak-v<from>` with `sqlite3`'s backup API. Skip this when nothing is pending.
- **Refuse newer stores.** If `user_version > len(MIGRATIONS)`, raise: "store was created by a newer dsos (schema v<n>); upgrade dsos."
- **Delete `_ADDED_COLUMNS` / `_add_missing_columns`.** `connect()` becomes: open → WAL → `busy_timeout=5000` → run migrations → return.

**Tests (write first)**
1. A fresh store ends at `user_version == len(MIGRATIONS)`.
2. A legacy store, built in the test from hard-coded v0.3 SQL (no `content_hash`) with 3 rows, upgrades. The rows are intact, `content_hash` exists, and a backup file exists.
3. Reopening an up-to-date store is a no-op and creates no new backup.
4. Inject a failing migration by patching the list: the store is unchanged, `user_version` is unchanged, and the error names the migration's number.
5. A store with `user_version` set to 99 is refused with the upgrade message.

### WP-A2 — `Database` handle (after A1)

**Owns:** `dsos/db.py`, `tests/database_concurrency_smoke_test.py` (new)

**Spec**
- `class Database(path)`. Migrations run once, in `__init__`.
- `.conn()` returns this thread's connection (`threading.local`), opened the way `connect()` opens one (row factory, WAL, `busy_timeout`, `_dsos_db_path`), but without re-running migrations.
- `.write()` is a context manager that holds a **process-wide `threading.RLock`** for the duration. The store functions keep their own `commit()` calls; the lock is what serialises writers.
- **Keep `connect()`** for existing callers and tests. Nothing else switches to `Database` in this WP; WP-C1 wires it in.

**Tests (write first)**
1. 8 threads each run 50 `save_artifact` calls under `db.write()` on their own `db.conn()`. Expect 400 artifact rows, 400 FTS rows, and no `database is locked`.
2. A reader thread running `search_artifacts` in a loop during (1) raises nothing.
3. `db.conn()` returns the same object within a thread and different objects across threads.

---

## Lane B — producer cuts

### WP-B1 — Bind inputs by alias, not title

**Owns:** `dsos/sandbox.py`, `dsos/execution.py`, `dsos/mcp_server.py` (the `run_sql`, `run_python`, `save_artifact` docstrings/payloads and `_execution_result_payload`), `dsos/seed.py` (skill text only), `tests/input_binding_smoke_test.py` (new), plus **every** existing test whose only breakage is a title-derived binding name

**Ownership exceptions granted to B1** (added during execution, after the WP found a real gap in its own `Owns` list). These are narrow and one-time; they are not a general relaxation of rule 3.

- **`dsos/store.py`, `safe_table_name` docstring only.** The spec below says to update that docstring, but `store.py` is owned by B2/B3/E2/F1/G1. B1 may change **only that docstring** — no code, no signature, no other function. A comment-only change cannot conflict.
- **`tests/smoke_test.py`, `tests/gui_smoke_test.py`, `tests/publish_smoke_test.py`** — owned by no WP and by B2 respectively; each breaks on `FROM toy_scores`. The "existing tests that assert title-derived names" clause is the TTD's own language and covers them. Change the binding reference only.
- **`tests/execution_hygiene_smoke_test.py`** (B3's) — this test's premise is the `t_` prefix workaround that B1 removes, so B1 invalidates it. **B1 fixes the binding assertion and docstring, and touches nothing else in the file.** B3's failed-run assertions are left intact; B3 branches after B1 merges and inherits a correct file.

**The line that matters, for all of the above:** B1 may fix breakage that *is* a binding-name change and nothing else. A logic difference, a status value, an execution id, or a response key other than `table_name` is a finding to report, not a fix to make.

**Spec**
- **SQL:** register each input as `in_1..in_N`, in `input_row_ids` order.
- **Python:** bind each input as `in_1..in_N`, and also bind `inputs = {row_id: obj, ...}`.
- **Input errors:**
  - An unknown `row_id` is an error: `unknown input_row_id '<id>'`. Today this fails with an `AttributeError` on `None`.
  - A duplicate `row_id` is an error: `duplicate input_row_id '<id>'`.
- **Responses** keep the field name `input_tables`, now `{row_id: "in_k"}`. **Remove `table_name`** from `save_artifact` responses and run payloads; it has no meaning any more.
- **`_schema_hint`** lists `in_1 "<title>" (col, ...)`.
- **`safe_table_name`** stays, but is used only to derive `artifact_id` for skills and templates. Update its docstring to say so.
- **Update every reference** to title-derived names: tool docstrings, `INSTRUCTIONS`, and the skill texts in `seed.py`. Grep `dsos/`, `tests/` and `benchmark/` for `table_name`, `input_tables` and `safe_table_name`. Report any hits in `benchmark/` rather than editing them; they belong to Lane H.

  **Grep result (Sep 27, B1 execution):** the binding-name breakage reaches further than the two originally anticipated. Beyond `mcp_smoke_test.py` and `dedupe_smoke_test.py`, `smoke_test.py`, `gui_smoke_test.py` and `publish_smoke_test.py` all bind `toy_scores`, and `execution_hygiene_smoke_test.py` reads `ds["table_name"]`. See the ownership exceptions above — this list is the authority, not the original `Owns` line.

**Tests (write first)**
1. Two inputs with **identical titles** can both be addressed as `in_1`/`in_2`, in SQL and in Python.
2. A digit-leading title works, and no `t_` prefix appears anywhere in the response.
3. `inputs["<row_id>"]` works in Python.
4. Code that uses `in_1` still works after the input gets a new version with a different title.
5. Duplicate and unknown ids return the specified errors.
6. A SQL column typo lists `in_1 "<title>" (...)` in the error.

### WP-B2 — Trim the MVP surface (after B1, same agent)

**Owns:** `dsos/mcp_server.py`, `dsos/execution.py`, `dsos/store.py` (search/prior-work type filters only), `tests/surface_smoke_test.py` (new), `tests/skills_templates_smoke_test.py`, `tests/publish_smoke_test.py`, `tests/mcp_smoke_test.py`, `tests/prior_work_smoke_test.py`

**`tests/prior_work_smoke_test.py` added to the Owns list** (Sep 27, before dispatch). It is not in the original list, but line 118 calls `list_skills` over MCP — a tool this WP removes — so it breaks on this WP and on no other. Same class of gap B1 hit: a test whose only breakage is the behaviour this WP changes. Fix its `list_skills` assertion to use the library-level `seed` path (the same conversion the other three test files get) and change nothing else in it.

**Known, deliberately not fixed here:** `benchmark/metrics.py` and `benchmark/report.py` both list `list_skills`/`list_templates` in `FINDABILITY_TOOLS`. Those entries become dead the moment this WP lands — harmless, because a removed tool can never appear in a transcript and so is never counted — but they are stale. They belong to Lane H; **report the hits, do not edit `benchmark/`**, and leave them for the next Lane H WP.

**Spec**
- **Remove these MCP tools:** `promote_scratch`, `list_skills`, `save_skill`, `list_templates`, `save_template`, `publish_report`. **Keep** `seed.py`, `templating.py`, `publish.py`, and the GUI report route; they are deferred, not dead.
- **`execution.py`:**
  - Delete `_SCRATCH_CACHE` and `promote_scratch`.
  - **One line here is load-bearing (found by B1):** `promote_scratch` rebuilds `input_row_ids` from `get_lineage`, whose order need not match the `input_row_ids` the run actually used. Left alone, the `input_tables` it reports after a promotion can attribute `in_1`/`in_2` in a different order than the code saw. The correct order is already in the scratch cache entry. Fixing it matters less than the fact that deleting `promote_scratch` removes the hazard entirely.
  - The scratch payload loses `scratch_id`.
  - `_persist_run` stays, because promotion no longer shares it.
- **Stop seeding:** remove the `_ensure_seeded()` call.
- **`store.search_artifacts` and `prior_work_signal`** exclude types `skill` and `template` unless `type=` explicitly asks for one of them.
- **`run_python`'s `style=`** stays. Its docstring mentions only the built-ins (`dsos`/`report`/`minimal`/`None`), and custom template references still resolve.
- **Rewrite `INSTRUCTIONS`:** drop step 3 (skills) and step 7 (publish), and mention `scratch=True` for quick checks.
- **Convert the skills/templates and publish tests** to call `seed`, `templating` and `publish` directly (library level), so the deferred code keeps its coverage. Delete their MCP-level calls.

**Tests (write first):** `surface_smoke_test.py`
1. `list_tools()` returns exactly `{start_session, search_artifacts, get_artifact, save_artifact, run_sql, run_python, get_lineage}`.
2. A scratch response has no `scratch_id`.
3. A fresh store has zero `skill` rows after the first `start_session`.
4. On a store with a pre-inserted skill row, a search with no `type` excludes it and `type="skill"` includes it.

### WP-B3 — A failed run is an execution, not an artifact (after A1, B2)

**Owns:** `dsos/db.py` (append M2 only), `dsos/execution.py`, `dsos/store.py` (`get_execution`), `dsos/mcp_server.py` (`_execution_result_payload`), `dsos/gui.py` and `dsos/templates/session_detail.html` (the failed-runs list), `tests/execution_hygiene_smoke_test.py`

**Spec**
- **M2 rebuilds `executions`** using the standard create-new → copy → drop → rename sequence, inside the migration's transaction:
  - `output_row_id` becomes nullable.
  - Add `session_id TEXT`, backfilled from `artifacts.session_id` via the output row.
  - Add `input_row_ids TEXT NOT NULL DEFAULT '[]'`, backfilled from the output row's `lineage` parents.
  - Add indexes on `output_row_id` and `session_id`.
- **`run_sql`/`run_python` return a `RunOutcome(status, row_id | None, execution_id)` dataclass.**
  - **On `ok`:** unchanged: an artifact is saved and the execution is recorded with `output_row_id`.
  - **On `error`, including dsos-side failures such as unknown inputs:** **no `save_artifact` call and no lineage.** The execution is recorded with `output_row_id = NULL`, plus `session_id` and `input_row_ids`.
- **The failure response** is `{status:"error", execution_id, error, stdout, stderr, input_tables, artifact_row_ids: <inputs>}`, with **no `row_id` key**.
- **`store.get_execution(conn, *, execution_id=None, output_row_id=None)`.**
- **GUI:** the session detail page lists that session's failed executions (kind, error, time).
- **Legacy `status='error'` artifact rows are left alone.** Keep the existing `!= 'error'` filters, and update their comments to say they exist for legacy stores.

**Tests (write first)**
1. A failing `run_python` creates **0** new `artifacts` rows and **0** lineage edges, and creates **1** `executions` row with `output_row_id IS NULL` and the right `session_id`/`input_row_ids`.
2. The response has `execution_id` and no `row_id`.
3. The success path is unchanged.
4. A legacy store containing an `error` artifact row migrates cleanly, and that row stays hidden.

---

### WP-B2R — Restore templates (after B2, before C1; supersedes part of B2)

**Owns:** `dsos/mcp_server.py` (`save_template` and `list_templates` only), `dsos/templating.py` (the agent-facing error strings only), `tests/surface_smoke_test.py` (the tool-count expectation), `tests/skills_templates_smoke_test.py`, `tests/publish_smoke_test.py`

**Why this exists:** the maintainer reversed the templates half of WP-B2's trim — see U1. Everything in U1's scope note applies. WP-B2R must run **before WP-C1**, because both own `dsos/mcp_server.py`.

**Spec**
- Re-register `save_template` and `list_templates` on the producer server. Both were thin MCP wrappers over `templating.py` functions that still exist, so the prior implementations are recoverable from git (`git show 5c363d6^:dsos/mcp_server.py`) — use them as the starting point rather than rewriting from scratch. **Read them before writing anything.**
- `save_template` keeps the whole prior contract: `kind` (`chart-style` | `report`), `content` or `base`, `artifact_id` for re-versioning, `title`/`description`/`tags`, and **validation at save time** via `validate_chart_style_text` / `validate_report_template_text` so a bad template fails in one call rather than at render time.
- `list_templates` keeps the prior contract: built-ins plus customs, optionally filtered by `kind`.
- **Fix the U1 dead-end.** `resolve_chart_style`'s "none saved yet — create one with save_template" and its three sibling strings now name a tool that exists again, so re-check each of the four (`templating.py:105`, `:121`, `:156`, `:263`) and make the wording correct. Where a string refers to `list_templates` to discover what exists, that is right again. Where it offers creation, that is right again. Do not leave a string referring to `publish_report` as an MCP tool, because that tool is **not** coming back — point report-template guidance at the GUI report route instead, which is what actually consumes them.
- **Do not restore `list_skills`, `save_skill`, `publish_report`, `promote_scratch`, or re-enable seeding.** Those cuts stand. A restored `save_template` must not require the store to be seeded.
- `INSTRUCTIONS` gains back a short templates step, and keeps its `scratch=True` mention and its current shape otherwise.
- **Update `tests/surface_smoke_test.py`:** `EXPECTED_TOOLS` becomes the 9-tool set. The other three assertions in that file (no `scratch_id`, zero skill rows after `start_session`, untyped search excluding skills) are **still correct and must keep passing unchanged** — this WP restores templates, not skills.

**Tests (write first)**
1. `list_tools()` returns exactly the 9 producer tools — the prior 7 plus `save_template` and `list_templates`.
2. A saved chart-style template is listed by `list_templates(kind="chart-style")` and resolves from `run_python(style=<row_id>)`.
3. A saved report template is listed by `list_templates(kind="report")` and resolves through the GUI's report route.
4. An invalid chart style fails **at save time** with a one-call error, not at use time.
5. Re-versioning on the same `artifact_id` keeps existing references resolving (`base_template_content` and `resolve_chart_style` still find it).
6. `list_templates` with no `kind` lists both kinds and the built-ins; a bad `kind` is rejected with the allowed set.
7. A fresh store is still unseeded: no `skill` rows, and `list_skills` is still absent from the tool list.

**Done when** the 9-tool assertion holds, the four `templating.py` strings are correct, and the store is still not seeded.

## Lane C — structure

### WP-C1 — Split the server into a package with factories (after B3, A2)

**Owns:** `dsos/server/` (new: `__init__.py`, `common.py`, `producer.py`, `consumer.py`, `instructions.py`), `dsos/ingest.py` (new), `dsos/present.py`, `dsos/mcp_server.py`, `tests/layering_smoke_test.py` (new), plus import-path fixes in existing tests

**Spec**
- **`ServerConfig(db: Database, python_path: str, base_url: str | None)`.**
- **`build_producer(config) -> FastMCP`** registers the producer tools as closures over `config`. **No module-level connection or `Database` anywhere in `dsos/server/`.**
- **`build_consumer(config) -> FastMCP`** has consumer instructions and **zero tools** for now; WP-F1 adds them.
- **`common.py`** holds `ToolCallLogger` (now constructed with a `db`) and `with_inline_image`. **`instructions.py`** holds both instruction strings plus the update notice.
- **Move out of the server:**
  - `_ingest_path` → `dsos/ingest.py`.
  - `_execution_result_payload` → `present.execution_payload(conn, outcome, input_row_ids)`. It must be MCP-free; the image wrapping stays in `server/common.py`.
- **Every tool that writes** wraps its store calls in `with config.db.write():` and gets its connection from `config.db.conn()`. **Read-only tools** only call `config.db.conn()`.
- **`dsos/mcp_server.py` becomes thin.** It builds the `Database` from `DSOS_DB_PATH` and exposes `mcp` **lazily** through a module `__getattr__`, so `from dsos.mcp_server import mcp` keeps working in tests. `__main__` runs it over stdio, as today; WP-D2 replaces that path.
- **Behaviour-preserving.** No tool's name, parameters or response changes.

**Tests (write first):** `layering_smoke_test.py`
1. An AST scan finds no `fastmcp` import outside `dsos/server/`, `dsos/mcp_server.py` and `dsos/daemon.py`.
2. No `connect(`/`Database(` call at module scope in `dsos/server/`.
3. Two producers built on two temp stores in one process: writing through one leaves the other empty.

### WP-C2 — The contract as a generated artifact (after C1)

**Owns:** `dsos/server/contract.py` (new), `tests/contract_smoke_test.py` (new), Doc II (the marker blocks only)

**Spec**
- `render(server) -> str` produces a markdown table with columns **tool | params** (`name: type`, required ones marked `*`) **| summary**.
- **The summary is the first SENTENCE, not the first physical line** (amended after execution — see below). Collapse the hard-wrap, then cut at the first sentence terminator, guarding abbreviations (`e.g.`, `i.e.` — both occur in these docstrings). Cap the cell at roughly 200-240 chars with an ellipsis. A tool with no docstring renders an empty cell rather than raising.
- **Keep the abbreviation guards until every tool set follows the unwrapped-summary convention** (see D14 and the E2 rewrap note). The producer table stopped exercising them once E2 rewrapped its docstrings, but the consumer profile is empty and WP-F1 will add four tools whose docstrings nobody has rewrapped. F1 is the natural point to revisit this, not E2.
- `python -m dsos.server.contract --write` rewrites the regions between `<!-- contract:producer -->…<!-- /contract:producer -->` and `<!-- contract:consumer -->…<!-- /contract:consumer -->` in Doc II. Find the file with the glob `Design/*Design Doc II*.md`.
- **Replace the tool table in Doc II §Architecture with these two generated blocks.**

**Why the spec changed.** It originally said "the first docstring line", assuming a convention the codebase does not follow. All nine producer docstrings in `producer.py` are hard-wrapped at ~80 columns, so the literal first physical line is a mid-sentence fragment in every case — e.g. `search_artifacts` starts `"Search over everything saved so far, across every past session, not"`. Generating that would have put nine truncated fragments into Doc II, which is a worse artifact than the drift this WP exists to prevent. The word "summary" in the original spec is also not satisfied by a fragment.

**The root cause, as a follow-up (not this WP).** If every tool docstring opened with a single deliberately *unwrapped* summary line and hard-wrapped only the prose beneath it, the original "first docstring line" wording would be literally true and the generator could stay a trivial line-split. That means rewriting nine docstrings in `dsos/server/producer.py`, which this WP does not own. **Fold it into whichever WP next edits those docstrings in bulk** — E2 adds the `status`/`caveats`/`confidence` parameters and will be rewriting them anyway. Until then `_summary()` carries the burden, which is why it is written to be obviously replaceable.

**Tests (write first):** for each profile, the doc block equals `render(...)`. On a mismatch the test prints a diff and says "run `python -m dsos.server.contract --write`".

---

## Lane D — daemon

### WP-D1 — One daemon owns the store (after C1)

**Owns:** `dsos/daemon.py` (new), `dsos/gui.py`, `dsos/store.py` (`failed_executions` only — see U3), `pyproject.toml` (pin `fastmcp>=4.0`), `tests/daemon_smoke_test.py` (new)

**Spec**
- **Entry point:** `python -m dsos.daemon [--host 127.0.0.1] [--port 8765]`, also configurable via env `DSOS_PORT`. **Refuse any non-loopback `--host`** with "remote access is not supported yet" (see Doc II open question 5).
- **One `Database` and one FastAPI app:**
  - The GUI routes. Refactor `gui.py` into `create_gui_router(db)`; `python -m dsos.gui` must still run standalone.
  - `build_producer(cfg).http_app(path="/")` mounted at **`/mcp/producer`**, and `build_consumer(cfg)` at **`/mcp/consumer`**.
  - **Compose both MCP apps' lifespans into the FastAPI lifespan.** FastMCP's mounted HTTP apps don't work without their lifespan.
- **Auth:** `StaticTokenVerifier` on both MCP servers. Import it from **`fastmcp.server.auth.providers.jwt`** — it is *not* re-exported from `fastmcp.server` in fastmcp 4.x, which is what `pyproject.toml` currently allows (`fastmcp>=0.4`, installed 4.0.10). Raise the declared floor to `fastmcp>=4.0` in the same change.
  - The token comes from `DSOS_TOKEN`; otherwise read or create `<db dir>/daemon.token` with `secrets.token_urlsafe(32)` (mode 0600 where the OS supports it).
  - GUI routes and `GET /healthz` need no token.
- **Single daemon per store:**
  - On start, write `<db dir>/daemon.json` with `{pid, port, base_url, db_path, started_at}`, and remove it on clean shutdown.
  - If that file exists, its pid is alive and `/healthz` answers, **refuse to start** and say which daemon is running.
  - A stale file (dead pid) is overwritten.
- **`cfg.base_url = http://<host>:<port>`.**
- **`/healthz`** returns `{ok, version, db_path}`.

**Tests (write first).** Start the daemon as a subprocess on a free port with a temp store, then check:
1. An MCP request with no token gets 401.
2. An authenticated `fastmcp.Client` over HTTP lists the producer tools at `/mcp/producer`, and the consumer tools at `/mcp/consumer` (empty until F1).
3. Two concurrent authenticated clients doing 20 `save_artifact` calls each produce 40 rows and no errors.
4. GUI `/` returns 200.
5. A second daemon on the same store refuses to start.

### WP-D2 — stdio shim and install docs (after D1)

**Owns:** `dsos/mcp_server.py` (`__main__` only), `AGENTS.md`, `README.md`, `tests/shim_smoke_test.py` (new)

**Spec**
- **`python -m dsos.mcp_server [--profile producer|consumer]`** reads `daemon.json` and `daemon.token` next to `DSOS_DB_PATH`, or uses `DSOS_URL` + `DSOS_TOKEN`. It then runs `fastmcp.server.create_proxy(<url>/mcp/<profile>, auth)` over stdio.
- **If the daemon isn't running:** exit non-zero with the stderr message "dsos daemon not running for <db>. Start it: <sys.executable> -m dsos.daemon". Auto-starting the daemon is deferred.
- **Importing `dsos.mcp_server` must not open the database** unless `mcp` is accessed, since the shim never needs it.
- **Rewrite AGENTS.md** §4 (register) and §5 (verify) around this flow:
  1. Start the daemon.
  2. Register the shim with `claude mcp add ... -- <python> -m dsos.mcp_server --profile producer`.
  3. For HTTP-capable clients, the direct alternative is `claude mcp add --transport http dsos http://127.0.0.1:8765/mcp/producer/ --header "Authorization: Bearer <token>"`. **Note the trailing slash** (see below).
  4. For a consumer agent, use `--profile consumer`.
- **§5's verify step is `start_session` with any question, not a "read-only tool"** (amended after execution). The original text said to call one read-only tool, naming `list_skills`/`list_templates`, and that has been wrong since before this refactor: **every** producer tool requires at least one argument — `list_templates` needs `session_id`, `search_artifacts` needs `query` and `session_id`, `get_artifact`/`get_lineage` need `row_id` — so there is no zero-argument read the reader can make on a fresh store. `start_session` needs no prior state and exercises daemon → auth → shim → store write, which is more evidence than a read would.
- **Do not name a tool that does not exist yet.** `find_evidence` is WP-F1; the consumer profile has zero tools until it lands. Documenting it here is the same drift WP-C2 exists to prevent, in the file a new user reads first.
- **The HTTP endpoints need the trailing slash.** `/mcp/producer` answers a `307` redirect to `/mcp/producer/` because the app is mounted with `path="/"`, and the auth check runs after the redirect — so an unauthenticated POST to the slashless form gets a `307`, not the `401` you would expect. Auth is still enforced correctly on the real endpoint. Document the slash; do not "fix" the mount, which is WP-D1's and working as specified.

**Tests (write first)**
1. With the daemon running, a stdio `fastmcp.Client` pointed at the shim lists the producer tools.
2. With no daemon, the shim exits non-zero with the specified message.

---

## Lane E — lifecycle and spine

### WP-E1 — Spine schema (after A1; migrations only)

**Owns:** `dsos/db.py` (append M3), `tests/migrations_smoke_test.py` (extend)

**Spec (M3)**
- **`artifacts`:**
  - `UPDATE status: 'ready','ok' → 'result'`. Leave `error` rows alone.
  - `ADD COLUMN confidence TEXT`, `caveats TEXT` and `superseded_by TEXT`.
- **`validations`:**
  - Columns: `id TEXT PK`, `row_id TEXT NOT NULL REFERENCES artifacts`, `verdict TEXT NOT NULL CHECK (verdict IN ('confirmed','contradicted','stale','needs_review'))`, `by TEXT NOT NULL CHECK (by IN ('model','human') OR by LIKE 'derived:%')`, `session_id TEXT`, `at TEXT NOT NULL`, `basis TEXT NOT NULL`, plus an index on `row_id`.
  - **Append-only is enforced in the schema:** `BEFORE UPDATE` and `BEFORE DELETE` triggers `RAISE(ABORT, 'validations are append-only')`.
  - **Write the triggers with explicit `conn.execute` calls, never as DDL in a shared `SCHEMA` string.** WP-A1's migration splitter cuts statements on `;` outside line comments, and a `CREATE TRIGGER ... BEGIN ... ; ... END` body would be split in half. `dsos/db.py` documents that limitation; do not reintroduce it.
- **`questions`:**
  - Columns: `id TEXT PK`, `question TEXT NOT NULL`, `hypothesis TEXT`, `status TEXT NOT NULL CHECK (status IN ('open','in_progress','answered','abandoned'))`, `asked_by TEXT`, `claimed_by TEXT`, `claimed_at TEXT`, `artifact_row_id TEXT`, `created_at TEXT NOT NULL`, `closed_at TEXT`.
  - Plus `questions_fts` (fts5: `id UNINDEXED`, `question`, `hypothesis`).
  - **Index `questions(status)`.** WP-G1's abandon sweep runs inside every `start_session` and filters on `status`; without this it is a table scan on the hot path.
- **`sessions`:** `ADD COLUMN kind TEXT NOT NULL DEFAULT 'producer'`, `question_id TEXT` and `client TEXT`.

**Tests (write first)**
1. A legacy store's `ready`/`ok` rows become `result`, and its `error` rows are unchanged.
2. UPDATE and DELETE on `validations` both abort.
3. A bad `verdict` or question `status` is rejected by the CHECK constraint.
4. A fresh store and a migrated store end up with identical schemas (compare the normalised `sqlite_master` SQL).

### WP-E2 — Lifecycle, `mark`, and search fields (after C1, E1)

**Owns:** `dsos/store.py`, `dsos/execution.py` (the `status` param), `dsos/server/producer.py`, `dsos/present.py`, `dsos/gui.py` + `templates/artifact_detail.html` (display only), `tests/lifecycle_smoke_test.py` (new)

**Spec**
- **`store.save_artifact(..., status='result', confidence=None, caveats=None)`**, with this validation:
  - `status` must be one of `exploratory|result|superseded`.
  - `caveats` is a list of at most **5** strings, each at most **200** chars. Otherwise raise "caveats are short properties of a result; write a narrative for longer notes".
  - `confidence` is a list of `{claim, level: high|medium|low, basis}`, and `basis` is required and must be non-empty. Requiring a basis is the guard against "high" every time.
  - **Every writer must pass `status` explicitly from this WP onward — none may rely on the column default.** WP-E1 left `DEFAULT 'ready'` in M1 deliberately, to keep fresh and migrated stores schema-identical. The consequence is a live inconsistency: after M3, a *migrated* store's old rows read `result`, while a row written by the current `save_artifact` still reads `ready`, because its Python default is unchanged. Nothing in the suite depends on it today, which is exactly why it is dangerous. This WP is where the writers change, so this is the moment to decide: make `status` required (or default it to `exploratory` in the Python signature) and, if the column default is worth changing at all, change it here in M4 where fresh and migrated stores stay in step.
  - Add `decision` to `ARTIFACT_TYPES`.
- **The `run_sql`/`run_python` tools** gain `status="exploratory"`. **The `save_artifact` tool** gains `status="result"`, `caveats` and `confidence`. **This WP also rewraps the producer tool docstrings** so each opens with a single unwrapped summary line before the hard-wrapped prose (see the C2 follow-up note) — it is the natural moment, because these parameters are being documented anyway.
- **`mark(row_id, session_id, status=None, verdict=None, basis=None, superseded_by=None)`:**
  - **Status transitions:** only `exploratory → result`, `exploratory → superseded` and `result → superseded`. Superseded is terminal; to revive something, save a new version.
  - **`superseded_by`** is required when the new status is `superseded`, and must be an existing row.
  - **`verdict`** appends a `validations` row with `by='model'`, and requires `basis`.
  - At least one of `status` or `verdict` is required.
- **`store.validation_status(conn, row_id) -> {current, history}`:**
  - `current` is the latest `human` verdict if there is any; otherwise the latest `model` verdict; otherwise `unvalidated`.
  - Then apply the **derived stale check** (D11): stale if any ancestor dataset's `source.refresh_after` (`^\d+[hdw]$`; `static` never expires) has elapsed since `source.fetched_at` (when it parses as ISO) or else since the dataset's `created_at`.
  - Stale overrides `confirmed` and `unvalidated`, but **not** `contradicted`. It's never written to the database.
- **`search_artifacts`:**
  - Results gain `status`, `created_at`, `validation` (current) and `superseded_by`.
  - New params `include_superseded=False` and `include_exploratory=True`; WP-G1 flips the latter.
- **`get_artifact` payload** gains `status`, `caveats`, `confidence` and `validation`.
- **GUI artifact page** shows status, caveats and the validation history.

**Tests (write first)**
1. A run defaults to `exploratory` and `save_artifact` to `result`.
2. Each illegal transition is rejected with a message naming the allowed ones.
3. A superseded row disappears from default search but is still returned by `get_artifact`, and its replacement is still searchable.
4. Two verdicts leave two `validations` rows; a human verdict beats a later model verdict.
5. A dataset with `refresh_after="1d"` and `fetched_at` 3 days ago makes its descendant `stale`. A `contradicted` descendant stays `contradicted`.
6. Oversized caveats, or a confidence entry without a basis, are rejected.

### WP-E3 — Questions and the coordination board (after E2)

**Owns:** `dsos/spine.py` (new), `dsos/server/producer.py` (`start_session`, `close_question`, `record_decision`), `tests/spine_smoke_test.py` (new)

**Spec (`spine.py`)**
- **Lease (D10):**
  - `lease_state(conn, question) -> {live: bool, expires_at}`.
  - Last activity = `max(claimed_at, latest tool_calls.ts of claimed_by)`.
  - The TTL is `DSOS_LEASE_MINUTES`, default 30.
- **`related_questions(conn, text, exclude_id=None, limit=5)`:**
  - Match via `questions_fts` using the same `_fts_query` AND-prefix rule as artifacts, plus exact normalised text matches (lowercase, collapsed whitespace, trailing `?` stripped).
  - Order: live `in_progress` first, then `open` (typically asked by a consumer), then `answered` (with `artifact_row_id`, which is reuse).
- **`start_session(question, question_id=None)`:**
  - **No `question_id`:** create a question with `status in_progress`, `claimed_by` and `asked_by` set to the new session.
  - **With `question_id`:** claim that question. If another session holds a **live** lease, fail with an error naming its last activity time and suggesting either the related work or waiting.
  - Set `sessions.question_id` in both cases.
  - **Response adds** `question_id` and `related_questions: [{question_id, question, status, claimed, lease_expires_at, artifact_row_id}]`, excluding the session's own question.
- **`close_question(session_id, question_id, status, artifact_row_id=None, note=None)`:**
  - `status` must be `answered` or `abandoned`.
  - `answered` requires `artifact_row_id` pointing at a `result` artifact or a decision.
  - Clears the claim and sets `closed_at`.
- **`record_decision(session_id, decision, rationale, evidence_row_ids, revisit_if=None, question_id=None)`:**
  - At least one evidence row is required.
  - Creates a `decision` artifact: JSON content `{decision, rationale, revisit_if}`, lineage parents = the evidence, status `result`.
  - With `question_id`, it also closes that question as answered by this decision.
- **`DSOS_DISABLE_BOARD=1`** makes `related_questions` return `[]`. It exists for benchmark H3's control condition.

**Tests (write first)**
1. Session 2 with the same question text sees session 1's question as live `in_progress`.
2. Claiming a live-leased question fails, and succeeds once the lease has expired (set `DSOS_LEASE_MINUTES` to a fraction via env and backdate the `tool_calls` timestamps).
3. A tool call by the claimant renews the lease.
4. `answered` without a result row is rejected.
5. `record_decision` links the evidence in lineage and closes the question.
6. A question inserted with `status open` and a different `asked_by` appears in a producer's `related_questions`.
7. The board flag suppresses `related_questions`.

---

## Lane F — consumer

### WP-F1 — Consumer profile (after E3; uses D1's `base_url` if present)

**Owns:** `dsos/server/consumer.py`, `dsos/server/common.py` (the consumer-session middleware), `dsos/server/instructions.py` (consumer text), `dsos/store.py` (read helpers only), `tests/consumer_smoke_test.py` (new)

**Spec**
- **Consumer sessions (D12):**
  - Middleware on the consumer server maps each MCP client session (`ctx.session_id`, falling back to one per server process if it's absent) to a `sessions` row with `kind='consumer'`, `client=<clientInfo name if available>` and `question="consumer: <client>"`.
  - Every consumer call is logged against it. Consumer tools take **no** `session_id` parameter.
- **`find_evidence(claim, top_k=5)`:**
  - **Eligible rows:** the latest version, `status='result'`, and type in `query, transform, chart, narrative, decision`. Datasets are sources, not findings, so they're excluded.
  - **Ranking:** as in search, but semantic hits below `DSOS_EVIDENCE_FLOOR` (default `0.35`) are dropped, and each hit carries `match: keyword|semantic`.
  - **Each hit contains:**
    - `row_id`, `type`, `title`, `finding` (the description), `validation` (current), `caveats`, `confidence`, `question`, `created_at` and `url`.
    - `question` is the question it answered: via `questions.artifact_row_id`, otherwise the creating session's question.
    - `numbers`: for a table, `row_count`, `columns` and at most 5 rows (clipped); for text, the first 500 chars; for a chart, a note pointing at `url`.
  - **No hits:** return `{"results": [], "hint": "No stated result covers this. ask(question) requests an analysis."}`.
- **`get_claim(row_id)`** returns the artifact's summary fields plus:
  - `status`, with an explicit warning when it's `exploratory` or `superseded` (including `superseded_by`).
  - `validation {current, history}`.
  - `derivation`: its ancestors plus itself in topological order (roots first). Each step is `{row_id, type, title, source (datasets), kind + code (from the executions whose output_row_id is that step)}`.
  - `question` and `url`.
- **`cite(row_id)`** returns `{reference: "<title> — dsos <row_id[:8]>, <YYYY-MM-DD>", url, status, validation}`. `url` is `<base_url>/artifacts/<row_id>`; with no `base_url` it's `dsos:<row_id>`. It warns rather than refuses when the row isn't a `result`.
- **`ask(question, context=None)`:**
  - If the normalised text exactly matches an open or in-progress question, return that id with `deduplicated: true`.
  - Otherwise create an `open` question with `asked_by` = the consumer session.
  - Returns `{question_id, status}`.

**Tests (write first).** Seed a store through the producer with: a result, an exploratory row, a superseded row, a result with a `contradicted` verdict, and a dataset with a source URL. Then check:
1. `find_evidence` returns only eligible rows, and the contradicted row is included but labelled.
2. An off-topic claim returns empty with the hint.
3. The `get_claim` derivation includes the dataset's source URL and the producing code.
4. The `cite` URL format is correct with and without `base_url`.
5. `ask` creates a question that a new producer `start_session` sees in `related_questions`, and a second identical `ask` dedupes.
6. The consumer server lists exactly 4 tools.
7. Consumer calls add **0** `artifacts` rows; only `tool_calls` and `questions` change.

---

## Lane G — cutover

### WP-G1 — Cutover (after F1)

**Owns:** `dsos/store.py`, `dsos/server/producer.py`, `dsos/server/instructions.py`, `dsos/spine.py`, `dsos/gui.py` + gallery template, `tests/cutover_smoke_test.py` (new)

**Spec**
- **Flip the exploratory default:** `search_artifacts` defaults to `include_exploratory=False`, and `prior_work_signal` excludes exploratory rows.
- **GUI gallery** hides exploratory rows unless `?include=exploratory`.
- **Abandon sweep:** runs lazily inside `start_session` (no background job). An `open`/`in_progress` question with no activity for `DSOS_ABANDON_DAYS` (default 14) becomes `abandoned`, with `closed_at` set and the claim cleared. Activity = `max(created_at, claimed_at, latest tool call of claimed_by)`. This runs on every `start_session`, so **M3 must add an index on `questions(status)`** or the sweep is a table scan on the hot path.
- **Producer `INSTRUCTIONS`:** runs are exploratory; claim what you stand behind with `mark(status="result")` or `status="result"` on the run; close your question.
- **Regenerate the contract** (`python -m dsos.server.contract --write`).

**Tests (write first)**
1. An exploratory row is absent from default search and present with the flag.
2. A 15-day-idle question is abandoned on the next `start_session`, while a 1-day-idle one is untouched.
3. The GUI gallery hides exploratory rows by default.

---

## Lane H — benchmark

All three WPs live in `benchmark/`, now tracked on `feature/spine-and-scale` and extended with an arm registry (`benchmark/arms.json`) that H1 added. **Read `benchmark/README.md` first.** None of them touch `dsos/`.

### WP-H1 — Files + manifest arm (start now)

**Spec**
- Add a third condition, `file_manifest`: the file arm plus a system prompt requiring a `MANIFEST.md` in the working directory.
- The manifest has one entry per derived file: path, the question it served, how it was computed, and its input paths. The agent reads it at round start and updates it at round end.
- Apply the same raw-CSV removal policy as the dsos arm, so the comparison is fair.
- Extend `metrics.py`/`report.py` to three-way tables, and run the 6-round diamonds chain.

**Done:** a three-way table (tokens, accuracy, re-fetch, findability) in `benchmark/results/`, plus a README section replacing "never ran the files+manifest arm".

### WP-H2 — Consumer arm (after F1)

**Spec**
- A PM-role agent connected **only** to `/mcp/consumer`, given claims derived from earlier chain rounds' answers: some true, some false, and some that rest on a result later superseded or contradicted.
- **Score:**
  - Claims correctly backed or refuted with a cited `row_id`.
  - Citations of contradicted, stale or superseded rows (target 0).
  - Tokens.
- **Ceiling condition:** the same PM agent with read access to the file_manifest arm's working directory.

### WP-H3 — Parallel arm (after E3, D1)

**Spec**
- Two producer agents run concurrently through one daemon, on overlapping questions.
- Measure the duplicate-work rate: both agents registering or computing the same thing. Compare the board on vs off (`DSOS_DISABLE_BOARD=1`).

---

## Unassigned — found during execution, needs an owner before cutover

These are real, and none of the WPs below owns the file they live in. Recorded here so they are not lost, not handed to an agent silently, and not fixed by whoever happens to be editing nearby.

### U1 — RESOLVED (Sep 27, maintainer): templates stay

**Decision: templates come back on the producer surface.** The maintainer's reason: a consistent chart style and a house report layout are genuinely useful to someone working a DS workstream across sessions, and that is the reuse case the store is for. This overrides §MVP scope's decision to defer skills and templates, for **templates only**.

**Scope of the reversal, and why it is two tools and not three:**

- `save_template` and `list_templates` come back as producer tools. Nothing is lost by putting them back: `templating.py` still holds every library function they need (`validate_chart_style_text`, `validate_report_template_text`, `base_template_content`, `template_kind`, `template_tags`), so this is restoring a thin MCP wrapper, not rebuilding anything.
- `list_templates` is **not** redundant with `search_artifacts`. Search filters on `query` + `type` and has no tag filter, while `list_templates(kind=...)` filters on the kind tag. A rank-based search is also the wrong tool for "list every chart style in the store".
- **`publish_report` does NOT come back, and this is deliberate.** Report templates look orphaned, but they are not: `dsos/gui.py`'s report route calls `render_report_html(conn, row_id, template=template)`, which resolves through `templating.resolve_report_template`. So a report template created by an agent is consumable by the GUI's report page today, with no dead end. Restoring `publish_report` would add a tool definition to every turn of every agent to serve a use the GUI already covers. If a producer agent later needs to render a report itself, that is the moment to add it — as a deliberate cost, not as a side effect of restoring templates.
- **Skills stay out.** `list_skills`/`save_skill` remain removed and the store is no longer seeded. The maintainer asked for templates; skills-as-shareable-bundles stays deferred for the reason §MVP scope gives (author, version, origin, portable export).
- **The `templating.py` error strings are fixed as part of the restoration** (see the WP below), so the dead-end that started this is closed rather than papered over.

**Cost, stated plainly:** the producer surface goes 7 -> 9 now, and 10 -> 12 at the full D6 list. Doc II's measured 25% premium is the reason the trim happened at all, so this spends part of it back. That is a trade the maintainer has chosen knowingly, not an oversight.

### U2 — stale `FINDABILITY_TOOLS` in the benchmark

`benchmark/metrics.py:47` and `benchmark/report.py:50` both list `list_skills` and `list_templates` in `FINDABILITY_TOOLS`. Dead since WP-B2. Harmless — a removed tool cannot appear in a transcript, so it is never counted — but wrong, and it will mislead anyone reading the findability numbers. **Belongs to the next Lane H WP (H2).**

### U3 — a raw SQL query living in `gui.py`
WP-B3 needed the session-detail page to list that session's failed executions. Its `store.py` ownership is `get_execution` only, so rather than widen the WP it put a small `conn.execute` in `dsos/gui.py` and flagged the gap instead. Correct call under rule 3, and the gap is real:

- §Refactor plan says `store.py` "is doing a coherent persistence job and is the layer the GUI already reads". SQL in the GUI route contradicts that, and it is the first crack in the layering WP-C1 is about to enforce.
- The natural home is `store.failed_executions(conn, session_id) -> list[Execution]`, sitting next to the `get_execution` B3 already added.

**Assigned to WP-D1**, which owns `dsos/gui.py` and is already a `gui.py` refactor (into `create_gui_router`). Move the query into `store.py` there and add it to D1's `Owns` note. It is small; the point is that it should not survive to the daemon, where the GUI becomes a mounted router over a shared `Database`.

### U5 — the coordination board only matches IDENTICAL phrasing (found by E3, confirmed by the integrator)

`related_questions` uses the same AND-prefix FTS rule as `search_artifacts`, plus exact normalised-text matching. Measured against a real store after WP-E3 merged:

| producer asks | board hits |
|---|---|
| `is red really the q3 leader?` (identical to the consumer's) | 1 |
| `which player leads q3?` | **0** |
| `who is leading q3` | **0** |
| `q3 leader` (high overlap, short) | **0** |

**Why this matters more than a ranking nit.** Doc II §Architecture says the board is "how the PM agent's request reaches an analysis agent" — a consumer calls `ask`, a producer picks the question up at `start_session`. With AND-matching, that only works if both agents phrase the question identically, which is the one thing two independent agents will not do. The consumer hand-off, and the duplicate-work rate H3 measures, both rest on this.

**The rule was designed for the other feature.** `_fts_query`'s own docstring says AND is deliberate because "OR'd terms make a long question match almost anything via common words, drowning the exact-term-wins signal." That reasoning is right for `search_artifacts` — artifact titles are short and keyword-like, and search has a semantic fallback behind it. It is **wrong for `questions`**, which are long natural-language sentences with no semantic fallback, because AND over two differently-phrased sentences almost never holds. Copying the rule to the board inherited a decision whose premises did not travel.

**The cost asymmetry is what settles it.** A spurious related question costs an agent one glance. A missed one means two agents do the same analysis — the exact failure this whole design exists to prevent, and the thing H3 is instrumented to measure. For the board, recall should beat precision.

**Options for the maintainer to decide (do not change this unilaterally):**
1. **OR'd FTS ranked by shared content terms** — no new schema, no embeddings, and the cost asymmetry justifies the false positives. Needs a ranking and a stopword list.
2. **A semantic fallback for questions**, mirroring `search_artifacts`. This requires an embedding column on `questions` and an M4 migration, which Doc II explicitly rejected on hot-path grounds.
3. **Keep AND** and accept that the consumer hand-off works only on near-identical phrasing — and say so in Doc II rather than discovering it in a demo.

Whatever is chosen, the decision belongs before **WP-F1**, whose `ask` tool exists specifically to put a consumer's question where a producer will see it.

### U6 — `with db.write()` serialises writers but does not COMMIT them

Found by E3, which introduced it: its first `start_session` left the `sessions` UPDATE uncommitted. The process-wide `RLock` is released when the tool returns, but the SQLite transaction on that thread's connection is **not**, so a RESERVED lock stayed on the file and every *other* thread's write then failed with `database is locked` after the busy timeout. The first tool call in the process died.

This is a general hazard, not an E3 bug: `Database.write()` gives mutual exclusion, not a transaction boundary. **Every write path must `commit()` inside the block** — the store functions already do their own `commit()`, which is why most call sites are safe, but any tool that writes directly must not assume the context manager ended the transaction. Worth a line in `db.py`'s `write()` docstring, which currently implies more than it delivers.

The FTS AND-prefix rule lives in `dsos/store.py:_fts_query`, a private function. `search_artifacts` uses it internally, and since **WP-E3** the question board uses it too, across a module boundary: `dsos/spine.py` imports `store._fts_query`.

That is a deliberate wart, approved during E3's execution rather than discovered later. The tidier fix is to promote it to a public helper, which is a change to `store.py` — a file E3, F1 and G1 all own and E3 did not. Copying the rule into `spine.py` was the alternative and was rejected: two spellings of one rule is the exact drift this project keeps paying to undo, and a tuned AND-prefix would silently diverge between artifact search and the board.

**Whoever is next in `store.py` for another reason should promote it** — a rename to `fts_match` with the underscore dropped, updating both call sites. It is a two-line mechanical change. Until then the import carries a comment saying it is deliberate.

## Out of scope for this TTD

- Skills and templates as shareable bundles across users (author, version, origin, SKILL.md-compatible export).
- `publish_report` / report templates back on the MCP surface.
- Auto-starting the daemon from the shim; GUI auth; human verdicts through the GUI.
- Non-loopback or remote access, and distributed deployment (Postgres plus object storage).
- A better embedding model. `DSOS_EVIDENCE_FLOOR` will need retuning when that happens.
