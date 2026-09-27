"""DS Artifact OS — a working layer for data-science artifacts.

See README.md for what this is; this package intentionally keeps its
public surface in submodules (store, execution, mcp_server, ...).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("dsos")
except PackageNotFoundError:  # running from source without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
