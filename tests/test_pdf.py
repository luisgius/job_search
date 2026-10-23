"""Tests for src/pdf.py — the user-supplied PDF hook indirection.

`src/render_pdf.py` is deliberately NOT shipped: the user writes it (there is
a working `render_pdf.example.py` to copy). Its absence is a real, supported
state, and it has a safety consequence — no PDF means auto-apply is skipped
and the job goes to the digest instead, because submitting an application
without the CV it promised to attach is worse than not submitting.

So `render_if_available` must be strict about what counts as success. Every
half-failure below (hook missing, `render` not callable, raises, writes
nothing, writes an empty file) has to return None.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import pdf as pdf_module
from src.pdf import MISSING_HOOK_MESSAGE, available, render_if_available

MARKDOWN = "# Ada Lovelace\n\n## Summary\nSenior backend engineer.\n"


class Hook:
    """Stand-in for a user's `src/render_pdf.py`."""

    def __init__(self, behaviour="write"):
        self.behaviour = behaviour
        self.calls: list[tuple[str, str]] = []

    def render(self, cv_markdown: str, out_path: str) -> None:
        self.calls.append((cv_markdown, str(out_path)))
        if self.behaviour == "raise":
            raise RuntimeError("reportlab exploded")
        if self.behaviour == "nothing":
            return
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.behaviour == "empty":
            target.write_bytes(b"")
        else:
            target.write_bytes(b"%PDF-1.4\nfake pdf content")


# ==========================================================================
# the happy path
# ==========================================================================


def test_a_working_hook_produces_a_pdf(tmp_path: Path):
    hook = Hook()
    out = tmp_path / "cv.pdf"
    assert render_if_available(MARKDOWN, out, module=hook) == str(out)
    assert out.exists() and out.stat().st_size > 0


def test_the_hook_receives_the_markdown_and_the_destination(tmp_path: Path):
    hook = Hook()
    out = tmp_path / "cv.pdf"
    render_if_available(MARKDOWN, out, module=hook)
    assert hook.calls == [(MARKDOWN, str(out))]


def test_nested_output_directories_are_created(tmp_path: Path):
    out = tmp_path / "applications" / "acme-1234" / "cv.pdf"
    assert render_if_available(MARKDOWN, out, module=Hook()) is not None
    assert out.exists()


def test_available_reflects_a_usable_hook():
    assert available(module=Hook()) is True
    assert available(module=None) is False


# ==========================================================================
# every way the hook can fail -> None
# ==========================================================================


def test_a_missing_hook_returns_none(tmp_path: Path):
    """The default state of a fresh checkout. Must be quiet and non-fatal."""
    assert render_if_available(MARKDOWN, tmp_path / "cv.pdf", module=None) is None


def test_a_module_without_render_returns_none(tmp_path: Path):
    class NoRender:
        pass

    assert render_if_available(MARKDOWN, tmp_path / "cv.pdf", module=NoRender()) is None


def test_a_non_callable_render_returns_none(tmp_path: Path):
    class BadRender:
        render = "not a function"

    assert render_if_available(MARKDOWN, tmp_path / "cv.pdf", module=BadRender()) is None


def test_a_hook_that_raises_returns_none(tmp_path: Path):
    """A user's half-finished ReportLab script must not take the run down."""
    out = tmp_path / "cv.pdf"
    assert render_if_available(MARKDOWN, out, module=Hook("raise")) is None
    assert not out.exists()


def test_a_hook_that_writes_nothing_returns_none(tmp_path: Path):
    """Returning the path of a file that does not exist would make
    `eligible()` believe a PDF is attached, and auto-apply would submit
    without one."""
    assert render_if_available(MARKDOWN, tmp_path / "cv.pdf",
                               module=Hook("nothing")) is None


def test_a_hook_that_writes_an_empty_file_returns_none(tmp_path: Path):
    out = tmp_path / "cv.pdf"
    assert render_if_available(MARKDOWN, out, module=Hook("empty")) is None


def test_failures_are_logged_with_a_reason(tmp_path: Path, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        render_if_available(MARKDOWN, tmp_path / "cv.pdf", module=Hook("raise"))
    assert caplog.text.strip()


# ==========================================================================
# the hook is genuinely absent from the repo
# ==========================================================================


def test_render_pdf_is_not_shipped():
    """If this ever fails, someone committed a personal PDF generator — and
    the "no PDF -> digest" path silently stops being exercised in the wild.

    "Shipped" means TRACKED, not present: the user's machine is supposed to
    have src/render_pdf.py (they wrote it, git ignores it), and the suite
    must pass there too."""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "src/render_pdf.py"], cwd=root,
            capture_output=True, check=True, timeout=30,
        ).stdout.decode().strip()
    except Exception:
        pytest.skip("not a git checkout — nothing is 'shipped' here")
    assert not tracked, "src/render_pdf.py is committed — it must stay personal"
    assert (root / "src" / "render_pdf.example.py").exists()


def test_the_example_hook_exposes_the_contract_signature():
    import ast

    root = Path(__file__).resolve().parent.parent
    source = (root / "src" / "render_pdf.example.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    render = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "render"), None)
    assert render is not None, "the example must define render()"
    assert [a.arg for a in render.args.args] == ["cv_markdown", "out_path"]


def test_the_example_does_not_import_reportlab_at_module_level():
    """Otherwise `pytest` on a fresh checkout fails at collection time."""
    import ast

    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "src" / "render_pdf.example.py").read_text(encoding="utf-8"))

    imported: list[str] = []
    for node in tree.body:            # module level only — nested imports are fine
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not any("reportlab" in name for name in imported), imported


def test_the_missing_hook_message_tells_the_user_what_to_do():
    assert "render_pdf" in MISSING_HOOK_MESSAGE
    assert "example" in MISSING_HOOK_MESSAGE.lower()


def test_importing_the_absent_hook_is_not_an_error(tmp_path: Path):
    """With no `module=` given, the real import is attempted — and on a
    checkout without the hook that must resolve to None rather than
    ImportError. On the user's machine the hook legitimately exists, and
    then this absent-state path simply cannot be exercised for real."""
    import importlib.util

    if importlib.util.find_spec("src.render_pdf") is not None:
        pytest.skip("a local src/render_pdf.py is installed — the absent "
                    "state is covered by the module=None tests instead")
    assert render_if_available(MARKDOWN, tmp_path / "cv.pdf") is None
    assert pdf_module.available() is False


# ==========================================================================
# the example hook keeps a heading with its content
# ==========================================================================


def _load_example():
    """`render_pdf.example.py` is not importable by name (the dot), and the
    user's own src/render_pdf.py may differ, so load the shipped file."""
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "render_pdf_example", root / "src" / "render_pdf.example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _placed(example, markdown, out, monkeypatch) -> list[tuple[int, str]]:
    """`(page, text)` of each flowable `render` placed — a flowable without
    text by its type name."""
    platypus = pytest.importorskip("reportlab.platypus")
    placed: list[tuple[int, str]] = []

    class Recording(platypus.SimpleDocTemplate):
        def afterFlowable(self, flowable):
            plain = getattr(flowable, "getPlainText", None)
            placed.append((self.page, plain() if callable(plain)
                           else type(flowable).__name__))

    monkeypatch.setattr(platypus, "SimpleDocTemplate", Recording)
    example.render(markdown, str(out))
    return placed


def _heading_and_bullet_pages(example, markdown, out, monkeypatch):
    """Pages on which "Project heading" and its first bullet were placed."""
    placed = _placed(example, markdown, out, monkeypatch)
    at = next(i for i, (_, text) in enumerate(placed) if text == "Project heading")
    # A list that fits is placed whole; one that does not is split per item.
    bullet = next(page for page, text in placed[at + 1:]
                  if text in ("ListFlowable", "first bullet"))
    return placed[at][0], bullet


def test_a_heading_at_the_foot_of_a_page_moves_down_with_its_first_bullet(
        tmp_path: Path, monkeypatch):
    pytest.importorskip("reportlab")
    example = _load_example()
    keep = example.HEADING_KEEP_PT

    def cv(lines: int) -> str:
        filler = "\n\n".join(f"Filler line {i}." for i in range(lines))
        return (f"# Ada Lovelace\n\n{filler}\n\n### Project heading\n\n"
                "- first bullet\n- second bullet\n")

    # With the keep switched off, find the CV length that strands the
    # heading: room on page 1 for the heading, none for its first bullet.
    monkeypatch.setattr(example, "HEADING_KEEP_PT", 0)
    stranded = next(
        (n for n in range(20, 80) if _heading_and_bullet_pages(
            example, cv(n), tmp_path / f"off-{n}.pdf", monkeypatch) == (1, 2)),
        None,
    )
    assert stranded is not None, "no CV length strands the heading — premise lost"

    monkeypatch.setattr(example, "HEADING_KEEP_PT", keep)
    assert _heading_and_bullet_pages(
        example, cv(stranded), tmp_path / "on.pdf", monkeypatch) == (2, 2)


def test_a_section_heading_moves_down_with_the_sub_heading_under_it(
        tmp_path: Path, monkeypatch):
    """`## Projects` right above `### Project heading`: each heading's own
    break moved the `###` and left the `##` alone at the foot of page 1 (13 of
    80 trimmed variants of the real CV). One break now covers the run."""
    pytest.importorskip("reportlab")
    example = _load_example()
    sections = []
    for lines in range(30, 46):
        filler = "\n\n".join(f"Filler line {i}." for i in range(lines))
        placed = _placed(example, f"# Ada Lovelace\n\n{filler}\n\n## Projects\n\n"
                         "### Project heading\n\n- first bullet\n- second bullet\n",
                         tmp_path / f"pair-{lines}.pdf", monkeypatch)
        section = next(page for page, text in placed if text == "Projects")
        at = next(i for i, (_, text) in enumerate(placed) if text == "Project heading")
        bullet = next(page for page, text in placed[at + 1:]
                      if text in ("ListFlowable", "first bullet"))
        assert section == placed[at][0] == bullet, f"{lines} filler lines"
        sections.append(section)
    assert {1, 2} <= set(sections), "the CVs no longer cross the foot of page 1"


# ==========================================================================
# pdf.verify — reading the PDF back as text
# ==========================================================================


def _pages(*texts: str):
    """A fake extractor returning `texts` as the pages, whatever the file."""
    return lambda path: list(texts)


def _fake_pdf(tmp_path: Path, body: bytes = b"") -> Path:
    out = tmp_path / "cv.pdf"
    out.write_bytes(b"%PDF-1.4\n" + body)
    return out


def test_verify_passes_a_clean_pdf(tmp_path: Path):
    """Name split over a line and in another case still counts as present —
    a false alarm under `block` would withhold a good CV."""
    problems = pdf_module.verify(
        _fake_pdf(tmp_path),
        require=("Ada Lovelace", "ada@example.com"), max_pages=2,
        headings=("Experience",),
        extractor=_pages("ADA\nLOVELACE · ada@example.com\nExperience\nBuilt it",
                         "More\nExperience"),
    )
    assert problems == []


def test_verify_flags_a_box_character(tmp_path: Path):
    problems = pdf_module.verify(
        _fake_pdf(tmp_path), extractor=_pages("Ada Lovelace ■ non■breaking"))
    assert len(problems) == 1 and "2 ■" in problems[0]


def test_verify_flags_replacement_and_cid_glyphs(tmp_path: Path):
    problems = pdf_module.verify(
        _fake_pdf(tmp_path), extractor=_pages("caf� (cid:12)"))
    assert len(problems) == 2


def test_verify_flags_a_missing_email(tmp_path: Path):
    problems = pdf_module.verify(
        _fake_pdf(tmp_path), require=("Ada Lovelace", "ada@example.com"),
        extractor=_pages("Ada Lovelace\nBerlin"),
    )
    assert len(problems) == 1 and "ada@example.com" in problems[0]


def test_verify_flags_page_overflow(tmp_path: Path):
    problems = pdf_module.verify(
        _fake_pdf(tmp_path), max_pages=2, extractor=_pages("one", "two", "three"))
    assert problems == ["3 pages, over the limit of 2"]


def test_verify_flags_a_heading_stranded_at_the_foot_of_a_page(tmp_path: Path):
    problems = pdf_module.verify(
        _fake_pdf(tmp_path),
        headings=pdf_module.headings_of("## Projects\n### **RAG** [System](https://x)\n"),
        extractor=_pages("Ada\nProjects\nRAG System", "- built it\nProjects"),
    )
    # The last page may end on anything; only page 1 is a problem.
    assert len(problems) == 1 and "page 1" in problems[0] and "RAG System" in problems[0]


def test_a_pdf_the_extractor_cannot_read_is_a_problem(tmp_path: Path):
    def broken(path):
        raise RuntimeError("pdftotext exited 1: Syntax Error")

    problems = pdf_module.verify(_fake_pdf(tmp_path), extractor=broken)
    assert len(problems) == 1 and "Syntax Error" in problems[0]


def test_no_extractor_skips_the_text_checks(tmp_path: Path):
    """No pdftotext and no pypdf on this machine: nothing can be read, so
    nothing is flagged — verification must never cost a PDF."""
    problems = pdf_module.verify(
        _fake_pdf(tmp_path), require=("someone else",), max_pages=1,
        extractor=lambda path: None,
    )
    assert problems == []


def test_no_extractor_still_counts_pages_from_the_page_tree(tmp_path: Path):
    """ReportLab writes `<< /Count N /Kids [...] /Type /Pages >>` uncompressed."""
    out = _fake_pdf(tmp_path, b"1 0 obj\n<<\n/Count 3 /Kids [ 4 0 R 5 0 R 6 0 R ] "
                              b"/Type /Pages\n>>\nendobj\n")
    assert pdf_module.page_count(out) == 3
    problems = pdf_module.verify(out, max_pages=2, extractor=lambda path: None)
    assert problems == ["3 pages, over the limit of 2"]


def test_with_no_tools_installed_extraction_reports_none(tmp_path: Path, monkeypatch):
    import sys

    monkeypatch.setattr(pdf_module, "find_pdftotext", lambda override="": None)
    monkeypatch.setitem(sys.modules, "pypdf", None)   # import -> ImportError
    assert pdf_module.extract_pages(_fake_pdf(tmp_path)) is None


def test_pdftotext_is_found_in_homebrew_when_path_is_bare(tmp_path: Path, monkeypatch):
    """launchd runs scripts/run_daily.sh with no Homebrew on PATH."""
    brew = tmp_path / "brew"
    brew.mkdir()
    binary = brew / "pdftotext"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(pdf_module, "PDFTOTEXT_DIRS", (str(brew),))
    assert pdf_module.find_pdftotext() == str(binary)
    assert pdf_module.find_pdftotext(str(binary)) == str(binary)


# ==========================================================================
# pdf.verify in the pipeline: off | warn | block
# ==========================================================================


def _rendered_job(tmp_path: Path, monkeypatch, problems: list[str]):
    """A tailored job whose PDFs render, and a `pdf.verify` that finds
    `problems`; returns the job and the list of verify calls."""
    from tests.conftest import make_scored

    item = make_scored(score=95)
    item.artifacts.dir = str(tmp_path / "out")
    item.tailored_cv_md = MARKDOWN
    item.cover_letter_md = "Dear team,\n\nAda Lovelace\n"

    def render(markdown, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"%PDF-1.4\nfake")
        return str(out_path)

    calls: list[dict] = []

    def verify(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return list(problems)

    monkeypatch.setattr(pdf_module, "available", lambda **kwargs: True)
    monkeypatch.setattr(pdf_module, "render_if_available", render)
    monkeypatch.setattr(pdf_module, "verify", verify)
    return item, calls


@pytest.mark.parametrize("setting", [None, "off", False])
def test_verify_off_is_never_called(tmp_path: Path, monkeypatch, setting):
    """`False` is what YAML makes of a bare `verify: off`."""
    from src import main as main_module
    from src.models import RunStats
    from tests.conftest import write_config

    overrides = {} if setting is None else {"pdf": {"verify": setting}}
    config = write_config(tmp_path, overrides)
    item, calls = _rendered_job(tmp_path, monkeypatch, ["1 ■ boxes"])
    stats = RunStats()
    main_module._render_pdfs([item], stats, config)
    assert calls == []
    assert item.artifacts.cv_pdf and item.artifacts.cover_pdf
    assert stats.errors == []


def test_verify_warn_records_the_problem_and_keeps_the_pdf(tmp_path: Path, monkeypatch):
    from src import main as main_module
    from src.models import RunStats
    from tests.conftest import write_config

    config = write_config(tmp_path, {"pdf": {"verify": "warn"}})
    item, calls = _rendered_job(tmp_path, monkeypatch, ["1 ■ boxes"])
    stats = RunStats()
    main_module._render_pdfs([item], stats, config)

    assert item.artifacts.cv_pdf
    cv_call = calls[0]
    assert cv_call["require"] == ("Ada Lovelace", "ada@example.com")
    assert cv_call["max_pages"] == 2 and cv_call["headings"] == ["Ada Lovelace", "Summary"]
    assert calls[1]["max_pages"] == 1          # the cover letter
    assert any("CV PDF check" in error for error in stats.errors)
    assert "CV PDF check: 1 ■ boxes" in item.status_detail


def test_verify_block_leaves_cv_pdf_unset_so_the_job_goes_to_the_digest(
        tmp_path: Path, monkeypatch):
    from src import main as main_module
    from src.apply.autoapply import eligible
    from src.models import RunStats
    from tests.conftest import write_config

    config = write_config(tmp_path, {"pdf": {"verify": "block"}})
    item, _ = _rendered_job(tmp_path, monkeypatch, ["3 pages, over the limit of 2"])
    stats = RunStats()
    main_module._render_pdfs([item], stats, config)

    assert item.artifacts.cv_pdf is None
    assert (tmp_path / "out" / "cv.pdf").exists()     # kept for the user to read
    assert any("over the limit" in error for error in stats.errors)
    ok, reason = eligible(item, config)
    assert not ok and "require_pdf" in reason


def test_a_clean_pdf_is_kept_under_block(tmp_path: Path, monkeypatch):
    from src import main as main_module
    from src.models import RunStats
    from tests.conftest import write_config

    config = write_config(tmp_path, {"pdf": {"verify": "block"}})
    item, calls = _rendered_job(tmp_path, monkeypatch, [])
    stats = RunStats()
    main_module._render_pdfs([item], stats, config)
    assert len(calls) == 2 and item.artifacts.cv_pdf and stats.errors == []


@pytest.mark.parametrize("settings, bad", [
    ({"verify": "warn"}, False),
    ({"verify": False}, False),
    ({"verify": "maybe"}, True),
    ({"verify": True}, True),        # YAML `on`: warn or block? refuse to guess
    ({"cv_max_pages": 0}, True),
    ({"cover_max_pages": "one"}, True),
])
def test_pdf_settings_are_validated(tmp_path: Path, settings, bad):
    from tests.conftest import write_config

    problems = write_config(tmp_path, {"pdf": settings}).validate(require_llm=False)
    assert any(p.startswith("pdf.") for p in problems) is bad
