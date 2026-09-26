"""Smoke test for dsos/update_check.py. Monkeypatches the network call and
the editable-install detection instead of hitting the real GitHub API or
depending on how this checkout happens to be installed — this needs to
pass the same way whether run from the dev venv (editable) or a real
release install.

Run: .venv/Scripts/python.exe tests/update_check_smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsos import update_check


def main() -> None:
    assert update_check._parse_version("v1.2.3") == (1, 2, 3)
    assert update_check._parse_version("0.1.0") == (0, 1, 0)
    assert update_check._parse_version("v2.0") == (2, 0)
    print("[ok] _parse_version handles a leading 'v' and bare X.Y.Z")

    # An editable dev install is always skipped, regardless of what the
    # (mocked) fetch would otherwise say — this is the false-positive case
    # for the person doing the releasing, not who the check is for.
    update_check._is_editable_install = lambda: True
    update_check._fetch_latest_release_tag = lambda: "v99.0.0"
    update_check._CACHE_PATH = Path(f"{Path(__file__).parent}/_update_check_cache_editable.json")
    update_check._CACHE_PATH.unlink(missing_ok=True)
    assert update_check.check_for_update() is None
    print("[ok] editable dev installs are skipped entirely")

    # A real release install, mocked as behind the latest tag.
    update_check._is_editable_install = lambda: False
    update_check._installed_version = lambda name: "0.1.0"
    update_check._fetch_latest_release_tag = lambda: "v0.2.0"
    update_check._CACHE_PATH = Path(f"{Path(__file__).parent}/_update_check_cache_behind.json")
    update_check._CACHE_PATH.unlink(missing_ok=True)
    notice = update_check.check_for_update()
    assert notice and "v0.2.0" in notice and "0.1.0" in notice, notice
    print(f"[ok] behind-latest-release produces a notice: {notice}")

    # Up to date: no notice.
    update_check._fetch_latest_release_tag = lambda: "v0.1.0"
    update_check._CACHE_PATH = Path(f"{Path(__file__).parent}/_update_check_cache_current.json")
    update_check._CACHE_PATH.unlink(missing_ok=True)
    assert update_check.check_for_update() is None
    print("[ok] already on the latest release produces no notice")

    # A network failure (offline, rate-limited, GitHub down, ...) must never
    # raise — it's swallowed and treated as "nothing to report".
    def _boom():
        raise RuntimeError("simulated network failure")

    update_check._fetch_latest_release_tag = _boom
    update_check._CACHE_PATH = Path(f"{Path(__file__).parent}/_update_check_cache_fail.json")
    update_check._CACHE_PATH.unlink(missing_ok=True)
    assert update_check.check_for_update() is None
    print("[ok] a failed fetch is swallowed, not raised")

    # Caching: a second call within the TTL must not call the (mocked)
    # fetch again — otherwise every concurrent/repeated server start would
    # hit GitHub's API afresh.
    calls = []
    update_check._fetch_latest_release_tag = lambda: (calls.append(1), "v0.2.0")[1]
    update_check._CACHE_PATH = Path(f"{Path(__file__).parent}/_update_check_cache_ttl.json")
    update_check._CACHE_PATH.unlink(missing_ok=True)
    update_check.check_for_update()
    update_check.check_for_update()
    assert len(calls) == 1, f"expected the cache to prevent a second fetch, got {len(calls)} calls"
    print("[ok] a cached result is reused within the TTL, not re-fetched")

    for name in ("editable", "behind", "current", "fail", "ttl"):
        Path(f"{Path(__file__).parent}/_update_check_cache_{name}.json").unlink(missing_ok=True)

    print("\nupdate_check smoke test passed.")


if __name__ == "__main__":
    main()
