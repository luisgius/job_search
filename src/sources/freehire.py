"""freehire.me — a keyless aggregator of ATS boards, via its public search API.

https://freehire.me serves its catalogue as an unauthenticated JSON API (its
robots.txt allows `/api/` for `User-agent: *`). `GET /api/v1/agent/jobs/
search` is the faceted search with every hit's full description hydrated
server-side, so one request returns a page of complete records. What it adds
is reach: on 2026-10-22 the Poland facet held ~25k open postings (~4k in
Kraków), many from upstreams no adapter here speaks — Workday, Oracle,
SuccessFactors, Traffit, Phenom. Evidence and limits: docs/FREEHIRE_SOURCE.md.

Three properties of the payload decide how it is read:

  * `posted_at` is RE-DATED. Six Capco postings captured on 2026-10-22 shared
    that afternoon's timestamp while their `created_at` spanned two months.
    Freshness therefore comes from `created_at` (when freehire first saw the
    posting — its own sighting, not the employer's publication), the
    re-dated value survives only as `raw["source_posted_at"]`, and the
    server-side `posted_within_days` filter, which reads the re-dated
    field, is never sent.
  * `url` is the upstream ATS posting, tagged `?utm_source=freehire.me`
    (`util.canonical_url` strips it), so the copy lands in the same dedupe
    group as the direct record when an ATS adapter fetched it too, and
    `filters.dedupe` keeps the direct one. Because that URL may be a real
    Greenhouse or Lever form, `autoapply.eligible` also refuses this source
    by name: an aggregator's restatement is never auto-submitted.
  * `enrichment`, `skills`, `regions`, `reality`, … are freehire's own
    inferences. They are kept under `raw["freehire_enrichment"]` as evidence
    and no hard filter reads them — a model's guess at "employment_type:
    contract" must not reject a posting the way the employer's field would.

Nothing here raises out of `fetch()`: a failed request or a reshaped payload
is logged, reported into `errors` (which marks the source degraded in the
digest), and costs this source only — never the run.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..config import FREEHIRE_RESERVED_PARAMS, WATCHLIST_DEFAULTS
from ..models import Job
from ..util import get_logger, html_to_text, http_get_json, parse_datetime

logger = get_logger(__name__)

API_URL = "https://freehire.me/api/v1/agent/jobs/search"

#: Ceilings over `watchlist.freehire`; a malformed value falls back to the
#: default. 20 requests of 100 is far inside the API's own paging guard
#: (`offset + limit <= 10000`) and its observed 300/600-per-window limits.
LIMIT_CEILING = 100
REQUESTS_CEILING = 20

#: freehire's own fields that are inferences rather than the upstream
#: posting's facts. Kept as evidence under `raw["freehire_enrichment"]`.
ENRICHMENT_FIELDS = (
    "enrichment", "skills", "regions", "cities", "metros", "collections",
    "is_tech", "reality", "auto_apply_available",
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean(value: Any) -> str:
    """Titles arrive as the upstream ATS indexed them: entity-escaped
    ("Fullstack &amp; AI") and with stray whitespace."""
    return " ".join(unescape(_text(value)).split())


#: A closing tag or a line break: markup, as opposed to a text description
#: that merely says "<2 years" or ">20k".
_HTML_RE = re.compile(r"</[a-z][a-z0-9]*\s*>|<br\s*/?>", re.IGNORECASE)


def _description(value: Any) -> str:
    """The description as text. It is requested as `description_format=text`,
    so only entities and spacing are tidied — `html_to_text` would read
    "<2 years … >20k" as one tag and delete it. One that still carries
    markup (an upstream that ignored the format) goes through `html_to_text`.
    """
    text = _text(value)
    if _HTML_RE.search(text):
        return html_to_text(text)
    lines = [" ".join(line.split()) for line in unescape(text).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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


def _safe_url(value: Any) -> str:
    """The posting URL, or "" when it is not a plain web link.

    It is rendered as the digest's apply link, so a `javascript:` or
    credential-bearing URL from a third party's payload never gets that far.
    """
    text = _text(value)
    if not text or "\\" in text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    if "@" in parts.netloc:
        return ""
    return text


def _countries(entry: Mapping[str, Any]) -> list[str]:
    value = entry.get("countries")
    codes: list[str] = []
    for code in value if isinstance(value, list) else []:
        code = _text(code).upper()
        if len(code) == 2 and code.isascii() and code.isalpha() and code not in codes:
            codes.append(code)
    return codes


def parse_job(entry: Mapping[str, Any]) -> Job | None:
    """One search hit -> `Job`, or None when it is not usable.

    Title, company and a web URL are the floor; a hit missing any of them is
    skipped, never completed with an invented employer.
    """
    title = _clean(entry.get("title"))
    company = _clean(entry.get("company"))
    url = _safe_url(entry.get("url"))
    if not title or not company or not url:
        return None

    countries = _countries(entry)
    work_mode = _text(entry.get("work_mode")).lower()
    # Hybrid is not remote, and an absent mode is unknown — only the two
    # explicit values say anything.
    remote = True if work_mode == "remote" else False if work_mode == "onsite" else None
    location = _text(entry.get("location"))
    if remote and not location:
        location = "Remote"

    slug = _text(entry.get("public_slug"))
    created = entry.get("created_at")
    posted = entry.get("posted_at")
    return Job(
        source="freehire",
        company=company,
        title=title,
        url=url,
        location=location,
        description=_description(entry.get("description")),
        # `created_at`, never `posted_at` — see the module doc.
        posted_at=parse_datetime(created if isinstance(created, str) else None),
        remote=remote,
        salary=None,  # only freehire's inferred salary exists; it stays in raw
        # A multi-country posting has no one country; the geo filters read
        # the location text instead.
        country=countries[0] if len(countries) == 1 else None,
        # Not an ATS record, whatever host the URL is on: `ats` stays None.
        ats=None,
        ats_job_id=slug or None,
        raw={
            "board": "freehire",
            "public_slug": slug or None,
            "external_id": _text(entry.get("external_id")) or None,
            "upstream_source": _text(entry.get("source")).lower() or None,
            "posted_at_source": "created_at",
            "source_posted_at": posted if isinstance(posted, str) else None,
            "work_mode": work_mode or None,
            "location_countries": countries,
            "freehire_enrichment": {
                key: entry[key] for key in ENRICHMENT_FIELDS if key in entry
            },
        },
    )


def _queries(value: Any, errors: list[str] | None) -> list[dict[str, Any]]:
    """The configured facet queries as request params.

    `config.validate` rejects a malformed list before a run starts; this is
    the same rule applied defensively, so a direct call cannot send the
    re-dated `posted_within_days` or override the adapter's paging.
    """
    if not isinstance(value, list):
        _report("freehire: watchlist.freehire.queries is not a list; nothing fetched",
                errors)
        return []
    queries: list[dict[str, Any]] = []
    for index, query in enumerate(value, 1):
        if not isinstance(query, Mapping) or not query:
            _report(f"freehire: query {index} is not a mapping of facets; skipped", errors)
            continue
        owned = sorted({str(k) for k in query} & FREEHIRE_RESERVED_PARAMS)
        if owned:
            _report(f"freehire: query {index} sets {', '.join(owned)}, which the "
                    "adapter owns; ignored", errors)
        params = {str(k): v for k, v in query.items()
                  if str(k) not in FREEHIRE_RESERVED_PARAMS}
        if params:
            queries.append(params)
    return queries


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Run each configured query for up to its share of `max_requests` pages.

    Never raises. The request budget is shared: each query may spend what is
    left divided by the queries still to run, so an early query that runs
    out of results hands its share on. `config=None` reads the defaults.
    """
    defaults = WATCHLIST_DEFAULTS["freehire"]
    watchlist = getattr(config, "watchlist", None)
    settings = watchlist.get("freehire") if isinstance(watchlist, Mapping) else None
    settings = settings if isinstance(settings, Mapping) else defaults

    queries = _queries(settings.get("queries", defaults["queries"]), errors)
    if not queries:
        return []
    limit = _cap(settings.get("limit"), defaults["limit"], LIMIT_CEILING)
    budget = _cap(settings.get("max_requests"), defaults["max_requests"],
                  REQUESTS_CEILING)
    names = settings.get("exclude_upstream", defaults["exclude_upstream"])
    excluded = {_text(n).lower() for n in names if _text(n)} \
        if isinstance(names, list) else set()

    jobs: list[Job] = []
    seen: set[str] = set()
    used = 0
    skipped_upstream = 0
    skipped_unusable = 0
    for index, query in enumerate(queries, 1):
        remaining = budget - used
        if remaining <= 0:
            _report(f"freehire: request cap ({budget}) spent before query {index}; "
                    "it was not asked", errors)
            continue
        share = max(1, remaining // (len(queries) - index + 1))
        offset = 0
        for _page in range(share):
            params = {**query, "limit": limit, "offset": offset,
                      "description_format": "text"}
            used += 1
            try:
                payload = http_get_json(API_URL, params=params, session=session)
            except Exception as exc:
                _report(f"freehire: query {index} offset {offset}: {exc}", errors)
                break
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, list):
                # Valid JSON that is not the search envelope — an error body,
                # a maintenance page. A silent zero would read, every
                # morning, as an empty market.
                _report(
                    f"freehire: query {index} offset {offset} answered 200 but "
                    "the body is not the search envelope (no 'data' list) — "
                    "the API may have changed shape",
                    errors,
                )
                break
            unusable = 0
            for entry in data:
                if not isinstance(entry, Mapping):
                    unusable += 1
                    continue
                if _text(entry.get("source")).lower() in excluded:
                    skipped_upstream += 1
                    continue
                if entry.get("closed_at"):
                    continue  # freehire saw it close
                try:
                    job = parse_job(entry)
                except Exception as exc:  # one bad hit must not kill the page
                    logger.debug("freehire: skipping malformed hit: %s", exc)
                    job = None
                if job is None:
                    unusable += 1
                    continue
                identity = job.raw.get("public_slug") or job.url
                if identity not in seen:
                    seen.add(identity)
                    jobs.append(job)
            skipped_unusable += unusable
            if len(data) > 1 and unusable == len(data):
                _report(
                    f"freehire: query {index} offset {offset}: every hit was "
                    "unusable (no title, company or URL) — shape drift suspected",
                    errors,
                )
            offset += len(data)
            meta = payload.get("meta")
            total = meta.get("total") if isinstance(meta, Mapping) else None
            if len(data) < limit or (
                isinstance(total, int) and not isinstance(total, bool)
                and offset >= total
            ):
                break
        else:
            # The share ran out with results still unread. Expected on a
            # broad facet — the cap is the coverage bound, not a fault.
            logger.info("freehire: query %d stopped at its %d-request share "
                        "(%d hits read)", index, share, offset)
    logger.info(
        "freehire: %d postings from %d request(s); %d excluded upstream, "
        "%d unusable hits skipped",
        len(jobs), used, skipped_upstream, skipped_unusable,
    )
    return jobs
