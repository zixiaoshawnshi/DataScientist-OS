# DS Artifact OS

An MCP server that makes queries, transforms, datasets, charts, and narratives
first-class, versioned, searchable artifacts — the working layer underneath
an agent's analysis, not a notebook or a dashboard. See
`Design/DS Artifact OS — Design Doc.md` for the full pitch, philosophy, data
model, and demo plan.

No release is cut — this runs from a local venv against the source tree.

## Status

- **Layer 1 (core library)** — `dsos/db.py`, `dsos/store.py`, `dsos/execution.py`, `dsos/embeddings.py`, `dsos/seed.py`. Built, tested.
- **Layer 2 (MCP server)** — `dsos/mcp_server.py`, including `publish_report`. Built, tested, wired into Claude Code and Pi.
- **Layer 3 (GUI, read-only)** — `dsos/gui.py`. Built, tested. Not an MCP tool — a separate process, run alongside the server (see below).

## Quickstart

```sh
python -m venv .venv
.venv/Scripts/pip install -e .

# layer 1: exercises store + execution via direct function calls
.venv/Scripts/python tests/smoke_test.py

# layer 2: exercises the MCP server over the real wire protocol
.venv/Scripts/python tests/mcp_smoke_test.py

# layer 2b: exercises publish_report over the real wire protocol
.venv/Scripts/python tests/publish_smoke_test.py

# layer 3: exercises the GUI's routes via FastAPI's TestClient
.venv/Scripts/python tests/gui_smoke_test.py
```

See `examples/README.md` for question pairs to try against a real agent
session once the server is wired in.

`-e .` is an editable install — no PyPI package, `dsos` just resolves via the
venv's own site-packages back to this source tree. That's also what lets the
MCP server run correctly from *any* directory (see below), without needing a
`PYTHONPATH`.

## Running the server standalone

```sh
.venv/Scripts/python -m dsos.mcp_server
```

Set `DSOS_DB_PATH` to an absolute path if you're launching this from outside
the repo (e.g. a coding agent open in an unrelated project) — otherwise it
defaults to `data/store.db` relative to wherever the process happens to be
launched, which silently scatters a fresh empty store into whatever directory
that is.

## Wiring into a coding agent

Both agents below use the identical command, so registering once makes it
available from any repo the agent opens — no per-project setup.

**Claude Code** (registered globally, one time):

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<repo>/data/store.db" \
  -- "<repo>/.venv/Scripts/python.exe" -m dsos.mcp_server
```

**Pi coding agent** (needs `pi install npm:pi-mcp-adapter` first; it reads the
standard tool-agnostic global config directly). Write `~/.config/mcp/mcp.json`:

```json
{
  "mcpServers": {
    "dsos": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["-m", "dsos.mcp_server"],
      "env": { "DSOS_DB_PATH": "<repo>/data/store.db" }
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

A read-only web view over the same store — sessions, the tool-call feed,
an artifact gallery/search, artifact detail (preview, execution trace,
lineage), and a Mermaid.js lineage graph. It's a separate process, not an
MCP tool: it reads `dsos.store` directly off the same SQLite file the MCP
server is writing to (safe under concurrent read/write — see `dsos/db.py`'s
`PRAGMA journal_mode=WAL`).

```sh
.venv/Scripts/python -m dsos.gui
```

Then open `http://127.0.0.1:8420`. Point `DSOS_DB_PATH` at the same file
you pointed the MCP server at (above) to browse the same live session.

## Publishing a report

`publish_report(row_id, session_id)` is an MCP tool the agent calls once
it has a `narrative` artifact (saved via `save_artifact` with
`{{artifact:row_id}}` embeds). It renders that narrative and everything it
embeds — datasets/queries/transforms as HTML tables, charts as inlined
base64 images — into one self-contained local `.html` file (default:
`data/reports/`). This is the seam where a finished report leaves the
working layer; it does not itself create a new artifact row.

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
  seed.py        bootstrap discovery-skill artifact
  present.py     shared artifact-payload rendering — used by both the MCP
                 server and the GUI so they can't drift apart
  mcp_server.py  FastMCP wrapper exposing the tools + a middleware that
                 auto-logs every tool call
  publish.py     renders a narrative + its embeds into one self-contained
                 .html file; wrapped as the publish_report MCP tool
  gui.py         read-only FastAPI+Jinja2+htmx browser over the store —
                 a separate process, not an MCP tool
  templates/     Jinja2 templates for the GUI
tests/
  smoke_test.py         layer 1 — direct function calls, no protocol
  mcp_smoke_test.py     layer 2 — real MCP wire protocol via FastMCP's Client
  publish_smoke_test.py layer 2b — publish_report over the real MCP wire
  gui_smoke_test.py     layer 3 — GUI routes via FastAPI's TestClient
Design/
  DS Artifact OS — Design Doc.md   philosophy, data model, MCP tool list,
                                    demo plan
```
