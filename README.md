# DS Artifact OS

An MCP server that makes queries, transforms, datasets, charts, and narratives
first-class, versioned, searchable artifacts — the working layer underneath
an agent's analysis, not a notebook or a dashboard. See
`Design/DS Artifact OS — Design Doc.md` for the full pitch, philosophy, data
model, and demo plan.

Runs from a local venv against the source tree during development; tagged
releases (currently `v0.5.0`) are citable checkpoints — see "Installing a
released version" below.

## Status

- **Layer 1 (core library)** — `dsos/db.py`, `dsos/store.py`, `dsos/execution.py`, `dsos/embeddings.py`, `dsos/seed.py`. Built, tested.
- **Layer 2 (MCP server)** — `dsos/mcp_server.py`, including `publish_report`. Built, tested, wired into Claude Code and Pi.
- **Layer 3 (GUI, read-only)** — `dsos/gui.py`. Built, tested. Not an MCP tool — a separate process, run alongside the server (see below).

## Quickstart

```sh
python -m venv .venv
.venv/Scripts/pip install -e ".[analysis]"

# the whole suite: every tests/*_test.py, each in its own subprocess
.venv/Scripts/python tests/run_all.py

# just the ones whose filename contains a substring
.venv/Scripts/python tests/run_all.py --only lifecycle
```

`run_all.py` prints one pass/fail line per script with its duration, then the
full output of anything that failed, and exits 1 if any script failed — that
is the command to run, rather than the individual scripts. Use
`--only <substring>` to narrow it to one area while iterating; run the whole
thing before you call a change done.

See `examples/README.md` for question pairs to try against a real agent
session once the server is wired in.

`-e .` is an editable install — no PyPI package, `dsos` just resolves via the
venv's own site-packages back to this source tree. That's also what lets the
MCP server run correctly from *any* directory (see below), without needing a
`PYTHONPATH`.

## Installing a released version

No PyPI package yet — install straight from a tagged commit instead of
tracking `main`:

```sh
pip install "git+https://github.com/zixiaoshawnshi/DataScientist-OS.git@latest"
```

`latest` is a git tag we force-move to the newest tagged release on every
cut (see "Cutting a release" below) — it's not a real git "latest release"
feature (git/pip have no such concept for `git+https` installs), just a
floating alias so this command never needs hand-editing. Pin an explicit
`@vX.Y.Z` instead if you want a reproducible install that won't shift under
you later (currently `v0.5.0`).

This is a real (non-editable) build, not the editable install above —
verified against a throwaway venv as part of cutting each release, since an
editable install hides packaging bugs (a missing file in `package-data`,
for instance) that only surface on a real build.

If you installed this way, the server checks GitHub's Releases API on
startup (best-effort, cached for 6 hours, never blocks or fails startup)
and prepends a short update notice to its MCP `instructions` if a newer
tagged release exists (`dsos/update_check.py`). It's skipped entirely for
an editable dev install (`pip install -e .`) — main is routinely ahead of
the last tagged release, so that comparison would be backwards for anyone
tracking main directly.

### Cutting a release

1. Bump `version` in `pyproject.toml` to match the new tag — the update
   check above compares against this value, so a forgotten bump makes every
   future release invisible to it.
2. Verify with a real (non-editable) install into a throwaway venv (not the
   dev one), same as above.
3. `git tag -a vX.Y.Z -m "..."`, `git push origin vX.Y.Z`.
4. `gh release create vX.Y.Z --notes-file ...` (or the GitHub web UI).
5. Move the `latest` alias to the same commit and force-push it:
   `git tag -f latest vX.Y.Z && git push origin latest --force`. Skipping
   this step is exactly what makes the `@latest` install command above
   silently stale.

## Running the server standalone

The store is owned by one long-lived daemon; `python -m dsos.mcp_server` is
only a stdio shim that forwards to it — and **starts it** the first time a
client needs it, with the client's `DSOS_DB_PATH` and `DSOS_PYTHON_PATH`.
So registering the shim (below) is enough; the daemon then keeps running,
detached, for every later client and the GUI, and logs to
`<store>.daemon.log`.

To run it yourself instead (a service, a login task), start it with the
store and the default analysis interpreter in *its* environment, and set
`DSOS_NO_AUTOSTART=1` on the registrations:

```sh
DSOS_DB_PATH="<abs path>/store.db" DSOS_PYTHON_PATH="<analysis python>" \
  .venv/Scripts/python -m dsos.daemon
```

(PowerShell: `$env:DSOS_DB_PATH = "..."; $env:DSOS_PYTHON_PATH = "..."; & .venv/Scripts/python -m dsos.daemon`.)

Use an absolute `DSOS_DB_PATH` — otherwise the daemon defaults to
`data/store.db` relative to wherever it happens to be launched, which
silently scatters a fresh empty store into whatever directory that is (the
shim refuses to start a daemon without one for exactly this reason). A
released install's daemon takes a port in 8765–8779, a source checkout's in
8780–8799, so the two never collide.

## Wiring into a coding agent

See `AGENTS.md` for the install-and-wire-up recipe written for a coding
agent to follow directly (e.g. "install dsos and wire it into Claude
Code"). The summary, for a human doing it manually:

Both agents below use the identical command, so registering once makes it
available from any repo the agent opens — no per-project setup. Both launch
the shim, which starts the store's daemon if none is running; the
registration's environment is what that daemon is started with. For a
released install add `-P` before `-m` (it stops the working directory
shadowing the installed package — see AGENTS.md).

**Claude Code** (registered globally, one time):

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<repo>/data/store.db" \
  -e DSOS_PYTHON_PATH="<analysis python>" \
  -- "<repo>/.venv/Scripts/python.exe" -m dsos.mcp_server
```

**Pi coding agent** (needs `pi install npm:pi-mcp-adapter` first). Write
`~/.pi/agent/mcp.json` (pi also reads the shared `~/.config/mcp/mcp.json`;
define each server name in only one of the two):

```json
{
  "mcpServers": {
    "dsos": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["-m", "dsos.mcp_server"],
      "env": { "DSOS_DB_PATH": "<repo>/data/store.db",
               "DSOS_PYTHON_PATH": "<analysis python>" }
    }
  }
}
```

Both configs point at the same store, shared globally across every repo you
open the agent in — matching the design doc's "working layer" framing (your
own working memory, not something scoped per codebase). Point `DSOS_DB_PATH`
at a different file if you want a repo-scoped store instead.

Before a real demo: point `DSOS_DB_PATH` at a fresh file, or delete the
existing one — otherwise dev/test traffic is already sitting in the store the
"round 1 starts from empty" demo beat depends on.

## Browsing the store (GUI)

A read-only web view over the same store — a persistent left nav (sessions,
all artifacts, quick per-type filters) around sessions, the tool-call feed,
an artifact gallery/search, and artifact detail (preview, execution trace),
with an inline Lineage tab (Mermaid.js graph, no separate page to navigate
to). It's a separate process, not an MCP tool: it reads `dsos.store`
directly off the same SQLite file the MCP server is writing to (safe under
concurrent read/write — see `dsos/db.py`'s `PRAGMA journal_mode=WAL`).

```sh
.venv/Scripts/python -m dsos.gui
```

Then open `http://127.0.0.1:8420`. Point `DSOS_DB_PATH` at the same file
you pointed the MCP server at (above) to browse the same live session.

A narrative artifact's detail page also links to `/artifacts/{row_id}/report`
— its published report rendered live by this same process (same rendering
core as `publish_report`, just served over HTTP instead of read from a
local file; nothing is written to disk on this path, so it's always
current).

## Publishing a report

`publish_report(row_id, session_id)` is an MCP tool the agent calls once
it has a `narrative` artifact (saved via `save_artifact` with
`{{artifact:row_id}}` embeds). It renders that narrative and everything it
embeds — datasets/queries/transforms as HTML tables, charts as inlined
base64 images — into one self-contained local `.html` file (default:
`data/reports/`). This is the seam where a finished report leaves the
working layer; it does not itself create a new artifact row.

`publish_report(..., dry_run=True)` resolves every `{{artifact:...}}` embed
and reports which ones exist and which are broken/stale, without writing a
file — a cheap check before spending a real publish.

`publish_report(..., template=...)` picks the report layout — the consistency
layer, see below.

## Skills and templates

Two consistency layers ship with the store, both built on the same artifact
machinery (versioned, searchable, lineage-tracked) rather than new subsystems:

**The skill library** — common workflow skills (dataset discovery, exploratory
analysis, charting, statistics, modeling, reporting), seeded at store-init
and listed via `list_skills`. They teach *workflow* — how to drive the tools
so work stays addressable and reusable — not prescriptive methodology. Read
one with `get_artifact`, follow it, and customize the system's behavior with
`save_skill`: an edit is a new version of the same skill (stable
`artifact_id`), and re-seeding never overwrites it.

**Templates** — chart styles and report layouts:

- `run_python(..., style=...)`: matplotlib rcParams applied *before* your code
  runs. Default `"dsos"` is the dark house style; `"report"` is the same look
  sized for charts embedded in published reports; `"minimal"` is a bare light
  style; `style=None` gives raw matplotlib defaults (every run resets, so
  styles never leak between runs).
- `publish_report(..., template=...)`: the HTML layout around the rendered
  narrative. Default `"report"` is the dark house layout (matching the chart
  style); `"default"` is the original plain look; `"minimal"` is bare HTML.
- `list_templates(kind=...)` shows what exists; `save_template(kind=...,
  base=...)` creates or re-versions a custom one. `base` copies an existing
  template so you customize instead of rewriting, and custom templates are
  referenced by stable `artifact_id` — an edit keeps the id, references keep
  resolving. Custom chart styles are validated against matplotlib's own
  parser at save time; custom report templates must declare where the body
  goes. Both fail in one call, not at first use.

## Project layout

```
dsos/
  db.py          schema + connection helper (SQLite)
  embeddings.py  semantic search — real model optional, dependency-free
                 hashing fallback by default
  store.py       artifact save/get/search, lineage, sessions, tool-call
                 log, reuse detection
  execution.py   run_sql (DuckDB) / run_python — auto-records each run and
                 auto-links its output into lineage
  seed.py        seeds the skill library (idempotently — an agent-edited
                 skill is never re-seeded over)
  templating.py  the template layer: chart-style + report-template
                 resolution/validation/rendering (built-ins live in assets/)
  present.py     shared artifact-payload rendering — used by both the MCP
                 server and the GUI so they can't drift apart
  mcp_server.py  the stdio shim: finds (or starts) the daemon and proxies one
                 profile to it, plus the lazily-built `mcp` the tests import
  autostart.py   starts a detached daemon for a store when the shim needs one
  daemon.py      the process that owns the store — GUI + both MCP profiles
                 off one Database, one per store, guarded by <store>.daemon.json
  channel.py     prod (released install) vs dev (source checkout): port ranges
  paths.py       one canonical spelling for a store path
  server/        the MCP layer proper: build_producer/build_consumer over a
                 ServerConfig, the tool-call logger, the instructions, and
                 contract.py (renders the tool table into Doc II)
  ingest.py      dataset ingestion shared by the server and the GUI
  sandbox.py     the per-call filesystem/workspace boundary for run_python
  publish.py     renders a narrative + its embeds into HTML via a template
                 (render_report_html); publish_report (the MCP tool) writes
                 that to one self-contained .html file, the GUI's
                 /artifacts/{row_id}/report route serves it live instead
  assets/        built-in chart styles (.mplstyle) and report layouts
                 (.html) — package files, not artifacts; customs are
                 `template` artifacts
  gui.py         read-only FastAPI+Jinja2+htmx browser over the store —
                 a router mounted by the daemon, still runnable standalone
                 with `python -m dsos.gui`
  templates/     Jinja2 templates for the GUI
  update_check.py  best-effort "a newer release exists" notice for people
                 on an installed release, prepended to the MCP instructions;
                 a no-op for editable dev installs (see Cutting a release)
tests/
  run_all.py                 runs every script below, one subprocess each
  smoke_test.py              layer 1 — direct function calls, no protocol
  mcp_smoke_test.py          layer 2 — real MCP wire protocol via FastMCP's Client
  publish_smoke_test.py      layer 2b — publish_report over the real MCP wire
  skills_templates_smoke_test.py
                             layer 2c — skill library + templates over the wire
  gui_smoke_test.py          layer 3 — GUI routes via FastAPI's TestClient
  update_check_smoke_test.py
                             update_check.py, with the network call mocked
  migrations_smoke_test.py   numbered migrations, backups, refusal to downgrade
  database_concurrency_smoke_test.py
                             one Database per process, serialised writers
  input_binding_smoke_test.py  inputs bound as in_1..in_N, not by title
  dedupe_smoke_test.py       content-hash dedupe on save_artifact
  execution_hygiene_smoke_test.py
                             a failed run is an execution, not an artifact
  surface_smoke_test.py      the exact producer tool list
  prior_work_smoke_test.py   reuse detection across sessions
  freshness_smoke_test.py    staleness derived from source.refresh_after
  layering_smoke_test.py     dsos/server/ has no module-level connection
  contract_smoke_test.py     Doc II's tool tables are the generated ones
  daemon_smoke_test.py       the daemon: auth, both profiles, one store
  shim_smoke_test.py         dsos.mcp_server proxies the daemon, owns no store
  autostart_smoke_test.py    the shim starts one detached daemon per store
  channel_smoke_test.py      prod/dev port ranges; busy ports refused early
Design/
  DS Artifact OS — Design Doc.md   philosophy, data model, MCP tool list,
                                    demo plan
  DS Artifact OS — Design Doc II (Spine & Scale).md
                                    the daemon, the lifecycle, the consumer
  DS Artifact OS — TTD (Spine & Scale).md
                                    the implementation breakdown, one WP each
benchmark/
  metrics.py, report.py, score.py, runner.mjs, arms.json
                             the measurement harness; see benchmark/README.md
```
