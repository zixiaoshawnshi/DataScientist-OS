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

import json
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
