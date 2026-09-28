"""WP-C2: Doc II's tool table is generated from the live FastMCP registry.

The tool surface is the product's main promise and its main drift risk. Doc
II §Architecture carried a hand-written `| Profile | Caller | MVP tools |`
table that had already gone stale — it listed a 13-tool surface and, for a
while, did not mention `save_template`/`list_templates` at all, because the
surface changed (WP-B1, WP-B2, the maintainer's U1 reversal) and the prose
did not. Nothing in the suite noticed: the table was a *copy* of the
registry, and nothing compared the two.

So the table is now an artifact. `dsos/server/contract.py` renders the
markdown from the servers themselves — `build_producer(config)` and
`build_consumer(config)` — and `python -m dsos.server.contract --write`
splices the result into the two marker regions in Doc II. This test is the
comparison that was missing: the doc block must equal what the registry
renders, for each profile, or the doc is lying about the surface.

Three things this pins down:

1. The producer block equals `render(build_producer(config))` — a
   mismatch prints a diff and names the one command that fixes it.
2. The consumer block equals `render(build_consumer(config))` and is
   currently **an empty table**. build_consumer has no tools until WP-F1
   (find_evidence/get_claim/cite/ask), and that is the honest rendering of
   the registry, not a gap to be papered over: a placeholder row here would
   go stale the moment F1 lands, which is exactly the failure this WP
   exists to prevent. When F1 adds the tools, the block fills in and this
   same check keeps it honest.
3. `--write` is idempotent. Running it twice produces no second diff — a
   generator that isn't idempotent makes this test flaky in a way that is
   hard to diagnose much later, when someone changes a docstring and gets
   an unexplained rewrite of a line they never touched.

Run: .venv/Scripts/python.exe tests/contract_smoke_test.py
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A dedicated subdirectory under data/test-runs/, like every other test:
# building the two servers needs a real Database, and this must not touch
# the developer's own store.
os.environ["DSOS_DB_PATH"] = "data/test-runs/contract_smoke_test/store.db"
shutil.rmtree(Path(os.environ["DSOS_DB_PATH"]).parent, ignore_errors=True)

from dsos.db import Database  # noqa: E402 — import after DSOS_DB_PATH is set
from dsos.server import ServerConfig, build_consumer, build_producer  # noqa: E402
from dsos.server import contract  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def check_block(label: str, profile: str, doc_text: str, expected: str) -> None:
    """Compare one marker region against the rendered registry, with a diff.

    The diff and the fix command are the point: a bare FAIL here tells a
    future agent only that the doc is stale, not how to make it match or
    what changed underneath them.
    """
    actual = contract.read_block(doc_text, profile)
    if actual == expected:
        check(label, True, f"{len(expected.splitlines()) - 2} row(s)")
        return
    print(f"--- contract diff: {profile} (doc block vs registry) ---")
    for line in difflib.unified_diff(
        (actual or "").splitlines(), expected.splitlines(),
        fromfile=f"Doc II <!-- contract:{profile} -->", tofile=f"render({profile})",
        lineterm="",
    ):
        print("    " + line)
    print("--- fix it with: python -m dsos.server.contract --write ---\n")
    check(label, False, "doc block differs from the live tool registry")


def main() -> None:
    config = ServerConfig(
        db=Database(os.environ["DSOS_DB_PATH"]),
        python_path=sys.executable,
        base_url=None,
    )
    producer = build_producer(config)
    consumer = build_consumer(config)

    doc_path = contract.find_design_doc()
    if doc_path is None:
        check("Doc II is findable via the Design/*Design Doc II*.md glob", False)
        return
    check("Doc II is findable via the Design/*Design Doc II*.md glob", True, str(doc_path))
    doc_text = doc_path.read_text(encoding="utf-8")

    producer_table = contract.render(producer)
    consumer_table = contract.render(consumer)

    check_block("producer block == render(build_producer(...))",
                "producer", doc_text, producer_table)
    check_block("consumer block == render(build_consumer(...))",
                "consumer", doc_text, consumer_table)

    # Rows in a rendered table, excluding the header and the `|---|` rule.
    # Counted the same way for both profiles so the empty consumer case and
    # the populated producer case cannot disagree about what a "row" is.
    def rows_of(table: str) -> list[str]:
        return [
            ln for ln in table.splitlines()
            if ln.startswith("|") and not ln.startswith("|---") and "tool |" not in ln
        ]

    # The consumer profile carries no tools until WP-F1, so its generated
    # table is a header with no rows. Asserted rather than assumed: this is
    # the assertion that will fail loudly (and correctly) when F1 lands and
    # someone forgets to regenerate, and the one that would fail if a
    # placeholder row had crept in to make the doc look finished.
    check("the consumer table is empty — zero tools until WP-F1",
          rows_of(consumer_table) == [],
          f"{len(rows_of(consumer_table))} row(s)")

    # The producer side is not asserted by name here on purpose:
    # tests/surface_smoke_test.py already pins the exact nine-tool set, and
    # duplicating that list is exactly the second source of truth this WP
    # removes. What matters here is only that the doc follows the registry.
    check("the producer block has a row per registered tool",
          len(rows_of(producer_table)) == 9,
          f"{len(rows_of(producer_table))} row(s)")

    # 3. Idempotency, on a *stale* copy of the real doc — never the working
    #    tree, and never the committed doc, which is already up to date
    #    (that is what the two checks above assert). Staleness is
    #    manufactured by blanking the generated region, so the first-write
    #    case is tested whether or not anyone has regenerated the doc: a
    #    check whose result depends on whether this test ran `--write` first
    #    is a check that fails for the wrong reason the day someone reverts
    #    the doc.
    copy_path = Path("data/test-runs/contract_smoke_test/doc2-copy.md")
    copy_path.parent.mkdir(parents=True, exist_ok=True)
    doc_bytes = doc_path.read_bytes()
    stale = re.sub(
        rb"<!-- contract:producer -->.*?<!-- /contract:producer -->",
        b"<!-- contract:producer -->\n<!-- /contract:producer -->",
        doc_bytes, flags=re.DOTALL,
    )
    check("the staleness fixture actually blanks the producer region",
          stale != doc_bytes and b"render" not in stale.split(b"contract:producer -->")[1]
          .split(b"<!-- /contract:producer -->")[0])
    copy_path.write_bytes(stale)

    changed_first = contract.write_blocks(
        copy_path, {"producer": producer_table, "consumer": consumer_table})
    after_first = copy_path.read_bytes()
    changed_second = contract.write_blocks(
        copy_path, {"producer": producer_table, "consumer": consumer_table})
    after_second = copy_path.read_bytes()

    check("--write reports a change when the doc is stale",
          changed_first, str(copy_path))
    check("--write is idempotent: second run reports no change",
          not changed_second)
    check("--write is byte-stable: second run leaves the file identical",
          after_first == after_second)
    check("--write converges on the committed doc's generated region",
          contract.read_block(after_first.decode("utf-8"), "producer")
          == producer_table)
    check("--write preserves the doc's existing line endings",
          _line_endings(doc_bytes) == _line_endings(after_first),
          f"{_line_endings(doc_bytes)!r} -> {_line_endings(after_first)!r}")
    # Everything outside the two generated regions has to survive untouched,
    # including the CRLF line endings: a writer that rewrote the file with
    # "\n".join() would turn a three-line doc change into a whole-file one.
    # Both sides are read as bytes for the same reason — read_text() applies
    # universal-newline translation, which would hide exactly the difference
    # this check exists to catch.
    check("--write leaves the surrounding prose alone",
          _outside_markers(after_first) == _outside_markers(doc_bytes),
          f"{len(_outside_markers(doc_bytes))} bytes outside the markers")
    check("--write touches nothing but the two marker regions",
          len(re.findall(r"<!-- contract:", after_first.decode("utf-8"))) == 2
          and len(re.findall(r"<!-- /contract:", after_first.decode("utf-8"))) == 2,
          "2 open + 2 close markers")


def _outside_markers(data: bytes) -> bytes:
    """The doc with both generated regions removed, as raw bytes.

    Comparing this before and after a `--write` is what proves the writer
    replaced only the regions — the prose, the headings, and the line
    endings around them. Diffing the whole files would hide that distinction
    behind the lines that are supposed to change, and going through
    `read_text` would normalise CRLF away and stop being able to see it.
    """
    return re.sub(
        rb"<!-- contract:(producer|consumer) -->.*?<!-- /contract:\1 -->",
        b"", data, flags=re.DOTALL,
    )


def _line_endings(data: bytes) -> str:
    """'crlf' / 'lf' / 'mixed' — Doc II is CRLF, and a rewrite that silently
    converts the whole file to LF is a whole-file diff dressed up as a
    three-line doc change."""
    text = data.decode("utf-8")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    return "crlf" if crlf else "lf"


if __name__ == "__main__":
    main()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        sys.exit(1)
    print("contract smoke test passed")
