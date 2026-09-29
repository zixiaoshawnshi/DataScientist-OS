"""What both server profiles need: the config they are built from, the
tool-call log, and the MCP-only parts of a response.

Nothing in here opens a store. A `ServerConfig` is handed *in*, already
holding a `Database` (decision D8: one per process, one connection per
thread, one process-wide write lock), and the two things that touch it —
`ToolCallLogger` and the tool closures in producer.py — ask that handle for
a connection when they need one. That is the whole point of the package:
before this, the connection was a module global, so a process could not hold
two servers over two stores and importing the server opened a database.
"""

from __future__ import annotations

import contextvars
import json
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from dsos import execution, present, store
from dsos.db import Database

# Reported to the client in the MCP initialize handshake's serverInfo
# (protocol-standard field; clients read it via getServerVersion()).
PRODUCER_NAME = "DS Artifact OS"
CONSUMER_NAME = "DS Artifact OS (consumer)"


def server_version() -> str:
    """The installed dsos version, or a dev placeholder when the package
    metadata isn't visible (a source checkout on sys.path)."""
    try:
        return _installed_version("dsos")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True)
class ServerConfig:
    """Everything a profile needs to be built, and nothing it can discover
    for itself.

    `db` is the store handle the profile reads and writes; `python_path` is
    the interpreter run_python targets when a call omits its own (the
    user's analysis Python, not this server's — see dsos/sandbox.py); and
    `base_url` is where the daemon serving this store can be reached, which
    is what lets cite() hand out a link. base_url is None on the stdio
    path, where there is no HTTP surface to point at.
    """
    db: Database
    python_path: str
    base_url: str | None = None


class ToolCallLogger(Middleware):
    """Logs every tool call automatically, after it completes — see design
    doc, Philosophy #4 and the `tool_calls` table. The agent never calls a
    logging tool itself; this is the only place that writes to it.

    Constructed with the `Database` rather than a connection, because the
    middleware runs on the event loop while the tools it wraps run in a
    thread pool: both get their own thread's connection from the same
    handle, and the write is serialised by the handle's process-wide lock
    like any other. The lock is taken only around the INSERT — never
    across the `await` of the tool itself, which would hold a
    thread-blocking lock for the whole duration of a run_python.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def on_call_tool(self, context, call_next):
        args = context.message.arguments or {}
        # Reject an unknown session_id instead of logging the call against it.
        # These ids are 32 chars of hex; a model that mistypes one previously
        # got a silent success and the call landed in tool_calls under an id no
        # session owns — invisible in the GUI's per-session trace and silently
        # dropped by the reuse metric, which matches on session_id.
        session_id = args.get("session_id")
        if session_id and not store.session_exists(self._db.conn(), session_id):
            # ToolError, not ValueError: FastMCP surfaces this text to the
            # caller, whereas an uncaught ValueError reaches the agent as a
            # bare "Internal server error" and it learns nothing.
            raise ToolError(
                f"unknown session_id {session_id!r}. Use the exact id returned by "
                f"start_session for this round; a call with an unrecognized id is "
                f"rejected so it is not logged outside the session. If you lost the "
                f"id, call start_session again with this question."
            )
        result = await call_next(context)
        payload = result.structured_content or {}
        session_id = session_id or payload.get("session_id")
        if session_id:
            with self._db.write() as conn:
                store.log_tool_call(
                    conn,
                    session_id,
                    context.message.name,
                    args,
                    json.dumps(payload, default=str)[:300],
                    payload.get("artifact_row_ids", []),
                )
        return result


# The dsos session a consumer tool call belongs to, published by
# ConsumerSessions for the duration of the call. A ContextVar rather than an
# argument because the consumer tools take NO session_id (D12): a reader with
# no session to manage has nothing to pass one for, and a parameter they
# would have to invent would be a parameter they get wrong.
_CONSUMER_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dsos_consumer_session", default=None
)

# The header a client sends to say "these calls are one session" when the
# protocol gives it no other way to. MCP 2026-07-28 is sessionless: no
# handshake, no `mcp-session-id`, a fresh connection per request. So a caller
# that wants its calls counted as one visit, however it reaches the daemon,
# sends a value that is stable for as long as it is one client. The stdio
# shim is the case that needs it: it opens a new backend connection per
# proxied call, so without the header its calls are only as separable as its
# clientInfo is. Lower-case because that is how Starlette hands headers back.
CLIENT_SESSION_HEADER = "x-dsos-client-session"

# How long a sessionless client can be quiet before its next call is a new
# visit. Only applies to a client that sent no identity of its own (see
# `_client_identity`): with nothing else to go on, a burst of calls separated
# by less than this is one session — the same rule web analytics uses for the
# same problem. Thirty minutes, as there.
SESSIONLESS_IDLE_SECONDS = 30 * 60

# How many client identities `ConsumerSessions` remembers. A bound because the
# daemon is long-lived and the header above is client-supplied: an unbounded
# map would grow by one entry per client for the life of the process, and by
# one per request for a client that sends a fresh header every time. Past it,
# the least recently used identity is forgotten, and that client's next call
# opens a new session — see ConsumerSessions for why that is accepted.
MAX_TRACKED_CLIENTS = 4096

# A client-supplied header lands in a dict key, not in the store, but it is
# still clipped: nothing about a session identity needs more than this.
_MAX_IDENTITY_CHARS = 128


def consumer_session_id() -> str:
    """The dsos session id for the consumer call in progress.

    Only valid inside a tool call on a server carrying `ConsumerSessions`,
    which is every server `build_consumer` produces. Raises rather than
    returning a plausible id if it is not, because every use of it is a
    write attributed to somebody: a wrong id here is a tool call logged
    against a session that never asked the question.
    """
    session_id = _CONSUMER_SESSION.get()
    if session_id is None:
        raise RuntimeError(
            "no consumer session is active for this call. The consumer tools "
            "only work on a server built by dsos.server.build_consumer, which "
            "registers the ConsumerSessions middleware."
        )
    return session_id


class ConsumerSessions(Middleware):
    """D12: every consumer call is logged, against a session of its own.

    The producer's log works because every producer tool takes a `session_id`
    the agent obtained from `start_session`. A consumer has no such call to
    make — `start_session` is not on its surface, and giving it one would put
    a producer tool on a profile whose whole reason to exist is that a PM
    never pays for `run_python`. So the session is established here, from the
    MCP client session, and the tools read it rather than being handed it.

    One `sessions` row per client session, with `kind='consumer'` and a
    question of `consumer: <client>` so the GUI and the reuse metric can tell
    a reader's session from a worker's without reading the tool log. Every
    call is then logged against it, which is what makes "how often does a PM
    come back to this store" a number the store can produce (D12).

    This replaces `ToolCallLogger` on the consumer profile rather than
    sitting beside it: that one keys off a `session_id` argument, which no
    consumer tool has, so it would log nothing at all.

    What "one client session" means depends on what the client can say about
    itself, because MCP 2026-07-28 — which FastMCP 4's client negotiates by
    default — has no sessions: no handshake, no `mcp-session-id`, and the SDK
    builds a fresh connection for every request. `ctx.session_id` on such a
    request is a uuid cached on that per-request connection, i.e. a per-call
    id, which is why keying on it gave every call a session of its own. So the
    identity is taken from the best source the call has (`_client_identity`):
    an explicit `x-dsos-client-session` header, then the handshake era's
    per-connection session, then — for a sessionless client that said
    nothing — its clientInfo and user agent, cut into visits by an idle gap
    of `SESSIONLESS_IDLE_SECONDS`. That last one is a guess, and it is the
    right guess for the common case (one PM's agent), but two concurrent
    clients with the same clientInfo and no header share a session. The
    header is the fix for a client that cares, and the stdio shim is the one
    that must send it.

    The identity -> session map is an LRU of `MAX_TRACKED_CLIENTS`, not a
    persistent lookup: the identity is not stored on the row (the `sessions`
    table has no column for it), so a client evicted after that many newer
    ones — or any client after a daemon restart — opens a new session on its
    next call. Both are the ends of a visit anyway, and the alternative is a
    map that grows for the life of the daemon.

    The session row is inserted here rather than through `store.py` because
    this WP's ownership of that file is read helpers only. That is a layering
    seam and is recorded as one, the same shape as TTD U3: the natural home
    for this INSERT is `store.py` beside `start_session`, and it should move
    there the next time a WP owns the file for another reason.
    """

    def __init__(
        self,
        db: Database,
        *,
        max_entries: int = MAX_TRACKED_CLIENTS,
        idle_seconds: float = SESSIONLESS_IDLE_SECONDS,
    ) -> None:
        self._db = db
        self._max_entries = max_entries
        self._idle_seconds = idle_seconds
        # client identity -> (dsos session id, monotonic time of its last
        # call), least recently used first. The only reason the INSERT below
        # runs once per client rather than per call. A plain OrderedDict is
        # safe: the event loop is single-threaded and the lookup-then-insert
        # in `_session_for` has no await in it.
        self._sessions: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def on_call_tool(self, context, call_next):
        ctx = context.fastmcp_context
        key, stable = _client_identity(ctx)
        session_id = self._session_for(key, stable, _client_info(ctx)[0], time.monotonic())

        args = context.message.arguments or {}
        # Set before the tool runs and reset after, so a tool that raises
        # cannot leave a stale id visible to the next call on this context.
        token = _CONSUMER_SESSION.set(session_id)
        try:
            result = await call_next(context)
        finally:
            _CONSUMER_SESSION.reset(token)

        payload = getattr(result, "structured_content", None) or {}
        with self._db.write() as conn:
            store.log_tool_call(
                conn,
                session_id,
                context.message.name,
                args,
                json.dumps(payload, default=str)[:300],
                payload.get("artifact_row_ids", []),
            )
        return result

    def _session_for(self, key: str, stable: bool, client: str, now: float) -> str:
        """The dsos session for this identity at `now`, opening one if needed.

        `stable` is whether the identity is the client's own (a header, a
        handshake session): that session lasts as long as the identity does.
        A derived identity's session ends after `idle_seconds` without a call,
        and the next call is a new visit.
        """
        entry = self._sessions.get(key)
        if entry is not None and (stable or now - entry[1] <= self._idle_seconds):
            session_id = entry[0]
        else:
            with self._db.write() as conn:
                session_id = _insert_consumer_session(conn, client)
        self._sessions[key] = (session_id, now)
        self._sessions.move_to_end(key)
        while len(self._sessions) > self._max_entries:
            self._sessions.popitem(last=False)
        return session_id


def _client_identity(ctx) -> tuple[str, bool]:
    """(the key this call's session is found under, whether it is stable).

    In order of how much the key can be trusted to mean "one client":

    1. `x-dsos-client-session`, when the client sent one. First, because a
       client that names its own session knows better than the transport
       does — the shim's backend connections are per call on any era.
    2. The handshake era's per-connection session: `mcp-session-id` on
       streamable HTTP, and on stdio or in-memory the connection itself,
       which lives for the whole client session. `ctx.session_id` is right
       for both there. It is only asked for on a handshake-era connection
       because on a sessionless one it answers a fresh uuid per request.
    3. Otherwise the client is sessionless and said nothing about itself, so
       the key is what it did say — clientInfo and user agent — and it is
       returned as not stable, which is what cuts it into visits.

    The tiers are prefixed so a header value can never collide with a
    transport session id or a derived key.
    """
    headers = _request_headers()
    explicit = headers.get(CLIENT_SESSION_HEADER) if headers is not None else None
    if explicit:
        return f"header:{explicit[:_MAX_IDENTITY_CHARS]}", True

    try:
        protocol_version = ctx.session.protocol_version
    except (AttributeError, RuntimeError):
        protocol_version = None
    if protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
        try:
            return f"session:{ctx.session_id}", True
        except RuntimeError:
            pass

    name, version = _client_info(ctx)
    agent = headers.get("user-agent", "") if headers is not None else ""
    return f"client:{name}\x00{version}\x00{agent[:_MAX_IDENTITY_CHARS]}", False


def _request_headers():
    """The HTTP request's headers, or None off HTTP (stdio, in-memory)."""
    try:
        return get_http_request().headers
    except RuntimeError:
        return None


def _client_info(ctx) -> tuple[str, str]:
    """The MCP client's `clientInfo` (name, version), or stand-ins for them.

    Both spellings are tried because the field has been `clientInfo` and
    `client_info` across MCP SDK versions, and pinning either one would make
    `client` null on the other. `clientInfo` is a client-supplied free string,
    so it is clipped rather than trusted: the name lands in a
    `sessions.question` column that the GUI renders.
    """
    try:
        params = ctx.session.client_params
    except (AttributeError, RuntimeError):
        params = None
    for attribute in ("client_info", "clientInfo"):
        info = getattr(params, attribute, None) if params is not None else None
        name = getattr(info, "name", None) if info is not None else None
        if name:
            return str(name)[:80], str(getattr(info, "version", "") or "")[:40]
    return "unknown client", ""


def _insert_consumer_session(conn: sqlite3.Connection, client: str) -> str:
    """The `kind='consumer'` row, and its id. See ConsumerSessions for why
    this INSERT is here rather than in store.py."""
    session_id = store._new_id()
    conn.execute(
        """INSERT INTO sessions (id, question, started_at, kind, client)
           VALUES (?, ?, ?, 'consumer', ?)""",
        (session_id, f"consumer: {client}", store._now(), client),
    )
    # Committed here, not left to the caller: `db.write()` is a Python lock,
    # not a transaction boundary (TTD U6), so an uncommitted write here would
    # hold a RESERVED lock on the file and lock out every other thread.
    conn.commit()
    return session_id


def with_inline_image(payload: dict, art: store.Artifact):
    """A chart artifact's png bytes rendered as an actual inline image
    block in the tool response — not just a "Figure(1400x800)"-style text
    placeholder the agent has to trust blindly (feedback #2). structured_content
    stays the same payload dict either way, so callers/logging/tests don't
    need to branch on which shape came back.

    The one thing in a run's response that is an MCP concept rather than a
    fact about the artifact, which is why it lives here and not in
    present.py next to the payload it wraps.
    """
    if art.content_format == "png" and isinstance(art.content, bytes):
        from fastmcp.tools import ToolResult
        from fastmcp.utilities.types import Image

        return ToolResult(content=Image(data=art.content, format="png"), structured_content=payload)
    return payload


def execution_response(conn, outcome: execution.RunOutcome, input_row_ids: list[str]):
    """`present.execution_payload` plus the inline image block, which is
    the whole of what a run's response needs on top of the payload.

    The artifact is looked up twice on the success path — once inside
    present to build the payload, once here for the png bytes — because
    present.py is shared with the GUI and must not know about MCP images.
    A second blob read on a run that has just executed a query or a
    subprocess is not worth threading an artifact through both.
    """
    payload = present.execution_payload(conn, outcome, input_row_ids)
    if outcome.row_id is None:
        # A failed run produced no artifact, so there is nothing to render.
        return payload
    art = store.get_artifact_by_row_id(conn, outcome.row_id, load_content=True)
    return with_inline_image(payload, art)
