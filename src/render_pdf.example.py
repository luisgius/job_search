# cp src/render_pdf.example.py src/render_pdf.py and adapt
"""A working starting point for the PDF hook `src/pdf.py` looks for.

Copy it to `src/render_pdf.py` (that filename is deliberately yours, not
mine — this file will never be imported), `pip install reportlab`, then bend
the styles until the output looks like a CV you would send.

The contract is one function:

    render(cv_markdown: str, out_path: str) -> None

It must leave a non-empty PDF at `out_path`; `pdf.render_if_available`
checks and treats anything else as "no PDF", which in turn means the job
goes to the digest instead of being auto-applied.

Supported markdown — the subset `tailor.py` actually emits:

    # H1 / ## H2 / ### H3      headings
    - bullet   * bullet        bullet lists
    1. item                    numbered lists
    ---                        horizontal rule
    **bold**  *italic*  `code`  [text](url)
    blank-line separated       paragraphs

`parse_blocks` is pure and reportlab-free on purpose: the parsing half can
be poked at in a REPL (or a test) without a PDF engine installed.
"""

from __future__ import annotations

import re

# -- page geometry (A4, margins wide enough to print without clipping) -----
PAGE_MARGIN_MM = 18.0
BODY_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"
BODY_SIZE = 9.6
LINE_SPACING = 1.32

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
# A rule is three or more of the same marker, optionally spaced: --- *** ___
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s{0,3}\d+[.)]\s+(.*)$")

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Single * or _ that is not part of ** and not glued to a word character, so
# snake_case identifiers and bare asterisks survive unharmed.
_ITALIC_RE = re.compile(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)\s*\)")


def inline_markup(text: str) -> str:
    """Markdown inline spans -> ReportLab's mini-HTML.

    Escaping happens *first*: a CV that mentions "C++ & <T>" must not turn
    into broken markup (ReportLab raises on malformed tags, which would fail
    the whole render).
    """
    out = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    out = _LINK_RE.sub(r'<link href="\2" color="#1a56b0">\1</link>', out)
    out = _CODE_RE.sub(r'<font face="Courier">\1</font>', out)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)          # before italics: ** wins over *
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    return out


def parse_blocks(markdown: str) -> list[tuple[str, object]]:
    """Split markdown into `(kind, payload)` blocks, top to bottom.

    Kinds: "h1".."h6" (str), "bullets" / "ordered" (list[str]), "rule"
    (None), "para" (str). Consecutive list items collapse into one block so
    the renderer can emit a single indented list.
    """
    blocks: list[tuple[str, object]] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_kind = ""

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(("para", " ".join(paragraph).strip()))
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if items:
            blocks.append((list_kind or "bullets", list(items)))
            items.clear()
        list_kind = ""

    for raw_line in str(markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()

        if not line.strip():
            flush_paragraph()
            flush_list()
            continue

        if _RULE_RE.match(line):
            flush_paragraph()
            flush_list()
            blocks.append(("rule", None))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append((f"h{len(heading.group(1))}", heading.group(2).strip()))
            continue

        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line) if not bullet else None
        if bullet or ordered:
            flush_paragraph()
            kind = "bullets" if bullet else "ordered"
            if list_kind and kind != list_kind:
                flush_list()
            list_kind = kind
            items.append((bullet or ordered).group(1).strip())
            continue

        # A line inside a list that is not a marker continues the last item;
        # anything else is ordinary prose.
        if items:
            items[-1] = f"{items[-1]} {line.strip()}"
            continue
        paragraph.append(line.strip())

    flush_paragraph()
    flush_list()
    return blocks


def render(cv_markdown: str, out_path: str) -> None:
    """Write `cv_markdown` to a single-column A4 PDF at `out_path`.

    Raises whatever ReportLab raises — `pdf.render_if_available` catches it,
    logs it and falls back to "no PDF", so failing loudly here is fine.
    """
    # Imported here, not at module scope: the rest of the pipeline (and its
    # test suite) must import cleanly without reportlab installed.
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    blocks = parse_blocks(cv_markdown)
    title = next((str(text) for kind, text in blocks if kind == "h1"), "CV")

    body = ParagraphStyle(
        "body",
        fontName=BODY_FONT,
        fontSize=BODY_SIZE,
        leading=BODY_SIZE * LINE_SPACING,
        spaceAfter=4,
        textColor="#111111",
    )
    styles = {
        "h1": ParagraphStyle("h1", parent=body, fontName=BOLD_FONT, fontSize=18,
                             leading=21, spaceBefore=0, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=body, fontName=BOLD_FONT, fontSize=12,
                             leading=14, spaceBefore=11, spaceAfter=3,
                             textColor="#1a1a1a"),
        "h3": ParagraphStyle("h3", parent=body, fontName=BOLD_FONT, fontSize=10.4,
                             leading=12.5, spaceBefore=7, spaceAfter=2),
        "item": ParagraphStyle("item", parent=body, spaceAfter=1.5),
    }
    # h4-h6 are rare in a CV; render them like an h3 rather than crashing.
    for level in ("h4", "h5", "h6"):
        styles[level] = styles["h3"]

    flowables: list[object] = []
    for kind, payload in blocks:
        if kind == "rule":
            flowables.append(Spacer(1, 3))
            flowables.append(HRFlowable(width="100%", thickness=0.6,
                                        color="#bbbbbb", spaceAfter=6))
        elif kind in ("bullets", "ordered"):
            entries = [
                ListItem(Paragraph(inline_markup(text), styles["item"]),
                         leftIndent=12, value=index + 1)
                for index, text in enumerate(payload or [])  # type: ignore[arg-type]
            ]
            if entries:
                flowables.append(ListFlowable(
                    entries,
                    bulletType="bullet" if kind == "bullets" else "1",
                    start="•" if kind == "bullets" else 1,
                    bulletFontSize=BODY_SIZE,
                    leftIndent=12,
                    spaceAfter=5,
                ))
        elif kind in styles:
            flowables.append(Paragraph(inline_markup(str(payload)), styles[kind]))
        else:
            flowables.append(Paragraph(inline_markup(str(payload)), body))

    margin = PAGE_MARGIN_MM * mm
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=title,
        author=title,
        subject="Curriculum Vitae",
    )
    # An empty document would produce a 0-byte file, which pdf.py reads as
    # failure; one blank paragraph keeps that path honest.
    doc.build(flowables or [Paragraph("", body)])
