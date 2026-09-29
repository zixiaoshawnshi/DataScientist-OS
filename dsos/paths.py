"""One spelling for a store path.

The daemon, the shim and autostart all turn DSOS_DB_PATH into a path, write
it into a manifest or /healthz, and compare it against another process's —
"is that daemon serving MY store?" is the question every one of them asks.
Two spellings of one file therefore look like two stores, and the
consequence is concrete: a shim that fails to recognise the daemon serving
its store starts a second one over the same file.

`Path.resolve()` on Windows produces two spellings. It normally strips the
`\\\\?\\` extended-length prefix that GetFinalPathNameByHandle returns, but
it keeps it when the file cannot be opened at that moment — a store that
another process is creating right then, which is exactly when two clients
are starting. So everything here resolves, then strips the prefix, then
case-folds for comparison.

Import-free beyond the standard library: the shim imports it.
"""

from __future__ import annotations

import os
from pathlib import Path

_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def store_path(raw: str | Path) -> Path:
    """`raw` made absolute and symlink-resolved, with no `\\\\?\\` prefix."""
    text = str(Path(raw).resolve())
    if text.startswith(_EXTENDED_UNC):
        text = "\\\\" + text[len(_EXTENDED_UNC):]
    elif text.startswith(_EXTENDED):
        text = text[len(_EXTENDED):]
    return Path(text)


def same_store(a: str | Path, b: str | Path) -> bool:
    """Do two spellings of a store path name the same file?"""
    return os.path.normcase(str(store_path(a))) == os.path.normcase(str(store_path(b)))
