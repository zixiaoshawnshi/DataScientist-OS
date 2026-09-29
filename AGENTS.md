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

`DSOS_PYTHON_PATH` is a **daemon** setting, not an MCP-client one: every
`run_python` call executes in the daemon (step 4), which reads the variable
once, at start-up. Setting it in an MCP client's registration does nothing,
because the stdio shim that client launches never reads it. To change it,
restart the daemon.

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

The daemon is the process that owns the store and runs the analysis, so it
is the one that needs **both** settings: `DSOS_DB_PATH` (which store) and
`DSOS_PYTHON_PATH` (which interpreter `run_python` uses by default). Set
them in the daemon's own environment. A daemon started with no
`DSOS_DB_PATH` opens `data/store.db` under whatever directory you launched
it from — exactly the pitfall step 2 warns about.

macOS/Linux (POSIX shell):

```sh
DSOS_DB_PATH="<db>" DSOS_PYTHON_PATH="<analysis-python>" "<python>" -m dsos.daemon
```

Windows (PowerShell):

```powershell
$env:DSOS_DB_PATH = "<db>"; $env:DSOS_PYTHON_PATH = "<analysis-python>"; & "<python>" -m dsos.daemon
```

It prints its channel, base URL and store, and writes a manifest named
after the store (`store.db.daemon.json` for `store.db`) and `daemon.token`
next to it — that is how the shim finds it, so the
daemon and the client must agree on `DSOS_DB_PATH` (the shim checks: a
daemon serving a different store is reported as "not running for `<db>`",
with both paths named). Leave it running for as long as you want dsos
available; it serves the read-only GUI at its base URL too.

**Ports: prod and dev never share one.** The daemon knows which channel it
is — `prod` for a released install, `dev` for a source checkout (override
with `DSOS_CHANNEL=prod|dev`) — and with no `--port`/`DSOS_PORT` it takes
the first free port in that channel's range: **prod 8765–8779, dev
8780–8799**. Clients never need the number (the shim reads it from the
manifest), so let it choose unless you register the HTTP endpoint directly.
An explicit port is used exactly: if it is taken, the daemon refuses before
opening the store and names who holds it.

**Released installs: start them with `python -P`.** `python -m` puts the
current directory first on `sys.path`, and MCP clients launch servers from
the project directory. Opened inside a dsos checkout, a release's
`-m dsos.mcp_server` then imports the *checkout's* code instead of its own.
`-P` (Python 3.11+) turns that off; use it in every command below for a
release (`"<python>" -P -m ...`). A source checkout does not need it.

Relative paths in tool calls — `content_path` on `save_artifact`,
`code_paths` and `python_path` on `run_python` — are resolved by the
daemon, against the directory the *daemon* was started from, not the
agent's. Pass absolute paths.

**Claude Code** (the shim, over stdio). The shim needs only `DSOS_DB_PATH`,
to find the store's manifest (`<store>.daemon.json`) and `daemon.token`
beside it; everything else
is the daemon's:

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<db>" \
  -- "<python>" -m dsos.mcp_server --profile producer
```

**Pi coding agent** (needs `pi install npm:pi-mcp-adapter` first). Add to
`~/.pi/agent/mcp.json` (pi also reads the shared `~/.config/mcp/mcp.json`;
define each server name in only one of the two, or the entries shadow each
other):

```json
{
  "mcpServers": {
    "dsos": {
      "command": "<python>",
      "args": ["-m", "dsos.mcp_server", "--profile", "producer"],
      "env": { "DSOS_DB_PATH": "<db>" }
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
  http://127.0.0.1:<port>/mcp/producer/ \
  --header "Authorization: Bearer <token>"
```

`<port>` is the one the daemon printed; pin it with `--port` for an HTTP
registration, since an auto-chosen port can differ after a restart.
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

It reaches `/mcp/consumer/`, which serves four read-oriented tools:
`find_evidence` (the stated results that speak to a claim, with the numbers
behind them), `get_claim` (one result in full, with its validation and
derivation), `cite` (a pasteable reference plus a link to the result's GUI
page) and `ask` (put a question the store cannot answer on the board for an
analysis agent). None of them runs code or writes a finding. Register both
profiles if you want both roles; the HTTP form works here too, at
`http://127.0.0.1:<port>/mcp/consumer/` with the same bearer token.

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

**Consumer profile:** call **`find_evidence`** with any claim:

```json
{"claim": "dsos is wired up"}
```

On a fresh store the correct answer is an empty `results` list plus a
`hint` suggesting `ask` — that still means the call went all the way
through. It needs no session and no prior state, so it is the consumer's
liveness check the way `start_session` is the producer's.

If either profile comes back with an empty tool list, the shim connected to
nothing: check that the daemon is still running (open
`<base_url>/healthz` — `base_url` is in `<store>.daemon.json` — which
answers `{ok, version, db_path, channel}` with
no token), and that its `db_path` is the store you registered — the client
and the daemon must be given the same `DSOS_DB_PATH`. Launching the shim by
hand prints the reason on stderr: no daemon at all, or a daemon that is
serving a different store (it names both).

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
  `DSOS_DB_PATH` — relative resolves against the daemon's launch cwd, not
  the caller's. The same goes for any path passed in a tool call.
- Both of those belong in the **daemon's** environment. The shim reads only
  `DSOS_DB_PATH` (plus the optional `DSOS_URL`/`DSOS_TOKEN` overrides, which
  skip the manifest and `daemon.token`; if `DSOS_DB_PATH` is set too, the
  daemon at `DSOS_URL` must be serving that store), and an HTTP
  registration reads nothing at all.
- `run_python` code executes with whatever ambient permissions the
  `DSOS_PYTHON_PATH` environment has (e.g. cloud credentials configured in
  one venv but not another) — worth a beat of thought when picking it,
  same as any other "run arbitrary code" tool.
