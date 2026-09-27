# Benchmark: dsos vs. plain file-based workflows

An A/B benchmark measuring the project's core claim — that data workers
spend more time **re-creating and re-finding** past work than doing new
analysis — against the plain-file baseline every agent already has.

Built on a subset of [InfiAgent-DABench](https://arxiv.org/abs/2401.05507)
(dev split), chosen because it is the right shape for reuse: **257
questions over 52 tables** (~5 per table) means follow-up questions on
data an earlier session already ingested. Its answers are tagged
(`@mean_fare[34.65]`), so scoring is deterministic — no LLM judge.

Dev tooling only; nothing here ships in the `dsos` package.

## Status — read this before reading any number

A first pilot (30 runs, `results/batch_*.log`) is in `results/`. **Its
headline result is contaminated and must not be cited as-is.** The
runner's first version discarded the MCP server's `INSTRUCTIONS` block
when bridging the tools, so the dsos arm ran with 13 bare tool
docstrings and no workflow — weaker than any real deployment.

Product-side fixes (dedupe, prior-work signal, session-id validation)
have since landed, and a single titanic R1→R2 verification pair was
re-run with the corrected arm design — the raw CSV removed after R1, so
the store was the only path. **That run reused prior work**: R2 called
`start_session`, then `search_artifacts`, then `get_artifact` on the
registered dataset, and computed from it. The store held one dataset
copy, not two.

So the earlier "the agent never searches" result was substantially an
artifact of the file sitting in the agent's own working directory, not
only of weak affordances. With the file genuinely absent, the reuse path
works end to end.

That verification also surfaced a real product bug: `get_artifact` and
`run_python` were logged against a mistyped `session_id` (…0185ae vs
…0185ee) that no session owned, because no tool validated the id. Such
calls are invisible in the per-session trace and silently dropped by the
reuse metric. The server now rejects an unknown `session_id` with a
message telling the agent to re-run `start_session`.

A full post-fix pilot across all five tables has not been run.

## What we measure

| Metric | Round | What it shows | Condition A (files) | Condition B (dsos) |
|---|---|---|---|---|
| **Accuracy** (tag match) | all | guardrail: reuse must not produce stale-artifact answers | `score.py` | `score.py` |
| **Re-fetch rate** | R2+ | did it re-ingest what a prior session already had? | raw-path touches in transcript | new dataset registrations in store (touching the *existing* dataset artifact is reuse, not re-fetch) |
| **Re-clean rate** (proxy) | R2+ | did it redo R1's transform work? | raw-vs-derived path touches | duplicate dataset registrations vs derived-artifact reuse |
| **Findability cost** | R2+ | calls spent locating prior work before analysis | ls/grep/glob calls | search_artifacts calls |
| **Reuse calls** | R2+ | the headline: tool calls that touched prior-session work | derived-file reads | store SQL (session boundary) |
| **Tokens / tool calls / time** | all | efficiency; expect B worse on R1 (registration overhead), better on R2+ | recorded per round | recorded per round |

Report **per-table totals** (R1+R2+R3), not per-question: reuse benefits
accumulate, so "cost to answer 3 questions about this table across
sessions" is the unit of comparison.

## Depth chains (the current design)

The original design — 5 DABench tables × 3 independent questions — is
retained for the pilot results but is **superseded**. It measured the wrong
regime: each round was one independent numeric question, so rebuilding it
cost a single `read_csv` and a store could never win. There was no headroom
for the effect being claimed.

`chains.py` + `build_chains.py` replace it with **3 datasets × 6 causally
dependent rounds**. Each round consumes something an earlier round
*produced*, so the chain cannot be answered without redoing the work:

| Dataset | Rows | Chain |
|---|---|---|
| `diamonds.csv` | 53,940 | drop impossible rows (x/y/z = 0) → `price_per_carat` → per-cut medians → Ideal×clarity → premium segment → premium∩Ideal → correlations |
| `vgsales.csv` | 16,598 | `total_earlier` → genre shares → top genre by decade → platform within that slice → publisher concentration → compare the two shares |
| `census.csv` | 32,561 | strip `?` markers → `capital_net` → positives by sex → long-hours segment → long-hours∩capital-positive → correlations |

Only R1 is standalone; R2–R6 are all load-bearing (audited in
`build_chains.py` output). Every gold answer is computed with pandas in
`build_chains.py` — no LLM judge, no DABench dependency for the leaf
questions. **18 runs per arm, 36 total.**

Chains are chosen to be an analysis sequence a competent person would
follow anyway (load → clean → derive → segment → compare), not
dependencies invented to defeat the file arm. The file arm faces the
identical chain with R1's outputs sitting in its workspace.

Both arms also get one identical instruction — *"if you produce an
intermediate result you would need again, save it now"* — because without
it the file arm is a strawman: an agent that computes inline and saves
nothing has nothing to reuse. With it, the file arm's R1 leaves
`diamonds_clean.csv` behind and R2 reuses it (input tokens 9.1k → 7.2k),
which is the effect under test. Each arm persists in its own idiom.

```sh
python build_chains.py                                                   # gold answers
node runner.mjs --condition file --manifest results/manifest_chains.json \
                --label chain_file --tables all
node runner.mjs --condition dsos --manifest results/manifest_chains.json \
                --label chain_dsos --tables all --reset-store
python score.py results/answers_chain_file.json --manifest results/manifest_chains.json
python report.py
```

`--manifest` and `--label` keep chain results out of the pilot's
`answers_*.json`, and `report.py` discovers arms from whatever answer files
exist, so no code change is needed per experiment.

## Arms

| Arm | Command | Raw CSV in R2/R3 | Meaning |
|---|---|---|---|
| `file` | `--condition file` | present | built-in file tools only; the most favorable file baseline |
| `dsos` | `--condition dsos` | **removed** | dsos tools against a dedicated store; the store is the only path to the data |
| `dsos --keep-raw` | `--condition dsos --keep-raw` | present | diagnostic: what happens when the file competes with the store |

The dsos arm removes the raw CSV after R1 **by default**, because that is
dsos's intended workflow (register once, then rely on the store) and
because leaving it in place makes the arm meaningless: with the file in
the agent's own cwd, "dsos" becomes "files + extra tools", the agent reads
the local file, and the store is never exercised. That was the pre-fix
pilot's central error, and it is what made reuse look impossible.

`--no-file-tools` additionally strips read/bash/edit/write/grep/find/ls
from the dsos arm, leaving the 13 dsos tools as the only way to touch
data. That is the purist variant; the default keeps file tools because
that is how a real agent is configured.

Round prompts are identical across arms. R1 names the file; R2/R3 do not,
so locating the data is part of what's measured. Workspaces live under
`%TEMP%/dsos-bench/<label>/<table>/`, not in the repo — inside it the arms
shared one `find()`-able tree and an agent read the *other* arm's scratch
file (and, when its own was gone, `curl`d a public copy from GitHub).

### Known confound in the dsos arm

Some DABench tables have public copies on the internet (`titanic.csv` is
the obvious one). A file-less agent can still `curl` one, which bypasses
the store without reusing anything. Watch for a `bash`/`curl` in the
transcript and for a second dataset registration with a *different*
content hash. Dedupe only collapses byte-identical content, so a
re-fetched variant registers as a separate artifact — visible, not silent.

## Running

```sh
python prepare.py                                        # tables -> data/, manifest.json
node runner.mjs --condition file  --tables all           # 15 runs
node runner.mjs --condition dsos  --tables all --reset-store
python score.py results/answers_file.json results/answers_dsos.json
python report.py
```

Useful flags: `--table <name>`, `--rounds 1,2`, `--model <id>`,
`--timeout-min N`. The runner spawns a **fresh in-memory session per
round** — the protocol's whole point is no ambient memory — and records
the final answer, tokens, cost, duration, tool calls, and a tool-call
transcript after every run.

**Cost:** ~30 runs on `deepseek-v4-flash` totals roughly $0.03 and ~12
minutes. The full 257-question corpus is explicitly out of scope.

## How the dsos tools get into the session

The pi-mcp-adapter only auto-boots MCP servers it has cached from prior
interactive sessions (uncached ones sit behind an interactive consent
flow that a headless session can't satisfy). So the runner spawns the
dsos server itself (`mcp_client.mjs`, plain stdio JSON-RPC) and registers
its tools as custom pi tools. The server's `INSTRUCTIONS` are attached as
`promptGuidelines` so the ordered workflow reaches the model through the
system prompt — without that, custom tools are omitted from the prompt's
tool list and the agent sees only bare docstrings.

## Pilot result (pre-fix, 30 runs, flash model)

| | R1 | R2 | R3 |
|---|---|---|---|
| accuracy file / dsos | 100% / 100% | 80% / 80% | 60% / 40% |
| mean tokens file / dsos | 5.2k / **12.8k** | 6.5k / 7.2k | 10.2k / 10.1k |

**The agent called `search_artifacts` zero times in all 10 R2/R3 rounds.**
It re-registered the same CSV (7/10 rounds — now fixed by dedupe) and, in
R3, went `ls`/`find`/`read` through the filesystem first. With the raw CSV
sitting in cwd, one `ls` is strictly cheaper than a semantic search, so
the file baseline wins by default. The two arms that *did* search — the
hidden-file arm, where filesystem search had already failed — found and
reused prior artifacts correctly, which is the encouraging signal here.

Read this as: the reuse affordance does not overcome the filesystem prior
on its own. The product-side fixes that matter are the ones that make the
store *visible at session start*, not merely available.

## Fixes applied

Product (`dsos/`):
- **`save_artifact` dedupes** (`store.py`, `db.py`, `mcp_server.py`) — identical content returns the existing `row_id` with `deduplicated: true`; `dedupe=False` forces a real second copy. Covered by `tests/dedupe_smoke_test.py`, including the `content_hash` column migration on stores created before it.
- **`start_session` reports prior work** (`store.prior_work_signal`) — artifact count by type (seeded skills excluded, so a fresh store still reads as empty) plus up to 3 candidate row_ids, each labelled `keyword` (literal hit) or `semantic` (embedding only). No score threshold is applied on purpose: the fallback embedder separates a clearly relevant question from an irrelevant one by only ~0.39 vs ~0.30, so a cutoff would imply confidence it does not have. Weak candidates ship with an explicit "may well be irrelevant; verify" caveat. Covered by `tests/prior_work_smoke_test.py`.
- **Unknown `session_id` is rejected** (middleware, `store.session_exists`) — raised as `ToolError` so the message survives to the agent, rather than a bare "Internal server error". Prevents orphan `tool_calls` rows that no session owns, which silently undercount the reuse metric.

Harness (`benchmark/`):
- **Bridge injects server instructions** (`mcp_client.mjs`, `runner.mjs`) — the pilot's central confound.
- **dsos arm is store-only by default** — raw CSV removed after R1; `--keep-raw` restores the old behaviour as a diagnostic arm, `--no-file-tools` is the purist variant.
- **Workspaces isolated** under `%TEMP%/dsos-bench/<label>/<table>/` and keyed per arm label.
- **`metrics.py` reports orphan tool calls** so a future session-id regression is visible instead of silently undercounting reuse.
- **Workspace snapshots** recorded to `results/workspaces/` so the file arm's accumulated scratch stays inspectable.
- **Reuse is measured symmetrically** (H4, fixed) — the runner snapshots what each round inherits (`results/carry/`) and the file arm earns `reuse_calls` for reading back a file an earlier round wrote, the same way dsos earns credit for touching a prior session's artifact. Previously the file arm could only be debited, so reusing a derived file and redoing the work from scratch scored identically. A bare directory listing counts as navigation, not as touching derived work. Rounds with no carry record print `n/a` rather than `0`, so unmeasured reuse never reads as a measured zero.
- **Model recorded per run** (H5, partial) — every run stamps its model id and the report lists the models seen per arm, so a comparison can be shown to hold (or not) across models. Running a second model is still a cost decision and has not been done.

## Known limitations

- Only one model has been run (`deepseek-v4-flash`). A single weak model cannot separate "the affordance is weak" from "the model is weak", so nothing here should be generalised to other models until a second one is run.
- The depth chains have been built and spot-checked on 2 of 18 rounds, but **the full 36-run chain experiment has not been run**.
- Pilot data (5 tables × 3 rounds) is still on disk and is explicitly *not* current; it predates the arm redesign, the dedupe/prior-work fixes, the H4 metric, and the persist instruction.
- Per-category answers use the keyed tag form `@tag[key:value]` (e.g. `@median_price[Fair:3282.00]`), which DABench's flat convention cannot express. `score.py` parses both; flat tags are unchanged (verified: re-scoring the pilot reproduces identical results).
- The task is easy to differentiate *against* dsos: a 58KB CSV in cwd is the best case for files, and as the first post-fix attempt showed, it wins even with the workflow in the system prompt and a candidate in hand. Removing the file (now the dsos default) is what makes the comparison meaningful.

## Scoring rules

- Tags extracted with `@tag[value]` regex; every gold tag must match
  for the question to pass.
- Numeric values compare with absolute tolerance 0.02 **or** relative
  1% (whichever is larger) — `--abs-tol` / `--rel-tol` on `score.py`.
  Non-numeric values compare case-insensitively, stripped.

## What this does *not* measure

Discovery from the web, report publishing, lineage UX, human factors —
those stay with the live demo pairs in `examples/README.md`, which should
remain unrehearsed. This benchmark covers the reuse claim only.
