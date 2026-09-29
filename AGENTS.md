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

`DSOS_PYTHON_PATH` is read by the **daemon** (step 4), once, when it starts:
every `run_python` call executes there. Put it in the MCP registration's
environment anyway — the shim passes its environment to the daemon it
starts. A daemon that is already running keeps the value it started with;
restart it to change it.

## 4. Register the MCP server (it starts the daemon for you)

Find the python executable from step 1 first (the venv's, not a system
one) — call it `<python>` below, the chosen path from step 2 `<db>`, and
the interpreter from step 3 `<analysis-python>`.

**How it runs.** The store is owned by one long-lived process per store,
the daemon (`python -m dsos.daemon`), which serves the GUI and both MCP
profiles. What an MCP client launches, `python -m dsos.mcp_server`, is a
thin stdio shim that forwards to it. **The shim starts the daemon itself**
the first time a client needs one: registering the shim is the whole
install. The daemon then keeps running, detached, for every later client
and the GUI; it is not tied to the client that happened to start it.

The registration's environment is the configuration:

- `DSOS_DB_PATH` — **required, absolute.** Which store. The shim will not
  start a daemon without it: the fallback is `data/store.db` under whatever
  directory the client launched from, and a daemon there would quietly
  create a new, empty store in some project folder.
- `DSOS_PYTHON_PATH` — the analysis interpreter from step 3.
- Optional: `DSOS_PORT` (default: the first free port in the channel's
  range, below), `DSOS_NO_AUTOSTART=1` (never start a daemon; see
  "Managing the daemon yourself"), `DSOS_AUTOSTART_TIMEOUT` (seconds to
  wait for a first start, default 90).

**Claude Code** (the shim, over stdio):

```sh
claude mcp add dsos -s user \
  -e DSOS_DB_PATH="<db>" \
  -e DSOS_PYTHON_PATH="<analysis-python>" \
  -- "<python>" -P -m dsos.mcp_server --profile producer
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
      "args": ["-P", "-m", "dsos.mcp_server", "--profile", "producer"],
      "env": { "DSOS_DB_PATH": "<db>", "DSOS_PYTHON_PATH": "<analysis-python>" }
    }
  }
}
```

**`-P` is for released installs** (drop it for a source checkout, where it
is harmless but unneeded). `python -m` puts the current directory first on
`sys.path`, and MCP clients launch servers from the project directory:
opened inside a dsos checkout, a release's `-m dsos.mcp_server` would
import the *checkout's* code instead of its own. `-P` (Python 3.11+) turns
that off. The daemon the shim starts is always started with `-P`.

**What you will see beside the store** once a daemon has run:
`<store>.daemon.json` (the manifest: pid, port, base URL, channel — how
shims find it), `daemon.token` (the bearer token, one per directory), and
`<store>.daemon.log` (the daemon's output — read this first if a client
reports that the daemon did not start). The GUI is at the base URL in the
manifest.

**Ports: prod and dev never share one.** The daemon knows which channel it
is — `prod` for a released install, `dev` for a source checkout (override
with `DSOS_CHANNEL=prod|dev`) — and with no `--port`/`DSOS_PORT` it takes
the first free port in that channel's range: **prod 8765–8779, dev
8780–8799**. Clients never need the number (the shim reads it from the
manifest), so let it choose unless you register the HTTP endpoint directly.
An explicit port is used exactly: if it is taken, the daemon refuses before
opening the store and names who holds it.

Relative paths in tool calls — `content_path` on `save_artifact`,
`code_paths` and `python_path` on `run_python` — are resolved by the
daemon, which runs from the store's directory, not the agent's. Pass
absolute paths.

**Managing the daemon yourself** (a service, a login task, a different
interpreter): set `DSOS_NO_AUTOSTART=1` on the registrations and start it
with the same two settings:

```sh
DSOS_DB_PATH="<db>" DSOS_PYTHON_PATH="<analysis-python>" "<python>" -P -m dsos.daemon
```

```powershell
$env:DSOS_DB_PATH = "<db>"; $env:DSOS_PYTHON_PATH = "<analysis-python>"; & "<python>" -P -m dsos.daemon
```

To stop a daemon, end the process whose pid is in `<store>.daemon.json`;
the next client that needs it starts a fresh one (unless autostart is off).

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
  -e DSOS_PYTHON_PATH="<analysis-python>" \
  -- "<python>" -P -m dsos.mcp_server --profile consumer
```

It reaches `/mcp/consumer/`, which serves four read-oriented tools:
`find_evidence` (the stated results that speak to a claim, with the numbers
behind them), `get_claim` (one result in full, with its validation and
derivation), `cite` (a pasteable reference plus a link to the result's GUI
page) and `ask` (put a question the store cannot answer on the board for an
analysis agent). None of them runs code or writes a finding. Register both
profiles if you want both roles — both shims share one daemon, whichever
starts first. The HTTP form works here too, at
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

The first call after registering can take several seconds: the shim is
starting the daemon, which opens (and, for an older store, migrates) the
store before it answers.

If a profile fails to connect or comes back with an empty tool list:

- Read `<store>.daemon.log` beside the store — a daemon that failed to
  start says why there, and the shim repeats its last lines on stderr.
- Open `<base_url>/healthz` (`base_url` is in `<store>.daemon.json`); it
  answers `{ok, version, db_path, channel}` with no token. Its `db_path`
  must be the store you registered.
- Launch the shim by hand with the registration's environment; it prints
  the reason on stderr — no `DSOS_DB_PATH` (so it would not start a
  daemon), autostart turned off, or a daemon serving a different store (it
  names both).

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
