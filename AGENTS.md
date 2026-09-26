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

## 3. Register the MCP server

Find the python executable from step 1 first (the venv's, not a system
one) — call it `<python>` below, and the chosen path from step 2 `<db>`.

**Claude Code:**

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<db>" \
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
      "env": { "DSOS_DB_PATH": "<db>" }
    }
  }
}
```

**Any other MCP-compatible client** (Claude Desktop, Cursor, etc.): the
same `command` / `args` / `env` shape applies — adapt to that client's own
config file location and schema.

## 4. Verify

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
