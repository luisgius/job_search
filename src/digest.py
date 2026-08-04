"""The daily digest — the only artefact of this pipeline a human actually reads.

Everything upstream is plumbing that feeds one HTML file the user opens with
their coffee. Three design choices follow from that:

  * **The template is dumb.** Every string it prints is computed here:
    formatted dates, relative ages, colour-grade classes, artifact hrefs,
    excerpts. A `.j2` file full of logic is a file nobody can debug at 08:00,
    and a template that can call methods on a `ScoredJob` is a template that
    can trip over a `None` score mid-render. Sections are lists of *plain
    dicts*, never model objects.
  * **Autoescaping is not optional.** Job titles and descriptions are
    attacker-controllable text fetched from the open internet and rendered in
    a local file that has file:// privileges. `autoescape=True`, and nothing
    job-sourced is ever passed through `|safe`.
  * **A broken digest is worse than an ugly one.** By the time this runs the
    LLM spend is already sunk, so a template failure falls back to a plain
    listing rather than losing the whole run's output.

`build_context` is pure — no filesystem, no clock unless you pass one — so the
entire page can be exercised from hand-built fixtures.
"""

from __future__ import annotations

import html as html_module
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

# jinja2 is a hard core dependency (see docs/ARCHITECTURE.md ground rule 1) and
# is the one third-party import allowed at module scope in this file.
from jinja2 import Environment, FileSystemLoader

from .models import ApplyStatus, ScoredJob, ensure_utc, utcnow
from .util import ensure_dir, get_logger, html_to_text, truncate

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "digest.html.j2"

#: Enough of the posting to recognise it without opening the tab.
DESCRIPTION_EXCERPT_CHARS = 400
#: Enough of the cover letter to judge its tone — roughly the opening paragraph.
COVER_PREVIEW_CHARS = 250

DEFAULT_THRESHOLD = 65
SOURCE_NAMES: tuple[str, ...] = ("greenhouse", "lever", "adzuna", "linkedin_email")

#: (context key, status) — the five outcome buckets the page is built around.
#: Any status not listed here lands in `other`, so a new `ApplyStatus` shows up
#: on the page instead of vanishing from it.
SECTIONS: tuple[tuple[str, ApplyStatus], ...] = (
    ("needs_click", ApplyStatus.DIGEST),
    ("auto_applied", ApplyStatus.APPLIED),
    ("dry_run", ApplyStatus.DRY_RUN),
    ("failed", ApplyStatus.APPLY_FAILED),
    ("below", ApplyStatus.SCORED_BELOW),
)

#: Sections whose cards are ordered by score, best first.
_SCORE_SORTED = ("needs_click", "below", "other")

_env: Environment | None = None


# --------------------------------------------------------------------------
# config / stats access (works with a `Config` *or* a plain nested dict)
# --------------------------------------------------------------------------


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    """Read a dotted key from a `Config` or a plain nested mapping."""
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(dotted, default)
    return default


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolved_path(config: Any, dotted: str, default: str) -> Path:
    """Resolve a configured path, honouring `Config.path` when available."""
    resolver = getattr(config, "path", None)
    if callable(resolver) and not isinstance(config, Mapping):
        try:
            return Path(resolver(dotted, default))
        except Exception:  # a hand-rolled config stub must not break the page
            pass
    return Path(str(_cfg(config, dotted, default) or default))


def _strings(values: Any) -> list[str]:
    """Coerce a model-supplied list into clean, non-empty strings."""
    if not values:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            out.append(text)
    return out


def _stats_dict(stats: Any) -> dict[str, Any]:
    """`RunStats` -> dict, tolerating a plain dict or None."""
    if stats is None:
        return {}
    to_dict = getattr(stats, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # a stats stub is not worth losing the digest over
            data = {}
        if isinstance(data, Mapping):
            return dict(data)
    if isinstance(stats, Mapping):
        return dict(stats)
    return {}


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------


def relative_time(dt: datetime | None, now: datetime | None = None) -> str:
    """Human-scale age of `dt`: "just now" / "17m ago" / "3h ago" / "yesterday".

    Returns "—" for an unknown date, which is a real and common case: LinkedIn
    alert items and some boards carry no trustworthy timestamp, and the digest
    must not imply a freshness it cannot back up.

    A posting dated slightly in the future is source clock skew, not news, so
    anything up to two hours ahead reads as "just now".
    """
    moment = ensure_utc(dt)
    if moment is None:
        return "—"
    reference = ensure_utc(now) or utcnow()
    seconds = (reference - moment).total_seconds()

    if seconds < 0:
        ahead = -seconds
        if ahead < 2 * 3600:
            return "just now"
        if ahead < 86400:
            return f"in {int(ahead // 3600)}h"
        return f"in {int(ahead // 86400)}d"

    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 2:
        return "yesterday"
    if days < 60:
        return f"{int(days)}d ago"
    return moment.strftime("%Y-%m-%d")


def _format_datetime(dt: datetime | None) -> str:
    moment = ensure_utc(dt)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


def _score_class(value: int, failed: bool) -> str:
    """Colour grade for the score badge. Kept out of the template on purpose."""
    if failed:
        return "score-error"
    if value >= 90:
        return "score-90"
    if value >= 80:
        return "score-80"
    if value >= 70:
        return "score-70"
    return "score-low"


def _artifact_href(raw: str | None, digest_dir: Path) -> str | None:
    """Link to a generated file from the digest page.

    Prefers a path *relative to the digest* so the whole `output/` directory
    can be moved, copied to another machine or synced without every link
    breaking; falls back to an absolute `file://` URI when the artifact lives
    outside the output directory. Pure string maths — never touches the disk,
    so `build_context` stays testable without one.
    """
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute() and digest_dir.is_absolute():
        try:
            relative = os.path.relpath(path, digest_dir)
        except ValueError:  # different drive on Windows — no relative form
            relative = ".."
        if not relative.startswith(".."):
            return quote(Path(relative).as_posix())
        try:
            return path.as_uri()
        except ValueError:  # pragma: no cover - as_uri only fails when relative
            return quote(path.as_posix())
    return quote(path.as_posix())


# --------------------------------------------------------------------------
# item construction
# --------------------------------------------------------------------------


def _status_value(status: Any) -> str:
    if isinstance(status, ApplyStatus):
        return status.value
    return str(status or "")


def _item(scored: ScoredJob, *, digest_dir: Path, now: datetime) -> dict[str, Any]:
    """Flatten one `ScoredJob` into the plain dict the template renders.

    Deliberately a dict and not the dataclass: the template can then only read
    what was prepared for it, and a missing artifact or absent score is an
    empty value rather than an exception halfway down the page.
    """
    job = scored.job
    score = scored.score
    failed = bool(score is not None and score.error)
    value = _int(getattr(score, "value", 0), 0)

    artifacts = scored.artifacts
    cv_md = _artifact_href(getattr(artifacts, "cv_md", None), digest_dir)
    cover_md = _artifact_href(getattr(artifacts, "cover_md", None), digest_dir)
    cv_pdf = _artifact_href(getattr(artifacts, "cv_pdf", None), digest_dir)
    cover_pdf = _artifact_href(getattr(artifacts, "cover_pdf", None), digest_dir)
    screenshot = _artifact_href(getattr(artifacts, "screenshot", None), digest_dir)

    # Descriptions arrive as HTML from Greenhouse and as plain text elsewhere;
    # flatten first so the excerpt is prose rather than escaped markup.
    excerpt = truncate(html_to_text(job.description), DESCRIPTION_EXCERPT_CHARS, suffix=" …")
    cover_preview = truncate(
        (scored.cover_letter_md or "").strip(), COVER_PREVIEW_CHARS, suffix=" …"
    )

    return {
        "key": job.key,
        "company": job.company or "Unknown company",
        "title": job.title or "Untitled role",
        "location": job.location,
        "country": job.country or "",
        "remote": job.remote,
        "salary": job.salary or "",
        "url": job.url,
        "source": job.source or "",
        "ats": job.ats or "",
        "posted_at": _format_datetime(job.posted_at),
        "posted_at_iso": job.posted_at.isoformat() if job.posted_at else "",
        "posted_relative": relative_time(job.posted_at, now),
        "score": value,
        "score_label": "?" if failed else str(value),
        "score_class": _score_class(value, failed),
        "score_error": (score.error if failed else "") or "",
        "score_model": getattr(score, "model", "") or "",
        "verdict": (getattr(score, "verdict", "") or "").strip(),
        "reasons": _strings(getattr(score, "reasons", None)),
        "strengths": _strings(getattr(score, "strengths", None)),
        "gaps": _strings(getattr(score, "gaps", None)),
        "status": _status_value(scored.status),
        "status_detail": (scored.status_detail or "").strip(),
        "artifacts_dir": _artifact_href(getattr(artifacts, "dir", None), digest_dir),
        "cv_md": cv_md,
        "cover_md": cover_md,
        "cv_pdf": cv_pdf,
        "cover_pdf": cover_pdf,
        "screenshot": screenshot,
        "has_artifacts": bool(cv_md or cover_md or cv_pdf or cover_pdf),
        "description_excerpt": excerpt,
        "cover_preview": cover_preview,
    }


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


def _config_summary(config: Any) -> dict[str, Any]:
    """The handful of settings that explain today's page to its reader."""
    sources = {name: bool(_cfg(config, f"sources.{name}", False)) for name in SOURCE_NAMES}
    return {
        "threshold": _int(_cfg(config, "scoring.threshold", DEFAULT_THRESHOLD), DEFAULT_THRESHOLD),
        "dry_run": bool(_cfg(config, "apply.dry_run", True)),
        "apply_enabled": bool(_cfg(config, "apply.enabled", False)),
        "apply_min_score": _int(_cfg(config, "apply.min_score", 80), 80),
        "scoring_model": str(_cfg(config, "scoring.model", "") or ""),
        "tailoring_model": str(_cfg(config, "tailoring.model", "") or ""),
        "tailoring_enabled": bool(_cfg(config, "tailoring.enabled", True)),
        "max_age_hours": _int(_cfg(config, "freshness.max_age_hours", 24), 24),
        "countries": _strings(_cfg(config, "filters.countries", [])),
        "sources": sources,
        "sources_enabled": [name for name, on in sources.items() if on],
        "output_dir": str(_resolved_path(config, "output.dir", "output")),
        "db_path": str(_resolved_path(config, "db.path", "output/tracker.sqlite3")),
    }


def build_context(
    scored_jobs: Iterable[ScoredJob] | None = None,
    stats: Any = None,
    config: Any = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build everything the template needs. Pure: no I/O, no ambient clock.

    Splits the run's jobs into the five outcome buckets the page is organised
    around (plus `other` for anything unclassified), formats every value the
    template prints, and carries the run funnel through so a quiet day is
    distinguishable from a broken pipeline.
    """
    moment = ensure_utc(now) or utcnow()
    digest_dir = _resolved_path(config, "output.dir", "output")
    summary = _config_summary(config)

    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SECTIONS}
    buckets["other"] = []
    by_status = {status.value: name for name, status in SECTIONS}

    for scored in list(scored_jobs or []):
        if scored is None or getattr(scored, "job", None) is None:
            continue
        try:
            item = _item(scored, digest_dir=digest_dir, now=moment)
        except Exception as exc:  # one malformed record must not blank the page
            logger.warning("skipping unrenderable digest item: %s", exc)
            continue
        buckets[by_status.get(item["status"], "other")].append(item)

    for name in _SCORE_SORTED:
        # Stable: equal scores keep the order scoring produced, so two runs over
        # the same input agree.
        buckets[name].sort(key=lambda item: item["score"], reverse=True)

    stats_data = _stats_dict(stats)
    errors = _strings(stats_data.get("errors") or getattr(stats, "errors", None))
    raw_filter_counts = stats_data.get("filter_counts")
    if raw_filter_counts is None:
        raw_filter_counts = getattr(stats, "filter_counts", None)
    filter_counts = _sorted_counts(raw_filter_counts)
    source_counts = _sorted_counts(
        stats_data.get("source_counts") or getattr(stats, "source_counts", None)
    )

    totals = {name: len(items) for name, items in buckets.items()}
    totals["all"] = sum(totals.values())

    context: dict[str, Any] = {
        "generated_at": moment,
        "generated_at_str": _format_datetime(moment),
        "date_str": moment.strftime("%Y-%m-%d"),
        "weekday": moment.strftime("%A"),
        "applicant": dict(_cfg(config, "applicant", {}) or {}),
        "config_summary": summary,
        "stats": stats_data,
        "funnel": [
            {"label": "fetched", "value": _int(stats_data.get("fetched"), 0)},
            {"label": "deduped", "value": _int(stats_data.get("after_dedupe"), 0)},
            {"label": "filtered", "value": _int(stats_data.get("after_filters"), 0)},
            {"label": "scored", "value": _int(stats_data.get("scored"), 0)},
            {"label": "matched", "value": _int(stats_data.get("matches"), 0)},
        ],
        "source_counts": source_counts,
        "filter_counts": filter_counts,
        "errors": errors,
        "totals": totals,
        "title": f"Job Hunter — {moment.strftime('%Y-%m-%d')}",
    }
    context.update(buckets)
    return context


def _sorted_counts(counts: Any) -> dict[str, int]:
    """Counts as a dict ordered biggest-first — the order they are displayed in."""
    if not isinstance(counts, Mapping):
        return {}
    pairs = [(str(k), _int(v, 0)) for k, v in counts.items()]
    pairs.sort(key=lambda pair: (-pair[1], pair[0]))
    return dict(pairs)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _environment() -> Environment:
    """Jinja environment for `src/templates`, autoescaping unconditionally.

    `autoescape=True` rather than `select_autoescape`: there is exactly one
    template here, it is HTML, and the text it renders comes from job boards —
    an extension-based heuristic is the wrong kind of clever for that.
    """
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _env


def render_html(context: Mapping[str, Any]) -> str:
    """Render the digest template with `context`. Returns the full HTML page."""
    template = _environment().get_template(TEMPLATE_NAME)
    return template.render(**dict(context or {}))


def _fallback_html(context: Mapping[str, Any], error: Exception) -> str:
    """A digest of last resort, built with string concatenation.

    If the template ever fails to render, the run's money has already been
    spent and the links are the only thing that matters — so emit them plainly
    (still escaped) rather than losing the day's output to a formatting bug.
    """
    esc = html_module.escape
    date_str = esc(str(context.get("date_str", "")))
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>Job Hunter — {date_str} (fallback)</title></head><body>",
        f"<h1>Job Hunter — {date_str}</h1>",
        "<p><strong>The digest template failed to render:</strong> ",
        esc(str(error)),
        ". Below is the raw list so nothing from this run is lost.</p>",
    ]
    for section in ("needs_click", "auto_applied", "dry_run", "failed", "below", "other"):
        items = context.get(section) or []
        parts.append(f"<h2>{esc(section)} ({len(items)})</h2><ul>")
        for item in items:
            if not isinstance(item, Mapping):
                continue
            parts.append(
                "<li>{score} — <a href=\"{url}\" target=\"_blank\" rel=\"noopener\">"
                "{company} — {title}</a> ({location})</li>".format(
                    score=esc(str(item.get("score", ""))),
                    url=esc(str(item.get("url", ""))),
                    company=esc(str(item.get("company", ""))),
                    title=esc(str(item.get("title", ""))),
                    location=esc(str(item.get("location", ""))),
                )
            )
        parts.append("</ul>")
    for error_line in context.get("errors") or []:
        parts.append(f"<pre>{esc(str(error_line))}</pre>")
    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def write_digest(
    scored_jobs: Iterable[ScoredJob] | None = None,
    stats: Any = None,
    config: Any = None,
    *,
    now: datetime | None = None,
) -> Path:
    """Render today's digest to `<output.dir>/digest_YYYY-MM-DD.html` and return it.

    An existing digest for the same day is **overwritten**: re-running the
    pipeline twice in one morning is normal (a fixed watchlist typo, a
    `--no-dry-run` second pass), and the second run's page is the accurate one.
    Anything you wanted to keep from the first is still in the tracker DB and
    in `output/applications/`.

    A copy is also written to `<output.dir>/digest_latest.html` so a cron user
    can bookmark one stable URL. That copy is best-effort — failing to write it
    never costs you the dated file.
    """
    moment = ensure_utc(now) or utcnow()
    context = build_context(scored_jobs, stats, config, now=moment)

    try:
        html = render_html(context)
    except Exception as exc:  # never lose a run to a template bug
        logger.warning("digest template failed to render (%s) — writing fallback", exc)
        html = _fallback_html(context, exc)

    out_dir = ensure_dir(_resolved_path(config, "output.dir", "output"))
    path = out_dir / f"digest_{context['date_str']}.html"
    path.write_text(html, encoding="utf-8")

    latest = out_dir / "digest_latest.html"
    try:
        latest.write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not update %s: %s", latest, exc)

    logger.info(
        "digest written to %s (%d need your click, %d auto-applied, %d dry-run, %d below)",
        path,
        context["totals"]["needs_click"],
        context["totals"]["auto_applied"],
        context["totals"]["dry_run"],
        context["totals"]["below"],
    )
    return path


#: `docs/ARCHITECTURE.md` names this stage `digest.render` in the pipeline
#: diagram and `write_digest` in the module contract. They are the same call.
render = write_digest
