"""The consumer profile: the read-only side of the same store.

Built here, with no tools, so the shape exists before anything depends on
it. WP-D1 mounts it beside the producer in one daemon and WP-F1 adds the
four tools (find_evidence, get_claim, cite, ask); a server that answers
nothing is a seam that can be proven to work, where a server whose tools
were registered a WP early would be a seam nobody could test until it
existed.
"""

from __future__ import annotations

from fastmcp import FastMCP

from dsos.server.common import CONSUMER_NAME, ServerConfig, server_version
from dsos.server.instructions import CONSUMER_INSTRUCTIONS


def build_consumer(config: ServerConfig) -> FastMCP:
    """A FastMCP server over the same store, with no tools yet.

    `config` is accepted and unused for the shape it is: WP-F1's tools all
    read the store (and cite() reads `base_url` for the link it hands out),
    so the profile is defined by which handle it gets, not by anything it
    does differently. Taking it now keeps the two factories symmetric, and
    keeps D1 from having to special-case one of them.
    """
    return FastMCP(
        CONSUMER_NAME, instructions=CONSUMER_INSTRUCTIONS, version=server_version()
    )
