"""Public ATS board APIs — the cheapest, highest-signal source.

Six vendors, one shape: an unauthenticated endpoint per company ("slug"), so
there are no keys, no scraping and no rate-limit games; better still, every
posting carries a stable ATS id, which is what keeps `Job.key` from drifting
when a company edits a title.

    greenhouse       boards-api.greenhouse.io      US-heavy, big tech
    lever            api.lever.co                  US-heavy, scale-ups
    workable         apply.workable.com            EU/Greek-founded, mid-size
    ashby            api.ashbyhq.com               modern start-ups
    smartrecruiters  api.smartrecruiters.com       EU enterprise (DE/FR/ES)
    personio         {slug}.jobs.personio.de       German/Spanish/Italian SMB

The last four are what a search run from Spain actually needs: Greenhouse and
Lever are where American companies post, and a watchlist made only of those
two answers the question "who is hiring in San Francisco?" rather than "who is
hiring in Valencia?".

The price of all six is that slugs rot silently: a company renames its board
and the pipeline just starts returning zero jobs for it forever. That is what
the `--check` CLI is for::

    python -m src.sources.ats_boards --check greenhouse spotify
    python -m src.sources.ats_boards --check personio acme
    python -m src.sources.ats_boards --check-all

Nothing in here raises out of `fetch()` — one dead board must never take the
whole run down with it.

**None of these boards may claim an `ats` value `apply.autoapply` accepts.**
`autoapply.SUPPORTED_ATS` is `("greenhouse", "lever")` and nothing else has
been through the screener-bail work, so every board below sets `ats=` to its
own vendor name. That keeps `Job.key` stable and unique per vendor while
guaranteeing `eligible()` routes the job to the digest for a human click.
"""

from __future__ import annotations

import argparse
import html as html_module
import json as json_lib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..config import Config, ConfigError
from ..models import Job
from ..util import (
    get_logger,
    html_to_text,
    http_get,
    http_get_json,
    parse_datetime,
    setup_logging,
    truncate,
)

logger = get_logger(__name__)

GREENHOUSE_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/{slug}"
WORKABLE_ACCOUNT_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
WORKABLE_JOB_URL = "https://apply.workable.com/{slug}/j/{shortcode}/"
ASHBY_JOB_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
ASHBY_JOB_URL = "https://jobs.ashbyhq.com/{slug}/{job_id}"
SMARTRECRUITERS_POSTINGS_URL = (
    "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
)
SMARTRECRUITERS_POSTING_URL = (
    "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
)
SMARTRECRUITERS_APPLY_URL = "https://jobs.smartrecruiters.com/{slug}/{posting_id}"
#: Personio is per-tenant rather than per-path: the slug *is* the subdomain.
#: Most tenants live on `.de`; a minority were provisioned on `.com`, which is
#: why `fetch_personio` falls back rather than making the user guess.
PERSONIO_PRIMARY_HOST = "{slug}.jobs.personio.de"
PERSONIO_FALLBACK_HOST = "{slug}.jobs.personio.com"
PERSONIO_XML_URL = "https://{host}/xml"
PERSONIO_JOB_URL = "https://{host}/job/{job_id}"

#: Boards this module knows how to talk to, in watchlist order.
BOARDS: tuple[str, ...] = (
    "greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio",
)

# Project root, so `--check` works from any working directory.
_ROOT = Path(__file__).resolve().parents[2]

_REMOTE_RE = re.compile(
    r"\b(remote(?:ly)?|work from home|wfh|home[- ]office|telecommut\w*|anywhere)\b",
    re.IGNORECASE,
)
# Greenhouse boards routinely carry placeholder offices; they are noise, not
# locations, and would confuse the geo filter downstream.
_PLACEHOLDER_LOCATIONS = {"", "n/a", "na", "none", "no office", "unknown", "-"}

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_STATUS_HINTS: dict[int, str] = {
    401: "board requires authentication",
    403: "board refused the request (blocked or private)",
    404: "slug not found",
    410: "board no longer exists",
    429: "rate limited — try again later",
}

# How long the joined location field may get when a posting lists many offices.
#
# A *count* cap used to live here, and it cost real jobs: US companies list
# their offices home-first — SF, NYC, Austin, Seattle, then Berlin — so cutting
# at the fourth left a string that reads unambiguously American, and the one
# posting genuinely open in Berlin was rejected as a US role. This field is not
# decoration; it is the entire input to the geo filter, so anything dropped
# here deletes a job invisibly.
#
# The remaining cap is a sanity bound on a pathological payload rather than a
# tidiness rule, and it is generous enough for every real multi-office posting
# (~20 offices). A posting with more than that can still lose its last office —
# the alternative is an unbounded string in the digest, which is the cheaper
# failure of the two.
_MAX_LOCATION_CHARS = 600


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------


#: A bare hostname: two or more dot-separated labels and nothing else.
_HOST_LIKE_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)+$")


def _clean_slug(value: Any) -> str:
    """Normalise a watchlist slug, tolerating a pasted board URL.

    People copy `https://boards.greenhouse.io/spotify` out of the browser far
    more often than they type `spotify`, and a wrong slug is indistinguishable
    from an empty board, so it is worth fixing here rather than debugging later.

    The host is recognised by *shape* rather than by a list of known domains.
    That list used to be `.io/` and `.co/` — Greenhouse and Lever — which meant
    a pasted `apply.workable.com/contoso` was passed through whole and requested
    verbatim as a slug, producing a 404 that read like a dead company.

    A host is only dropped when something follows it: `booking.com` on its own
    is a slug, `apply.workable.com/contoso` is a URL.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.IGNORECASE)
    text = text.split("?")[0].split("#")[0]
    parts = [p for p in text.split("/") if p.strip()]
    if not parts:
        return ""
    if len(parts) > 1 and _HOST_LIKE_RE.match(parts[0]):
        parts = parts[1:]
    return parts[0].strip().strip("/").strip()


def company_from_slug(slug: str) -> str:
    """Guess a display company name from a board slug ("acme-corp" -> "Acme Corp").

    Neither the Greenhouse nor the Lever payload contains a company field, so
    this heuristic is the default. Callers override it by writing the watchlist
    entry as ``{slug: acme-corp, company: ACME Corporation}``.
    """
    cleaned = _clean_slug(slug).replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else str(slug or "").strip()


def _mentions_remote(*values: Any) -> bool:
    """True when any of `values` looks like a remote-work marker."""
    for value in values:
        if value and _REMOTE_RE.search(str(value)):
            return True
    return False


def _clean_location(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDER_LOCATIONS else text


def _join_locations(values: Iterable[Any]) -> str:
    """De-duplicate and join location strings, bounded by `_MAX_LOCATION_CHARS`.

    Every distinct office is kept: the geo filter reads this string, and a
    truncated office list is how a role open in Berlin gets rejected as
    American. Only an absurdly long list is cut, and always on a whole entry.
    """
    seen: list[str] = []
    for value in values:
        text = _clean_location(value)
        if text and text.lower() not in {s.lower() for s in seen}:
            seen.append(text)

    kept: list[str] = []
    length = 0
    for text in seen:
        extra = len(text) + (2 if kept else 0)
        if kept and length + extra > _MAX_LOCATION_CHARS:
            logger.debug(
                "location list truncated at %d of %d entries (over %d chars)",
                len(kept), len(seen), _MAX_LOCATION_CHARS,
            )
            break
        kept.append(text)
        length += extra
    return "; ".join(kept)


def _describe_error(exc: Exception) -> str:
    """Turn an exception into the short human message the CLI prints.

    `util.HttpError` embeds the URL and the status; users only need the status
    and what it means for them ("HTTP 404 (slug not found)").
    """
    text = str(exc).strip() or exc.__class__.__name__
    match = _HTTP_STATUS_RE.search(text)
    if match:
        status = int(match.group(1))
        hint = _STATUS_HINTS.get(status)
        return f"HTTP {status} ({hint})" if hint else f"HTTP {status}"
    return truncate(text, 200, suffix=" …")


def _as_list(payload: Any, *keys: str) -> list[Any]:
    """Coerce an API payload into a list of postings, whatever shape it took."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _text(value: Any) -> str:
    """A trimmed string, but only for values that really are strings.

    Every field on every payload below is optional and none of them are typed,
    so `str(value)` on a stray dict would put `{'label': 'Full-time'}` into a
    job title. Anything that is not a string is treated as absent.
    """
    return value.strip() if isinstance(value, str) else ""


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """The first of `keys` that holds a non-empty string.

    Vendors rename fields between API versions and some serve both spellings
    at once (Workable's `countryCode` / `country_code`). Asking for both costs
    nothing and turns a silent empty field into a filled one.
    """
    for key in keys:
        found = _text(payload.get(key))
        if found:
            return found
    return ""


def _joined_sections(sections: Iterable[tuple[str, Any]]) -> str:
    """Flatten `(heading, html)` blocks into one plain-text description.

    Workable, SmartRecruiters and Personio all split an ad the way Lever does
    with `lists`: an intro, then the requirements, then the benefits, each in
    its own HTML field. The requirements block is the single most
    scoring-relevant part of any ad, so dropping it would leave the model
    judging a paragraph of company boilerplate.
    """
    parts: list[str] = []
    for heading, raw in sections:
        body = html_to_text(raw if isinstance(raw, str) else "").strip()
        if not body:
            continue
        title = str(heading or "").strip()
        # A heading that the block already opens with would read twice.
        if title and not body.lower().startswith(title.lower()):
            parts.append(f"{title}\n{body}")
        else:
            parts.append(body)
    return "\n\n".join(parts).strip()


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------


def _greenhouse_location(posting: Mapping[str, Any]) -> str:
    """Location for a Greenhouse posting.

    `location` is missing or null on plenty of real boards, in which case the
    offices list is the only hint we have.
    """
    location = posting.get("location")
    if isinstance(location, Mapping):
        name = _clean_location(location.get("name"))
        if name:
            return name
    elif isinstance(location, str):
        name = _clean_location(location)
        if name:
            return name

    offices = posting.get("offices")
    if isinstance(offices, list):
        candidates = []
        for office in offices:
            if isinstance(office, Mapping):
                candidates.append(office.get("location") or office.get("name"))
            else:
                candidates.append(office)
        return _join_locations(candidates)
    return ""


def _greenhouse_salary(posting: Mapping[str, Any]) -> str | None:
    """Pull a pay range out of the board's custom `metadata` list, if present.

    Greenhouse has no salary field; boards that publish one put it in metadata
    under a name of the company's choosing, so match loosely and give up quietly.
    """
    metadata = posting.get("metadata")
    if not isinstance(metadata, list):
        return None
    for entry in metadata:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").lower()
        if not any(word in name for word in ("salary", "compensation", "pay range")):
            continue
        value = entry.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _parse_greenhouse_posting(
    posting: Mapping[str, Any], slug: str, company: str
) -> Job | None:
    """Convert one Greenhouse posting into a `Job`, or None when unusable."""
    title = str(posting.get("title") or "").strip()
    if not title:
        return None

    job_id = posting.get("id")
    ats_job_id = str(job_id).strip() if job_id not in (None, "") else None
    url = str(posting.get("absolute_url") or "").strip()
    if not url and ats_job_id:
        url = f"https://boards.greenhouse.io/{slug}/jobs/{ats_job_id}"
    if not url:
        return None

    # `content` is HTML *entity-escaped* HTML ("&lt;p&gt;"). Unescape exactly
    # once to recover real markup, then flatten it to text.
    description = html_to_text(html_module.unescape(str(posting.get("content") or "")))

    # `updated_at` moves every time anyone touches the posting, so it overstates
    # freshness badly; `first_published` is the real publication date.
    posted_at = parse_datetime(posting.get("first_published")) or parse_datetime(
        posting.get("updated_at")
    )

    location = _greenhouse_location(posting)
    departments = [
        str(d.get("name")).strip()
        for d in posting.get("departments") or []
        if isinstance(d, Mapping) and d.get("name")
    ]

    return Job(
        source="greenhouse",
        company=company,
        title=title,
        url=url,
        location=location,
        description=description,
        posted_at=posted_at,
        # Only ever assert remote-ness positively: "no remote marker" is not
        # evidence of an onsite role, and False would wrongly narrow filters.
        remote=True if _mentions_remote(location, title) else None,
        salary=_greenhouse_salary(posting),
        ats="greenhouse",
        ats_job_id=ats_job_id,
        raw={
            "board": "greenhouse",
            "slug": slug,
            "id": job_id,
            "requisition_id": posting.get("requisition_id"),
            "first_published": posting.get("first_published"),
            "updated_at": posting.get("updated_at"),
            "departments": departments,
        },
    )


def fetch_greenhouse(
    slug: str, *, session: Any = None, content: bool = True
) -> list[Job]:
    """Fetch every open posting on a Greenhouse board.

    `content=False` skips the (large) description payload — used by `--check`,
    which only needs to know the board answers and how many roles it has.

    Raises on transport/HTTP failure; `fetch` and `check_slug` are the layers
    that turn that into a logged, non-fatal error.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty greenhouse slug")

    payload = http_get_json(
        GREENHOUSE_BOARD_URL.format(slug=clean),
        params={"content": "true"} if content else None,
        session=session,
    )
    company = company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "jobs"):
        if not isinstance(posting, Mapping):
            logger.debug("greenhouse/%s: skipping non-object posting %r", clean, posting)
            continue
        try:
            # Rare, but some boards do embed a proper display name.
            name = str(posting.get("company_name") or "").strip()
            job = _parse_greenhouse_posting(posting, clean, name or company)
        except Exception as exc:  # one bad posting must not kill the board
            logger.debug(
                "greenhouse/%s: skipping malformed posting %r: %s",
                clean, posting.get("id"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "greenhouse/%s: skipping posting without title/url (id=%r)",
                clean, posting.get("id"),
            )
            continue
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------


def _lever_location(posting: Mapping[str, Any], categories: Mapping[str, Any]) -> str:
    """Location for a Lever posting: the primary one first, extras appended.

    `allLocations` is where multi-city postings really live, and the geo filter
    only ever sees `Job.location`, so a location-less "Remote" posting still
    has to say "Remote" somewhere.
    """
    values: list[Any] = [categories.get("location")]
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        values.extend(all_locations)
    joined = _join_locations(values)
    if joined:
        return joined
    workplace = str(posting.get("workplaceType") or "").strip()
    return "Remote" if workplace.lower() == "remote" else ""


def _lever_description(posting: Mapping[str, Any]) -> str:
    """Assemble the full posting text.

    Lever splits a posting into an intro (`description`) plus `lists` blocks,
    and the lists are where the requirements live — dropping them would throw
    away the single most scoring-relevant part of the ad.
    """
    parts: list[str] = []
    intro = str(posting.get("descriptionPlain") or "").strip()
    if not intro:
        intro = html_to_text(posting.get("description"))
    if intro:
        parts.append(intro)

    for block in posting.get("lists") or []:
        if not isinstance(block, Mapping):
            continue
        heading = html_to_text(block.get("text")).strip()
        body = html_to_text(block.get("content")).strip()
        chunk = "\n".join(p for p in (heading, body) if p)
        if chunk:
            parts.append(chunk)

    # Closing boilerplate sometimes carries visa/relocation info worth keeping.
    closing = str(posting.get("additionalPlain") or "").strip()
    if not closing:
        closing = html_to_text(posting.get("additional"))
    if closing:
        parts.append(closing)

    return "\n\n".join(parts).strip()


def _lever_salary(posting: Mapping[str, Any]) -> str | None:
    """Format `salaryRange` when the board publishes one."""
    salary = posting.get("salaryRange")
    if not isinstance(salary, Mapping):
        return None
    low, high = salary.get("min"), salary.get("max")
    currency = str(salary.get("currency") or "").strip()
    interval = str(salary.get("interval") or "").strip()
    if low is None and high is None:
        return None
    amount = f"{low}-{high}" if (low is not None and high is not None) else str(
        low if low is not None else high
    )
    return " ".join(p for p in (currency, amount, interval) if p) or None


def _parse_lever_posting(posting: Mapping[str, Any], slug: str, company: str) -> Job | None:
    """Convert one Lever posting into a `Job`, or None when unusable."""
    title = str(posting.get("text") or posting.get("title") or "").strip()
    if not title:
        return None

    posting_id = posting.get("id")
    ats_job_id = str(posting_id).strip() if posting_id not in (None, "") else None
    url = str(posting.get("hostedUrl") or posting.get("applyUrl") or "").strip()
    if not url and ats_job_id:
        url = f"https://jobs.lever.co/{slug}/{ats_job_id}"
    if not url:
        return None

    categories = posting.get("categories")
    if not isinstance(categories, Mapping):
        categories = {}

    location = _lever_location(posting, categories)
    workplace = str(
        posting.get("workplaceType") or categories.get("workplaceType") or ""
    ).strip().lower()

    if workplace == "remote" or _mentions_remote(location, categories.get("allLocations")):
        remote: bool | None = True
    elif workplace in {"onsite", "on-site", "hybrid"}:
        remote = False  # Lever states this explicitly, so it is trustworthy.
    else:
        remote = None

    # `createdAt` is a millisecond epoch; util.parse_datetime detects that.
    posted_at = parse_datetime(posting.get("createdAt")) or parse_datetime(
        posting.get("publishedAt")
    )

    return Job(
        source="lever",
        company=company,
        title=title,
        url=url,
        location=location,
        description=_lever_description(posting),
        posted_at=posted_at,
        remote=remote,
        salary=_lever_salary(posting),
        ats="lever",
        ats_job_id=ats_job_id,
        raw={
            "board": "lever",
            "slug": slug,
            "id": posting_id,
            "commitment": categories.get("commitment"),
            "team": categories.get("team"),
            "department": categories.get("department"),
            "workplace_type": workplace or None,
            "apply_url": posting.get("applyUrl"),
            "created_at": posting.get("createdAt"),
        },
    )


def fetch_lever(slug: str, *, session: Any = None) -> list[Job]:
    """Fetch every published posting on a Lever board.

    Raises on transport/HTTP failure; callers (`fetch`, `check_slug`) contain it.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty lever slug")

    payload = http_get_json(
        LEVER_POSTINGS_URL.format(slug=clean),
        params={"mode": "json"},
        session=session,
    )
    company = company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "data", "postings", "results"):
        if not isinstance(posting, Mapping):
            logger.debug("lever/%s: skipping non-object posting %r", clean, posting)
            continue
        try:
            job = _parse_lever_posting(posting, clean, company)
        except Exception as exc:  # one bad posting must not kill the board
            logger.debug(
                "lever/%s: skipping malformed posting %r: %s",
                clean, posting.get("id"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "lever/%s: skipping posting without title/url (id=%r)",
                clean, posting.get("id"),
            )
            continue
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------
# Workable
# --------------------------------------------------------------------------

#: `state` values that mean "this is not open to applicants". Matched as an
#: allow-list of *closures* rather than requiring `state == "published"`: an
#: unrecognised or absent state must never delete a real job, and Workable has
#: renamed these before.
_WORKABLE_CLOSED_STATES = frozenset(
    {"draft", "closed", "archived", "cancelled", "canceled", "on_hold", "on hold"}
)


def _workable_location(node: Any) -> tuple[str, bool | None]:
    """`(location string, remote flag)` for a Workable posting.

    Workable gives the location as structured parts and never as a sentence,
    so it has to be assembled here — `Job.location` is the *entire* input to
    the geo filter, and a bare "Valencia" with the country dropped is how a
    Spanish role ends up unresolvable.
    """
    if not isinstance(node, Mapping):
        return "", None

    # Both spellings are accepted on purpose: the widget API and the v3 API
    # disagree on the casing of this one field, and reading only one of them
    # silently loses the country on half of all payloads.
    country = _first_text(node, "country", "countryCode", "country_code")
    parts = [
        _clean_location(node.get("city")),
        _clean_location(node.get("region")),
        _clean_location(country),
    ]
    location = _join_locations(parts).replace("; ", ", ")

    telecommuting = node.get("telecommuting")
    remote: bool | None = True if telecommuting is True else None
    if remote and not location:
        # A remote posting with no place attached still has to *say* "remote",
        # or the geo filter sees an empty string and drops it as unresolvable.
        location = "Remote"
    return location, remote


def _parse_workable_posting(
    posting: Mapping[str, Any], slug: str, company: str
) -> Job | None:
    """Convert one Workable posting into a `Job`, or None when unusable."""
    title = _first_text(posting, "title", "name")
    if not title:
        return None

    state = _text(posting.get("state")).lower()
    if state in _WORKABLE_CLOSED_STATES:
        return None

    # `shortcode` is the stable public id and what every Workable URL is built
    # from; `code` is the customer's own requisition reference and changes.
    ats_job_id = _first_text(posting, "shortcode") or None
    if ats_job_id is None:
        ident = posting.get("id")
        ats_job_id = str(ident).strip() if ident not in (None, "") else None

    url = _first_text(posting, "url", "shortlink", "application_url", "apply_url")
    if not url and ats_job_id:
        url = WORKABLE_JOB_URL.format(slug=slug, shortcode=ats_job_id)
    if not url:
        return None

    location_node = posting.get("location")
    location, remote = _workable_location(location_node)

    # Description, requirements and benefits are three separate HTML blocks and
    # all three matter: "requirements" is where the years-of-experience and the
    # stack live, and "benefits" is where visa/relocation support is stated.
    description = _joined_sections([
        ("", posting.get("description")),
        ("Requirements", posting.get("requirements")),
        ("Benefits", posting.get("benefits")),
    ])

    if remote is None and _mentions_remote(location, title):
        remote = True

    posted_at = (
        parse_datetime(posting.get("created_at"))
        or parse_datetime(posting.get("published_on"))
        or parse_datetime(posting.get("published"))
    )

    return Job(
        source="workable",
        company=company,
        title=title,
        url=url,
        location=location,
        description=description,
        posted_at=posted_at,
        remote=remote,
        salary=None,          # the public widget payload carries no pay range
        ats="workable",
        ats_job_id=ats_job_id,
        raw={
            "board": "workable",
            "slug": slug,
            "id": ats_job_id,
            "code": posting.get("code"),
            "state": posting.get("state"),
            "department": posting.get("department"),
            # `employment_type` is one of `filters.EMPLOYMENT_TYPE_KEYS`, which
            # is what lets `employment_type_exclude` drop an internship whose
            # title says nothing at all.
            "employment_type": posting.get("employment_type"),
            "telecommuting": (
                location_node.get("telecommuting")
                if isinstance(location_node, Mapping) else None
            ),
            "apply_url": posting.get("application_url"),
            "created_at": posting.get("created_at"),
        },
    )


def fetch_workable(slug: str, *, session: Any = None, details: bool = True) -> list[Job]:
    """Fetch every published posting on a Workable account.

    `details=False` drops the (large) description/requirements/benefits blocks
    — used by `--check`, which only needs to know the account answers.

    Raises on transport/HTTP failure; `fetch` and `check_slug` contain that.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty workable slug")

    payload = http_get_json(
        WORKABLE_ACCOUNT_URL.format(slug=clean),
        params={"details": "true"} if details else None,
        session=session,
    )
    # Workable is the one vendor here that publishes a real display name, so
    # the slug heuristic is only the fallback.
    company = ""
    if isinstance(payload, Mapping):
        company = _first_text(payload, "name", "company_name")
    company = company or company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "jobs", "results"):
        if not isinstance(posting, Mapping):
            logger.debug("workable/%s: skipping non-object posting %r", clean, posting)
            continue
        try:
            job = _parse_workable_posting(posting, clean, company)
        except Exception as exc:  # one bad posting must not kill the account
            logger.debug(
                "workable/%s: skipping malformed posting %r: %s",
                clean, posting.get("shortcode"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "workable/%s: skipping posting without title/url or not published "
                "(shortcode=%r)", clean, posting.get("shortcode"),
            )
            continue
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------


def _ashby_secondary_locations(posting: Mapping[str, Any]) -> list[Any]:
    """Every extra city an Ashby posting is open in.

    Entries are objects (`{"location": "Madrid", "address": {...}}`) on the
    current API and were bare strings on an older one, so both are read. This
    matters for exactly the reason `allLocations` matters on Lever: a role open
    in Berlin *and* Valencia must not be pinned to whichever one landed in the
    primary `location` field, because `filters.passes_location` gates on
    `geo.countries_of(Job.location)` and can only see this string.
    """
    values: list[Any] = []
    for entry in posting.get("secondaryLocations") or []:
        if isinstance(entry, Mapping):
            values.append(
                _first_text(entry, "location", "locationName", "name") or None
            )
        elif isinstance(entry, str):
            values.append(entry)
    return values


def _ashby_address_country(posting: Mapping[str, Any]) -> str:
    """`address.postalAddress.addressCountry`, when the board fills it in."""
    address = posting.get("address")
    if not isinstance(address, Mapping):
        return ""
    postal = address.get("postalAddress")
    if not isinstance(postal, Mapping):
        postal = address
    return _text(postal.get("addressCountry"))


def _ashby_location(posting: Mapping[str, Any], remote: bool | None) -> str:
    """Location for an Ashby posting: primary, then every secondary."""
    secondary = _ashby_secondary_locations(posting)
    values: list[Any] = [posting.get("location")] + secondary
    joined = _join_locations(values)

    # Ashby's `location` is very often a bare city ("Valencia"), and the geo
    # city table only covers the larger hubs. The structured country is the
    # cheapest possible rescue — but only for a single-location posting, where
    # it unambiguously belongs to the one city named.
    if joined and not secondary:
        country = _ashby_address_country(posting)
        if country and country.lower() not in joined.lower():
            joined = f"{joined}, {country}"

    if joined:
        return joined
    return "Remote" if remote else ""


def _ashby_salary(posting: Mapping[str, Any]) -> str | None:
    """A pay range from `?includeCompensation=true`, when one is published."""
    compensation = posting.get("compensation")
    if not isinstance(compensation, Mapping):
        return None
    summary = _first_text(
        compensation,
        "compensationTierSummary",
        "scrapeableCompensationSalarySummary",
        "summary",
    )
    return summary or None


def _parse_ashby_posting(
    posting: Mapping[str, Any], slug: str, company: str
) -> Job | None:
    """Convert one Ashby posting into a `Job`, or None when unusable."""
    title = _first_text(posting, "title", "jobTitle")
    if not title:
        return None

    # Only an explicit False hides a posting. `isListed` missing is not the
    # same statement as `isListed: false`, and treating it as one would empty
    # every board that stops sending the field.
    if posting.get("isListed") is False:
        return None

    ident = posting.get("id")
    ats_job_id = str(ident).strip() if ident not in (None, "") else None
    url = _first_text(posting, "jobUrl", "applyUrl")
    if not url and ats_job_id:
        url = ASHBY_JOB_URL.format(slug=slug, job_id=ats_job_id)
    if not url:
        return None

    workplace = _text(posting.get("workplaceType")).lower()
    remote: bool | None = True if (
        posting.get("isRemote") is True or workplace == "remote"
    ) else None

    location = _ashby_location(posting, remote)
    if remote is None and _mentions_remote(location, title):
        remote = True

    description = _text(posting.get("descriptionPlain"))
    if not description:
        description = html_to_text(posting.get("descriptionHtml"))

    return Job(
        source="ashby",
        company=company,
        title=title,
        url=url,
        location=location,
        description=description,
        # `publishedAt` only. Ashby also sends `updatedAt`, which moves on any
        # edit, and `freshness.skip_undated` exists precisely so an unknown
        # date can be handled honestly rather than papered over with a guess.
        posted_at=parse_datetime(posting.get("publishedAt")),
        remote=remote,
        salary=_ashby_salary(posting),
        ats="ashby",
        ats_job_id=ats_job_id,
        raw={
            "board": "ashby",
            "slug": slug,
            "id": ident,
            "department": posting.get("department"),
            "team": posting.get("team"),
            "employment_type": posting.get("employmentType"),
            "workplace_type": workplace or None,
            "is_remote": posting.get("isRemote"),
            "apply_url": posting.get("applyUrl"),
            "published_at": posting.get("publishedAt"),
            "secondary_locations": [v for v in _ashby_secondary_locations(posting) if v],
        },
    )


def fetch_ashby(slug: str, *, session: Any = None) -> list[Job]:
    """Fetch every listed posting on an Ashby job board.

    Raises on transport/HTTP failure; callers (`fetch`, `check_slug`) contain it.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty ashby slug")

    payload = http_get_json(
        ASHBY_JOB_BOARD_URL.format(slug=clean),
        params={"includeCompensation": "true"},
        session=session,
    )
    company = company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "jobs", "results"):
        if not isinstance(posting, Mapping):
            logger.debug("ashby/%s: skipping non-object posting %r", clean, posting)
            continue
        try:
            job = _parse_ashby_posting(posting, clean, company)
        except Exception as exc:  # one bad posting must not kill the board
            logger.debug(
                "ashby/%s: skipping malformed posting %r: %s",
                clean, posting.get("id"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "ashby/%s: skipping unlisted posting or one without title/url "
                "(id=%r)", clean, posting.get("id"),
            )
            continue
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------
# SmartRecruiters
# --------------------------------------------------------------------------

#: The list endpoint caps a page here; asking for more silently returns 100.
SMARTRECRUITERS_PAGE_LIMIT = 100

#: SmartRecruiters is the only board here whose listing carries **no
#: description** — that lives behind one extra request *per posting*. A company
#: with 400 open roles would otherwise mean 400 HTTP calls in a stage that is
#: supposed to be the cheap one, so the fetch is capped and says so in the log.
#: A posting whose description was not fetched still scores, just worse:
#: `scoring` handles a thin description and the digest shows what it saw.
SMARTRECRUITERS_MAX_DESCRIPTIONS = 60

#: `jobAd.sections` keys, in reading order. `qualifications` is the block that
#: decides most scores; `companyDescription` is boilerplate but carries the
#: office/relocation language a Spain-based applicant needs.
SMARTRECRUITERS_SECTIONS: tuple[str, ...] = (
    "companyDescription", "jobDescription", "qualifications", "additionalInformation",
)


def _smartrecruiters_location(node: Any) -> tuple[str, bool | None]:
    """`(location string, remote flag)` for a SmartRecruiters posting."""
    if not isinstance(node, Mapping):
        return "", None

    country = _first_text(node, "country", "countryCode")
    parts = [
        _clean_location(node.get("city")),
        _clean_location(_first_text(node, "region", "regionCode")),
        # The country arrives as a lowercase ISO-2 code ("es"); `geo` matches
        # it as a whole word and is case-insensitive, but upper-casing keeps
        # the digest readable.
        _clean_location(country.upper() if len(country) == 2 else country),
    ]
    location = _join_locations(parts).replace("; ", ", ")

    remote: bool | None = True if node.get("remote") is True else None
    if remote and not location:
        location = "Remote"
    return location, remote


def _smartrecruiters_description(payload: Any) -> str:
    """Assemble the ad from `jobAd.sections`, in reading order."""
    if not isinstance(payload, Mapping):
        return ""
    job_ad = payload.get("jobAd")
    if not isinstance(job_ad, Mapping):
        return ""
    sections = job_ad.get("sections")
    if not isinstance(sections, Mapping):
        return ""

    blocks: list[tuple[str, Any]] = []
    for key in SMARTRECRUITERS_SECTIONS:
        node = sections.get(key)
        if not isinstance(node, Mapping):
            continue
        blocks.append((_text(node.get("title")), node.get("text")))
    return _joined_sections(blocks)


def _parse_smartrecruiters_posting(
    posting: Mapping[str, Any], slug: str, company: str
) -> Job | None:
    """Convert one SmartRecruiters *listing* into a `Job` (no description yet)."""
    title = _first_text(posting, "name", "title")
    if not title:
        return None

    ident = posting.get("id") or posting.get("uuid")
    ats_job_id = str(ident).strip() if ident not in (None, "") else None
    if not ats_job_id:
        return None

    # The applicant-facing host, which is not the API host. `company.identifier`
    # is the canonical spelling of the slug and is what the public URL uses.
    company_node = posting.get("company")
    identifier = slug
    display = ""
    if isinstance(company_node, Mapping):
        identifier = _first_text(company_node, "identifier") or slug
        display = _first_text(company_node, "name")
    url = SMARTRECRUITERS_APPLY_URL.format(slug=identifier, posting_id=ats_job_id)

    location, remote = _smartrecruiters_location(posting.get("location"))

    employment = posting.get("typeOfEmployment")
    employment_label = (
        _first_text(employment, "label", "id") if isinstance(employment, Mapping)
        else _text(employment)
    )
    experience = posting.get("experienceLevel")
    experience_label = (
        _first_text(experience, "label", "id") if isinstance(experience, Mapping)
        else _text(experience)
    )
    department = posting.get("department")
    department_label = (
        _first_text(department, "label", "name") if isinstance(department, Mapping)
        else _text(department)
    )

    if remote is None and _mentions_remote(location, title):
        remote = True

    return Job(
        source="smartrecruiters",
        company=display or company,
        title=title,
        url=url,
        location=location,
        description="",       # filled in by the per-posting detail call
        posted_at=parse_datetime(posting.get("releasedDate"))
        or parse_datetime(posting.get("createdOn")),
        remote=remote,
        salary=None,
        ats="smartrecruiters",
        ats_job_id=ats_job_id,
        raw={
            "board": "smartrecruiters",
            "slug": identifier,
            "id": ats_job_id,
            "uuid": posting.get("uuid"),
            "ref_number": posting.get("refNumber"),
            "department": department_label,
            # One of `filters.EMPLOYMENT_TYPE_KEYS`, so an "Internship" whose
            # title is a plain "Software Engineer" is still dropped.
            "employment_type": employment_label,
            "experience_level": experience_label,
            "released_date": posting.get("releasedDate"),
            "description_fetched": False,
        },
    )


def fetch_smartrecruiters(
    slug: str,
    *,
    session: Any = None,
    details: bool = True,
    max_descriptions: int = SMARTRECRUITERS_MAX_DESCRIPTIONS,
) -> list[Job]:
    """Fetch one page of postings for a SmartRecruiters company.

    Two requests-shapes, because the vendor splits them: one listing call, then
    one detail call per posting for the description. `details=False` skips the
    second entirely (used by `--check`), and `max_descriptions` bounds it so a
    company with hundreds of open roles cannot turn the cheap stage expensive.

    A detail call that fails costs that posting its description and nothing
    else — the job still reaches the digest with its title, company and
    location, which is far better than losing it.

    Raises on transport/HTTP failure of the *listing*; callers contain that.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty smartrecruiters slug")

    payload = http_get_json(
        SMARTRECRUITERS_POSTINGS_URL.format(slug=clean),
        params={"limit": SMARTRECRUITERS_PAGE_LIMIT},
        session=session,
    )
    company = company_from_slug(clean)

    jobs: list[Job] = []
    for posting in _as_list(payload, "content", "postings", "results"):
        if not isinstance(posting, Mapping):
            logger.debug(
                "smartrecruiters/%s: skipping non-object posting %r", clean, posting
            )
            continue
        try:
            job = _parse_smartrecruiters_posting(posting, clean, company)
        except Exception as exc:  # one bad posting must not kill the company
            logger.debug(
                "smartrecruiters/%s: skipping malformed posting %r: %s",
                clean, posting.get("id"), exc,
            )
            continue
        if job is None:
            logger.debug(
                "smartrecruiters/%s: skipping posting without title/id (id=%r)",
                clean, posting.get("id"),
            )
            continue
        jobs.append(job)

    total = payload.get("totalFound") if isinstance(payload, Mapping) else None
    if isinstance(total, (int, float)) and not isinstance(total, bool) \
            and total > len(jobs):
        logger.info(
            "smartrecruiters/%s: %d of %d postings (single page of %d)",
            clean, len(jobs), int(total), SMARTRECRUITERS_PAGE_LIMIT,
        )

    if details:
        _load_smartrecruiters_descriptions(
            jobs, clean, session=session, limit=max_descriptions
        )
    return jobs


def _load_smartrecruiters_descriptions(
    jobs: list[Job], slug: str, *, session: Any = None, limit: int
) -> None:
    """Fill in each job's description with one extra request per posting.

    Mutates `jobs` in place and never raises. The cap is announced in the log
    rather than applied silently, because "these 40 jobs were scored on their
    titles alone" is something the user has to be able to find out.
    """
    budget = max(0, int(limit))
    if not jobs or not budget:
        if jobs:
            logger.info(
                "smartrecruiters/%s: description fetching is off — %d posting(s) "
                "keep title-only descriptions", slug, len(jobs),
            )
        return

    if len(jobs) > budget:
        logger.info(
            "smartrecruiters/%s: fetching descriptions for %d of %d postings "
            "(cap: SMARTRECRUITERS_MAX_DESCRIPTIONS=%d) — the remainder are "
            "scored on title, company and location alone",
            slug, budget, len(jobs), budget,
        )

    fetched = 0
    for job in jobs[:budget]:
        url = SMARTRECRUITERS_POSTING_URL.format(
            slug=job.raw.get("slug") or slug, posting_id=job.ats_job_id
        )
        try:
            detail = http_get_json(url, session=session)
        except Exception as exc:
            # One posting's description, not the board. Debug rather than
            # warning: on a big board this would otherwise be pages of noise.
            logger.debug(
                "smartrecruiters/%s: no description for %s: %s",
                slug, job.ats_job_id, exc,
            )
            continue
        try:
            description = _smartrecruiters_description(detail)
        except Exception as exc:
            logger.debug(
                "smartrecruiters/%s: unreadable description for %s: %s",
                slug, job.ats_job_id, exc,
            )
            continue
        if description:
            job.description = description
            job.raw["description_fetched"] = True
            fetched += 1
    logger.debug(
        "smartrecruiters/%s: %d description(s) fetched", slug, fetched
    )


# --------------------------------------------------------------------------
# Personio
# --------------------------------------------------------------------------


def _personio_slug(value: Any) -> str:
    """Tenant identity for a Personio feed.

    Personio is per-*subdomain*, not per-path, so the slug is either the bare
    tenant name (`acme`) or the whole host (`acme.jobs.personio.com`, for the
    minority of tenants provisioned on `.com`). `_clean_slug` is wrong here on
    purpose: it drops the host and keeps the first path segment, which is
    exactly backwards for this vendor.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.IGNORECASE)
    text = text.split("/")[0].split("?")[0].strip().strip(".")
    return text.lower()


def _personio_host(slug: str) -> str:
    """The feed host for a tenant: a full host is used as given."""
    clean = _personio_slug(slug)
    if not clean:
        return ""
    if "personio." in clean:
        return clean
    return PERSONIO_PRIMARY_HOST.format(slug=clean)


def _personio_company(slug: str) -> str:
    """Display name from the tenant label ("acme.jobs.personio.de" -> "Acme")."""
    clean = _personio_slug(slug)
    label = clean.split(".")[0] if clean else ""
    return company_from_slug(label) if label else company_from_slug(slug)


def _element_text(element: Any, tag: str) -> str:
    """Trimmed text of a direct child element, or "" when it is absent."""
    if element is None:
        return ""
    found = element.find(tag)
    if found is None:
        return ""
    return (found.text or "").strip()


def _personio_description(position: Any) -> str:
    """Concatenate the titled `<jobDescription>` sections into one text.

    Each section is a heading plus a CDATA-wrapped HTML body ("Deine Aufgaben",
    "Dein Profil", "Was wir bieten"), and the profile section is where every
    requirement lives — the same reason Lever's `lists` blocks are kept.
    """
    container = position.find("jobDescriptions")
    if container is None:
        return ""
    blocks: list[tuple[str, Any]] = []
    for section in container.iter("jobDescription"):
        blocks.append((_element_text(section, "name"), _element_text(section, "value")))
    return _joined_sections(blocks)


def _parse_personio_position(position: Any, host: str, company: str) -> Job | None:
    """Convert one `<position>` element into a `Job`, or None when unusable."""
    title = _element_text(position, "name")
    if not title:
        return None

    ats_job_id = _element_text(position, "id") or None
    if not ats_job_id:
        return None

    office = _element_text(position, "office")
    subcompany = _element_text(position, "subcompany")
    department = _element_text(position, "department")
    employment_type = _element_text(position, "employmentType")
    schedule = _element_text(position, "schedule")
    seniority = _element_text(position, "seniority")

    description = _personio_description(position)
    # `office` is the only place a Personio feed states geography — there is no
    # country field at all — so it is passed through untouched for `geo` to
    # resolve ("Valencia" -> ES, "München" -> DE).
    location = _clean_location(office)
    remote = True if _mentions_remote(location, title, department) else None
    if remote and not location:
        location = "Remote"

    return Job(
        source="personio",
        company=company,
        title=title,
        url=PERSONIO_JOB_URL.format(host=host, job_id=ats_job_id),
        location=location,
        description=description,
        posted_at=parse_datetime(_element_text(position, "createdAt") or None),
        remote=remote,
        salary=None,
        ats="personio",
        ats_job_id=ats_job_id,
        raw={
            "board": "personio",
            "slug": host,
            "id": ats_job_id,
            "subcompany": subcompany or None,
            "department": department or None,
            "recruiting_category": _element_text(position, "recruitingCategory") or None,
            # One of `filters.EMPLOYMENT_TYPE_KEYS`. Personio states this as
            # "permanent" / "intern" / "trainee" / "freelance", which is the
            # only signal that a neutrally-titled posting is an internship.
            "employment_type": employment_type or None,
            "schedule": schedule or None,
            "seniority": seniority or None,
            "years_of_experience": _element_text(position, "yearsOfExperience") or None,
            "occupation": _element_text(position, "occupation") or None,
            "created_at": _element_text(position, "createdAt") or None,
        },
    )


#: The documented root element of a Personio feed.
PERSONIO_ROOT_TAG = "workzag-jobs"


def _parse_personio_xml(text: str, host: str, company: str) -> list[Job]:
    """Parse a `<workzag-jobs>` document into jobs. Raises when it is not one.

    Uses the stdlib `xml.etree.ElementTree`, which **does not resolve external
    entities** — no DTD fetching, no `xxe` file reads, no billion-laughs
    expansion of external references. That is what makes it acceptable to point
    at a third-party feed, and it is the reason there is no `defusedxml`
    dependency here: the hardening this feed needs, the stdlib already has.

    The root tag is checked because an HTML error page is *well-formed XML*.
    Without the check, a tenant answering 200 with a login page or a Cloudflare
    challenge parses cleanly into zero positions and reads, every morning, as a
    company that simply is not hiring.
    """
    import xml.etree.ElementTree as ElementTree  # stdlib, imported lazily

    root = ElementTree.fromstring(text)
    # Tolerate a namespace prefix ("{urn:x}workzag-jobs") without demanding one.
    tag = str(root.tag).rsplit("}", 1)[-1].strip().lower()
    if tag != PERSONIO_ROOT_TAG and root.find(".//position") is None:
        raise ValueError(
            f"root element is <{tag}>, not <{PERSONIO_ROOT_TAG}> — this is not a "
            "Personio job feed (an error page, a login wall or the wrong host)"
        )

    jobs: list[Job] = []
    for position in root.iter("position"):
        try:
            job = _parse_personio_position(position, host, company)
        except Exception as exc:  # one bad position must not kill the feed
            logger.debug("personio/%s: skipping malformed position: %s", host, exc)
            continue
        if job is None:
            logger.debug("personio/%s: skipping position without name/id", host)
            continue
        jobs.append(job)
    return jobs


def fetch_personio(slug: str, *, session: Any = None) -> list[Job]:
    """Fetch every published position from a Personio tenant's XML feed.

    Personio is the one board here that speaks XML rather than JSON, and the
    one whose tenants are split across two hosts: most are `.jobs.personio.de`,
    a minority `.jobs.personio.com`. A 404 on the first is retried on the
    second, so the watchlist entry can stay a bare `acme` — write the full host
    (`acme.jobs.personio.com`) to skip the guess.

    Raises on transport/HTTP failure of the last host tried.
    """
    clean = _personio_slug(slug)
    if not clean:
        raise ValueError("empty personio slug")

    hosts = [_personio_host(clean)]
    if "personio." not in clean:
        hosts.append(PERSONIO_FALLBACK_HOST.format(slug=clean))

    company = _personio_company(clean)
    last_error: Exception | None = None
    for index, host in enumerate(hosts):
        try:
            response = http_get(PERSONIO_XML_URL.format(host=host), session=session)
        except Exception as exc:
            last_error = exc
            status = _HTTP_STATUS_RE.search(str(exc))
            # Only a "no such tenant" is worth a second host. A 403 or a 500
            # means the tenant exists and something else is wrong, and trying
            # the other domain would only replace a useful error with a
            # confusing one.
            if index + 1 < len(hosts) and status and status.group(1) in ("404", "410"):
                logger.debug("personio/%s: %s — trying %s", host, exc, hosts[index + 1])
                continue
            raise
        text = getattr(response, "text", "") or ""
        try:
            return _parse_personio_xml(text, host, company)
        except Exception as exc:
            raise ValueError(f"{host} did not return parseable XML: {exc}") from exc
    raise last_error or ValueError(f"personio: no host answered for {clean!r}")


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def _fetch_board(board: str, slug: str, *, session: Any = None, **kwargs: Any) -> list[Job]:
    """Dispatch to the right fetcher.

    Deliberately resolves the public `fetch_*` functions at call time via the
    module globals rather than a lookup table captured at import, so tests that
    monkeypatch the public fetchers really do intercept every caller.
    """
    if board == "greenhouse":
        return fetch_greenhouse(slug, session=session, **kwargs)
    if board == "lever":
        return fetch_lever(slug, session=session)
    if board == "workable":
        return fetch_workable(slug, session=session, **kwargs)
    if board == "ashby":
        return fetch_ashby(slug, session=session)
    if board == "smartrecruiters":
        return fetch_smartrecruiters(slug, session=session, **kwargs)
    if board == "personio":
        return fetch_personio(slug, session=session)
    raise ValueError(f"unknown board {board!r}")


#: Extra keyword arguments that make one board's `--check` cheap. Checking a
#: slug only proves the board answers, so the expensive half of each fetch
#: (Greenhouse's descriptions, Workable's detail blocks, SmartRecruiters' one
#: request per posting) is skipped.
_CHEAP_CHECK_KWARGS: dict[str, dict[str, Any]] = {
    "greenhouse": {"content": False},
    "workable": {"details": False},
    "smartrecruiters": {"details": False},
}


# --------------------------------------------------------------------------
# watchlist -> jobs
# --------------------------------------------------------------------------


def _slug_for(board: str, value: Any) -> str:
    """Normalise a watchlist value the way `board` identifies its tenants.

    Every board but Personio is `host/SLUG`, so `_clean_slug` (which throws the
    host away and keeps the first path segment) is right. Personio is
    `SLUG.jobs.personio.de` — the slug *is* the host — so the same rule would
    delete the identity entirely.
    """
    if board == "personio":
        return _personio_slug(value)
    return _clean_slug(value)


def _watchlist_entries(raw: Any, board: str = "") -> list[tuple[str, str | None]]:
    """Normalise a watchlist board section into `(slug, company_override)` pairs.

    Accepts the three shapes people actually write::

        greenhouse: [spotify, {slug: acme-corp, company: ACME Corporation}]
        greenhouse: {acme-corp: ACME Corporation}
        greenhouse: spotify

    `board` selects the slug convention; omitting it keeps the `host/SLUG` one.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, Mapping):
        raw = [{"slug": k, "company": v} for k, v in raw.items()]
    if not isinstance(raw, Sequence):
        logger.warning("watchlist entry ignored, expected a list: %r", raw)
        return []

    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for entry in raw:
        company: str | None = None
        if isinstance(entry, Mapping):
            slug = _slug_for(
                board,
                entry.get("slug") or entry.get("board") or entry.get("id")
                or entry.get("host") or entry.get("name"),
            )
            override = entry.get("company") or entry.get("display_name")
            company = str(override).strip() if override else None
        else:
            slug = _slug_for(board, entry)
        if not slug:
            logger.warning("watchlist entry has no usable slug, skipping: %r", entry)
            continue
        if slug.lower() in seen:
            continue
        seen.add(slug.lower())
        pairs.append((slug, company or None))
    return pairs


def fetch(config: Config, *, session: Any = None, errors: list[str] | None = None) -> list[Job]:
    """Fetch every enabled board in the watchlist, across all six vendors.

    Each slug is isolated: a renamed board, a 500 or a malformed payload costs
    that company's postings and nothing else. Failures are logged and appended
    to `errors`; this function never raises.
    """
    jobs: list[Job] = []
    for board in BOARDS:
        if not config.source_enabled(board):
            logger.debug("%s disabled in config.sources, skipping", board)
            continue

        entries = _watchlist_entries(config.watchlist.get(board), board)
        if not entries:
            logger.warning("%s is enabled but watchlist.%s is empty", board, board)
            continue

        for slug, company_override in entries:
            try:
                found = _fetch_board(board, slug, session=session)
            except Exception as exc:
                message = f"{board}/{slug}: {_describe_error(exc)}"
                logger.warning("%s", message)
                if errors is not None:
                    errors.append(message)
                continue
            if company_override:
                for job in found:
                    job.company = company_override
            logger.info("%s/%s: %d postings", board, slug, len(found))
            jobs.extend(found)
    return jobs


# --------------------------------------------------------------------------
# slug checking / CLI
# --------------------------------------------------------------------------


def _check(board: str, slug: str, *, session: Any = None) -> tuple[bool, str, int | None]:
    """`check_slug` plus the posting count, which `--json` reports separately."""
    board_name = str(board or "").strip().lower()
    if board_name not in BOARDS:
        return False, f"unknown board {board!r} (expected one of {', '.join(BOARDS)})", None
    clean = _slug_for(board_name, slug)
    if not clean:
        return False, "empty slug", None

    try:
        # Descriptions are not needed to prove a board exists, and they are the
        # expensive half of every fetch — skip them where the vendor allows it.
        found = _fetch_board(
            board_name, clean, session=session, **_CHEAP_CHECK_KWARGS.get(board_name, {})
        )
    except Exception as exc:
        return False, _describe_error(exc), None

    count = len(found)
    if count == 0:
        return True, "0 postings (board reachable but empty)", 0
    return True, f"{count} posting{'s' if count != 1 else ''}", count


def check_slug(board: str, slug: str, *, session: Any = None) -> tuple[bool, str]:
    """Verify one board slug. Returns `(ok, human message)`, never raises.

    A reachable board with zero postings counts as OK: that is a real state
    (nobody is hiring today), not a broken slug.
    """
    ok, message, _count = _check(board, slug, session=session)
    return ok, message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.sources.ats_boards",
        description=(
            "Verify ATS board slugs before trusting them. Boards: "
            + ", ".join(BOARDS)
        ),
        epilog=(
            "examples:\n"
            "  python -m src.sources.ats_boards --check greenhouse spotify\n"
            "  python -m src.sources.ats_boards --check lever plaid\n"
            "  python -m src.sources.ats_boards --check workable acme\n"
            "  python -m src.sources.ats_boards --check ashby acme\n"
            "  python -m src.sources.ats_boards --check smartrecruiters Acme\n"
            "  python -m src.sources.ats_boards --check personio acme\n"
            "  python -m src.sources.ats_boards --check-all --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        nargs=2,
        metavar=("BOARD", "SLUG"),
        help=f"check a single slug, e.g. --check greenhouse spotify "
             f"(boards: {', '.join(BOARDS)})",
    )
    parser.add_argument(
        "--check-all",
        action="store_true",
        help="check every board slug in the watchlist",
    )
    parser.add_argument(
        "--config",
        default=str(_ROOT / "config.yaml"),
        help="path to config.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--watchlist",
        default=str(_ROOT / "watchlist.yaml"),
        help="path to watchlist.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of text"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `--check BOARD SLUG` / `--check-all`.

    Exit code 0 when every checked slug answered, 1 on any failure (including
    "you gave me nothing to check", so a typo in CI is never mistaken for a pass).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Keep stdout to the OK/FAIL lines; library logging goes to stderr only
    # when something is genuinely wrong.
    setup_logging("WARNING")

    targets: list[tuple[str, str]] = []
    if args.check:
        targets.append((str(args.check[0]).lower(), str(args.check[1])))
    if args.check_all:
        try:
            config = Config.load(args.config, args.watchlist)
        except ConfigError as exc:
            if args.json:
                print(json_lib.dumps({"ok": False, "error": str(exc), "results": []}))
            else:
                print(f"FAIL — {exc}")
            return 1
        for board in BOARDS:
            targets.extend(
                (board, slug) for slug, _company in _watchlist_entries(
                    config.watchlist.get(board), board
                )
            )

    if not targets:
        if args.check_all:
            problem = f"no board slugs found in {args.watchlist}"
            if args.json:
                print(json_lib.dumps({"ok": False, "error": problem, "results": []}))
            else:
                print(f"FAIL — {problem}")
        else:
            parser.print_help()
        return 1

    results: list[dict[str, Any]] = []
    failures = 0
    for board, slug in targets:
        ok, message, count = _check(board, slug)
        results.append(
            {"board": board, "slug": slug, "ok": ok, "count": count, "message": message}
        )
        if not ok:
            failures += 1
        if not args.json:
            print(f"{'OK' if ok else 'FAIL'} {board}/{slug} — {message}")

    if args.json:
        print(json_lib.dumps(
            {"ok": failures == 0, "checked": len(results), "failures": failures,
             "results": results},
            indent=2,
        ))
    elif len(results) > 1:
        print(f"\n{len(results) - failures}/{len(results)} slugs OK")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
