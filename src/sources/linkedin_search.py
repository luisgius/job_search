"""LinkedIn job search, read from LinkedIn's anonymous `jobs-guest` pages.

**This breaks LinkedIn's Terms of Service.** Luis accepted that on 2026-10-23
for personal, low-volume use. No account, cookie or login is ever sent, so the
LinkedIn account he needs for the job hunt is not at stake; the worst outcome
is that LinkedIn refuses this machine's IP for a while. The design keeps the
volume small and backs off at the first sign of refusal:

  * `max_requests` search pages a run (10 cards each), shared by the queries;
  * `max_descriptions` detail pages, spent only on cards that already pass
    `filters.passes_title` and `filters.passes_location` — most cards on a
    "data scientist" search are Senior or Staff and never cost a second GET;
  * one attempt per request, `delay_seconds` (jittered) between requests;
  * the first 429, 403 or 999 (LinkedIn's "request denied") stops every
    further LinkedIn request in the run and is reported as a degraded source.

The parsing follows ai-job-search's `linkedin-search` CLI (MIT, Mads
Lorentzen, e16acee): cards split on `urn:li:jobPosting:<id>`, the description
taken from the `show-more-less-html__markup` block with nested-div tracking,
the "No longer accepting applications" banner looked for in the top card only.

Freshness: a card carries only a date (`2026-10-23`). The query's `f_TPR`
window already guarantees the posting is younger than `past_hours`, so
`posted_at` is the later of that date's midnight and the window's start — the
earliest moment consistent with both, never a guess that makes a job look
fresher than LinkedIn said it is.

URLs are canonical `https://www.linkedin.com/jobs/view/<id>`, the form
`linkedin_email` emits, so a posting found both ways dedupes into one group.
Nothing here raises out of `fetch()`.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable, Mapping

from ..config import WATCHLIST_DEFAULTS
from ..models import Job, utcnow
from ..util import HttpError, get_logger, html_to_text, http_get, parse_datetime

logger = get_logger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
CANONICAL_URL = "https://www.linkedin.com/jobs/view/{job_id}"

PAGE_SIZE = 10
REQUESTS_CEILING = 40
DESCRIPTIONS_CEILING = 80
PAST_HOURS_CEILING = 24 * 30

#: Statuses that mean "LinkedIn is refusing us": stop for the day.
REFUSAL_STATUSES = frozenset({403, 429, 999})

#: Workplace type, LinkedIn's `f_WT`.
WORK_TYPES = {"onsite": "1", "on-site": "1", "remote": "2", "hybrid": "3"}

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SPLIT_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:')
_ID_RE = re.compile(r"^(\d{6,})")
_TITLE_RE = re.compile(r'class="base-search-card__title"[^>]*>([\s\S]*?)</h3>', re.I)
_SR_TITLE_RE = re.compile(r'class="sr-only"[^>]*>([\s\S]*?)</span>', re.I)
_COMPANY_RE = re.compile(r'class="base-search-card__subtitle"[^>]*>([\s\S]*?)</h4>', re.I)
_LOCATION_RE = re.compile(r'class="job-search-card__location"[^>]*>([\s\S]*?)</span>', re.I)
_DATE_RE = re.compile(r'class="job-search-card__listdate[^"]*"[^>]*datetime="([^"]+)"', re.I)
_SALARY_RE = re.compile(r'class="job-search-card__salary-info"[^>]*>([\s\S]*?)</span>', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_CRITERIA_RE = re.compile(
    r'class="description__job-criteria-subheader"[^>]*>([\s\S]*?)</h3>[\s\S]*?'
    r'class="description__job-criteria-text[^"]*"[^>]*>([\s\S]*?)</span>',
    re.I,
)
_DESCRIPTION_START_RE = re.compile(
    r'class="(?:show-more-less-html__markup|description__text)', re.I)
_CLOSED_RE = re.compile(r"closed-job__flavor|no longer accepting applications", re.I)
_REMOTE_RE = re.compile(r"\bremote\b", re.I)


def _clean(fragment: str) -> str:
    return _SPACE_RE.sub(" ", unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def _div_content(html: str, class_name: str) -> str | None:
    """Inner HTML of the first `<div>` carrying `class_name`, nested divs
    included — a regex up to the first `</div>` would cut the description at
    its first inner block."""
    opening = re.search(
        r'<div[^>]*class="[^"]*' + re.escape(class_name) + r'[^"]*"[^>]*>', html, re.I)
    if not opening:
        return None
    position = opening.end()
    depth = 1
    while depth:
        next_open = html.find("<div", position)
        next_close = html.find("</div>", position)
        if next_close == -1:
            return None
        if next_open != -1 and next_open < next_close:
            depth += 1
            position = next_open + 4
        else:
            depth -= 1
            position = next_close + 6
    return html[opening.end():position - 6]


def parse_cards(html: str) -> list[dict[str, str]]:
    """Every job card on a search page. One malformed card is skipped, never
    the page."""
    cards = []
    for chunk in _SPLIT_RE.split(html or "")[1:]:
        found = _ID_RE.match(chunk)
        if not found:
            continue
        title_match = _TITLE_RE.search(chunk) or _SR_TITLE_RE.search(chunk)
        title = _clean(title_match.group(1)) if title_match else ""
        if not title:
            continue
        company = _COMPANY_RE.search(chunk)
        location = _LOCATION_RE.search(chunk)
        date = _DATE_RE.search(chunk)
        salary = _SALARY_RE.search(chunk)
        cards.append({
            "id": found.group(1),
            "title": title,
            "company": _clean(company.group(1)) if company else "",
            "location": _clean(location.group(1)) if location else "",
            "date": date.group(1) if date else "",
            "salary": _clean(salary.group(1)) if salary else "",
        })
    return cards


def parse_detail(html: str) -> dict[str, Any]:
    """Description, job criteria and the closed banner from a posting page."""
    markup = (_div_content(html, "show-more-less-html__markup")
              or _div_content(html, "description__text") or "")
    criteria = {
        _clean(label).lower(): _clean(value)
        for label, value in _CRITERIA_RE.findall(html or "")
    }
    # The closed banner lives in the top card. Recruiter boilerplate in the
    # description sometimes quotes the same words, and a false "closed" would
    # hide a live job, so the search stops where the description starts.
    start = _DESCRIPTION_START_RE.search(html or "")
    top_card = (html or "")[:start.start()] if start else (html or "")
    return {
        "description": html_to_text(markup) if markup else "",
        "seniority": criteria.get("seniority level", ""),
        "employment_type": criteria.get("employment type", ""),
        "job_function": criteria.get("job function", ""),
        "industries": criteria.get("industries", ""),
        "closed": bool(_CLOSED_RE.search(top_card)),
    }


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def _cap(value: Any, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(1, number))


def _delay(value: Any, default: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return min(30.0, max(0.0, seconds))


def _queries(value: Any, errors: list[str] | None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _report("linkedin_search: watchlist.linkedin_search.queries must be a list",
                errors)
        return []
    queries = []
    for index, query in enumerate(value, 1):
        keywords = query.get("keywords") if isinstance(query, Mapping) else None
        if not isinstance(keywords, str) or not keywords.strip():
            _report(f"linkedin_search: query {index} has no keywords; skipped", errors)
            continue
        queries.append(dict(query))
    return queries


def _params(query: Mapping[str, Any], past_hours: int, start: int) -> dict[str, Any]:
    params: dict[str, Any] = {"keywords": str(query["keywords"]).strip(),
                              "f_TPR": f"r{past_hours * 3600}", "start": start}
    location = str(query.get("location") or "").strip()
    if location:
        params["location"] = location
    work_type = WORK_TYPES.get(str(query.get("work_type") or "").strip().lower())
    if work_type:
        params["f_WT"] = work_type
    return params


def _posted_at(date: str, window_start: datetime) -> datetime:
    day = parse_datetime(date) if date else None
    if day is None:
        return window_start
    if day.tzinfo is None:
        day = day.replace(tzinfo=timezone.utc)
    return max(day, window_start)


def card_to_job(card: Mapping[str, str], query: Mapping[str, Any],
                window_start: datetime) -> Job:
    remote_query = str(query.get("work_type") or "").strip().lower() == "remote"
    location = card.get("location", "")
    return Job(
        source="linkedin_search",
        company=card.get("company") or "Unknown company",
        title=card["title"],
        url=CANONICAL_URL.format(job_id=card["id"]),
        location=location,
        posted_at=_posted_at(card.get("date", ""), window_start),
        remote=True if remote_query or _REMOTE_RE.search(location) else None,
        salary=card.get("salary") or None,
        raw={"linkedin_id": card["id"], "card_date": card.get("date", ""),
             "query": str(query["keywords"]).strip(),
             "query_location": str(query.get("location") or "")},
    )


class _Refused(Exception):
    """LinkedIn said no; every further request this run would make it worse."""


def fetch(
    config: Any,
    *,
    session: Any = None,
    errors: list[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> list[Job]:
    """Search every query, then fill descriptions for the promising cards.

    `config=None` reads the defaults. Never raises.
    """
    from ..filters import passes_location, passes_title  # filters imports sources' models

    defaults = WATCHLIST_DEFAULTS["linkedin_search"]
    watchlist = getattr(config, "watchlist", None)
    settings = watchlist.get("linkedin_search") if isinstance(watchlist, Mapping) else None
    settings = settings if isinstance(settings, Mapping) else defaults

    queries = _queries(settings.get("queries", defaults["queries"]), errors)
    if not queries:
        return []
    budget = _cap(settings.get("max_requests"), defaults["max_requests"], REQUESTS_CEILING)
    max_descriptions = _cap(settings.get("max_descriptions"),
                            defaults["max_descriptions"], DESCRIPTIONS_CEILING)
    past_hours = _cap(settings.get("past_hours"), defaults["past_hours"],
                      PAST_HOURS_CEILING)
    delay = _delay(settings.get("delay_seconds"), defaults["delay_seconds"])
    window_start = (now or utcnow()) - timedelta(hours=past_hours)

    requests_made = 0

    def get(url: str, params: dict[str, Any] | None = None) -> str:
        nonlocal requests_made
        if requests_made and delay:
            sleep(delay * (1 + random.random() / 2))
        requests_made += 1
        try:
            response = http_get(url, params=params, headers=_HEADERS,
                                session=session, retries=1, timeout=15)
        except HttpError as exc:
            if exc.status_code in REFUSAL_STATUSES:
                raise _Refused(f"HTTP {exc.status_code}") from exc
            raise
        return getattr(response, "text", "") or ""

    jobs: list[Job] = []
    seen: set[str] = set()
    used = 0
    refused = ""
    for index, query in enumerate(queries, 1):
        if refused:
            break
        remaining = budget - used
        if remaining <= 0:
            _report(f"linkedin_search: request cap ({budget}) spent before query "
                    f"{index}; it was not asked", errors)
            continue
        share = max(1, remaining // (len(queries) - index + 1))
        for page in range(share):
            used += 1
            try:
                html = get(SEARCH_URL, _params(query, past_hours, page * PAGE_SIZE))
            except _Refused as exc:
                refused = str(exc)
                break
            except Exception as exc:
                _report(f"linkedin_search: query {index} page {page + 1}: {exc}", errors)
                break
            cards = parse_cards(html)
            if not cards and page == 0 and "jobPosting" in html:
                _report(f"linkedin_search: query {index} answered but no card "
                        "parsed — the markup may have changed", errors)
            for card in cards:
                if card["id"] not in seen:
                    seen.add(card["id"])
                    jobs.append(card_to_job(card, query, window_start))
            if len(cards) < PAGE_SIZE:
                break

    # Only cards the filters would keep are worth a detail page. The rest go
    # out as cards, are rejected downstream and recorded as seen.
    promising = [job for job in jobs
                 if passes_title(job, config)[0] and passes_location(job, config)[0]]
    described = closed = 0
    attempted = 0
    if not refused:
        for job in promising:
            if attempted >= max_descriptions:
                logger.info("linkedin_search: description cap (%d) reached",
                            max_descriptions)
                break
            attempted += 1
            try:
                detail = parse_detail(get(DETAIL_URL.format(job_id=job.raw["linkedin_id"])))
            except _Refused as exc:
                refused = str(exc)
                break
            except Exception as exc:
                logger.debug("linkedin_search: detail %s failed: %s",
                             job.raw["linkedin_id"], exc)
                continue
            if detail["closed"]:
                job.raw["closed"] = True
                closed += 1
                continue
            job.description = detail["description"]
            for name in ("seniority", "employment_type", "job_function", "industries"):
                if detail[name]:
                    job.raw[name] = detail[name]
            described += bool(job.description)
    if refused:
        _report(f"linkedin_search: LinkedIn refused a request ({refused}); no more "
                "LinkedIn requests this run", errors)
    # A promising card left without a description (cap reached, fetch failed,
    # refused) is held back: the scorer cannot judge a title alone, and the
    # card would take one of the run's `scoring.max_jobs` slots to fail.
    blind = {id(job) for job in promising if not job.description}
    kept = [job for job in jobs
            if not job.raw.get("closed") and id(job) not in blind]
    logger.info(
        "linkedin_search: %d cards from %d request(s); %d of %d promising cards "
        "described, %d closed and %d undescribed held back",
        len(kept), requests_made, described, len(promising), closed, len(blind) - closed,
    )
    return kept
