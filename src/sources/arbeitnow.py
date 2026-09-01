"""Arbeitnow job-board API — a free, keyless feed of the German market.

https://www.arbeitnow.com/api/job-board-api is the documented public API: one
JSON document per page, `links.next` for pagination, no auth of any kind. Two
properties make it worth a slot next to the watchlist boards:

  * it is Germany-heavy in exactly the segment the watchlist misses — the
    SMBs and startups nobody thought to list (postings historically carried
    a `visa_sponsorship` boolean; the live feed dropped it in 2026, and the
    parser still reads it defensively for the day it returns);
  * `created_at` is a plain unix timestamp set when the posting appeared on
    the board, so freshness filtering has a real date to work with (unlike
    Adzuna's ingest-time `created`).

The trade-off is the usual aggregator one: the apply URL points at
Arbeitnow's own page, so when the same role also arrives through the
company's ATS, `filters.dedupe` keeps the ATS record (`SOURCE_RANK`).

Nothing here raises out of `fetch()`: a dead page or a reshaped payload is
logged, reported into `errors` (which is what marks the source degraded in
the digest), and costs this source only — never the run.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..models import Job
from ..util import get_logger, html_to_text, http_get_json, parse_datetime

logger = get_logger(__name__)

API_URL = "https://www.arbeitnow.com/api/job-board-api"

#: How many pages one run reads. The feed serves ~100 postings per page,
#: newest first, and the daily freshness window makes everything past the
#: first few pages stale by construction — deeper reads would fetch postings
#: only to drop them on their dates.
MAX_PAGES = 3


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def parse_job(payload: Mapping[str, Any]) -> Job | None:
    """One feed entry -> `Job`, or None when it is not usable.

    Title, company and URL are the floor: a posting missing any of them
    cannot be shown, deduped or applied to, and this pipeline never invents
    the missing field.
    """
    title = _text(payload.get("title"))
    company = _text(payload.get("company_name"))
    url = _text(payload.get("url"))
    if not title or not company or not url:
        return None

    location = _text(payload.get("location"))
    remote = payload.get("remote")
    remote = remote if isinstance(remote, bool) else None
    if remote and not location:
        location = "Remote"

    job_types = payload.get("job_types")
    types = [_text(t) for t in job_types if _text(t)] if isinstance(job_types, list) else []
    tags = payload.get("tags")
    tag_list = [_text(t) for t in tags if _text(t)] if isinstance(tags, list) else []

    slug = _text(payload.get("slug"))
    return Job(
        source="arbeitnow",
        company=company,
        title=title,
        url=url,
        location=location,
        description=html_to_text(_text(payload.get("description"))),
        posted_at=parse_datetime(payload.get("created_at")),
        remote=remote,
        salary=None,  # the feed does not publish one
        ats=None,
        ats_job_id=slug or None,
        raw={
            "board": "arbeitnow",
            "slug": slug or None,
            # "internship" / "trainee" arrive here, which is the only signal a
            # neutrally-titled posting is one — `filters.employment_type_exclude`
            # reads this key.
            "employment_type": ", ".join(types) or None,
            "tags": tag_list or None,
            # No ATS in this pipeline publishes this; for a Spanish national
            # it is noise, but it stays in `raw` because the digest may want
            # it the day the answer to "where can I work?" changes.
            "visa_sponsorship": payload.get("visa_sponsorship")
            if isinstance(payload.get("visa_sponsorship"), bool) else None,
        },
    )


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch up to `MAX_PAGES` of the board, newest first. Never raises."""
    jobs: list[Job] = []
    url = API_URL
    for page in range(1, MAX_PAGES + 1):
        try:
            payload = http_get_json(url, params={"page": page}, session=session)
        except Exception as exc:
            _report(f"arbeitnow: page {page}: {exc}", errors)
            break
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            # Valid JSON that is not the feed — an error envelope, a
            # maintenance page. A silent zero here would read, every morning,
            # as an empty board.
            _report(
                f"arbeitnow: page {page} answered 200 but the body is not the "
                "job-board payload (no 'data' list) — the endpoint may have "
                "changed shape",
                errors,
            )
            break
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            try:
                job = parse_job(entry)
            except Exception as exc:  # one bad entry must not kill the page
                logger.debug("arbeitnow: skipping malformed entry: %s", exc)
                continue
            if job is not None:
                jobs.append(job)
        links = payload.get("links") if isinstance(payload, Mapping) else None
        if not (isinstance(links, Mapping) and _text(links.get("next"))):
            break
    logger.info("arbeitnow: %d postings", len(jobs))
    return jobs
