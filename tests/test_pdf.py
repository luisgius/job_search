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
