"""Reading an artifact's content off the local filesystem.

`save_artifact`'s `content_path` arm: the agent already has the data in a
file it wrote with its own tools, and dsos has to decide what that file is.
The knowledge involved — which extensions are ingestible, and that a dataset
becomes parquet while everything else is read as text — is about files, not
about MCP, so it lives here rather than inside the server package. That way
it is importable and testable without building a server, and the server stays
what its docstring says it is: a translation layer over the core library.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def ingest_path(path: str, type: str, content_format: str) -> tuple:
    """Return `(content, content_format)` for a local file.

    For a dataset the format is decided by the extension and the caller's
    `content_format` is ignored — the file is read into a DataFrame and
    stored as parquet, which is the one tabular shape the rest of the system
    knows how to preview, chart and query uniformly. For every other type
    the file is read as text and the caller's format describes it.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"no such file: {path}")
    if type != "dataset":
        # encoding explicit: read_text defaults to the locale codec (e.g.
        # GBK on Chinese-locale Windows), which chokes on Unicode content
        return p.read_text(encoding="utf-8"), content_format

    ext = p.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(p)
    elif ext == ".tsv":
        df = pd.read_csv(p, sep="\t")
    elif ext == ".json":
        df = pd.read_json(p)
    elif ext == ".parquet":
        df = pd.read_parquet(p)
    else:
        raise ValueError(f"don't know how to ingest {ext!r} as a dataset (csv/tsv/json/parquet)")
    return df, "parquet"
