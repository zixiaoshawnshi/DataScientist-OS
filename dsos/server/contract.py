"""The tool contract, generated from the live FastMCP registry into Doc II.

Doc II §Architecture used to carry a hand-written `| Profile | Caller | MVP
tools |` table. It was a *copy* of the tool surface, and it drifted: it
listed a 13-tool surface and at one point did not mention
save_template/list_templates at all, because the surface changed (WP-B1,
WP-B2, the maintainer's U1 reversal) and the prose did not. Nothing noticed,
because nothing compared the copy against the original.

    python -m dsos.server.contract --write     # rewrite the marker regions
    python -m dsos.server.contract             # print the tables, no writes

So the table is an artifact now. This module reads the two servers'
registries — `build_producer(config)` and `build_consumer(config)`, the same
factories the daemon mounts — and renders one markdown table per profile:

    | tool | params | summary |
    |---|---|---|
    | `run_sql` | `code*: str`, `input_row_ids*: list[str]` | Run SQL (DuckDB) against ... |

`params` is the tool's JSON-schema parameter map, rendered `name: type` with
required parameters marked `*`. `summary` is the tool's description, which is
the agent-facing docstring — most of the product for a tool surface, since an
agent decides what to call from it.

Two things this deliberately does not do:

- It does not name the tools. A hard-coded list here would be a second
  source of truth, which is the thing that rotted. The registry is the only
  one; adding a tool and forgetting this file cannot make the doc wrong,
  because the doc is not written by hand.
- It does not special-case the empty profile. `build_consumer` carries zero
  tools until WP-F1, so its table is a header and no rows — the honest
  rendering of the registry, and the one that fills in by itself when F1
  lands. A placeholder row would be a hand-maintained lie in a file whose
  entire purpose is not to contain those.

`--write` only replaces the text *between* the marker comments, never the
comments or anything around them, and is idempotent: a second run over an
unchanged registry reports no change and does not touch the file. Doc II is
stored with CRLF line endings, so a rewrite preserves whatever the file
already uses rather than converting the whole thing to LF and turning a
three-line doc change into a whole-file diff.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from fastmcp import FastMCP

# Where the contract lives, and the markers that fence the generated region
# in it. The doc is found by glob rather than hard-coded path because the
# filename carries an em dash and " (Spine & Scale)"; a literal string in
# source is a typo waiting to happen and breaks on any rename.
DESIGN_GLOB = "Design/*Design Doc II*.md"
PROFILE_MARKERS = ("producer", "consumer")

# Ceiling on one summary cell. A summary is a table cell, not a paragraph:
# run_python's first sentence is a hundred words, and pasting all of it in
# makes the table unreadable, which defeats generating it at all. Truncation
# is at a word boundary with an ellipsis, so a cut cell still reads as cut.
MAX_SUMMARY_CHARS = 220

# Abbreviations whose trailing period is not a sentence end. `producer.py`'s
# docstrings use "e.g." and "i.e." freely; splitting on the first "." turns
# "e.g. a title" into "e.g" and orphans the rest of the sentence. Guarded
# explicitly rather than with a general heuristic, because a false negative
# here is cosmetic (a slightly long cell) and a false positive mangles a
# word — under-splitting is the safe direction.
_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf", "resp", "approx"})

# A sentence ends at . ! or ?, followed by whitespace. The trailing
# whitespace is what keeps decimals and versions intact — in "dsos 0.4.1",
# the periods are followed by a digit, not a space, so they are not
# candidate ends. The abbreviation guard is applied to the word before the
# period, so "e.g." is excluded.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def render(server: FastMCP) -> str:
    """One markdown table describing every tool `server` exposes.

    Reads the live registry (`list_tools()`), so it reflects the surface as
    built rather than a list maintained beside it. Rows are sorted by name so
    the output is stable across runs — a generated block that reordered
    itself between two invocations would make `--write` non-idempotent in
    everything but content, and turn every regeneration into a diff.
    """
    tools = sorted(_list_tools(server), key=lambda t: t.name)
    lines = [
        "| tool | params | summary |",
        "|---|---|---|",
    ]
    for tool in tools:
        lines.append(
            f"| `{tool.name}` | {_escape(_params(tool.parameters))} "
            f"| {_summary(tool.description)} |"
        )
    return "\n".join(lines)


def _list_tools(server: FastMCP) -> list[Any]:
    """The server's tools, via the public async API.

    `FastMCP.list_tools` is a coroutine and `render` is sync, so this runs it
    on a fresh event loop. That is not incidental: the registry is only
    reachable through the async API, and reaching into private attributes to
    avoid the loop would couple the generator to one FastMCP release's
    internals for no benefit.
    """
    import asyncio

    return list(asyncio.run(server.list_tools()))


def _params(parameters: dict | None) -> str:
    """`name: type` per parameter, required ones marked `*`.

    Read off the tool's own JSON schema — the same shape the MCP client sees
    — rather than the Python signature, so a parameter added by a wrapper or
    a TypeAdapter still appears. A parameter with no type (unusual, but legal
    in JSON Schema) renders as `any` rather than as a blank that reads like a
    rendering bug.
    """
    if not parameters:
        return ""
    required = set(parameters.get("required") or ())
    parts = []
    for name, schema in parameters.get("properties", {}).items():
        mark = "*" if name in required else ""
        parts.append(f"`{name}{mark}`: {_schema_type(schema or {})}")
    return ", ".join(parts)


def _schema_type(schema: dict) -> str:
    """A JSON Schema node as a short type name.

    `anyOf`/`oneOf` (how `str | None` arrives) become a `|`-joined union,
    which is how the annotation reads in the source. `$ref` is not resolved:
    the producer's schema is inline, and resolving a ref would mean walking
    the schema document this function was handed a fragment of.
    """
    for key in ("anyOf", "oneOf"):
        if key in schema:
            members = [_schema_type(m) for m in schema[key]]
            # Order is the schema's, so `str | None` stays in source order;
            # de-duplicated so `dict | None` does not print a union of two
            # identical members.
            seen: list[str] = []
            for m in members:
                if m not in seen:
                    seen.append(m)
            return " | ".join(seen)
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(str(k) for k in kind)
    if kind == "array":
        items = schema.get("items")
        inner = _schema_type(items) if isinstance(items, dict) else None
        return f"list[{inner}]" if inner else "list"
    return str(kind) if kind else "any"


def _summary(description: str | None) -> str:
    """The tool's one-line summary: the first sentence of its docstring.

    The TTD asks for "the first docstring line", and every docstring in
    `producer.py` is hard-wrapped at ~80 columns, so the literal first
    physical line is a mid-sentence fragment in all nine cases ("Search over
    everything saved so far, across every past session, not"). A fragment is
    not a summary, so the wrap is collapsed first and the first *sentence* is
    taken, with the cell capped. Kept in its own function because that is the
    one judgement call in the renderer, and reverting it is a one-line
    change.

    Empty rather than raising for a tool with no docstring: this is run over
    a growing registry by a command anyone can invoke, and crashing on one
    undocumented tool is a worse failure mode than a blank cell that a
    reader can see is blank.
    """
    if not description:
        return ""
    text = " ".join(description.split())
    if not text:
        return ""
    first = _first_sentence(text)
    if len(first) > MAX_SUMMARY_CHARS:
        cut = first[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:—-")
        first = f"{cut}…"
    return _escape(first)


def _first_sentence(text: str) -> str:
    """The first sentence of `text`, terminator included.

    Conservative by design: a missed split leaves a slightly long cell,
    while a wrong one mangles a word. Only a terminator followed by
    whitespace ends a sentence, and the word before it is checked against
    `_ABBREVIATIONS` so "e.g." is not mistaken for a stop.
    """
    for match in _SENTENCE_END.finditer(text):
        end = match.start()
        head = text[:end]
        last_word = re.split(r"[\s(\[]", head)[-1].lower().rstrip(".!?")
        if last_word in _ABBREVIATIONS:
            continue
        return head
    # No terminator anywhere: the whole thing is the summary. A tool whose
    # docstring is one run-on line is not a reason to render nothing.
    return text


def _escape(cell: str) -> str:
    """Make `cell` safe inside a markdown table row.

    A pipe would silently add a column, which is not hypothetical here: every
    optional parameter renders as `str | None`, so without this each such cell
    would split into three and the whole table would be malformed. That is
    the only escape a markdown table cell needs, and it applies to both
    columns: a docstring containing a raw pipe would break the row just as a
    parameter type does.
    """
    return cell.replace("|", "\\|")


def find_design_doc(root: Path | None = None) -> Path | None:
    """The Doc II file to update, or None if it isn't there.

    Searched from the repo root, which is two parents up from this file
    (`dsos/server/contract.py`), and overridable for the test that runs the
    writer against a copy. A missing doc is a None rather than an exception:
    `--write` turns that into a clear message, whereas a traceback from a
    glob is not an explanation.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    matches = sorted(base.glob(DESIGN_GLOB))
    return matches[0] if matches else None


def read_block(text: str, profile: str) -> str | None:
    """The generated region's current contents, or None if it is not there.

    Returns the text strictly between the markers, so comparing it against
    `render(server)` is a like-for-like comparison — nothing is stripped or
    normalised in between to hide a difference. The one normalisation that
    *is* applied is CRLF → LF inside the region: Doc II is stored with CRLF
    endings while `render` always emits LF, so without this the block could
    never compare equal on Windows and the whole check would be vacuously
    false. The line ending is a property of the file, not of the content
    being generated.
    """
    pattern = re.compile(
        rf"<!--\s*contract:{re.escape(profile)}\s*-->(.*?)<!--\s*/contract:{re.escape(profile)}\s*-->",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).replace("\r\n", "\n").strip("\n")


def write_blocks(path: Path, tables: dict[str, str]) -> bool:
    """Replace each profile's generated region in `path`, from
    `{profile: rendered_table}`. Returns whether the file changed.

    All profiles are rewritten in memory and compared as a set, and the file
    is touched only if the result differs — so a no-op run leaves the mtime
    alone, and a doc that is half up to date can never end up half rewritten.
    """
    original_bytes = path.read_bytes()
    text = original_bytes.decode("utf-8")
    # Doc II is CRLF in this repo. The markers and their region are matched
    # and rewritten with \n internally, then re-encoded in whatever the file
    # already used, so a regeneration is a three-line diff rather than a
    # whole-file one.
    newline = "\r\n" if "\r\n" in text else "\n"
    normalised = text.replace("\r\n", "\n")

    updated = normalised
    for prof, rendered in tables.items():
        pattern = re.compile(
            rf"(<!--\s*contract:{re.escape(prof)}\s*-->).*?"
            rf"(<!--\s*/contract:{re.escape(prof)}\s*-->)",
            re.DOTALL,
        )
        if not pattern.search(updated):
            raise MarkerError(
                f"no `<!-- contract:{prof} -->` region in {path}. Add the markers "
                f"(with the prose you want around them) and re-run."
            )
        updated, n = pattern.subn(
            lambda m: f"{m.group(1)}\n{rendered}\n{m.group(2)}", updated
        )
        if n != 1:
            raise MarkerError(
                f"found {n} `contract:{prof}` regions in {path}; expected exactly one."
            )

    if updated == normalised:
        return False
    path.write_bytes(updated.replace("\n", newline).encode("utf-8"))
    return True


class MarkerError(RuntimeError):
    """A marker region is missing or duplicated in the target doc."""


def _rendered_profiles() -> Iterable[tuple[str, str]]:
    """(profile, table) for both profiles, built from throwaway servers.

    The factories are pure, so building both here opens no store and reads no
    environment: the registry is fully populated by the decorators at build
    time. A temp directory is passed anyway rather than a real store path,
    because "pure today" is not a guarantee this command should depend on.
    """
    import tempfile

    from dsos.db import Database
    from dsos.server.common import ServerConfig
    from dsos.server.consumer import build_consumer
    from dsos.server.producer import build_producer

    with tempfile.TemporaryDirectory(prefix="dsos-contract-") as tmp:
        config = ServerConfig(
            db=Database(Path(tmp) / "contract.db"),
            python_path=sys.executable,
            base_url=None,
        )
        yield "producer", render(build_producer(config))
        yield "consumer", render(build_consumer(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dsos.server.contract",
        description="Render the MCP tool contract, and write it into Doc II.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the generated regions in Doc II (default: print the tables)",
    )
    parser.add_argument(
        "--doc",
        type=Path,
        default=None,
        help="the Doc II file to update, instead of the globbed default",
    )
    args = parser.parse_args(argv)

    tables = list(_rendered_profiles())

    if not args.write:
        for profile, table in tables:
            print(f"<!-- contract:{profile} -->")
            print(table)
            print()
        return 0

    path = args.doc or find_design_doc()
    if path is None:
        print(
            f"no Doc II found via {DESIGN_GLOB}. Pass --doc <path>.",
            file=sys.stderr,
        )
        return 1
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    try:
        changed = write_blocks(path, dict(tables))
    except MarkerError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if changed:
        print(f"rewrote the contract blocks in {path}")
    else:
        print(f"{path} is already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
