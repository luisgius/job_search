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
import math
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

# jinja2 is a hard core dependency (see docs/ARCHITECTURE.md ground rule 1) and
# is the one third-party import allowed at module scope in this file.
from jinja2 import Environment, FileSystemLoader

from . import config as config_module
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
#: Imported rather than repeated: a source the page does not know about is a
#: source whose "0 jobs today" never reaches the reader.
SOURCE_NAMES: tuple[str, ...] = config_module.SOURCE_NAMES

#: Imported, never re-typed. The window is defined once in `config.py`; a
#: literal here would let the page report a number the filter is not using.
DEFAULT_MAX_AGE_HOURS: int = config_module.DEFAULT_MAX_AGE_HOURS
DEFAULT_REPOST_MIN_GAP_DAYS: int = config_module.DEFAULT_REPOST_MIN_GAP_DAYS

#: Past this age `relative_time` prints a calendar date instead of a day
#: count, because "2026-04-02" is easier to place than "124d ago". The card
#: then prints the day count alongside it, so "how old is this?" still has an
#: answer on the postings where the date alone stops answering it.
RELATIVE_DAYS_LIMIT = 60

#: (context key, status) — the five outcome buckets the page is built around.
#: Any status not listed here lands in `other`, so a new `ApplyStatus` shows up
#: on the page instead of vanishing from it.
SECTIONS: tuple[tuple[str, ApplyStatus], ...] = (
    ("unconfirmed", ApplyStatus.SUBMITTED_UNCONFIRMED),
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
    if days < RELATIVE_DAYS_LIMIT:
        return f"{int(days)}d ago"
    return moment.strftime("%Y-%m-%d")


def posting_age_days(
    posted_at: datetime | None, now: datetime | None = None
) -> float | None:
    """Age of a posting in days, or `None` when it carries no date.

    `None` is a third state, and it is neither zero nor "fresh": an undated
    posting has no age, and the card must say so rather than invent one.

    In particular this never falls back to `first_seen_at`. That is the day
    *we* first fetched the job — a fact about our cron schedule, not about the
    employer — and printing it as the posting date would manufacture a
    freshness the source never claimed. An undated posting has to look undated.

    Negative for a posting dated in the future, which is ordinary source clock
    skew (`filters.FUTURE_TOLERANCE_HOURS` covers the same ground) and can
    never make anything look old.
    """
    moment = ensure_utc(posted_at)
    if moment is None:
        return None
    reference = ensure_utc(now) or utcnow()
    return (reference - moment).total_seconds() / 86400.0


def _round_days(value: float) -> int:
    """Days as a whole number, rounded half **up**.

    Not `int()`, which truncates: it turned a true age of 30.5 days into the
    sentence "on the market 30 days — past the 30-day mark", and a 23-hour-old
    posting into an age of `0`. Not the builtin `round()` either — that is
    banker's rounding, so `round(30.5)` is 30 and reproduces the first bug at
    exactly the values a reader is most likely to notice.
    """
    return math.floor(float(value) + 0.5)


def _format_datetime(dt: datetime | None) -> str:
    moment = ensure_utc(dt)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


def _score_class(value: int, failed: bool, unscored: bool = False) -> str:
    """Colour grade for the score badge. Kept out of the template on purpose."""
    if unscored:
        # Never grade an unscored job. Painting it red would say "bad fit"
        # when the truth is "nobody looked".
        return "score-unscored"
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
    if not path.is_absolute():
        return quote(path.as_posix())
    if digest_dir.is_absolute():
        try:
            relative = os.path.relpath(path, digest_dir)
        except ValueError:  # different drive on Windows — no relative form
            relative = ".."
        if not relative.startswith(".."):
            return quote(Path(relative).as_posix())
    return path.as_uri()


# --------------------------------------------------------------------------
# item construction
# --------------------------------------------------------------------------


def _status_value(status: Any) -> str:
    if isinstance(status, ApplyStatus):
        return status.value
    return str(status or "")


def _ghost_flags(
    job: Any,
    *,
    now: datetime,
    tracker: Any = None,
    repost_min_gap_days: float = DEFAULT_REPOST_MIN_GAP_DAYS,
) -> tuple[list[str], float | None]:
    """The ghost-job note this card carries, if any.

    Returns `(sentences, repost_gap_days)`.

    **This is a flag, not a filter.** Nothing here drops, hides, reorders or
    rejects a posting: the caller renders the card in full either way, and a
    flagged job is still counted, still sorted by score and still linked. That
    asymmetry is the whole design — a wrong flag costs a glance, a wrong
    deletion costs an opportunity the user never learns existed.

    **There is one signal, and it is not posting age.** Age was tried and it
    cannot work here: every card came through `filters.is_fresh`, so it is
    younger than `freshness.max_age_hours` by construction. At the shipped 72
    hours a "flag anything older than N days" rule can only fire for N < 3,
    i.e. on everything. The knob that asked for 30 days was unreachable in
    every run the pipeline could produce, and only looked alive because its
    tests called this function directly with a hand-built `posted_at`.

    What does work is the tracker's memory. A re-listed role gets a *new* date
    each time, so it walks straight through the freshness window while the
    tracker still holds the earlier sighting — which measures the thing worth
    knowing, how long this role has been circulating. See
    `db.Tracker.repost_gap_days` for what counts as an earlier sighting and,
    just as importantly, what does not.

    A tracker that raises, or hands back something that is not a number, costs
    this one flag and nothing else. The digest is the last artefact of a run
    whose money is already spent, so the card outlives any problem here.
    """
    flags: list[str] = []
    gap: float | None = None

    if tracker is not None:
        # The whole computation lives inside the `try`, not just the call.
        # A stub that returns `"40"` or a `Mock()` used to raise `TypeError`
        # out of here, out of `_item`, and into `build_context`'s "skipping
        # unrenderable digest item" — deleting the card over an advisory line.
        try:
            raw = tracker.repost_gap_days(
                getattr(job, "dedupe_key", ""),
                key=getattr(job, "key", ""),
                source=getattr(job, "source", ""),
                posted_at=getattr(job, "posted_at", None),
                now=now,
            )
            if raw is None:
                gap = None
            elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
                # Deliberately strict rather than coercive. `float("40")` would
                # work, but a seam that answers with a string is a seam that is
                # broken, and this flag names an employer — it does not get to
                # run on a guess.
                logger.debug(
                    "repost check returned %r (not a number) for %s — ignoring",
                    raw, getattr(job, "url", "?"),
                )
                gap = None
            else:
                gap = float(raw)
                threshold = float(repost_min_gap_days)
                # 0 turns the flag off, like every other 0 in this config.
                # It is the one value that must NOT read as "flag everything":
                # that would put a ghost-job accusation on the same-day
                # duplicate this threshold exists to protect.
                if threshold > 0 and gap >= threshold:
                    flags.append(
                        f"On the market {_round_days(gap)} days or more: the same "
                        f"company, title and city were already listed on "
                        f"{getattr(job, 'source', '') or 'this board'} under a "
                        "different job id, that long before this posting went "
                        "up. A role that keeps circulating can mean the first "
                        "search failed, or that it is being advertised rather "
                        "than filled — it can also just be a second headcount. "
                        "Worth a look, not worth waiting on."
                    )
        except Exception as exc:  # a tracker problem must not cost the page
            logger.debug("repost check failed for %s: %s", getattr(job, "url", "?"), exc)
            gap = None
            flags = []

    return flags, gap


def _item(
    scored: ScoredJob,
    *,
    digest_dir: Path,
    now: datetime,
    tracker: Any = None,
    repost_min_gap_days: float = DEFAULT_REPOST_MIN_GAP_DAYS,
) -> dict[str, Any]:
    """Flatten one `ScoredJob` into the plain dict the template renders.

    Deliberately a dict and not the dataclass: the template can then only read
    what was prepared for it, and a missing artifact or absent score is an
    empty value rather than an exception halfway down the page.
    """
    job = scored.job
    score = scored.score
    # No score at all is a third state, distinct from "scored 0" and from
    # "the scorer failed": it is what `--no-llm` produces, and rendering it as
    # 0 would tell the reader the exact opposite of the truth.
    unscored = score is None
    failed = bool(score is not None and score.error)
    value = _int(getattr(score, "value", 0), 0)

    detail = (scored.status_detail or "").strip()
    # The card already shows the scorer error in its own alert; scoring's
    # status_detail quotes that same error, and printing it twice makes a
    # one-line problem look like two.
    if failed and score.error and score.error in detail:
        detail = ""

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

    relative = relative_time(job.posted_at, now)
    age_days = posting_age_days(job.posted_at, now)
    # Rounded, never truncated. `int()` turns 30.5 days into "30 days" and a
    # 23-hour-old posting into "0 days" — a printed number that disagrees with
    # the sentence around it, which is how a card ends up contradicting itself.
    age_label = None if age_days is None else _round_days(age_days)
    flags, repost_gap = _ghost_flags(
        job,
        now=now,
        tracker=tracker,
        repost_min_gap_days=repost_min_gap_days,
    )

    if job.posted_at is None:
        # Undated postings are common (LinkedIn alerts carry no per-job date);
        # say so rather than printing "posted —", and never substitute the day
        # we happened to fetch it.
        posted_label = "no posting date"
    elif age_days is not None and age_days >= RELATIVE_DAYS_LIMIT:
        # `relative_time` switches to a calendar date at exactly this age,
        # which is easier to place but stops answering the question the age is
        # on the card to answer. Print both, so "how old is this?" always has
        # an answer. The boundary is `>=` because `relative_time`'s own is
        # `days < RELATIVE_DAYS_LIMIT`: at exactly 60 days it has already
        # switched, so this branch has to have switched too.
        posted_label = f"posted {relative} — {age_label} days ago"
    else:
        posted_label = f"posted {relative}"

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
        "posted_relative": relative,
        "posted_label": posted_label,
        # None, not 0: an undated posting has no age, and an unknown age is
        # not evidence of an old posting. The card says "no posting date".
        "posted_age_days": age_label,
        "repost_gap_days": None if repost_gap is None else _round_days(repost_gap),
        #: Advisory notes, rendered as `p.advisory` — amber, deliberately not
        #: the red `p.alert` an actual failure uses. Never a reason to leave
        #: the card off the page.
        "flags": flags,
        "score": value,
        "unscored": unscored,
        "score_label": "—" if unscored else ("?" if failed else str(value)),
        "score_class": _score_class(value, failed, unscored),
        "score_error": (score.error if failed else "") or "",
        "score_model": getattr(score, "model", "") or "",
        "verdict": (getattr(score, "verdict", "") or "").strip(),
        "reasons": _strings(getattr(score, "reasons", None)),
        "strengths": _strings(getattr(score, "strengths", None)),
        "gaps": _strings(getattr(score, "gaps", None)),
        "status": _status_value(scored.status),
        "status_detail": detail,
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
        "max_age_hours": _int(
            _cfg(config, "freshness.max_age_hours", DEFAULT_MAX_AGE_HOURS),
            DEFAULT_MAX_AGE_HOURS,
        ),
        "repost_min_gap_days": _int(
            _cfg(config, "freshness.repost_min_gap_days", DEFAULT_REPOST_MIN_GAP_DAYS),
            DEFAULT_REPOST_MIN_GAP_DAYS,
        ),
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
    tracker: Any = None,
) -> dict[str, Any]:
    """Build everything the template needs. No filesystem, no network; `now`
    is the only clock it consults and it is injectable.

    Splits the run's jobs into the five outcome buckets the page is organised
    around (plus `other` for anything unclassified), formats every value the
    template prints, and carries the run funnel through so a quiet day is
    distinguishable from a broken pipeline.

    `tracker=` is optional and read-only: it supplies the sighting history the
    repost flag needs (`db.Tracker.repost_gap_days`). Omitting it costs that
    one flag and changes nothing else — no job appears, disappears or moves
    because a tracker was or was not passed.
    """
    moment = ensure_utc(now) or utcnow()
    digest_dir = _resolved_path(config, "output.dir", "output")
    summary = _config_summary(config)
    repost_min_gap_days = summary["repost_min_gap_days"]

    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SECTIONS}
    buckets["other"] = []
    by_status = {status.value: name for name, status in SECTIONS}

    for scored in list(scored_jobs or []):
        if scored is None or getattr(scored, "job", None) is None:
            continue
        try:
            item = _item(
                scored,
                digest_dir=digest_dir,
                now=moment,
                tracker=tracker,
                repost_min_gap_days=repost_min_gap_days,
            )
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


def _skeleton() -> dict[str, Any]:
    """Empty-but-complete context.

    `render_html` merges the caller's context onto this so a hand-built or
    partial context (a test fixture, the fallback path) still renders every
    section instead of dying on a missing key halfway down the page.
    """
    sections = {name: [] for name, _ in SECTIONS}
    sections["other"] = []
    return {
        "title": "Job Hunter",
        "generated_at": None,
        "generated_at_str": "",
        "date_str": "",
        "weekday": "",
        "applicant": {},
        "config_summary": _config_summary(None),
        "stats": {},
        "funnel": [],
        "source_counts": {},
        "filter_counts": {},
        "errors": [],
        "totals": {name: 0 for name in list(sections) + ["all"]},
        **sections,
    }


def render_html(context: Mapping[str, Any]) -> str:
    """Render the digest template with `context`. Returns the full HTML page."""
    data = _skeleton()
    data.update(dict(context or {}))
    template = _environment().get_template(TEMPLATE_NAME)
    return template.render(**data)


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
    tracker: Any = None,
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
    context = build_context(scored_jobs, stats, config, now=moment, tracker=tracker)

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
