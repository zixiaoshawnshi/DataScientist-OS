"""Which dsos this is: a released install (`prod`) or a source checkout (`dev`).

The two are run side by side on one machine — a released install serving
the store real work lives in, and a checkout under development serving test
stores — and before this they collided on everything they shared by default:
the daemon's port above all. A daemon for a dev store would take 8765, and
the prod daemon started after it would find the port gone.

So each channel owns a port range (dsos.daemon), reports itself in
/healthz and the manifest, and the shim notes a mismatch. The channel is
decided by where the `dsos` package was imported from, because that is the
one thing that cannot lie about which code is running:

- a package directory that sits next to a `pyproject.toml` is a source
  checkout (an editable install, or `python -m` from the repo root): dev;
- anything else — a wheel in site-packages — is a release: prod.

`DSOS_CHANNEL=prod|dev` overrides it, for the case detection cannot see
(a release unpacked by hand next to a pyproject, a CI checkout that should
behave like prod).

Kept free of every other dsos import, because the stdio shim imports it and
the shim is meant to start without loading the store's dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path

CHANNEL_ENV = "DSOS_CHANNEL"
CHANNELS = ("prod", "dev")


def channel() -> str:
    """`prod` or `dev`. See the module docstring for how it is decided."""
    forced = os.environ.get(CHANNEL_ENV, "").strip().lower()
    if forced in CHANNELS:
        return forced
    return "dev" if _is_source_checkout() else "prod"


def _is_source_checkout() -> bool:
    package_dir = Path(__file__).resolve().parent
    return (package_dir.parent / "pyproject.toml").is_file()
