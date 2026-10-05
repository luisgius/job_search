"""Just Join IT — the Polish tech board, via its internal JSON API. Tier 2.

There is no official API. The frontend at justjoin.it calls
`api.justjoin.it/v2/user-panel/offers`, and this adapter speaks to that —
which is the deal the user accepted for Tier 2 sources: real coverage of the
Polish market (the one the user lives in) in exchange for an endpoint that
owes us nothing and may change shape without notice. Everything below is
built around that bargain:

  * **nothing raises out of `fetch()`** — an HTTP error or a reshaped payload
    logs a warning, lands in `errors` (which the digest shows), and costs
    this source only;
  * a 200 whose body is not the offers envelope is reported as *shape drift*
    by name, never read as an empty board;
  * the pipeline-level backstop is `src/health.py`: a source whose recent
    runs averaged >0 postings and which suddenly reports 0 raises the
    "went silent" alert — the exact failure an unowned endpoint produces;
  * the recorded fixture (`tests/fixtures/justjoin_offers.json`) plus the
    `network`-marked live contract in `test_live_contract.py` are how drift
    is *seen* rather than suffered.

Filtering happens client-side, deliberately. The API accepts category ids,
but the ids are an internal enumeration that has already been renumbered
once in this board's history — sending a stale id would slice the wrong
category silently, which is worse than slicing none. So the request narrows
only by experience (junior/mid — a documented string enum, re-checked
client-side anyway), and the DS/ML cut is made here on title and skills,
where drift is at least visible in the counts this module logs.

The listing payload carries no ad body, so `description` is synthesized from
the skills lists and marked `raw["snippet_only"]`, the same contract Adzuna
set: scoring may read it, but it must know it is reading a teaser.

What 2026-10-05 showed (evidence in docs/JUSTJOIN_SOURCE.md):

  * the API host answered an nginx 503 on 2026-09-01 and on each of the four
    runs with network in the retained logs (09-25, 10-01, 10-02, 10-05); on
    10-05 also on the bare host and to plain headers. No retained run shows
    an offer from it. The 503 is persistent; its cause is not known;
  * the site's own listing pages are served to the pipeline's plain identity,
    robots.txt leaves them open, and each one embeds its first 50 offers in
    the API's shape. When page 1 of the API fails, `fetch()` reads a bounded
    number of those pages instead and says so in `errors`. That is a slice of
    two categories, never the board;
  * every offer carries two publication fields. In 135 of 150 captured
    offers `publishedAt` is later than `lastPublishedAt`, never earlier.
    What either field means is not established. The earlier complete one is
    used as a conservative freshness date; it is not a proven first
    publication, and the later one is kept as a conflicting source date,
    never as an update time.

Every request this module makes goes through `_get`: no automatic redirects,
one shared request budget per walk, and a byte cap enforced while reading.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from ..models import Job
from ..util import DEFAULT_TIMEOUT, USER_AGENT, get_logger, parse_datetime

logger = get_logger(__name__)

API_URL = "https://api.justjoin.it/v2/user-panel/offers"

#: The request shape the site's own frontend uses. Every run that reached the
#: network through 2026-10-05 was answered 503 with these headers, and on
#: 2026-10-05 so was the bare host with the pipeline's plain identity: an
#: nginx error page relayed by the CDN, not a challenge. Why the host refuses
#: this client is not established; nothing here tries to get around it.
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://justjoin.it",
    "Referer": "https://justjoin.it/",
}

#: The job page for one offer slug.
JOB_URL = "https://justjoin.it/job-offer/{slug}"

#: Pages of 100, at most three. This is a request bound, not a statement
#: about how much of the board three pages hold: the API has never answered,
#: so that was never measured. A full last page is reported as partial.
PER_PAGE = 100
MAX_PAGES = 3

#: The public listing pages read when the API refuses page 1. Paths, the
#: `experience-levels` key, the comma-joined value and `page` are the ones the
#: site's own pagination links carry; only these two categories were observed
#: answering. Each page embeds 50 offers.
LISTING_URL = "https://justjoin.it/job-offers/all-locations/{category}"
LISTING_CATEGORIES = ("data", "ai")
LISTING_PAGE_SIZE = 50
LISTING_MAX_PAGES = 3
#: Every HTTP request of the listing walk, redirect hops included, is charged
#: here: six pages and two spare. Nothing is retried; a page that fails ends
#: its category and is reported.
LISTING_MAX_REQUESTS = 8
#: Bytes of one response body, enforced while it is read (observed pages are
#: about 1.6 MB). Text handed to `parse_listing_page` is measured in UTF-8.
MAX_BODY_BYTES = 4_000_000
_LISTING_MAX_ROWS = 100
_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
#: A complete calendar date, optionally with a time and offset. Anything less
#: ("Oct 5", "2026-10", "10:30", "yesterday") would have the generic parser
#: fill the gaps from today's date.
_COMPLETE_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)

#: The spec for this source is junior+mid. Sent as a request param AND
#: re-checked per offer, because an internal API is allowed to start ignoring
#: its own query string and a leaked "senior" would waste scoring tokens.
EXPERIENCE_LEVELS = ("junior", "mid")

#: The client-side DS/ML cut, applied to the title and the skill names.
#: Word-bounded for the usual reasons ("ML" must not hide inside "HTML", nor
#: "AI" inside "Retail"); broader than `filters.title_include` on purpose —
#: stage 2 stays the only real decider.
DS_RE = re.compile(
    r"\b("
    r"data scien(?:ce|tist)s?|machine[- ]learning|deep learning|"
    r"ml|ai|nlp|llm|computer vision|mlops|"
    r"applied scien(?:ce|tist)s?|decision scien(?:ce|tist)s?|"
    r"product analyst|experimentation|causal inference|analytics|data analyst|data engineer"
    r")\b",
    re.IGNORECASE,
)

_WORKPLACE_REMOTE = frozenset({"remote", "fully_remote", "fully-remote"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def _stated_date(value: Any) -> datetime | None:
    """A publication field as UTC, or None unless it is a complete date."""
    text = _text(value)
    return parse_datetime(text) if _COMPLETE_DATE_RE.fullmatch(text) else None


def _too_large(size: int) -> bool:
    return size > MAX_BODY_BYTES


def _body(response: Any) -> str:
    """The response body as text, refusing more than `MAX_BODY_BYTES`."""
    try:
        declared = int(response.headers.get("Content-Length", ""))
    except (AttributeError, TypeError, ValueError):
        declared = 0
    if _too_large(declared):
        raise ValueError("response body limit exceeded")
    if getattr(response, "raw", None) is not None and callable(getattr(response, "iter_content", None)):
        data = bytearray()
        for chunk in response.iter_content(65536):
            data += chunk
            if _too_large(len(data)):
                raise ValueError("response body limit exceeded")
        return data.decode("utf-8", errors="replace")
    text = str(response.text)
    if _too_large(len(text)) or _too_large(len(text.encode("utf-8"))):
        raise ValueError("response body limit exceeded")
    return text


def _get(session: Any, url: str, *, params: Mapping[str, Any], headers: Mapping[str, str],
         budget: list[int], follow: bool = False) -> str:
    """One bounded GET. Never follows a redirect on its own.

    Each request, including a redirect hop, takes one unit from `budget`.
    With `follow`, a redirect is taken only when it stays on the same HTTPS
    host and path; anything else is an error naming where it pointed.
    """
    import requests  # local import: keeps stdlib-only tests importable

    client = session if session is not None else requests
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8"}
    merged.update(headers)
    origin = urlsplit(url)
    query: Mapping[str, Any] | None = params
    while True:
        if budget[0] <= 0:
            raise ValueError("request cap reached; coverage partial")
        budget[0] -= 1
        response = client.get(url, params=query, headers=merged, timeout=DEFAULT_TIMEOUT,
                              allow_redirects=False, stream=True)
        try:
            status = getattr(response, "status_code", 0)
            if status in {301, 302, 303, 307, 308}:
                target = urlsplit(urljoin(url, str(response.headers.get("Location") or "")))
                if not follow:
                    raise ValueError(f"{origin.netloc}{origin.path} -> HTTP {status} redirect not followed")
                if (target.scheme, target.netloc, target.path) != ("https", origin.netloc, origin.path):
                    raise ValueError(f"redirect leaves the listing route ({target.netloc}{target.path})")
                url, query = target.geturl(), None
                continue
            if status != 200:
                raise ValueError(f"{origin.scheme}://{origin.netloc}{origin.path} -> HTTP {status}")
            return _body(response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


def _skill_names(value: Any) -> list[str]:
    """Skill labels from either shape the API has used.

    v1 sent `[{"name": "Python", "level": 4}]`, v2 sends plain strings; a
    payload mid-migration could plausibly send both at once.
    """
    names: list[str] = []
    if not isinstance(value, list):
        return names
    for entry in value:
        if isinstance(entry, Mapping):
            name = _text(entry.get("name"))
        else:
            name = _text(entry)
        if name:
            names.append(name)
    return names


def _money(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return _text(value)


def _salary(offer: Mapping[str, Any]) -> str | None:
    """The best range across the employment types the offer lists.

    One offer often carries two ranges (b2b and permanent); the digest has
    one salary line, so the widest advertised range wins — it is the number
    the board itself headlines.
    """
    best: tuple[float, str] | None = None
    types = offer.get("employmentTypes")
    if not isinstance(types, list):
        return None
    # The site also lists each range converted into four other currencies.
    # Those are its arithmetic, not the employer's offer.
    types = [n for n in types if not (isinstance(n, Mapping) and n.get("currencySource") == "conversion")]
    for node in types:
        if not isinstance(node, Mapping):
            continue
        low = _money(node.get("from"))
        high = _money(node.get("to"))
        if not low and not high:
            continue
        amount = f"{low}–{high}" if low and high and low != high else (low or high)
        currency = _text(node.get("currency")).upper()
        unit = _text(node.get("unit")).lower()
        text = f"{amount} {currency}".strip()
        if unit:
            text = f"{text}/{unit}"
        try:
            magnitude = float(high or low)
        except ValueError:
            magnitude = 0.0
        if best is None or magnitude > best[0]:
            best = (magnitude, text)
    return best[1] if best else None


def _location(offer: Mapping[str, Any]) -> str:
    cities = [_text(offer.get("city"))]
    nodes = offer.get("multilocation")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, Mapping):
                cities.append(_text(node.get("city")))
    seen: list[str] = []
    for city in cities:
        if city and city not in seen:
            seen.append(city)
    return ", ".join(seen)


def _is_ds_ml(title: str, skills: list[str]) -> bool:
    if DS_RE.search(title):
        return True
    return any(DS_RE.search(skill) for skill in skills)


def parse_offer(offer: Mapping[str, Any]) -> Job | None:
    """One listing entry -> `Job`, or None when it is not usable."""
    title = _text(offer.get("title"))
    company = _text(offer.get("companyName"))
    slug = _text(offer.get("slug"))
    if not title or not company or not slug:
        return None

    skills = _skill_names(offer.get("requiredSkills"))
    nice = _skill_names(offer.get("niceToHaveSkills"))
    parts = []
    if skills:
        parts.append("Required skills: " + ", ".join(skills) + ".")
    if nice:
        parts.append("Nice to have: " + ", ".join(nice) + ".")

    workplace = _text(offer.get("workplaceType")).lower()
    location = _location(offer)
    remote = True if workplace in _WORKPLACE_REMOTE else None
    if remote and not location:
        location = "Remote"

    # Two publication fields, meaning unverified (see the module docstring).
    # The earliest complete one dates the job conservatively; both stay in
    # `raw` as sent and a difference is flagged, not explained.
    stated = sorted(
        (moment, key) for key in ("publishedAt", "lastPublishedAt")
        if (moment := _stated_date(offer.get(key))) is not None
    )

    return Job(
        source="justjoin_it",
        company=company,
        title=title,
        url=JOB_URL.format(slug=slug),
        location=location,
        description=" ".join(parts),
        posted_at=stated[0][0] if stated else None,
        remote=remote,
        salary=_salary(offer),
        country=None,  # the listing names cities; geo resolves them
        ats=None,
        ats_job_id=slug,
        raw={
            "board": "justjoin_it",
            "slug": slug,
            "experience": _text(offer.get("experienceLevel")) or None,
            "workplace_type": workplace or None,
            "category_id": offer.get("categoryId"),
            "skills": skills or None,
            "publishedAt": offer.get("publishedAt"),
            "lastPublishedAt": offer.get("lastPublishedAt"),
            "posted_at_source": stated[0][1] if stated else None,
            "source_dates": {key: moment.isoformat() for moment, key in stated},
            "date_conflict": len(stated) == 2 and stated[0][0] != stated[1][0],
            # The listing has no ad body — scoring must know it is reading a
            # synthesized teaser, not the posting. Same contract as Adzuna.
            "snippet_only": True,
        },
    )


def _usable(entry: Any) -> bool:
    """Does this row carry the three fields a job cannot exist without?"""
    return isinstance(entry, Mapping) and all(
        _text(entry.get(key)) for key in ("slug", "title", "companyName"))


def parse_listing_page(body: str) -> tuple[list[Any], int | None]:
    """The offers one public listing page embeds, and its last page number.

    Recognising the envelope and validating rows are separate steps. The
    envelope is the single non-empty `"offers":[...]` array in the Next.js
    flight payload with at least one row carrying a `slug`; its rows are
    returned as sent, bad ones included, for the caller to count and skip.
    Raises `ValueError` (drift, never an empty board) when there is no flight
    payload, when an offers array cannot be decoded, when a non-empty one has
    no recognisable row, or when more than one non-empty array is present.
    Only when every offers array is empty is the page a valid empty result.
    """
    if _too_large(len(body)) or _too_large(len(body.encode("utf-8"))):
        raise ValueError("response body limit exceeded")
    chunks = _FLIGHT_RE.findall(body)
    if not chunks:
        raise ValueError("no embedded page data (the listing page has changed shape)")
    try:
        flight = "".join(json.loads(chunk) for chunk in chunks)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"unreadable embedded page data: {exc}") from exc
    decoder = json.JSONDecoder()
    found = 0
    filled: list[list[Any]] = []
    for match in re.finditer(r'"offers":(?=\[)', flight):
        try:
            offers, _ = decoder.raw_decode(flight, match.end())
        except ValueError as exc:
            raise ValueError("an offers list in the embedded page data cannot be "
                             f"decoded (the listing page has changed shape): {exc}") from exc
        found += 1
        if offers:
            filled.append(offers)
    if not found:
        raise ValueError("no offers list in the embedded page data (the listing page has changed shape)")
    if not filled:
        return [], None
    if len(filled) > 1:
        raise ValueError(f"{len(filled)} non-empty offers lists in the embedded page data; "
                         "which one is the listing is ambiguous (the listing page has changed shape)")
    offers = filled[0]
    if not any(isinstance(entry, Mapping) and _text(entry.get("slug")) for entry in offers):
        raise ValueError("the offers list has no row with a slug — malformed offers "
                         "(the listing page has changed shape)")
    pages = [int(n) for n in re.findall(r'[?&]page=(\d{1,4})"', flight)]
    return offers, max(pages) if pages else None


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch up to `MAX_PAGES` of junior/mid offers, DS/ML only. Never raises.

    When the API fails on its first page, the bounded public listing pages
    are read instead; a failure on a later page keeps what was read. No path
    claims the whole board: a full last API page, a later failure and every
    listing-page run are each reported as partial coverage.
    """
    jobs: list[Job] = []
    seen: set[str] = set()
    skipped = {"category": 0, "experience": 0, "malformed": 0}

    def keep(data: list[Any], surface: str) -> None:
        for entry in data:
            if not _usable(entry):
                skipped["malformed"] += 1
                continue
            level = _text(entry.get("experienceLevel")).lower()
            if level and level not in EXPERIENCE_LEVELS:
                skipped["experience"] += 1
                continue
            if not _is_ds_ml(_text(entry.get("title")), _skill_names(entry.get("requiredSkills"))):
                skipped["category"] += 1
                continue
            try:
                job = parse_offer(entry)
            except Exception as exc:  # one bad entry must not kill the page
                logger.debug("justjoin_it: skipping malformed entry: %s", exc)
                skipped["malformed"] += 1
                continue
            if job is not None and job.ats_job_id not in seen:
                seen.add(job.ats_job_id)
                job.raw["surface"] = surface
                # Neither surface is known to be complete: the API walk is
                # capped at MAX_PAGES and the listing pages are a bounded
                # slice of two categories.
                job.raw["coverage"] = "partial" if surface == "listing_page" else "bounded_api"
                jobs.append(job)

    api_refused = False
    budget = [MAX_PAGES]  # one request a page: nothing is retried or followed
    for page in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {
            "page": page,
            "perPage": PER_PAGE,
            "experienceLevels[]": list(EXPERIENCE_LEVELS),
        }
        try:
            body = _get(session, API_URL, params=params,
                        headers=_BROWSER_HEADERS, budget=budget)
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise ValueError(f"{API_URL} did not return JSON: {exc}") from exc
        except Exception as exc:
            _report(f"justjoin_it: page {page}: {exc}"
                    + ("" if page == 1 else "; earlier pages kept, coverage partial"), errors)
            api_refused = page == 1
            break
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            _report(
                f"justjoin_it: page {page} answered 200 but the body is not "
                "the offers payload (no 'data' list) — the internal API this "
                "Tier 2 source depends on has changed shape",
                errors,
            )
            break
        keep(data, "api")
        if len(data) < PER_PAGE:
            break
        if page == MAX_PAGES:
            _report(f"justjoin_it: page cap reached ({MAX_PAGES} full pages of "
                    f"{PER_PAGE}); more offers may exist, coverage partial", errors)
    if api_refused:
        _fetch_listing_pages(keep, skipped, session, errors)
    logger.info(
        "justjoin_it: %d postings kept, %d non-DS/ML skipped, %d senior+ skipped, "
        "%d malformed rows skipped",
        len(jobs), skipped["category"], skipped["experience"], skipped["malformed"],
    )
    return jobs


def _fetch_listing_pages(keep: Any, skipped: dict[str, int], session: Any,
                         errors: list[str] | None) -> None:
    """Read the bounded public listing pages and say what was not covered."""
    read = 0
    failed = False
    capped: list[str] = []
    budget = [LISTING_MAX_REQUESTS]
    for category in LISTING_CATEGORIES:
        for page in range(1, LISTING_MAX_PAGES + 1):
            params: dict[str, Any] = {"experience-levels": ",".join(EXPERIENCE_LEVELS)}
            if page > 1:
                params["page"] = page
            try:
                offers, last = parse_listing_page(_get(
                    session, LISTING_URL.format(category=category), params=params,
                    headers={}, budget=budget, follow=True))
            except Exception as exc:
                _report(f"justjoin_it: listing page {category}/{page}: {exc}", errors)
                failed = True
                break
            if len(offers) > _LISTING_MAX_ROWS:
                _report(f"justjoin_it: listing page {category}/{page}: record cap "
                        "reached; coverage partial", errors)
            rows = offers[:_LISTING_MAX_ROWS]
            before = skipped["malformed"]
            read += len(rows)
            keep(rows, "listing_page")
            bad = skipped["malformed"] - before
            if bad:
                _report(f"justjoin_it: listing page {category}/{page}: skipped {bad} "
                        f"malformed row(s) of {len(rows)}; valid rows kept", errors)
            if len(offers) < LISTING_PAGE_SIZE or (last is not None and page >= last):
                break
            if page == LISTING_MAX_PAGES:
                capped.append(f"{category} {page}/{last if last is not None else '?'}")
    if not read and not failed:
        _report(
            "justjoin_it: API unavailable; the public listing pages of "
            f"{', '.join(LISTING_CATEGORIES)} answered and listed no offers",
            errors,
        )
    if read:
        _report(
            f"justjoin_it: API unavailable; read {read} offers from the public "
            f"listing pages of {', '.join(LISTING_CATEGORIES)} only — partial "
            "coverage, not the board"
            + (f"; page cap reached ({', '.join(capped)})" if capped else ""),
            errors,
        )
