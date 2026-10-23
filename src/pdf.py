"""Optional PDF rendering, behind a user-supplied hook.

Turning markdown into a PDF that looks like *your* CV is a taste problem, not
an engineering one, so this module refuses to guess: it looks for
`src/render_pdf.py` — a file the user writes (starting from
`src/render_pdf.example.py`) — and calls its `render(markdown, out_path)`.

The absence of that hook is a supported, boring state, not an error:

    no hook  ->  no PDF  ->  auto-apply is skipped  ->  the job goes to the
                                                        digest for one click.

Because that consequence is real, `render_if_available` is deliberately
strict. It returns a path *only* when a non-empty file actually exists on
disk afterwards; a hook that returns cleanly without writing anything, or
writes a zero-byte file, is a failure. Anything less would let auto-apply
upload an empty "CV".

`verify` goes one step further, when `pdf.verify` asks it to: it reads the
PDF back as text — what an ATS parser will see — and lists what is wrong.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

from .util import get_logger

logger = get_logger(__name__)

#: Module name of the hook, resolved inside this package (`src.render_pdf`).
HOOK_MODULE = "render_pdf"
HOOK_PATH = "src/render_pdf.py"
EXAMPLE_PATH = "src/render_pdf.example.py"

MISSING_HOOK_MESSAGE = (
    f"No PDF renderer: {HOOK_PATH} does not exist. Tailored CVs stay as "
    "markdown and every match goes to the digest instead of being "
    "auto-applied.\n"
    f"  1. cp {EXAMPLE_PATH} {HOOK_PATH}\n"
    "  2. pip install reportlab\n"
    f"  3. edit {HOOK_PATH} to taste — it only has to expose\n"
    "         def render(cv_markdown: str, out_path: str) -> None\n"
    "     and leave a non-empty PDF at out_path."
)


def _import_hook() -> tuple[Any | None, str]:
    """Import the user's hook module. Returns `(module, reason_if_missing)`.

    Distinguishes "you never created the hook" (expected, explained by
    `MISSING_HOOK_MESSAGE`) from "your hook exists but blew up on import"
    (usually a missing `reportlab`), because the fixes are different.
    """
    # The hook is frequently created *while* the tool is in use; without this
    # a freshly written src/render_pdf.py stays invisible for the whole run.
    importlib.invalidate_caches()

    package = __package__ or "src"
    try:
        spec = importlib.util.find_spec(f".{HOOK_MODULE}", package)
    except (ImportError, ValueError) as exc:
        return None, f"could not look for {HOOK_PATH}: {exc}"
    if spec is None:
        return None, MISSING_HOOK_MESSAGE

    try:
        return importlib.import_module(f".{HOOK_MODULE}", package), ""
    except Exception as exc:
        # ImportError (no reportlab), SyntaxError, anything at module scope.
        return None, f"{HOOK_PATH} failed to import: {exc}"


#: Default for `module=`: "nothing injected, import the real hook". Distinct
#: from None on purpose — the user's machine legitimately HAS a hook
#: installed (src/render_pdf.py is theirs to write, git-ignored), so tests
#: need a way to say "behave as if there were none" that does not depend on
#: what this particular machine carries. `module=None` is that way.
_UNSET: Any = object()


def _resolve(module: Any) -> tuple[Any | None, str]:
    """Return `(render_callable, reason)` for an injected or imported hook."""
    hook = module
    if hook is _UNSET:
        hook, reason = _import_hook()
        if hook is None:
            return None, reason
    elif hook is None:
        return None, MISSING_HOOK_MESSAGE

    render = getattr(hook, "render", None)
    if not callable(render):
        return None, (
            f"{getattr(hook, '__name__', HOOK_PATH)} has no callable `render` — "
            "it must expose render(cv_markdown: str, out_path: str) -> None"
        )
    return render, ""


def available(*, module: Any = _UNSET) -> bool:
    """True when a usable PDF hook is importable and exposes `render`.

    Cheap enough to call before tailoring, so the run can say once, up front,
    that PDFs (and therefore auto-apply) are off. Never raises.
    `module=None` means "there is no hook" regardless of what this machine
    has installed; anything else is injected as the hook itself.
    """
    render, reason = _resolve(module)
    if render is None:
        logger.debug("PDF rendering unavailable: %s", reason.splitlines()[0])
    return render is not None


def render_if_available(
    markdown: str,
    out_path: str | Path,
    *,
    module: Any = _UNSET,
) -> str | None:
    """Render `markdown` to `out_path` via the user's hook.

    Returns the path as a string on success, or None — and None is a real
    decision, not an inconvenience: the apply stage treats "no PDF" as "do
    not touch this application", so every ambiguous outcome (no hook, no
    `render`, an exception, a missing or empty output file) must return None.

    `module=` injects a hook object directly and is the test seam;
    `module=None` forces the no-hook state, whatever this machine carries.
    """
    out = Path(out_path)

    if not str(markdown or "").strip():
        logger.warning("nothing to render: empty markdown for %s", out)
        return None

    render, reason = _resolve(module)
    if render is None:
        logger.warning("%s", reason)
        return None

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cannot create %s: %s", out.parent, exc)
        return None

    try:
        render(str(markdown), str(out))
    except Exception as exc:
        logger.warning("%s render() failed for %s: %s", HOOK_PATH, out, exc)
        return None

    try:
        size = out.stat().st_size
    except OSError:
        logger.warning(
            "%s render() returned but wrote no file at %s", HOOK_PATH, out
        )
        return None
    if size == 0:
        logger.warning("%s render() wrote a zero-byte file at %s", HOOK_PATH, out)
        return None

    if out.is_file() and not _looks_like_pdf(out):
        # A hook that shells out to a converter leaves the converter's error
        # page behind when it fails — HTML, non-empty, and named cv.pdf. Four
        # magic bytes are the whole check, and without them auto-apply
        # attaches "Conversion failed: no such binary" to an application as
        # the user's CV.
        #
        # Only the header is verified: a PDF that ReportLab started and never
        # finished still begins with %PDF, and structural validation is a much
        # heavier job than this function claims to do. A directory at
        # `out_path` cannot be read at all and is deliberately left to
        # `eligible()`'s `is_file()` check rather than conflated with this.
        logger.warning(
            "%s render() wrote %s, but it does not start with %%PDF — refusing "
            "to hand a non-PDF to an employer as a CV", HOOK_PATH, out
        )
        return None

    logger.info("wrote %s (%d bytes)", out, size)
    return str(out)


def _looks_like_pdf(path: Path) -> bool:
    """True when the file starts with the `%PDF` magic bytes."""
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"%PDF"
    except OSError as exc:
        logger.warning("could not read %s back after rendering: %s", path, exc)
        return False


# --------------------------------------------------------------------------
# reading a rendered PDF back
# --------------------------------------------------------------------------

#: Searched after PATH. launchd starts scripts/run_daily.sh with a bare PATH,
#: so a Homebrew pdftotext is invisible to `shutil.which` in a scheduled run.
PDFTOTEXT_DIRS: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")

#: Text that means a glyph did not survive rendering. ReportLab draws ■ for a
#: character its font lacks; U+FFFD and "(cid:N)" are what extractors emit
#: for a glyph they cannot map back to a character.
BAD_GLYPHS: dict[str, str] = {
    "\u25a0": "■ boxes (characters the font could not draw)",
    "\ufffd": "U+FFFD replacement characters",
    "(cid:": "(cid:N) glyphs with no text mapping",
}

#: Page texts for a PDF, or None when this machine has no way to extract them.
Extractor = Callable[[Path], "list[str] | None"]

_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$")
_LINK_TEXT_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# A dictionary with no dictionary nested inside it, for `page_count`.
_FLAT_DICT_RE = re.compile(rb"<<((?:(?!<<|>>).)*)>>", re.DOTALL)
_PAGES_TYPE_RE = re.compile(rb"/Type\s*/Pages\b")
_COUNT_RE = re.compile(rb"/Count\s+(\d+)")

_logged_once: set[str] = set()


def _log_once(message: str) -> None:
    """Warn about a machine-level gap once per process, not once per job."""
    if message not in _logged_once:
        _logged_once.add(message)
        logger.warning("%s", message)


def find_pdftotext(override: str = "") -> str | None:
    """Path of a usable `pdftotext`: `override` (pdf.pdftotext), PATH, then
    `PDFTOTEXT_DIRS`. None when there is none."""
    if override:
        found = shutil.which(override)
        if found:
            return found
        _log_once(f"pdf.pdftotext is {override!r}, which is not an executable "
                  "— looking for pdftotext elsewhere")
    found = shutil.which("pdftotext")
    if found:
        return found
    for directory in PDFTOTEXT_DIRS:
        found = shutil.which("pdftotext", path=directory)
        if found:
            return found
    return None


def extract_pages(path: str | Path, *, pdftotext: str = "") -> list[str] | None:
    """Text of each page via pdftotext, else pypdf if importable.

    None means this machine has no extractor. An extractor that exists but
    cannot read the file raises RuntimeError: that is news about the PDF.
    """
    binary = find_pdftotext(pdftotext)
    if binary:
        try:
            result = subprocess.run(
                [binary, "-enc", "UTF-8", str(path), "-"],
                capture_output=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"pdftotext failed: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "ignore").strip().splitlines()
            raise RuntimeError(
                f"pdftotext exited {result.returncode}"
                + (f": {detail[0]}" if detail else "")
            )
        # Every page, the last included, ends with a form feed.
        text = result.stdout.decode("utf-8", "ignore")
        return text.removesuffix("\f").split("\f")
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return [page.extract_text() or "" for page in PdfReader(str(path)).pages]
    except Exception as exc:
        raise RuntimeError(f"pypdf could not read it: {exc}") from exc


def page_count(path: str | Path) -> int | None:
    """Pages in the root of the page tree, read with a regex, no PDF library.

    Enough for ReportLab output, whose page tree is uncompressed; None (not a
    guess) when the tree sits inside a compressed object stream.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    counts = []
    for match in _FLAT_DICT_RE.finditer(data):
        body = match.group(1)
        count = _COUNT_RE.search(body) if _PAGES_TYPE_RE.search(body) else None
        if count:
            counts.append(int(count.group(1)))
    return max(counts) if counts else None


def headings_of(markdown: str) -> list[str]:
    """Plain text of each markdown heading, for `verify(headings=...)`."""
    headings = []
    for line in str(markdown or "").splitlines():
        match = _HEADING_LINE_RE.match(line)
        if match:
            text = _LINK_TEXT_RE.sub(r"\1", match.group(1))
            text = re.sub(r"[*`]", "", text).strip()
            if text:
                headings.append(text)
    return headings


def _flat(text: str) -> str:
    """Comparable form: NFKC, single spaces, casefolded."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).split()).casefold()


def verify(
    path: str | Path,
    *,
    require: Iterable[str] = (),
    max_pages: int | None = None,
    headings: Iterable[str] = (),
    pdftotext: str = "",
    extractor: Extractor | None = None,
) -> list[str]:
    """Read a rendered PDF back and list what is wrong with it; [] is clean.

    Flags `BAD_GLYPHS`, a `require` string (name, email) missing from the
    text, more than `max_pages` pages, and a non-final page whose last line
    is one of `headings` (its content starts on the next page).

    With no extractor on this machine only the page count is checked (via
    `page_count`) and the rest is skipped with one warning per run: the check
    is an extra, so a missing tool must never cost a PDF. `extractor`
    replaces the pdftotext/pypdf lookup; `pdftotext` overrides the binary.
    """
    target = Path(path)
    try:
        if extractor is None:
            pages = extract_pages(target, pdftotext=pdftotext)
        else:
            pages = extractor(target)
    except Exception as exc:
        return [f"its text could not be read back ({exc})"]

    problems: list[str] = []
    count = len(pages) if pages is not None else page_count(target)
    if max_pages is not None and count is not None and count > max_pages:
        problems.append(f"{count} pages, over the limit of {max_pages}")
    if pages is None:
        _log_once("no pdftotext (poppler) or pypdf found — PDF text checks are "
                  "skipped; install poppler or set pdf.pdftotext")
        return problems

    text = "\n".join(pages)
    for marker, meaning in BAD_GLYPHS.items():
        if marker in text:
            problems.append(f"{text.count(marker)} {meaning}")
    flat = _flat(text)
    for needle in require:
        if _flat(needle) and _flat(needle) not in flat:
            problems.append(f"{str(needle).strip()!r} is missing from its text")
    wanted = {_flat(h): str(h).strip() for h in headings if _flat(h)}
    for number, page in enumerate(pages[:-1], start=1):
        lines = [line for line in page.splitlines() if line.strip()]
        # Two lines too, for a heading long enough to wrap.
        for tail in (lines[-1:], lines[-2:]):
            heading = wanted.get(_flat(" ".join(tail))) if tail else None
            if heading:
                problems.append(f"page {number} ends with the heading "
                                f"{heading!r}, its content on the next page")
                break
    return problems
