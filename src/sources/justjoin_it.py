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
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..models import Job
from ..util import get_logger, http_get_json, parse_datetime

logger = get_logger(__name__)

API_URL = "https://api.justjoin.it/v2/user-panel/offers"

#: The request shape the site's own frontend uses. The first live run
#: answered 503 to this pipeline's default client identity; an internal API
#: fronted by a CDN often admits only browser-shaped traffic, and matching
#: the frontend's own headers is part of the Tier 2 bargain. If 503 persists
#: even so, the block is TLS-fingerprint-level and this source stays
#: degraded until the endpoint is re-scouted in the site's devtools.
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

#: Newest-first pages of 100. Three pages of junior/mid postings comfortably
#: covers a day of this board's DS/ML output; anything deeper is stale by the
#: freshness window's construction, same argument as the other global feeds.
PER_PAGE = 100
MAX_PAGES = 3

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
    r"analytics|data analyst|data engineer"
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

    return Job(
        source="justjoin_it",
        company=company,
        title=title,
        url=JOB_URL.format(slug=slug),
        location=location,
        description=" ".join(parts),
        posted_at=parse_datetime(_text(offer.get("publishedAt")) or None),
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
            # The listing has no ad body — scoring must know it is reading a
            # synthesized teaser, not the posting. Same contract as Adzuna.
            "snippet_only": True,
        },
    )


def fetch(
    config: Any, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch up to `MAX_PAGES` of junior/mid offers, DS/ML only. Never raises."""
    jobs: list[Job] = []
    skipped_category = 0
    skipped_experience = 0
    for page in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {
            "page": page,
            "perPage": PER_PAGE,
            "experienceLevels[]": list(EXPERIENCE_LEVELS),
        }
        try:
            payload = http_get_json(API_URL, params=params, session=session,
                                    headers=dict(_BROWSER_HEADERS))
        except Exception as exc:
            _report(f"justjoin_it: page {page}: {exc}", errors)
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
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            level = _text(entry.get("experienceLevel")).lower()
            if level and level not in EXPERIENCE_LEVELS:
                skipped_experience += 1
                continue
            title = _text(entry.get("title"))
            if title and not _is_ds_ml(title, _skill_names(entry.get("requiredSkills"))):
                skipped_category += 1
                continue
            try:
                job = parse_offer(entry)
            except Exception as exc:  # one bad entry must not kill the page
                logger.debug("justjoin_it: skipping malformed entry: %s", exc)
                continue
            if job is not None:
                jobs.append(job)
        if len(data) < PER_PAGE:
            break
    logger.info(
        "justjoin_it: %d postings kept, %d non-DS/ML skipped, %d senior+ skipped",
        len(jobs), skipped_category, skipped_experience,
    )
    return jobs
