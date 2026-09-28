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
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

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

# The key used when the transport gives no per-client session id. One consumer
# session for the whole server process, which is the honest reading of "this
# client has no session": attributing every such call to one row keeps them
# countable rather than dropping them or inventing a session per call.
_PROCESS_SESSION_KEY = "\x00process"


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

    The session row is inserted here rather than through `store.py` because
    this WP's ownership of that file is read helpers only. That is a layering
    seam and is recorded as one, the same shape as TTD U3: the natural home
    for this INSERT is `store.py` beside `start_session`, and it should move
    there the next time a WP owns the file for another reason.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        # client session key -> dsos session id. One entry per client session
        # for the life of the process, and the only reason the INSERT below
        # runs once rather than per call. A plain dict is safe: the event loop
        # is single-threaded and the check-then-insert has no await in it.
        self._sessions: dict[str, str] = {}

    async def on_call_tool(self, context, call_next):
        ctx = context.fastmcp_context
        key, client = self._client(context)
        session_id = self._sessions.get(key) or self._open_session(key, client)

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

    def _client(self, context) -> tuple[str, str]:
        """(the key to attribute calls under, the client's name).

        `ctx.session_id` is the per-client session on every transport FastMCP
        supports here; the fallback is for a request context that has no
        session at all, where attributing to one process-wide row is more
        honest than dropping the call or minting a session per call.
        """
        ctx = context.fastmcp_context
        try:
            key = ctx.session_id
        except RuntimeError:
            key = _PROCESS_SESSION_KEY
        return key, _client_name(ctx)

    def _open_session(self, key: str, client: str) -> str:
        with self._db.write() as conn:
            session_id = _insert_consumer_session(conn, client)
        self._sessions[key] = session_id
        return session_id


def _client_name(ctx) -> str:
    """The MCP client's `clientInfo.name`, or a stand-in for one.

    Both spellings are tried because the field has been `clientInfo` and
    `client_info` across MCP SDK versions, and pinning either one would make
    `client` null on the other. `clientInfo` is a client-supplied free string,
    so it is clipped rather than trusted: it lands in a `sessions.question`
    column that the GUI renders.
    """
    try:
        params = ctx.session.client_params
    except (AttributeError, RuntimeError):
        params = None
    for attribute in ("client_info", "clientInfo"):
        info = getattr(params, attribute, None) if params is not None else None
        name = getattr(info, "name", None) if info is not None else None
        if name:
            return str(name)[:80]
    return "unknown client"


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
