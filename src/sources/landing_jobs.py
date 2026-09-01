"""Landing.jobs public API — Portugal-anchored tech board, keyless.

https://landing.jobs/api/v1/jobs.json is the documented v1 endpoint:
`offset`/`limit` pagination, no auth for the jobs and companies listings.
The board is Lisbon/Porto-first with a strong remote-EU tail — exactly the
market the ATS watchlist under-covers — and postings carry a salary range
more often than any ATS here does.

Unlike the watchlist boards this is a *global* feed, so it gets the one
piece of client-side filtering the pipeline otherwise leaves to stage 2: a
coarse DS/ML title gate. The gate is deliberately broader than
`filters.title_include` — its job is to stop hundreds of sales and frontend
postings from riding through dedupe every morning, not to decide what you
see. Stage 2 stays the only decider; everything the gate drops is counted
and logged so a drifting title fashion ("AI" becoming "Intelligent Systems")
is visible in the log rather than silent.

Nothing here raises out of `fetch()`: a dead page or a reshaped payload is
logged, reported into `errors` (which is what marks the source degraded in
the digest), and costs this source only — never the run.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..models import Job
from ..util import get_logger, html_to_text, http_get_json, parse_datetime

logger = get_logger(__name__)

API_URL = "https://landing.jobs/api/v1/jobs.json"

#: One company's public record; the listing names companies only by id
#: (verified live 2026-09-01, matching the official docs), so names are
#: resolved here and cached for the run. Unauthenticated, like the listing.
COMPANY_URL = "https://landing.jobs/api/v1/companies/{company_id}.json"

#: Page size asked for. The server may serve fewer; a short page ends the run.
PAGE_LIMIT = 100
#: How many pages one run reads — the freshness window makes deeper pages
#: stale by construction, same argument as Arbeitnow's cap.
MAX_PAGES = 3

#: The coarse DS/ML net. Word-bounded so "AI" cannot hide inside "Retail" and
#: "ML" cannot hide inside "HTML"; broader than any sane `title_include` on
#: purpose — a posting this regex drops never reaches stage 2, so it must
#: only drop what stage 2 could not conceivably keep.
DS_TITLE_RE = re.compile(
    r"\b("
    r"data scien(?:ce|tist)s?|machine[- ]learning|deep learning|"
    r"ml|ai|nlp|llm|computer vision|mlops|"
    r"applied scien(?:ce|tist)s?|decision scien(?:ce|tist)s?|"
    r"analytics|data analyst"
    r")\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        found = _text(payload.get(key))
        if found:
            return found
    return ""


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def _company(payload: Mapping[str, Any]) -> str:
    """The employer's name, from the two shapes the API has served it in."""
    name = _first_text(payload, "company_name")
    if name:
        return name
    node = payload.get("company")
    if isinstance(node, Mapping):
        return _first_text(node, "name")
    return _text(node)


def _money(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return _text(value)


def _salary(payload: Mapping[str, Any]) -> str | None:
    low = _money(payload.get("salary_low"))
    high = _money(payload.get("salary_high"))
    if not low and not high:
        return None
    amount = f"{low}–{high}" if low and high and low != high else (low or high)
    currency = _first_text(payload, "currency").upper()
    return f"{amount} {currency}".strip()


def _sections(payload: Mapping[str, Any]) -> str:
    """Role + requirements + nice-to-have, flattened like the ATS adapters do.

    The requirements block is the scoring-relevant part; shipping only
    `role_description` would leave the model judging company boilerplate.
    """
    parts: list[str] = []
    for heading, key in (
        ("", "role_description"),
        ("Requirements", "main_requirements"),
        ("Nice to have", "nice_to_have"),
    ):
        body = html_to_text(_text(payload.get(key))).strip()
        if not body:
            continue
        if heading and not body.lower().startswith(heading.lower()):
            parts.append(f"{heading}\n{body}")
        else:
            parts.append(body)
    return "\n\n".join(parts).strip()


def parse_job(
    payload: Mapping[str, Any], *, company_fallback: str = ""
) -> Job | None:
    """One listing -> `Job`, or None when it is not usable.

    `company_fallback` is the name `fetch()` resolved from the companies
    endpoint for this listing's `company_id`; inline names (the API's older
    shapes) still win when present, and a job that ends with neither is
    unusable — this pipeline never invents an employer.
    """
    title = _text(payload.get("title"))
    company = _company(payload) or _text(company_fallback)
    url = _first_text(payload, "url", "share_url", "landing_page")
    if not url:
        raw_id = payload.get("id")
        if isinstance(raw_id, int):
            url = f"https://landing.jobs/jobs/{raw_id}"
    if not title or not company or not url:
        return None

    city = _first_text(payload, "city", "location")
    code = _first_text(payload, "country_code").upper()
    country = code if len(code) == 2 and code.isalpha() else None
    remote = payload.get("remote")
    remote = remote if isinstance(remote, bool) else None
    location = city
    if remote and not location:
        location = "Remote"

    raw_id = payload.get("id")
    return Job(
        source="landing_jobs",
        company=company,
        title=title,
        url=url,
        location=location,
        description=_sections(payload),
        posted_at=parse_datetime(
            _first_text(payload, "published_at", "created_at") or None
        ),
        remote=remote,
        salary=_salary(payload),
        country=country,
        ats=None,
        ats_job_id=str(raw_id) if raw_id not in (None, "") else None,
        raw={
            "board": "landing_jobs",
            "id": raw_id,
            # "Internship" arrives here when it arrives at all —
            # `filters.employment_type_exclude` reads this key.
            "employment_type": _first_text(payload, "type", "contract_type") or None,
            "experience": _first_text(payload, "experience_level") or None,
            "citizenship": _first_text(payload, "citizenship") or None,
            "company_id": payload.get("company_id"),
        },
    )


def _company_name(
    company_id: Any, cache: dict[Any, str], session: Any
) -> str:
    """The employer's name for one `company_id`, cached for the run.

    A failed lookup caches "" so one dead id costs one request, not one per
    listing — and the affected jobs are skipped rather than shipped with an
    invented employer.
    """
    if company_id in cache:
        return cache[company_id]
    name = ""
    if isinstance(company_id, int):
        try:
            payload = http_get_json(
                COMPANY_URL.format(company_id=company_id), session=session
            )
            if isinstance(payload, Mapping):
                name = _first_text(payload, "name", "company_name", "trade_name")
        except Exception as exc:
            logger.warning("landing_jobs: company %s lookup failed: %s",
                           company_id, exc)
    cache[company_id] = name
    return name


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch up to `MAX_PAGES` of DS/ML listings. Never raises.

    The DS/ML title gate runs BEFORE company resolution on purpose: the
    board is all of tech, and resolving employers for postings the gate is
    about to drop would multiply the request count for nothing.
    """
    jobs: list[Job] = []
    companies: dict[Any, str] = {}
    skipped_titles = 0
    for page in range(MAX_PAGES):
        offset = page * PAGE_LIMIT
        try:
            payload = http_get_json(
                API_URL, params={"offset": offset, "limit": PAGE_LIMIT},
                session=session,
            )
        except Exception as exc:
            _report(f"landing_jobs: offset {offset}: {exc}", errors)
            break
        if isinstance(payload, list):
            batch: list[Any] = payload
        elif isinstance(payload, Mapping) and isinstance(payload.get("jobs"), list):
            batch = payload["jobs"]
        else:
            # Valid JSON that is not the listing — an error envelope, a
            # maintenance page. A silent zero here would read, every morning,
            # as an empty board.
            _report(
                f"landing_jobs: offset {offset} answered 200 but the body is "
                "not a jobs listing (neither a bare list nor a 'jobs' list) — "
                "the endpoint may have changed shape",
                errors,
            )
            break
        for entry in batch:
            if not isinstance(entry, Mapping):
                continue
            title = _text(entry.get("title"))
            if title and not DS_TITLE_RE.search(title):
                skipped_titles += 1
                continue
            fallback = ""
            if not _company(entry):
                fallback = _company_name(entry.get("company_id"), companies,
                                         session)
            try:
                job = parse_job(entry, company_fallback=fallback)
            except Exception as exc:  # one bad entry must not kill the page
                logger.debug("landing_jobs: skipping malformed entry: %s", exc)
                continue
            if job is not None:
                jobs.append(job)
        if len(batch) < PAGE_LIMIT:
            break
    logger.info(
        "landing_jobs: %d postings kept, %d non-DS/ML titles skipped",
        len(jobs), skipped_titles,
    )
    return jobs
