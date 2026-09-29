"""Layer 2: the MCP servers, as factories.

`dsos.mcp_server` used to be this layer, in one module, with a store
connection as a module global. It is now a package so that a process can
hold more than one server at a time — a producer and a consumer over the
same store in one daemon, two producers over two stores in one test, the
contract rendered for two profiles — none of which a module-level
connection allowed.

    from dsos.db import Database
    from dsos.server import ServerConfig, build_consumer, build_producer

    config = ServerConfig(db=Database(path), python_path=python, base_url=None)
    producer = build_producer(config)

The two factories are pure: they register tools against the config they are
given and return a server, opening nothing and reading no environment. The
only module that knows where a store path or an analysis interpreter comes
from is the thin `dsos.mcp_server` shim, which is what a stdio MCP client
still launches.
"""

from dsos.server.common import ServerConfig
from dsos.server.consumer import build_consumer
from dsos.server.producer import build_producer

__all__ = ["ServerConfig", "build_consumer", "build_producer"]
