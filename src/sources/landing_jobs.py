"""Landing.jobs public API — Portugal-anchored tech board, keyless.

https://landing.jobs/api/v1/jobs.json is the documented v1 endpoint:
`offset`/`limit` pagination, no auth for the jobs and companies listings.
The board is Lisbon/Porto-first with a strong remote-EU tail — exactly the
market the ATS watchlist under-covers — and postings carry a salary range
more often than any ATS here does. One catch, recorded live 2026-09-01: the
listing names NO employer in any spelling, so each kept posting costs one
extra request to the per-job detail endpoint to learn who is hiring.

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

#: One job's full public record. The listing carries NO employer field at
#: all (schema recorded live 2026-09-01), so for every posting the DS/ML
#: gate keeps, `fetch()` reads the detail and takes the employer from
#: whichever spelling it serves — an inline name, a `company_id` for the
#: companies endpoint below, or the company slug as the last honest resort.
JOB_DETAIL_URL = "https://landing.jobs/api/v1/jobs/{job_id}.json"

#: One company's public record, for details that name the employer only by
#: id. Resolved names are cached for the run. Unauthenticated, like the rest.
COMPANY_URL = "https://landing.jobs/api/v1/companies/{company_id}.json"

#: Detail lookups one run may spend. Employer resolution costs one request
#: per gated posting; the gate keeps a handful a day, so this is headroom,
#: not a working limit. Postings past the budget are skipped (never shipped
#: employer-less) and the shortfall is logged — the same bounded-probing
#: discipline as `--discover`.
DETAIL_MAX_REQUESTS = 60

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
        # 0 is the API's "not published"; .10g keeps a seven-figure amount
        # (a CZK/HUF salary) out of the scientific notation bare :g uses.
        return f"{value:.10g}" if value else ""
    return _text(value)


def _salary(payload: Mapping[str, Any]) -> str | None:
    """`gross_salary_low/high` + `currency_code` (live schema 2026-09-01),
    with the pre-2026 spellings kept readable as the fallback."""
    low = _money(payload.get("gross_salary_low")) or _money(payload.get("salary_low"))
    high = _money(payload.get("gross_salary_high")) or _money(payload.get("salary_high"))
    if not low and not high:
        return None
    amount = f"{low}–{high}" if low and high and low != high else (low or high)
    currency = _first_text(payload, "currency_code", "currency").upper()
    return f"{amount} {currency}".strip()


def _locations(payload: Mapping[str, Any]) -> tuple[str, str]:
    """(joined city list, 2-letter country code) from either geography shape.

    The live schema sends a `locations` list — strings or objects — where the
    older flat `city`/`country_code` pair used to be; both stay readable.
    """
    cities: list[str] = []
    country = ""
    nodes = payload.get("locations")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, Mapping):
                city = _first_text(node, "city", "name", "label")
                code = _first_text(node, "country_code", "country")
            else:
                city, code = _text(node), ""
            if city and city not in cities:
                cities.append(city)
            if not country and len(code) == 2 and code.isalpha():
                country = code.upper()
    if not cities:
        flat = _first_text(payload, "city", "location")
        if flat:
            cities.append(flat)
    code = _first_text(payload, "country_code")
    if not country and len(code) == 2 and code.isalpha():
        country = code.upper()
    return ", ".join(cities), country


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

    `company_fallback` is the name `fetch()` resolved for this listing —
    through the per-job detail endpoint in the live schema, or the companies
    endpoint for the pre-2026 `company_id` shape. Inline names (the API's
    oldest shapes) still win when present, and a job that ends with neither
    is unusable — this pipeline never invents an employer.
    """
    title = _text(payload.get("title"))
    company = _company(payload) or _text(company_fallback)
    url = _first_text(payload, "url", "share_url", "landing_page")
    if not url:
        raw_id = payload.get("id")
        # ids are integers today, but JSON APIs flip them to strings without
        # warning; a digit string still names the same page. A slug does not.
        if isinstance(raw_id, int) or (
            isinstance(raw_id, str) and raw_id.isdigit()
        ):
            url = f"https://landing.jobs/jobs/{raw_id}"
    if not title or not company or not url:
        return None

    location, code = _locations(payload)
    country = code or None
    remote = payload.get("remote")
    remote = remote if isinstance(remote, bool) else None
    if remote and not location:
        location = "Remote"

    tags = payload.get("tags")
    tag_list = [_text(t) for t in tags if _text(t)] if isinstance(tags, list) else []

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
            # The board pays relocation for some roles — digest-worthy for a
            # candidate moving markets, so it survives into `raw`.
            "relocation_paid": payload.get("relocation_paid")
            if isinstance(payload.get("relocation_paid"), bool) else None,
            "tags": tag_list or None,
            "expires_at": _first_text(payload, "expires_at") or None,
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


def _employer_from_detail(
    job_id: Any, companies: dict[Any, str], session: Any
) -> str:
    """The employer for one posting, from the per-job detail endpoint.

    The listing stopped naming employers altogether (schema recorded live
    2026-09-01), so this is the resolution path for every posting the gate
    keeps: read the detail and take whichever spelling it serves — an inline
    name, a `company_id` for the (cached) companies endpoint, or the company
    slug humanized as the last resort: real data, worse typography. Returns
    "" when the detail names nobody, and the caller then skips the job —
    this pipeline never invents an employer.
    """
    try:
        payload = http_get_json(
            JOB_DETAIL_URL.format(job_id=job_id), session=session
        )
    except Exception as exc:
        logger.warning("landing_jobs: job %s detail lookup failed: %s",
                       job_id, exc)
        return ""
    if not isinstance(payload, Mapping):
        return ""
    name = _company(payload)
    if name:
        return name
    if payload.get("company_id") is not None:
        name = _company_name(payload.get("company_id"), companies, session)
        if name:
            return name
    slug = _text(payload.get("company_slug"))
    return slug.replace("-", " ").title() if slug else ""


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch up to `MAX_PAGES` of DS/ML listings. Never raises.

    The DS/ML title gate runs BEFORE employer resolution on purpose: the
    board is all of tech, resolution now costs one detail request per
    posting, and paying it for postings the gate is about to drop would
    multiply the request count for nothing.
    """
    jobs: list[Job] = []
    companies: dict[Any, str] = {}
    skipped_titles = 0
    skipped_budget = 0
    detail_requests = 0
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
            # A titleless entry can never ship, so it must not spend requests.
            if title and not _company(entry):
                if entry.get("company_id") is not None:
                    # The pre-2026 listing shape named employers by id.
                    fallback = _company_name(entry.get("company_id"),
                                             companies, session)
                elif entry.get("id") not in (None, ""):
                    if detail_requests >= DETAIL_MAX_REQUESTS:
                        skipped_budget += 1
                        continue
                    detail_requests += 1
                    fallback = _employer_from_detail(entry.get("id"),
                                                     companies, session)
            try:
                job = parse_job(entry, company_fallback=fallback)
            except Exception as exc:  # one bad entry must not kill the page
                logger.debug("landing_jobs: skipping malformed entry: %s", exc)
                continue
            if job is not None:
                jobs.append(job)
        if len(batch) < PAGE_LIMIT:
            break
    if skipped_budget:
        logger.warning(
            "landing_jobs: detail budget (%d) spent — %d gated postings "
            "skipped unresolved; they resurface tomorrow if still fresh",
            DETAIL_MAX_REQUESTS, skipped_budget,
        )
    logger.info(
        "landing_jobs: %d postings kept, %d non-DS/ML titles skipped",
        len(jobs), skipped_titles,
    )
    return jobs
