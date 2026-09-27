"""Best-effort check against GitHub's public Releases API: is a newer
tagged release available than what's installed? Only for people running an
installed release (`pip install git+...@vX.Y.Z`) — an editable dev checkout
(`pip install -e .`, tracking main) is skipped on purpose: its installed
version stays whatever pyproject.toml last said at the previous release cut
even though main may be well ahead of it, so "a newer release is available"
would be a false, backwards-looking nudge for exactly the person doing the
releasing.

Never raises and never meaningfully blocks server startup: any failure
(offline, rate-limited, no releases yet) is swallowed and treated as
"nothing to report". The result is cached for _CACHE_TTL_SECONDS so
frequent restarts / multiple concurrent server processes don't hit
GitHub's API (or add network latency to startup) every single time.
"""

from __future__ import annotations

import json
import tempfile
import time
import urllib.request
from importlib.metadata import PackageNotFoundError
from importlib.metadata import distributions
from importlib.metadata import version as _installed_version
from pathlib import Path

REPO = "zixiaoshawnshi/DataScientist-OS"
_TIMEOUT_SECONDS = 2.0
_CACHE_PATH = Path(tempfile.gettempdir()) / "dsos_update_check_cache.json"
_CACHE_TTL_SECONDS = 6 * 3600


def _parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _fetch_latest_release_tag() -> str | None:
    """Isolated so tests can monkeypatch it instead of hitting the real API."""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8")).get("tag_name")


def _cached_latest_tag() -> str | None:
    try:
        cache = json.loads(_CACHE_PATH.read_text())
        if time.time() - cache["checked_at"] < _CACHE_TTL_SECONDS:
            return cache["latest_tag"]
    except Exception:
        pass  # no cache yet, corrupt cache, ... — just re-check below

    tag = None
    try:
        tag = _fetch_latest_release_tag()
    except Exception:
        pass  # offline, rate-limited, GitHub down, ... — never fatal

    try:
        _CACHE_PATH.write_text(json.dumps({"checked_at": time.time(), "latest_tag": tag}))
    except Exception:
        pass  # an unwritable cache just means we check again next time

    return tag


def _is_editable_install() -> bool:
    """True if any *visible* dsos distribution is an editable install.

    More than one can be visible at once: launched with cwd = a source
    checkout, the checkout's in-tree .egg-info (which has no
    direct_url.json) shadows the venv's dist-info (which has it), so
    Distribution.from_name's first match can miss the real answer.
    Check every dsos distribution instead.
    """
    try:
        for dist in distributions():
            if (dist.metadata.get("Name") or "").lower() != "dsos":
                continue
            try:
                text = dist.read_text("direct_url.json")
                if text and json.loads(text).get("dir_info", {}).get("editable"):
                    return True
            except Exception:
                continue  # corrupt direct_url.json on one dist — keep looking
    except Exception:
        pass
    return False


def check_for_update() -> str | None:
    """A short human-readable notice if a newer release exists, else None
    (up to date, an editable dev checkout, no installed package metadata at
    all, or the check failed for any reason)."""
    if _is_editable_install():
        return None

    try:
        installed = _installed_version("dsos")
    except PackageNotFoundError:
        return None  # no installed package metadata at all

    latest_tag = _cached_latest_tag()
    if not latest_tag:
        return None

    if _parse_version(latest_tag) <= _parse_version(installed):
        return None

    return (
        f"A newer dsos release is available: {latest_tag} (installed: v{installed}). "
        f'Update: pip install --upgrade "git+https://github.com/{REPO}.git@{latest_tag}". '
        f"Release notes: https://github.com/{REPO}/releases/tag/{latest_tag}"
    )
