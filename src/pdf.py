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
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

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
