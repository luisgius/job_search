"""Hard filters — the cheap gate in front of the (expensive) LLM stage.

Everything here is deterministic and free: no network, no model calls. A job
that survives `apply_filters` is one we are willing to pay Anthropic to score.

Two things matter as much as the filtering itself:

* **Reasons.** Every rejection carries a human-readable sentence (shown in the
  digest so you can tell "my filters are too tight" from "nothing was posted
  today") plus a short stable slug used for grouping.
* **Determinism.** Same input list, same output order, every run — otherwise
  the digest reshuffles for no reason and the tracker looks noisy.

Filter order is cheapest-first: title -> employment type -> location ->
freshness -> keywords -> description length.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from . import geo
from .models import Job, collapse_initialisms, ensure_utc, normalize_text, utcnow
from .util import get_logger

logger = get_logger(__name__)

# Stable slugs. The digest groups rejections on these, so they are part of the
# contract: add new ones freely, never rename an existing one.
REASON_CATEGORIES: tuple[str, ...] = (
    "title_excluded",
    "title_not_included",
    "employment_type_excluded",
    "location_outside_eu",
    "stale",
    "undated",
    "missing_keyword",
    "description_excluded",
    "description_too_short",
    "filter_error",
)

# Richness ranking for `dedupe`: a real ATS record beats an aggregator's copy,
# which beats whatever could be scraped out of a LinkedIn alert email.
SOURCE_RANK: dict[str, int] = {
    "greenhouse": 3,
    "lever": 3,
    "ashby": 3,
    "workable": 3,
    "adzuna": 2,
    "linkedin_email": 1,
}

# A posting dated slightly in the future is a source clock/timezone quirk, not
# a time machine. Anything inside this window is silently accepted as fresh.
FUTURE_TOLERANCE_HOURS = 2.0


@dataclass
class FilterResult:
    """Outcome of one filtering pass."""

    kept: list[Job] = field(default_factory=list)
    rejected: list[tuple[Job, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.kept)

    @property
    def total(self) -> int:
        return len(self.kept) + len(self.rejected)

    def summary(self) -> str:
        """One-line log/digest summary: `12 kept, 30 dropped (stale=21 ...)`."""
        detail = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"{len(self.kept)} kept, {len(self.rejected)} dropped" + (
            f" ({detail})" if detail else ""
        )


class _Check(NamedTuple):
    """Internal result of one stage: pass/fail + reason + grouping slug."""

    ok: bool
    reason: str
    category: str = ""


# --------------------------------------------------------------------------
# config access
# --------------------------------------------------------------------------


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    """Read a dotted key from a `Config` *or* a plain nested dict.

    Tests (and `--check` CLIs) often hand in a bare dict; `dict.get` would
    happily return the default for "filters.countries", so mappings are
    walked manually.
    """
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


def _terms(value: Any) -> list[str]:
    """Coerce a config value into a clean list of normalised search terms."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        return []
    out: list[str] = []
    for item in value:
        term = _fold(str(item))
        if term:
            out.append(term)
    return out


def _countries(config: Any) -> set[str]:
    raw = _cfg(config, "filters.countries", []) or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(c).strip().upper() for c in raw if str(c).strip()}


# --------------------------------------------------------------------------
# whole-word matching
# --------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Normalise text for whole-word matching, collapsing dotted initialisms.

    "U.S. citizen" and "US citizen" are the same requirement written two ways,
    and only the second one survives plain `normalize_text` as a single token.
    Applied to the config terms as well, so both sides agree.
    """
    return normalize_text(collapse_initialisms(text))


def _tokens(*parts: str) -> list[str]:
    """Accent-folded, punctuation-free tokens for whole-word matching.

    Parts are joined with a sentinel so a phrase can never straddle the
    boundary between, say, a title and a description.
    """
    joined: list[str] = []
    for index, part in enumerate(parts):
        if index:
            joined.append("\x00")
        joined.extend(_fold(part).split())
    return joined


def _matches(tokens: list[str], term: str) -> bool:
    """True when `term` (already normalised) occurs as a whole word/phrase.

    This is what keeps "intern" from rejecting "International Sales Manager"
    while still rejecting "Backend Intern" and "Engineering Intern (m/f/d)".
    """
    needle = term.split()
    span = len(needle)
    if not span or span > len(tokens):
        return False
    return any(tokens[i:i + span] == needle for i in range(len(tokens) - span + 1))


def _first_match(tokens: list[str], terms: Iterable[str]) -> str | None:
    for term in terms:
        if _matches(tokens, term):
            return term
    return None


# --------------------------------------------------------------------------
# individual filters
# --------------------------------------------------------------------------


def _check_title(job: Job, config: Any) -> _Check:
    exclude = _terms(_cfg(config, "filters.title_exclude", []))
    include = _terms(_cfg(config, "filters.title_include", []))
    # normalize_text (not normalize_title): the parenthetical tail is exactly
    # where "(Internship)" and "(m/f/d)" hide.
    tokens = _tokens(job.title)

    hit = _first_match(tokens, exclude)
    if hit:
        return _Check(False, f"title contains excluded term {hit!r}", "title_excluded")
    if include and not _first_match(tokens, include):
        wanted = ", ".join(sorted(include)[:6])
        return _Check(
            False,
            f"title matches none of filters.title_include ({wanted})",
            "title_not_included",
        )
    return _Check(True, "")


def passes_title(job: Job, config: Any) -> tuple[bool, str]:
    """Whole-word, case-insensitive, accent-folded title include/exclude.

    An empty `filters.title_include` means "any title is fine" — it must
    never be read as "nothing matches".
    """
    check = _check_title(job, config)
    return check.ok, check.reason


#: Where a source records the employment type on `Job.raw`. Lever states it
#: as `categories.commitment`, Adzuna as `contract_type`; both are already
#: fetched and stored, and until this filter existed neither was ever read.
EMPLOYMENT_TYPE_KEYS: tuple[str, ...] = (
    "commitment", "contract_type", "employment_type", "employmentType",
)


def _check_employment_type(job: Job, config: Any) -> _Check:
    """Reject on the employment type the *source* states, not on the title.

    A Lever posting titled plainly "Software Engineer" with
    `commitment: Internship` passes every title rule ever written, because the
    title says nothing — the structured field is the only place the truth is
    recorded. Same for an Adzuna result with `contract_type: contract`.

    Deliberately narrow: only the keys in `EMPLOYMENT_TYPE_KEYS` are read
    (never `contract_time`, which says full/part-time, not what kind of
    engagement it is), and a posting that states nothing is never rejected
    here. The trade-off in the default list is documented in `config.DEFAULTS`.
    """
    exclude = _terms(_cfg(config, "filters.employment_type_exclude", []))
    if not exclude:
        return _Check(True, "")
    raw = getattr(job, "raw", None)
    if not isinstance(raw, Mapping):
        return _Check(True, "")

    for key in EMPLOYMENT_TYPE_KEYS:
        value = raw.get(key)
        if not value or isinstance(value, bool):
            continue
        hit = _first_match(_tokens(str(value)), exclude)
        if hit:
            return _Check(
                False,
                f"the board states this is {str(value)!r} ({key}), which matches "
                f"filters.employment_type_exclude ({hit!r})",
                "employment_type_excluded",
            )
    return _Check(True, "")


def _check_location(job: Job, config: Any) -> _Check:
    allowed = _countries(config)
    allow_remote = bool(_cfg(config, "filters.allow_remote", True))
    require_hint = bool(_cfg(config, "filters.remote_requires_eu_hint", True))

    resolved = geo.resolve(job)
    # Side effect by design: downstream stages (scoring prompt, digest,
    # tracker) all want the country, and this is the only place that knows it.
    if resolved.country and not job.country:
        job.country = resolved.country.upper()
    if job.remote is None:
        job.remote = resolved.remote

    if not allowed:
        return _Check(True, "")  # no country list configured -> no location gate

    # A posting open in several countries is one job you may take from any of
    # them: "Remote (Portugal, Spain, Poland)" must not be pinned to whichever
    # country happens to be named last. Any allowed country is a pass, and the
    # job is then filed under *that* one rather than under the primary — the
    # digest's country is meant to tell the user where they could work.
    #
    # The trade-off runs the other way too: a location that merely *mentions*
    # an allowed country ("London, UK (occasional travel to Berlin)") now
    # survives. That costs one wrong card in the digest, which the user can
    # see and dismiss; pinning a multi-country role to one country loses a
    # real job with no trace anywhere.
    country = resolved.country
    allowed_hit = next((c for c in resolved.countries if c in allowed), None)
    if allowed_hit:
        if allowed_hit != job.country:
            job.country = allowed_hit
        return _Check(True, "")

    if country:
        # Resolved somewhere we cannot work. Say where, so the digest can
        # tell "wrong country" from "unparseable".
        return _Check(
            False,
            f"location {job.location!r} resolves to {geo.country_name(country)} "
            f"({country}), which is not in filters.countries",
            "location_outside_eu",
        )

    # An explicit "Remote (US)" is the most authoritative sentence in the
    # posting, and it is checked *before* the remote branch on purpose: half
    # of all US company descriptions mention their European offices, and the
    # EU hint that rescues a genuinely pan-European remote role would
    # otherwise rescue a US-only one on the strength of "we also have an
    # office in Berlin".
    #
    # `eu_stated` is the deliberate exception, and it is what keeps this from
    # over-reaching: "Remote (US or Europe)" and "Remote - US, Germany" name
    # both sides in the *location*, and those are jobs an EU applicant can
    # take. Only a European mention that lives solely in the prose is
    # discounted.
    if resolved.us and not resolved.eu_stated:
        return _Check(
            False,
            f"location {job.location!r} looks like the United States",
            "location_outside_eu",
        )

    if resolved.remote:
        if not allow_remote:
            return _Check(
                False,
                f"remote-only posting ({job.location!r}) and filters.allow_remote "
                "is false",
                "location_outside_eu",
            )
        if require_hint and not resolved.eu_hint:
            return _Check(
                False,
                f"remote posting with no European hint in location {job.location!r}, "
                "title or description (filters.remote_requires_eu_hint)",
                "location_outside_eu",
            )
        return _Check(True, "")

    return _Check(
        False,
        f"location {job.location!r} could not be resolved to an allowed country",
        "location_outside_eu",
    )


def passes_location(job: Job, config: Any) -> tuple[bool, str]:
    """Country / remote gate honouring `filters.countries`,
    `filters.allow_remote` and `filters.remote_requires_eu_hint`.

    Stamps `job.country` (and `job.remote`, when the source left it unknown)
    as a side effect — the geo resolution is not worth doing twice.

    `allow_remote: false` rejects postings that are *only* remote; a role with
    a real office in an allowed country is kept whether or not it also offers
    remote work.
    """
    check = _check_location(job, config)
    return check.ok, check.reason


def is_fresh(
    job: Job,
    max_age_hours: float,
    *,
    skip_undated: bool = True,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Is this posting new enough to bother with?

    Undated postings cannot be *proven* fresh; `skip_undated` decides whether
    that counts against them.

    A `posted_at` in the future is always accepted, but only *reported* once
    it is more than `FUTURE_TOLERANCE_HOURS` out: a few minutes is ordinary
    timezone rounding, while a board that is hours or days ahead is a real
    signal that its dates cannot be trusted for freshness at all.
    """
    moment = ensure_utc(now) or utcnow()
    if job.posted_at is None:
        if skip_undated:
            return False, "no posting date and freshness.skip_undated is on"
        return True, "no posting date — kept because freshness.skip_undated is off"

    age = job.age_hours_at(moment)
    assert age is not None  # posted_at is set, so age is too
    limit = float(max_age_hours)

    if age < -FUTURE_TOLERANCE_HOURS:
        return True, (
            f"posted_at is {abs(age):.1f}h in the future (source clock skew) — "
            "treated as fresh"
        )
    if age <= limit:
        return True, ""
    return False, f"posted {age:.1f}h ago, older than the {limit:g}h limit"


def _check_freshness(job: Job, max_age_hours: float, skip_undated: bool,
                     now: datetime | None) -> _Check:
    ok, reason = is_fresh(job, max_age_hours, skip_undated=skip_undated, now=now)
    category = "" if ok else ("undated" if job.posted_at is None else "stale")
    return _Check(ok, reason, category)


def _check_keywords(job: Job, config: Any) -> _Check:
    exclude = _terms(_cfg(config, "filters.description_exclude", []))
    require_any = _terms(_cfg(config, "filters.require_keywords_any", []))
    tokens = _tokens(job.title, job.description)

    hit = _first_match(tokens, exclude)
    if hit:
        return _Check(
            False,
            f"description contains excluded phrase {hit!r}",
            "description_excluded",
        )
    if require_any and not _first_match(tokens, require_any):
        wanted = ", ".join(sorted(require_any)[:6])
        return _Check(
            False,
            f"neither title nor description mentions any of "
            f"filters.require_keywords_any ({wanted})",
            "missing_keyword",
        )
    return _Check(True, "")


def passes_keywords(job: Job, config: Any) -> tuple[bool, str]:
    """`filters.description_exclude` (kill switch) and
    `filters.require_keywords_any` (any-of over title + description).

    Both match whole words/phrases over accent-folded text, so "go" cannot
    match "going" and "c++" is compared as the token "c".
    """
    check = _check_keywords(job, config)
    return check.ok, check.reason


def _check_length(job: Job, min_chars: int) -> _Check:
    if min_chars <= 0:
        return _Check(True, "")
    length = len((job.description or "").strip())
    if length >= min_chars:
        return _Check(True, "")
    return _Check(
        False,
        f"description is {length} chars, below filters.min_description_chars "
        f"({min_chars})",
        "description_too_short",
    )


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------


def apply_filters(
    jobs: Iterable[Job],
    config: Any,
    *,
    now: datetime | None = None,
) -> FilterResult:
    """Run every hard filter over `jobs`, cheapest stage first.

    Stops at the first failing stage so one rejection reason is reported per
    job — the one the user can act on. Also stamps `job.country` for the jobs
    that reach the location stage.
    """
    moment = ensure_utc(now) or utcnow()
    max_age = _cfg(config, "freshness.max_age_hours", 24)
    try:
        max_age = float(max_age)
    except (TypeError, ValueError):
        max_age = 24.0
    skip_undated = bool(_cfg(config, "freshness.skip_undated", True))
    try:
        min_chars = int(_cfg(config, "filters.min_description_chars", 0) or 0)
    except (TypeError, ValueError):
        min_chars = 0

    result = FilterResult()
    for job in jobs or []:
        failure: _Check | None = None
        try:
            stages = (
                lambda j=job: _check_title(j, config),
                lambda j=job: _check_employment_type(j, config),
                lambda j=job: _check_location(j, config),
                lambda j=job: _check_freshness(j, max_age, skip_undated, moment),
                lambda j=job: _check_keywords(j, config),
                lambda j=job: _check_length(j, min_chars),
            )
            for stage in stages:
                check = stage()
                if not check.ok:
                    failure = check
                    break
        except Exception as exc:  # a malformed posting must not end the run
            logger.warning("filtering %s failed: %s", job.url or job.label, exc)
            failure = _Check(False, f"could not be filtered: {exc}", "filter_error")

        if failure is None:
            result.kept.append(job)
            continue
        result.rejected.append((job, failure.reason))
        category = failure.category or "filter_error"
        result.counts[category] = result.counts.get(category, 0) + 1

    logger.info("filters: %s", result.summary())
    return result


# --------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------


def _richness(job: Job) -> tuple[int, int, int, float]:
    """Sort key for "which copy of this posting do we keep?".

    A real date first (freshness filtering depends on it), then the longest
    description (the scorer reads it), then the most trustworthy source, and
    only then the most recent posting date.

    Recency is *last* on purpose. Boards accumulate — the same role sits there
    as a two-day-old req and as today's repost — and when the two copies tie
    on everything else the older one used to win on input order and then be
    dropped as stale, taking the job with it. But promoting recency any higher
    would hand every posting to whichever source claims the newest date, and
    that is the LinkedIn alert email: every job in it inherits the email's
    receipt time, so it looks fresher than the ATS record it was scraped from
    while carrying no description and a linkedin.com URL nobody can apply
    through.
    """
    rank = SOURCE_RANK.get((job.source or "").strip().lower(), 0)
    if job.ats and rank == 0:
        rank = 3  # an unknown source that still carries an ATS id is an ATS
    posted = job.posted_at.timestamp() if job.posted_at else 0.0
    return (1 if job.posted_at else 0, len(job.description or ""), rank, posted)


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse the same posting seen through several sources.

    Groups on `Job.dedupe_key` and keeps the richest record per group. Output
    follows the order in which each group was *first* seen, and ties are
    broken by input order, so two runs over the same data agree exactly.

    `Job.dedupe_key` falls back to the first two words of the raw location
    when `country` is unset, which would keep "Berlin" and "Berlin, Germany"
    apart — the exact case it exists to merge. So any job still missing a
    country gets one stamped here first (same resolution `apply_filters`
    does, and idempotent with it).
    """
    best: dict[str, Job] = {}
    order: list[str] = []
    for job in jobs or []:
        if not job.country:
            resolved = geo.country_of(job.location)
            if resolved:
                job.country = resolved.upper()
        key = job.dedupe_key
        current = best.get(key)
        if current is None:
            best[key] = job
            order.append(key)
        elif _richness(job) > _richness(current):
            best[key] = job  # strictly better; ties keep the first sighting
    return [best[key] for key in order]
