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

## 4. Start the daemon, then register the MCP server

Find the python executable from step 1 first (the venv's, not a system
one) — call it `<python>` below, the chosen path from step 2 `<db>`, and
the interpreter from step 3 `<analysis-python>`.

**First, start the daemon.** The store is owned by a long-lived process, not
by the MCP server: one `python -m dsos.daemon` per store, serving the GUI and
both MCP profiles off one database connection. `dsos.mcp_server` is now only
a shim — it finds that daemon and forwards to it over stdio, which is what
lets a stdio-only client talk to a server that is not a subprocess of its
own. Without a daemon, the shim exits immediately and says so.

```sh
"<python>" -m dsos.daemon
```

It prints its base URL and where the store is, and writes `daemon.json` and
`daemon.token` next to the store — that is how the shim finds it, so the
daemon and the client must agree on `DSOS_DB_PATH`. Leave it running for as
long as you want dsos available; it serves the read-only GUI at
`http://127.0.0.1:8765/` too.

**Claude Code** (the shim, over stdio):

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<db>" \
  -e DSOS_PYTHON_PATH="<analysis-python>" \
  -- "<python>" -m dsos.mcp_server --profile producer
```

**Pi coding agent** (needs `pi install npm:pi-mcp-adapter` first). Add to
`~/.config/mcp/mcp.json`:

```json
{
  "mcpServers": {
    "dsos": {
      "command": "<python>",
      "args": ["-m", "dsos.mcp_server", "--profile", "producer"],
      "env": { "DSOS_DB_PATH": "<db>", "DSOS_PYTHON_PATH": "<analysis-python>" }
    }
  }
}
```

**Any other MCP-compatible client** (Claude Desktop, Cursor, etc.): the
same `command` / `args` / `env` shape applies — adapt to that client's own
config file location and schema.

**If your client speaks HTTP, skip the shim** and register the endpoint
directly. One fewer process, and no proxy hop per tool call:

```sh
claude mcp add --transport http -s user dsos \
  http://127.0.0.1:8765/mcp/producer/ \
  --header "Authorization: Bearer <token>"
```

`<token>` is what the daemon printed, or the contents of the `daemon.token`
file next to the store. If `DSOS_TOKEN` is set in the daemon's environment
instead of written to a file, pass that same value here.

Keep the **trailing slash**. The daemon mounts the MCP app with `path="/"`,
so `/mcp/producer` answers a `307` redirect to `/mcp/producer/` before the
auth check even runs. Clients that follow redirects cope; ones that don't
report the server as broken. (An unauthenticated POST to the slashless form
therefore gets a `307`, not the `401` you would expect — check auth against
the real endpoint.)

**For a consumer agent** — a PM or reviewer role that reads results and asks
questions rather than running analysis — use the other profile:

```sh
claude mcp add dsos-consumer -s user \
  -e DSOS_DB_PATH="<db>" \
  -- "<python>" -m dsos.mcp_server --profile consumer
```

It reaches `/mcp/consumer/`, which is mounted and authenticated today but
serves an empty tool set — its read-oriented tools arrive in a later
release. Register it now and it will be there when they land; register both
profiles if you want both roles.

## 5. Verify

Restart/reconnect the agent, then call **`start_session`** with any question
you actually care about:

```json
{"question": "Is dsos wired up?"}
```

It should return a `session_id`. `start_session` is the one producer tool
that needs no prior state, which makes it the only honest liveness check on
a store you have not written to yet — every other tool takes a `session_id`
it does not have. It is also the better check, not just the easier one: a
successful call exercises the entire path (daemon, bearer token, shim, a
real write into the store), so it is more evidence of a working setup than
any read would be.

Then confirm reads come back over the same path, reusing the id:

```json
{"query": "dsos", "session_id": "<the id start_session returned>", "top_k": 5}
```

An empty result list is the correct answer on a fresh store and still means
the call worked — what you are checking is that it returns at all.

**Consumer profile:** there is nothing to call yet. `/mcp/consumer/` is
mounted and authenticated, but it serves an empty tool set today; its
read-oriented tools arrive in a later release. So the check is that it
connects — a `list_tools` that returns cleanly with zero tools is the
expected result, not a failure. Once the tools land, call one of those
instead.

If a call on the *producer* comes back with an empty tool list, the shim
connected to nothing: check that the daemon is still running (open
`http://127.0.0.1:8765/healthz`, which answers `{ok, version, db_path}` with
no token) and that the client and the daemon were given the same
`DSOS_DB_PATH`. Launching the shim by hand prints the reason on stderr if it
cannot find a daemon at all.

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
