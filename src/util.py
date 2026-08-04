"""Small shared helpers: logging, HTTP with retries, HTML/text cleanup.

`requests` is imported lazily so the pure-logic modules (and their tests) can
run in an environment where only the stdlib is installed.
"""

from __future__ import annotations

import html as html_module
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
USER_AGENT = (
    "job-hunter/1.0 (+https://github.com/; personal job-search automation; "
    "contact via config applicant.email)"
)

T = TypeVar("T")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


def setup_logging(level: str = "INFO", stream: Any = None) -> None:
    """Configure root logging once. Safe to call repeatedly."""
    root = logging.getLogger()
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(resolved)
    if not root.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setLevel(resolved)
    # These libraries are chatty at INFO and say nothing we need.
    for noisy in ("urllib3", "httpx", "httpcore", "googleapiclient", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3


class HttpError(RuntimeError):
    """Raised after retries are exhausted."""


def http_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = 1.5,
    session: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """GET with bounded exponential backoff.

    Retries on transport errors and on 429/5xx. Raises `HttpError` when the
    budget is spent; callers are expected to log-and-skip, never to crash the
    run over one flaky endpoint.
    """
    import requests  # local import: keeps stdlib-only tests importable

    sess = session or requests
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8"}
    merged.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        if attempt:
            sleep(backoff ** attempt)
        try:
            response = sess.get(url, params=params, headers=merged, timeout=timeout)
        except Exception as exc:  # transport-level failure
            last_error = exc
            continue
        status = getattr(response, "status_code", 0)
        if status == 429 or 500 <= status < 600:
            last_error = HttpError(f"{url} -> HTTP {status}")
            continue
        if status >= 400:
            # 404/403 will not improve by trying again.
            raise HttpError(f"{url} -> HTTP {status}")
        return response
    raise HttpError(f"GET {url} failed after {retries} attempts: {last_error}")


def http_get_json(url: str, **kwargs: Any) -> Any:
    response = http_get(url, **kwargs)
    try:
        return response.json()
    except Exception as exc:
        raise HttpError(f"{url} did not return JSON: {exc}") from exc


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section)[^>]*>", re.IGNORECASE
)
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_MULTI_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    """Flatten an HTML job description into readable plain text.

    Good enough for feeding an LLM and for keyword filters; not a parser.
    """
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", str(raw))
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return _MULTI_NL_RE.sub("\n\n", "\n".join(l for l in lines if l)).strip()


def truncate(text: str | None, limit: int, suffix: str = "\n\n[...truncated]") -> str:
    """Cut `text` to `limit` characters on a word boundary where possible."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    # Back up to the last word boundary unless that would throw away most of
    # the budget — a clean cut reads better than a truncated word.
    if space >= limit * 0.7:
        cut = cut[:space]
    return cut.rstrip() + suffix


def slugify(value: str, max_length: int = 60) -> str:
    """Filesystem-safe slug for artifact directories."""
    from .models import normalize_text

    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")
    return (slug[:max_length].rstrip("-")) or "untitled"


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort parse of the many date shapes ATS APIs emit.

    Returns tz-aware UTC, or None when the value cannot be trusted. Callers
    treat None as "freshness unknown", which by default means "skip".
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Heuristic: anything past ~2001 in ms is a millisecond timestamp.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_datetime(int(text))

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        from dateutil import parser as dateutil_parser  # optional dependency

        parsed = dateutil_parser.parse(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------


def safe_call(
    fn: Callable[..., T],
    *args: Any,
    default: T | None = None,
    logger: logging.Logger | None = None,
    label: str = "",
    errors: list[str] | None = None,
    **kwargs: Any,
) -> T | None:
    """Run `fn`, swallow any exception, log it, and keep the pipeline alive.

    Used for anything network-facing: one broken board must never abort the
    whole run.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        message = f"{label or getattr(fn, '__name__', 'call')} failed: {exc}"
        (logger or get_logger(__name__)).warning(message, exc_info=False)
        if errors is not None:
            errors.append(message)
        return default


def chunked(items: Iterable[T], size: int) -> Iterable[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def open_in_browser(path: str | Path) -> bool:
    """Open a local file in the default browser. Never fatal."""
    import webbrowser

    try:
        return webbrowser.open(Path(path).resolve().as_uri())
    except Exception:
        return False


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
