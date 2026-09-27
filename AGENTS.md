# AGENTS.md

Instructions for a coding agent that needs to **install and wire up DS
Artifact OS (`dsos`) as an MCP server** — either for the user working in
this repo, or for a user in an unrelated repo who wants `dsos` available
globally. See `README.md` for what the project does and does not do;
this file is just the install/wiring recipe.

## 1. Install

No PyPI package — install straight from GitHub. Prefer a dedicated venv,
not the system/user Python:

```sh
python -m venv .dsos-venv
# Windows: .dsos-venv/Scripts/pip
# macOS/Linux: .dsos-venv/bin/pip
.dsos-venv/Scripts/pip install "git+https://github.com/zixiaoshawnshi/DataScientist-OS.git@latest"
```

`@latest` is a floating git tag maintained by the maintainer to always
point at the newest tagged release (not `main`, which is unreleased dev
work). Pin `@vX.Y.Z` instead if the user wants a reproducible install that
won't shift later — check `https://github.com/zixiaoshawnshi/DataScientist-OS/releases`
for the current tag list.

If you're already working inside a checkout of this repo (i.e. this file
is what you're reading), use the editable install instead so the MCP
server runs against the source tree:

```sh
python -m venv .venv
.venv/Scripts/pip install -e .
```

## 2. Pick a database path

`dsos` stores everything in one SQLite file, path controlled by the
`DSOS_DB_PATH` env var. Decide up front whether this should be:

- **Global** (shared across every repo/session the agent opens) —
  something like `~/.dsos/store.db`. This matches the project's own
  intended usage (a working layer, not a per-repo tool).
- **Repo-scoped** — `<repo>/data/store.db`.

Use an **absolute path**. If `DSOS_DB_PATH` is unset, the server defaults
to `data/store.db` relative to whatever directory the process happens to
launch from, which silently creates a fresh empty store in the wrong place
the moment the agent is invoked from a different cwd.

## 3. Pick a default analysis Python

`run_python` never execs code inside the MCP server itself — every call
runs in a subprocess against a configured interpreter, `DSOS_PYTHON_PATH`.
This is a *default only*: an individual `run_python` call can still
override it per-call via `python_path=` (e.g. to target a specific repo's
own venv), so don't over-think getting this exactly right.

Resolve or ask the user for the Python they already do analysis in — the
one with pandas/matplotlib/etc. already installed: `which python` /
`where python`, their usual conda env, or whatever they name. Unlike the
server's own interpreter above (must be the venv's, never a bare
`python`/`python3` off `PATH`), `DSOS_PYTHON_PATH` is deliberately whatever
full-featured Python the user already has — reusing it is the point, so
most `run_python` calls need no extra setup at all. It needs at least
pandas and pyarrow; if it's missing something a particular call needs, the
agent can pass `requirements=[...]` on that call to fill the gap via `uv`
rather than reinstalling globally.

If the user has no such interpreter to point at (or wants full isolation
instead of reusing one), fall back to today's approach: install the
analysis stack into the dedicated venv from step 1 —
`.dsos-venv/Scripts/pip install "dsos[analysis] @ git+https://github.com/zixiaoshawnshi/DataScientist-OS.git@latest"`
(or `.venv/Scripts/pip install -e ".[analysis]"` for the editable-install
case) — and point `DSOS_PYTHON_PATH` at that same venv's interpreter.

## 4. Register the MCP server

Find the python executable from step 1 first (the venv's, not a system
one) — call it `<python>` below, the chosen path from step 2 `<db>`, and
the interpreter from step 3 `<analysis-python>`.

**Claude Code:**

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<db>" \
  -e DSOS_PYTHON_PATH="<analysis-python>" \
  -- "<python>" -m dsos.mcp_server
```

**Pi coding agent** (needs `pi install npm:pi-mcp-adapter` first). Add to
`~/.config/mcp/mcp.json`:

```json
{
  "mcpServers": {
    "dsos": {
      "command": "<python>",
      "args": ["-m", "dsos.mcp_server"],
      "env": { "DSOS_DB_PATH": "<db>", "DSOS_PYTHON_PATH": "<analysis-python>" }
    }
  }
}
```

**Any other MCP-compatible client** (Claude Desktop, Cursor, etc.): the
same `command` / `args` / `env` shape applies — adapt to that client's own
config file location and schema.

## 5. Verify

Restart/reconnect the agent, then confirm the server is live by calling
one read-only tool, e.g. `list_skills` or `list_templates`. Both should
return without error on a fresh store (skills are seeded automatically on
first use).

## Notes for the agent doing the installing

- Don't guess a python — always resolve the venv's own interpreter path,
  not whatever `python`/`python3` resolves to on `PATH`.
- Don't overwrite an existing `dsos` MCP registration without telling the
  user — check first (`claude mcp list`, or read the target JSON config)
  since a different `DSOS_DB_PATH` there means a different store already
  has data in it.
- This project has no CLI and no HTTP API beyond the read-only GUI
  (`python -m dsos.gui`, see README) — MCP tool calls are the only way to
  read or write artifacts.
- Use an **absolute path** for `DSOS_PYTHON_PATH` too, same reasoning as
  `DSOS_DB_PATH` — relative resolves against the server's launch cwd, not
  the caller's.
- `run_python` code executes with whatever ambient permissions the
  `DSOS_PYTHON_PATH` environment has (e.g. cloud credentials configured in
  one venv but not another) — worth a beat of thought when picking it,
  same as any other "run arbitrary code" tool.
