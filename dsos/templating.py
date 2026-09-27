"""Templates: the consistency layer for charts and published reports.

Two kinds, one mechanism:

- **chart styles** (matplotlib rcParams in .mplstyle syntax) — selected via
  run_python's `style` parameter, so every chart from every session gets the
  same house look by default.
- **report templates** (HTML with `{{title}}` / `{{body}}` / `{{published_at}}`
  / `{{session_question}}` tokens) — selected via publish_report's `template`
  parameter, so every published report shares one layout.

Built-ins live as package assets (dsos/assets/), versioned with the code and
always resolvable — they are files, not artifacts, because the default style
must exist before any store row does. Custom templates are `template` type
artifacts (created/versioned via the save_template tool, tags ["template",
<kind>]) and are referenced by row_id or artifact_id, exactly like every
other artifact. Seeding stays out of this on purpose: a customized template
belongs to the store that customized it; the built-ins are the baseline.

Token contract (why plain replacement, not str.format/Jinja): a report
template wraps *rendered narrative HTML*, which already contains the store's
own `{{artifact:<row_id>}}` embeds — format() would choke on unescaped CSS
braces and Jinja would try to parse `{{artifact:...}}` as an expression.
Literal token replacement ignores everything it doesn't recognize.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from dsos.store import Artifact, get_artifact, get_artifact_by_row_id, list_artifacts

ASSETS_DIR = Path(__file__).parent / "assets"
CHART_STYLES_DIR = ASSETS_DIR / "chartstyles"
REPORT_TEMPLATES_DIR = ASSETS_DIR / "reports"

TEMPLATE_TYPE = "template"
CHART_STYLE_KIND = "chart-style"
REPORT_KIND = "report"
TEMPLATE_KINDS = {CHART_STYLE_KIND, REPORT_KIND}

# name -> (one-line description, what it's for). The names are reserved: a
# style/template reference is checked against these BEFORE any artifact
# lookup, so a custom artifact id can never shadow the defaults.
CHART_STYLE_BUILTINS: dict[str, str] = {
    "dsos": "House style (the default): dark mode — dark canvas, light text, soft grid, muted spines.",
    "report": "For charts embedded in published reports: the dark house look at report scale (larger type, higher dpi).",
    "minimal": "Bare essentials on a light canvas: no grid, thin spines, near-raw matplotlib.",
}
REPORT_TEMPLATE_BUILTINS: dict[str, str] = {
    "report": "House report layout (the default): header with title/date/question, styled artifact embeds, footer.",
    "default": "Plain single-column page — the original DSOS publish look.",
    "minimal": "Bare HTML: title, body, almost no styling.",
}

# Report templates must at minimum say where the rendered body goes; a
# template without {{body}} would silently publish an empty report.
_BODY_TOKEN = "{{body}}"
_REPORT_TOKENS = ("{{title}}", "{{body}}", "{{published_at}}", "{{session_question}}")


# ---------------------------------------------------------------- resolution


def _lookup_template_artifact(conn: sqlite3.Connection, ref: str) -> Artifact | None:
    """A template reference by row_id (what list_templates/save_template
    returned) or artifact_id (the stable id, so 'my-brand' style survives
    version edits). Latest version, content lazy."""
    return (
        get_artifact_by_row_id(conn, ref, load_content=False)
        or get_artifact(conn, ref, load_content=False)
    )


def _custom_template_refs(conn: sqlite3.Connection, kind: str) -> list[str]:
    """Available custom template ids for a kind, for error messages — the
    agent should be able to fix a bad reference from the error alone."""
    return [
        a.artifact_id
        for a in list_artifacts(conn, type=TEMPLATE_TYPE)
        if kind in a.tags
    ]


def resolve_chart_style(conn: sqlite3.Connection, style: str | None) -> str | None:
    """A `style` reference -> a matplotlib-style path that plt.style.use
    accepts. None/"" -> no styling at all (raw matplotlib defaults).

    Raises ValueError with the full list of options on an unknown reference,
    *before* any code runs — a bad style name should fail in one call, not
    as a matplotlib error deep inside the user's chart code.
    """
    if style is None or style == "":
        return None
    if style in CHART_STYLE_BUILTINS:
        return str(CHART_STYLES_DIR / f"{style}.mplstyle")

    art = _lookup_template_artifact(conn, style)
    if art is not None:
        if art.type != TEMPLATE_TYPE:
            raise ValueError(
                f"style {style!r} is a {art.type!r} artifact, not a chart-style "
                f"template — create chart styles with save_template(kind='chart-style')"
            )
        if CHART_STYLE_KIND not in art.tags:
            raise ValueError(
                f"style {style!r} is a {template_kind(art) or 'template'} template, "
                f"not a chart-style one"
            )
        # Absolute: this path gets handed to a subprocess whose cwd is a
        # throwaway tempdir, not wherever the server started from — a
        # relative content_ref (DSOS_DB_PATH unset/relative) would silently
        # resolve against the wrong directory there.
        return str(Path(art.content_ref).resolve())

    raise ValueError(
        f"unknown chart style {style!r}. Built-ins: "
        f"{', '.join(sorted(CHART_STYLE_BUILTINS))}. Custom chart-style templates: "
        f"{', '.join(_custom_template_refs(conn, CHART_STYLE_KIND)) or '(none saved yet — create one with save_template)'}"
    )


def resolve_report_template(conn: sqlite3.Connection, template: str) -> str:
    """A `template` reference -> the template's HTML text (tokens intact,
    ready for render_report). Raises ValueError listing the options if the
    reference matches neither a built-in name nor a template artifact."""
    if template in REPORT_TEMPLATE_BUILTINS:
        return (REPORT_TEMPLATES_DIR / f"{template}.html").read_text(encoding="utf-8")

    art = _lookup_template_artifact(conn, template)
    if art is not None:
        if art.type != TEMPLATE_TYPE or REPORT_KIND not in art.tags:
            raise ValueError(
                f"template {template!r} is a {art.type}/{template_kind(art) or 'template'}, "
                f"not a report template"
            )
        full = get_artifact_by_row_id(conn, art.row_id, load_content=True)
        if full.content_error:
            raise ValueError(
                f"report template {template!r}: its content blob is missing on disk "
                f"({full.content_error}) — re-save the template"
            )
        text = full.content
        if _BODY_TOKEN not in text:
            raise ValueError(
                f"report template {template!r} has no {_BODY_TOKEN} token — publish "
                f"would render an empty report. Tokens: {', '.join(_REPORT_TOKENS)}"
            )
        return text

    raise ValueError(
        f"unknown report template {template!r}. Built-ins: "
        f"{', '.join(sorted(REPORT_TEMPLATE_BUILTINS))}. Custom report templates: "
        f"{', '.join(_custom_template_refs(conn, REPORT_KIND)) or '(none saved yet — create one with save_template)'}"
    )


# ------------------------------------------------------------------ applying


def base_template_content(conn: sqlite3.Connection, kind: str, base: str) -> str:
    """The text of an existing template — a built-in name or a custom
    template's row_id/artifact_id — for save_template's base= parameter:
    customize an existing template instead of rewriting one from scratch.
    Resolved through the same paths that select it at use time, so what you
    copied is exactly what run_python/publish_report would have rendered."""
    if kind == CHART_STYLE_KIND:
        return Path(resolve_chart_style(conn, base)).read_text(encoding="utf-8")
    return resolve_report_template(conn, base)


def sandbox_style_block(style_path: str | None) -> str:
    """The wrapper lines that apply a chart style inside the run_python
    subprocess: rcdefaults (so a previous style can't leak in), style, then
    re-force Agg (rcdefaults resets the backend rcParam too, and every
    other backend assumes a GUI main-loop thread this subprocess doesn't
    have). Empty when the run asked for no style."""
    if not style_path:
        return ""
    return (
        "try:\n"
        "    import matplotlib as _dsos_mpl\n"
        "    import matplotlib.pyplot as _dsos_plt\n"
        "    _dsos_plt.rcdefaults()\n"
        f"    _dsos_plt.style.use({style_path!r})\n"
        "    _dsos_mpl.use('Agg', force=True)\n"
        "except ImportError:\n"
        "    pass\n"
    )


# ---------------------------------------------------------------- validation


# Run inside `python_path` (never this server process — the server no
# longer requires matplotlib at all). Mirrors the old in-process check:
# mpl's rc parser WARN-and-skips lines with no colon and silently drops
# unknown keys, so arbitrary prose "parses" to an empty config instead of
# raising — an empty config means this wasn't a style at all, which is
# exactly the save-time failure to catch.
_VALIDATE_STYLE_SCRIPT = """\
import sys
try:
    import matplotlib
except ImportError:
    print("DSOS_NO_MATPLOTLIB", file=sys.stderr)
    sys.exit(1)
try:
    rc = matplotlib.rc_params_from_file(sys.argv[1], fail_on_error=True, use_default_template=False)
except Exception as exc:
    print(f"DSOS_PARSE_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
if not rc:
    print("DSOS_EMPTY_RCPARAMS", file=sys.stderr)
    sys.exit(1)
"""


def validate_chart_style_text(content: str, python_path: str) -> None:
    """A custom chart-style template must parse as matplotlib rcParams —
    checked at save_template time (fail fast, one call), not first use
    (which would surface as a confusing mid-chart error two rounds later).
    Runs matplotlib's own parser inside `python_path` (validated against
    DEFAULT_PYTHON_PATH by mcp_server.py — save_template has no run_python-
    style per-call override) so the accepted syntax is exactly what
    plt.style.use will later accept for a run_python call against that same
    interpreter."""
    from dsos.sandbox import run_interpreter

    if not Path(python_path).is_absolute():
        raise ValueError(
            f"python_path {python_path!r} must be an absolute path — a relative "
            "one resolves against this server's launch cwd, not the caller's"
        )
    if not Path(python_path).is_file():
        raise ValueError(f"python_path {python_path!r} is not a file")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mplstyle", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        path = f.name
    try:
        proc = run_interpreter(python_path, ["-c", _VALIDATE_STYLE_SCRIPT, path], timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)  # delete=False keeps it alive while open

    if proc.returncode == 0:
        return
    stderr = proc.stderr or ""
    if "DSOS_NO_MATPLOTLIB" in stderr:
        raise ValueError(
            f"chart-style validation needs matplotlib installed in {python_path!r} "
            "(the default run_python interpreter) — install it there, or point "
            "DSOS_PYTHON_PATH at an interpreter that has it"
        )
    if "DSOS_EMPTY_RCPARAMS" in stderr:
        raise ValueError(
            "chart-style template contains no valid rcParams lines — expected "
            "'key: value' lines like 'axes.grid: True' (see the built-ins via "
            "save_template(base='dsos'))"
        )
    raise ValueError(
        f"chart-style template does not parse as matplotlib rcParams: "
        f"{stderr.strip() or f'exit code {proc.returncode}'}"
    )


def validate_report_template_text(content: str) -> None:
    """A custom report template must declare where the rendered body goes.
    The other tokens are optional (a template may hardcode a title style);
    {{body}} is the one without which the output is a lie."""
    if _BODY_TOKEN not in content:
        raise ValueError(
            f"a report template must contain {_BODY_TOKEN} (where the rendered "
            f"narrative goes). Available tokens: {', '.join(_REPORT_TOKENS)}"
        )


# ----------------------------------------------------------------- rendering

# Body last, always: every other token is substituted while the body's HTML
# is still out of the string, so rendered content can never be re-scanned
# for template tokens (a narrative that literally writes "{{title}}" in
# its text must stay untouched, not get substituted a second time).
def render_report(
    template_text: str, *, title: str, body: str,
    published_at: str, session_question: str | None,
) -> str:
    html = template_text
    for token, value in (
        ("{{title}}", title),
        ("{{published_at}}", published_at),
        ("{{session_question}}", session_question or ""),
    ):
        html = html.replace(token, value)
    return html.replace(_BODY_TOKEN, body)


# -------------------------------------------------------------------- listing


def template_kind(art: Artifact) -> str | None:
    """A template artifact's kind, from its tags (["template",
    "chart-style"] or ["template", "report"]) — the same convention
    save_template writes, so a template saved through the generic
    save_artifact with the right tags is still recognized."""
    for tag in art.tags:
        if tag in TEMPLATE_KINDS:
            return tag
    return None


def template_tags(kind: str) -> list[str]:
    """The tag set save_template writes for a kind — single source for the
    convention both the writer (save_template) and readers (resolve/list)
    share."""
    if kind not in TEMPLATE_KINDS:
        raise ValueError(f"template kind must be one of {sorted(TEMPLATE_KINDS)}, got {kind!r}")
    return ["template", kind]
