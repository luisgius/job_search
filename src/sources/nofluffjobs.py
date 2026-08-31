"""No Fluff Jobs — Polish/CEE board, via its internal JSON API. Tier 2.

No official API here either. The site's own frontend reads
`nofluffjobs.com/api/posting` — one document listing every active posting —
and this adapter reads the same thing. The Tier 2 bargain and its
consequences are the same as Just Join IT's (see `justjoin_it.py` for the
full argument): nothing raises out of `fetch()`, a non-listing 200 is
reported as shape drift by name, `src/health.py`'s "went silent" alert is
the backstop for the failure mode an unowned endpoint actually produces,
and the recorded fixture plus the `network`-marked live contract are how a
rename is seen rather than suffered.

Two things are specific to this board:

  * the document is the *whole* board (a few MB, thousands of postings) in
    one request — there is no pagination to walk, so one GET a morning is
    also the politest thing this adapter could possibly do;
  * postings carry a `category` slug ("data", "artificial-intelligence",
    …) and a `seniority` list ("Junior", "Mid", …), so the data/AI +
    junior/mid cut the spec asks for is made on structured fields, with the
    title regex only as a fallback for postings whose category is missing.

The listing has no ad body, so `description` is synthesized from the
structured fields and marked `raw["snippet_only"]` — Adzuna's contract:
scoring may read it, but it must know it is reading a teaser.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..models import Job
from ..util import get_logger, http_get_json, parse_datetime

logger = get_logger(__name__)

API_URL = "https://nofluffjobs.com/api/posting"

#: The job page for one posting's `url` slug.
JOB_URL = "https://nofluffjobs.com/job/{slug}"

#: Category slugs that count as data/AI. `business-intelligence` is included
#: because this board files analytics-engineer-shaped roles there; the title
#: fallback below would catch most of them anyway, and the double net is
#: cheaper than losing them to a filing quirk.
DATA_CATEGORIES = frozenset({
    "data", "artificial-intelligence", "ai", "machine-learning",
    "big-data", "business-intelligence",
})

#: The spec for this source is junior+mid. A posting listing several levels
#: is kept when ANY of them is junior/mid; trainee-only and senior-only are
#: dropped. Matched case-insensitively — the API capitalises ("Mid").
WANTED_SENIORITY = frozenset({"junior", "mid"})

#: Title fallback for postings with no usable category — same word-bounded
#: coarse net as the other global feeds, stage 2 stays the decider.
DS_RE = re.compile(
    r"\b("
    r"data scien(?:ce|tist)s?|machine[- ]learning|deep learning|"
    r"ml|ai|nlp|llm|computer vision|mlops|"
    r"applied scien(?:ce|tist)s?|decision scien(?:ce|tist)s?|"
    r"analytics|data analyst|data engineer"
    r")\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def _seniorities(posting: Mapping[str, Any]) -> list[str]:
    value = posting.get("seniority")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_text(v).lower() for v in value if _text(v)]


def _wanted_level(levels: list[str]) -> bool:
    """Junior/mid postings pass; an empty list passes too (nothing stated is
    not evidence of seniority, and stage 2's title rules still apply)."""
    if not levels:
        return True
    return any(level in WANTED_SENIORITY for level in levels)


def _wanted_category(posting: Mapping[str, Any]) -> bool:
    category = _text(posting.get("category")).lower()
    if category:
        return category in DATA_CATEGORIES
    title = _text(posting.get("title"))
    return bool(title and DS_RE.search(title))


def _money(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return _text(value)


def _salary(posting: Mapping[str, Any]) -> str | None:
    node = posting.get("salary")
    if not isinstance(node, Mapping):
        return None
    low = _money(node.get("from"))
    high = _money(node.get("to"))
    if not low and not high:
        return None
    amount = f"{low}–{high}" if low and high and low != high else (low or high)
    currency = _text(node.get("currency")).upper()
    return f"{amount} {currency}".strip()


def _places(posting: Mapping[str, Any]) -> tuple[str, str | None, bool]:
    """`(display cities, ISO country, fully_remote)` from the location node."""
    node = posting.get("location")
    node = node if isinstance(node, Mapping) else {}
    remote = bool(node.get("fullyRemote"))
    cities: list[str] = []
    country: str | None = None
    places = node.get("places")
    if isinstance(places, list):
        for place in places:
            if not isinstance(place, Mapping):
                continue
            city = _text(place.get("city"))
            if city and city.lower() != "remote" and city not in cities:
                cities.append(city)
            if country is None:
                nation = place.get("country")
                code = (
                    _text(nation.get("code")) if isinstance(nation, Mapping) else ""
                ).upper()
                if len(code) == 2 and code.isalpha():
                    country = code
    return ", ".join(cities), country, remote


def parse_posting(posting: Mapping[str, Any]) -> Job | None:
    """One listing entry -> `Job`, or None when it is not usable."""
    title = _text(posting.get("title"))
    company = _text(posting.get("name"))
    slug = _text(posting.get("url"))
    if not title or not company or not slug:
        return None

    location, country, fully_remote = _places(posting)
    remote = True if fully_remote else None
    if remote and not location:
        location = "Remote"

    levels = _seniorities(posting)
    technology = _text(posting.get("technology"))
    parts: list[str] = []
    if technology:
        parts.append(f"Main technology: {technology}.")
    if levels:
        parts.append("Seniority: " + ", ".join(levels) + ".")
    category = _text(posting.get("category"))
    if category:
        parts.append(f"Category: {category}.")

    posting_id = posting.get("id")
    return Job(
        source="nofluffjobs",
        company=company,
        title=title,
        url=JOB_URL.format(slug=slug),
        location=location,
        description=" ".join(parts),
        posted_at=parse_datetime(posting.get("posted")),
        remote=remote,
        salary=_salary(posting),
        country=country,
        ats=None,
        ats_job_id=str(posting_id) if posting_id not in (None, "") else slug,
        raw={
            "board": "nofluffjobs",
            "slug": slug,
            "category": category or None,
            "seniority": levels or None,
            "technology": technology or None,
            # `renewed` is the board bumping a stale ad back to the top; kept
            # for the tracker's repost logic to see, never used as freshness.
            "renewed": posting.get("renewed"),
            # No ad body in the listing — Adzuna's contract, see module doc.
            "snippet_only": True,
        },
    )


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch the board and keep the data/AI, junior/mid slice. Never raises."""
    try:
        payload = http_get_json(API_URL, session=session)
    except Exception as exc:
        _report(f"nofluffjobs: {exc}", errors)
        return []
    postings = payload.get("postings") if isinstance(payload, Mapping) else None
    if not isinstance(postings, list):
        _report(
            "nofluffjobs: answered 200 but the body is not the posting "
            "listing (no 'postings' list) — the internal API this Tier 2 "
            "source depends on has changed shape",
            errors,
        )
        return []

    jobs: list[Job] = []
    skipped_category = 0
    skipped_seniority = 0
    for entry in postings:
        if not isinstance(entry, Mapping):
            continue
        if not _wanted_category(entry):
            skipped_category += 1
            continue
        if not _wanted_level(_seniorities(entry)):
            skipped_seniority += 1
            continue
        try:
            job = parse_posting(entry)
        except Exception as exc:  # one bad posting must not kill the board
            logger.debug("nofluffjobs: skipping malformed posting: %s", exc)
            continue
        if job is not None:
            jobs.append(job)
    logger.info(
        "nofluffjobs: %d postings kept, %d other categories skipped, "
        "%d senior-only skipped",
        len(jobs), skipped_category, skipped_seniority,
    )
    return jobs
