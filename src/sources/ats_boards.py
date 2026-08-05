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

`--discover` is the other half: it goes from a company *name* to the board and
slug to paste into `watchlist.yaml`, which was otherwise a manual trawl through
careers pages looking at where the Apply button points::

    python -m src.sources.ats_boards --discover "Glovo" "Factorial HR"

**It is the one thing in this tool that makes unsolicited requests to third
parties.** Everything else fetches boards the user chose; discovery guesses, and
guessing is six vendors times several slug spellings times N companies of 404s
from a single IP. A tool that looks like a scanner gets its user blocked from
the boards they actually need, so the sweep is bounded twice
(`DISCOVER_MAX_SLUGS_PER_COMPANY`, `DISCOVER_MAX_REQUESTS`), says out loud what
each bound dropped, stops as soon as a board answers with real postings, and
reuses `util.http_get`'s retry policy rather than adding a second one — a 404 is
an answer and is never retried. It reports a *confidence*, never a verdict, and
it never writes to `watchlist.yaml`: a confident-looking wrong slug is worse
than no slug, because it produces an empty board every morning that is
indistinguishable from a quiet market.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..config import Config, ConfigError
from ..models import Job, collapse_initialisms, normalize_company, normalize_text
from ..util import (
    HttpError,
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
#: Values the feed documents for `?language=`. Omitting the parameter entirely
#: is the default *and the safe one*: the feed then answers in the tenant's own
#: language, whereas asking a German SMB for `en` risks an empty or degraded
#: ad for a role that only exists in German — the job-losing direction. It is
#: therefore opt-in, per watchlist entry (`{slug: acme, language: en}`).
PERSONIO_LANGUAGES: frozenset[str] = frozenset(
    {"de", "en", "fr", "es", "nl", "it", "pt"}
)

#: Boards this module knows how to talk to, in watchlist order.
BOARDS: tuple[str, ...] = (
    "greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio",
)

# Project root, so `--check` works from any working directory.
_ROOT = Path(__file__).resolve().parents[2]

#: Remote markers, in the languages this tool actually searches in.
#:
#: English-only was a real gap rather than a theoretical one. The watchlist
#: covers AT/BE/CH/DE/ES/FR/GB/IT/NL/PL/PT, and a German SMB writes
#: "Homeoffice" as one word — which `home[- ]office` did not match — a Spaniard
#: writes "teletrabajo", an Italian "smart working" and a Pole "praca zdalna".
#: Every one of those postings came back `remote=None`, losing the only signal
#: that a place-less ad is remote rather than unresolvable, and an
#: unresolvable location is a job the geo filter drops.
#:
#: Deliberately *not* included: the bare prepositional phrases "a distancia",
#: "à distance", "op afstand". They occur in "formación a distancia" and its
#: kin, and `remote=True` only ever *widens* the location filter — so a false
#: positive here is a US-only role arriving in a European digest, which is the
#: one direction this file is not allowed to be careless in.
_REMOTE_RE = re.compile(
    r"\b("
    r"remote(?:ly)?|remot[oa]s?"                    # en / es / it / pt
    r"|work from home|wfh|home[- ]?office|anywhere"
    r"|telecommut\w*"
    r"|telearbeit|ortsunabh\w+|mobiles? arbeit\w*"  # de
    r"|teletrabajo|teletrabalho"                    # es / pt
    r"|t[ée]l[ée]travail"                           # fr
    r"|smart working|lavoro agile"                  # it
    r"|thuiswerk\w*"                                # nl
    r"|praca zdalna|zdaln[aey]\w*"                  # pl
    r")\b",
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

#: `boards.greenhouse.io/embed/job_board?for=spotify` is the *embed* URL, and
#: it is what a great many careers pages actually link to — so it is what gets
#: pasted into a watchlist. Its slug is in the query string rather than the
#: path, and dropping the query leaves the literal segment `embed`: a slug that
#: 404s, which is indistinguishable from a company that closed its board.
_EMBED_SLUG_RE = re.compile(r"[?&]for=([\w.-]+)", re.IGNORECASE)
#: Path segments that are never a slug — only ever the scaffolding around one.
_EMBED_PATH_SEGMENTS = frozenset({"embed", "job_board", "job_app", "jobs"})


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

    The one exception to "the slug is the first path segment" is Greenhouse's
    embed URL, where the slug is `?for=…` and the path says only `embed`. That
    is rescued rather than left to 404 — but only when the path really is the
    embed scaffolding, so a normal `?lang=en` on a normal URL is untouched.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.IGNORECASE)
    embedded = _EMBED_SLUG_RE.search(text)
    text = text.split("?")[0].split("#")[0]
    parts = [p for p in text.split("/") if p.strip()]
    if not parts:
        return embedded.group(1).strip() if embedded else ""
    if len(parts) > 1 and _HOST_LIKE_RE.match(parts[0]):
        parts = parts[1:]
    if embedded and (not parts or parts[0].lower() in _EMBED_PATH_SEGMENTS):
        return embedded.group(1).strip()
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


#: Keys a Workable payload might hang its *extra* offices off. The widget
#: payload calls it `locations`; the v3 API and the older widget spell the same
#: idea differently, and the cost of reading a key that is not there is nil
#: while the cost of missing the one that is there is a deleted job.
_WORKABLE_LOCATION_LIST_KEYS: tuple[str, ...] = (
    "locations", "secondary_locations", "secondaryLocations",
    "additional_locations", "additionalLocations", "other_locations",
)

#: Every spelling of "this is a remote role" seen on a Workable location or
#: posting. `telecommuting` is the documented one; `workplace_type` is what the
#: hybrid/on-site/remote picker writes, and reading only the first loses the
#: flag on any payload that moved to the second.
_WORKABLE_REMOTE_FLAG_KEYS: tuple[str, ...] = (
    "telecommuting", "telecommute", "is_remote", "remote",
)
_WORKABLE_WORKPLACE_KEYS: tuple[str, ...] = (
    "workplace_type", "workplaceType", "workplace", "remote_type", "remoteType",
)
_WORKABLE_REMOTE_WORKPLACES = frozenset({"remote", "fully_remote", "fully remote"})


def _workable_location_string(node: Any) -> str:
    """One office, assembled from its structured parts ("Valencia, …, Spain").

    Workable gives the location as parts and never as a sentence, so it has to
    be assembled here — `Job.location` is the *entire* input to the geo filter,
    and a bare "Valencia" with the country dropped is how a Spanish role ends
    up unresolvable.

    Every field is read under all of its known spellings, for the reason
    `_first_text` exists: the widget API and the v3 API disagree, and reading
    one spelling silently empties the field on the payloads that use the other.
    """
    if not isinstance(node, Mapping):
        return ""
    country = _first_text(
        node, "country", "country_name", "countryName", "countryCode", "country_code",
    )
    region = _first_text(
        node, "region", "regionCode", "region_code", "state", "stateCode", "state_code",
    )
    parts = [
        _clean_location(node.get("city")),
        _clean_location(region),
        _clean_location(country),
    ]
    return _join_locations(parts).replace("; ", ", ")


def _workable_location_nodes(posting: Mapping[str, Any]) -> list[Any]:
    """The primary office followed by every secondary one.

    This is the `allLocations`/`secondaryLocations` problem again, and it bites
    harder here than anywhere else: a posting whose offices are San Francisco,
    Valencia and Berlin reads, from `location` alone, as an unambiguously
    American role and is thrown away by the US veto. `_MAX_LOCATION_CHARS`
    documents the same failure at length.
    """
    nodes: list[Any] = []
    primary = posting.get("location")
    if isinstance(primary, list):
        nodes.extend(primary)
    elif primary is not None:
        nodes.append(primary)
    for key in _WORKABLE_LOCATION_LIST_KEYS:
        extra = posting.get(key)
        if isinstance(extra, list):
            nodes.extend(extra)
        elif isinstance(extra, Mapping):
            nodes.append(extra)
    return nodes


def _workable_remote(*nodes: Any) -> bool | None:
    """True when any node asserts remote work, else None — never False.

    Positive assertions only: `telecommuting: false` is a statement about one
    office, not about the whole arrangement, and `False` would wrongly narrow
    the location filter.
    """
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        for key in _WORKABLE_REMOTE_FLAG_KEYS:
            if node.get(key) is True:
                return True
        workplace = _first_text(node, *_WORKABLE_WORKPLACE_KEYS).lower()
        if workplace.replace("-", "_") in _WORKABLE_REMOTE_WORKPLACES:
            return True
    return None


def _workable_location(posting: Mapping[str, Any]) -> tuple[str, bool | None]:
    """`(location string, remote flag)` for a whole Workable posting."""
    nodes = _workable_location_nodes(posting)
    location = _join_locations(_workable_location_string(n) for n in nodes)

    remote = _workable_remote(posting, *nodes)
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
    location, remote = _workable_location(posting)

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

    # The *publication* date, never the record's creation date.
    #
    # `created_at` is when the requisition row was created — i.e. when a
    # recruiter started the draft. Drafting weeks ahead of going live is
    # ordinary practice, so a req begun on 6 July and published on 4 August
    # arrives here looking a month old, is rejected as stale, and never reaches
    # the digest. `published_on` is the date the posting actually went live,
    # and it is the only one of the two that answers "is this new?".
    #
    # `created_at` stays as the *last* resort rather than being dropped: it can
    # only ever understate freshness (a record cannot be created after it was
    # published), so it is a floor, and a floor beats no date at all when
    # `freshness.skip_undated` is on. `updated_at` is deliberately absent — it
    # moves on any edit and would overstate freshness, which is the mistake
    # `_parse_ashby_posting` documents.
    posted_at = (
        parse_datetime(posting.get("published_on"))
        or parse_datetime(posting.get("published_at"))
        or parse_datetime(posting.get("published"))
        or parse_datetime(posting.get("created_at"))
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
            # Provenance: the primary office's own flag, verbatim. `Job.remote`
            # is the decision and is drawn from more than this — every office
            # and every spelling — so the two can legitimately differ.
            "telecommuting": (
                location_node.get("telecommuting")
                if isinstance(location_node, Mapping) else None
            ),
            "workplace_type": _first_text(posting, *_WORKABLE_WORKPLACE_KEYS) or None,
            "apply_url": posting.get("application_url"),
            "published_on": posting.get("published_on"),
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

#: How many listing pages one company may cost.
#:
#: The endpoint is offset-paginated (`?offset=0&limit=100`, with `totalFound`
#: in the envelope — https://developers.smartrecruiters.com/docs/pagination),
#: and for a long time this fetcher asked for one page and stopped. A company
#: with 250 open roles contributed 100 of them and lost 150 with no error and
#: nothing above DEBUG: exactly the silent deletion this module exists to
#: avoid. The loop below follows the offsets to the end.
#:
#: This bound is the guard against the other failure — a board whose
#: `totalFound` is wrong, or that keeps answering full pages forever, turning
#: one slug into an unbounded request loop. 20 pages is 2,000 postings, far
#: past any real employer. Stopping here is announced in the log at WARNING,
#: never silently, for the same reason `SMARTRECRUITERS_MAX_DESCRIPTIONS` is.
SMARTRECRUITERS_MAX_PAGES = 20

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
    max_pages: int = SMARTRECRUITERS_MAX_PAGES,
) -> list[Job]:
    """Fetch every posting for a SmartRecruiters company, following the offsets.

    Two request shapes, because the vendor splits them: the listing (paged),
    then one detail call per posting for the description. `details=False` skips
    the second entirely (used by `--check`), and `max_descriptions` bounds it so
    a company with hundreds of open roles cannot turn the cheap stage
    expensive.

    The listing is walked with `?offset=…&limit=100` until the company runs
    out of postings, because one page is not the whole board: a company with
    250 open roles used to contribute exactly 100 and lose the rest in silence.
    `max_pages` bounds that walk, and stopping early is logged rather than
    hidden.

    A detail call that fails costs that posting its description and nothing
    else — the job still reaches the digest with its title, company and
    location, which is far better than losing it.

    Raises on transport/HTTP failure of the *listing*; callers contain that.
    """
    clean = _clean_slug(slug)
    if not clean:
        raise ValueError("empty smartrecruiters slug")

    company = company_from_slug(clean)
    url = SMARTRECRUITERS_POSTINGS_URL.format(slug=clean)
    page_cap = max(1, int(max_pages))

    jobs: list[Job] = []
    total: int | None = None
    offset = 0
    pages = 0
    stopped_early = False

    while True:
        payload = http_get_json(
            url,
            params={"limit": SMARTRECRUITERS_PAGE_LIMIT, "offset": offset},
            session=session,
        )
        pages += 1

        if isinstance(payload, Mapping):
            reported = payload.get("totalFound")
            if isinstance(reported, (int, float)) and not isinstance(reported, bool):
                total = int(reported)

        entries = _as_list(payload, "content", "postings", "results")
        for posting in entries:
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

        offset += len(entries)
        # A short page is the end of the board — the only stop condition that
        # does not trust `totalFound`, which is why it is checked first.
        if len(entries) < SMARTRECRUITERS_PAGE_LIMIT:
            break
        if total is not None and offset >= total:
            break
        if pages >= page_cap:
            stopped_early = True
            break

    if stopped_early:
        logger.warning(
            "smartrecruiters/%s: stopped after %d page(s) at %d posting(s); the "
            "company reports %s — the remaining postings were NOT fetched and "
            "will not appear in the digest (cap SMARTRECRUITERS_MAX_PAGES=%d)",
            clean, pages, len(jobs),
            total if total is not None else "an unknown number",
            SMARTRECRUITERS_MAX_PAGES,
        )
    elif total is not None and total > len(jobs):
        # Not a truncation: postings without a title or an id are dropped by
        # design, so parsed < reported is normal. Said out loud anyway, because
        # "40 of your 250 roles are unparseable" is worth being able to find.
        logger.info(
            "smartrecruiters/%s: %d posting(s) parsed from %d page(s); the "
            "company reports %d (the difference is postings with no title "
            "or no id)",
            clean, len(jobs), pages, total,
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
            "(cap %d, default SMARTRECRUITERS_MAX_DESCRIPTIONS=%d) — the "
            "remaining %d are scored on title, company and location alone",
            slug, budget, len(jobs), budget, SMARTRECRUITERS_MAX_DESCRIPTIONS,
            len(jobs) - budget,
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


def _local_name(tag: Any) -> str:
    """`"{urn:x}position"` -> `"position"`; a plain tag is returned unchanged."""
    return str(tag).rsplit("}", 1)[-1].strip()


def _find_child(element: Any, tag: str) -> Any:
    """First direct child whose *local* name is `tag`, namespaced or not.

    `ElementTree.find("id")` matches the literal tag, so on a feed served with
    a default namespace (`<workzag-jobs xmlns="…">`) every child is really
    called `{…}id` and every lookup returns None. The root gate below tolerates
    a namespace; before this helper existed the children did not, so such a
    feed parsed into **zero positions and raised nothing** — which reads, every
    morning and forever, as a company that simply is not hiring. That is the
    exact silent failure the root gate was added to prevent, arriving one level
    further down.
    """
    if element is None:
        return None
    for child in element:
        if _local_name(child.tag) == tag:
            return child
    return None


def _iter_local(element: Any, tag: str) -> Iterable[Any]:
    """Every descendant (self included) whose local name is `tag`."""
    if element is None:
        return
    for node in element.iter():
        if _local_name(node.tag) == tag:
            yield node


def _element_text(element: Any, tag: str) -> str:
    """Trimmed text of a direct child element, or "" when it is absent."""
    found = _find_child(element, tag)
    if found is None:
        return ""
    return (found.text or "").strip()


def _personio_description(position: Any) -> str:
    """Concatenate the titled `<jobDescription>` sections into one text.

    Each section is a heading plus a CDATA-wrapped HTML body ("Deine Aufgaben",
    "Dein Profil", "Was wir bieten"), and the profile section is where every
    requirement lives — the same reason Lever's `lists` blocks are kept.
    """
    container = _find_child(position, "jobDescriptions")
    if container is None:
        return ""
    blocks: list[tuple[str, Any]] = []
    for section in _iter_local(container, "jobDescription"):
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

    A namespace on the feed is tolerated rather than rejected, and — since
    `_find_child` and `_iter_local` match on local names — tolerated all the
    way down, not only at the root. A gate that admits a namespaced document
    and then hands it to namespace-blind child lookups is worse than no gate:
    it converts a loud "this is not a job feed" into a quiet zero.
    """
    import xml.etree.ElementTree as ElementTree  # stdlib, imported lazily

    root = ElementTree.fromstring(text)
    # Tolerate a namespace prefix ("{urn:x}workzag-jobs") without demanding one.
    tag = _local_name(root.tag).lower()
    if tag != PERSONIO_ROOT_TAG and next(_iter_local(root, "position"), None) is None:
        raise ValueError(
            f"root element is <{tag}>, not <{PERSONIO_ROOT_TAG}> — this is not a "
            "Personio job feed (an error page, a login wall or the wrong host)"
        )

    jobs: list[Job] = []
    for position in _iter_local(root, "position"):
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


def fetch_personio(
    slug: str, *, session: Any = None, language: str | None = None
) -> list[Job]:
    """Fetch every published position from a Personio tenant's XML feed.

    Personio is the one board here that speaks XML rather than JSON, and the
    one whose tenants are split across two hosts: most are `.jobs.personio.de`,
    a minority `.jobs.personio.com`. A 404 on the first is retried on the
    second, so the watchlist entry can stay a bare `acme` — write the full host
    (`acme.jobs.personio.com`) to skip the guess.

    `language` is the documented `?language=` parameter (de/en/fr/es/nl/it/pt).
    It defaults to **not being sent**, which makes the feed answer in the
    tenant's own language. Forcing `en` on every tenant would be the obvious
    move and the wrong one: a German SMB's posting may exist only in German,
    and asking for a language the career site does not publish returns an empty
    or degraded ad — a job made worse or invisible, to buy an English
    description we do not need. `_REMOTE_RE` reads German, Spanish, French,
    Italian, Dutch and Polish for exactly this reason, and the scoring model is
    not monolingual either. Set it per entry when a tenant really does publish
    a language you would rather read.

    Raises on transport/HTTP failure of the last host tried.
    """
    clean = _personio_slug(slug)
    if not clean:
        raise ValueError("empty personio slug")

    hosts = [_personio_host(clean)]
    if "personio." not in clean:
        hosts.append(PERSONIO_FALLBACK_HOST.format(slug=clean))

    wanted = str(language or "").strip().lower()
    if wanted and wanted not in PERSONIO_LANGUAGES:
        # Drop it rather than send it: an unknown value risks an error page or
        # an empty feed, and the tenant's own language is always a real answer.
        logger.warning(
            "personio/%s: ignoring unknown language %r (documented: %s) — "
            "falling back to the tenant's own language",
            clean, language, ", ".join(sorted(PERSONIO_LANGUAGES)),
        )
        wanted = ""
    params = {"language": wanted} if wanted else None

    company = _personio_company(clean)
    last_error: Exception | None = None
    for index, host in enumerate(hosts):
        try:
            response = http_get(
                PERSONIO_XML_URL.format(host=host), params=params, session=session
            )
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
        return fetch_personio(slug, session=session, **kwargs)
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


#: Per-entry watchlist options each board's fetcher accepts, by board. Anything
#: not listed here is ignored rather than raised: a typo in an option must cost
#: the option, never the company's postings.
_ENTRY_OPTIONS: dict[str, tuple[str, ...]] = {"personio": ("language",)}


def _watchlist_entries(
    raw: Any, board: str = ""
) -> list[tuple[str, str | None, dict[str, Any]]]:
    """Normalise a watchlist board section into `(slug, company, options)`.

    Accepts the shapes people actually write::

        greenhouse: [spotify, {slug: acme-corp, company: ACME Corporation}]
        greenhouse: {acme-corp: ACME Corporation}
        greenhouse: spotify
        personio: [{slug: acme, language: en}]

    `board` selects the slug convention (omitting it keeps the `host/SLUG` one)
    and which per-entry options are recognised — see `_ENTRY_OPTIONS`.
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

    allowed = _ENTRY_OPTIONS.get(board, ())
    pairs: list[tuple[str, str | None, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in raw:
        company: str | None = None
        options: dict[str, Any] = {}
        if isinstance(entry, Mapping):
            slug = _slug_for(
                board,
                entry.get("slug") or entry.get("board") or entry.get("id")
                or entry.get("host") or entry.get("name"),
            )
            override = entry.get("company") or entry.get("display_name")
            company = str(override).strip() if override else None
            for key in allowed:
                value = _text(entry.get(key))
                if value:
                    options[key] = value
        else:
            slug = _slug_for(board, entry)
        if not slug:
            logger.warning("watchlist entry has no usable slug, skipping: %r", entry)
            continue
        if slug.lower() in seen:
            continue
        seen.add(slug.lower())
        pairs.append((slug, company or None, options))
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

        for slug, company_override, options in entries:
            try:
                found = _fetch_board(board, slug, session=session, **options)
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


#: The five distinguishable answers a board can give about one slug.
#:
#: Collapsing any two of them is how a wrong slug gets into the watchlist. "The
#: board answered with zero postings" is a company that exists and is not hiring
#: today; "the board says there is no such slug" is a spelling that is simply
#: wrong; "nothing answered at all" is a statement about *our* network and no
#: evidence about the company either way. `--check` only needs ok/not-ok and
#: flattens the last three; discovery needs all five, because it is deciding
#: what to recommend rather than reporting on a slug the user already trusts.
PROBE_FOUND = "found"                #: answered, >= 1 open posting
PROBE_EMPTY = "empty"                #: answered, 0 postings — a real state
PROBE_ABSENT = "absent"              #: answered "no such slug" (404/410)
PROBE_ERROR = "error"                #: answered, but not with a usable board
PROBE_UNREACHABLE = "unreachable"    #: never answered — DNS, refused, timeout

#: `util.http_get` raises this exact shape, and only this shape, when no
#: response was ever received. Everything else it raises quotes a status line,
#: which means the far end answered — including "did not return JSON", where the
#: server replied with an error page. Matching the sentence rather than the
#: exception type is what keeps "the host timed out" apart from "the host served
#: us a login wall", and those two must never read the same.
_TRANSPORT_FAILURE_RE = re.compile(r"failed after \d+ attempts?:")


def _classify_probe_error(exc: Exception) -> str:
    """Which of the five answers an exception represents."""
    text = str(exc)
    status = _HTTP_STATUS_RE.search(text)
    if status:
        # 404/410 is the board telling us this slug does not exist — an answer,
        # and the most useful one discovery gets. 401/403/429/5xx means the
        # board exists and would not talk to us, which is not evidence about
        # the company at all.
        return PROBE_ABSENT if status.group(1) in ("404", "410") else PROBE_ERROR
    if _TRANSPORT_FAILURE_RE.search(text):
        return PROBE_UNREACHABLE
    # A parse failure (Personio's "did not return parseable XML", a body that is
    # not JSON) or a rejected argument. Something answered; it was not a board.
    return PROBE_ERROR


@dataclass
class BoardProbe:
    """One (board, slug) question and the answer it got.

    `count` is deliberately `None` rather than 0 for every non-answer: zero
    postings is a measurement and "we do not know" is not, and a report that
    prints 0 for both invites the reader to average them.
    """

    board: str
    slug: str
    status: str
    count: int | None = None
    message: str = ""
    #: What the payload called the employer, for the two vendors that say.
    company_name: str = ""
    #: True when that published name is not plausibly the company asked for.
    name_mismatch: bool = False

    @property
    def answered(self) -> bool:
        """The board exists under this slug — with or without open postings."""
        return self.status in (PROBE_FOUND, PROBE_EMPTY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "board": self.board, "slug": self.slug, "status": self.status,
            "count": self.count, "message": self.message,
            "company_name": self.company_name or None,
            "name_mismatch": self.name_mismatch,
        }


#: The two vendors whose payload carries the employer's own name. The other four
#: derive it from the slug (`company_from_slug`), so comparing that back against
#: what the user asked for would compare a string to itself and always agree —
#: an assertion that cannot fail, which is worse than none.
_BOARDS_WITH_A_PUBLISHED_NAME = frozenset({"workable", "smartrecruiters"})


def _names_disagree(asked: str, published: str) -> bool:
    """True when a board's own name for the company is not the one we asked for.

    The failure this guards against: `acme` on Lever is a different company from
    `acme` on Greenhouse, and slug collisions across vendors are common enough
    that "a board answered" is not the same as "your company is on that board".
    Where the vendor publishes a name, that is checkable.

    Deliberately lenient — containment either way counts as agreement — because
    "Acme" and "Acme Insurance Group" are one company and a false mismatch would
    downgrade a correct answer. Only a name with nothing in common is reported.
    """
    a = normalize_company(asked)
    b = normalize_company(published)
    if not a or not b:
        return False
    return a not in b and b not in a


def _probe(
    board: str,
    slug: str,
    *,
    session: Any = None,
    kwargs: Mapping[str, Any] | None = None,
    expect: str = "",
) -> BoardProbe:
    """Ask one board about one slug and classify the answer. Never raises."""
    try:
        found = _fetch_board(board, slug, session=session, **dict(kwargs or {}))
    except Exception as exc:
        return BoardProbe(
            board=board, slug=slug, status=_classify_probe_error(exc),
            count=None, message=_describe_error(exc),
        )

    count = len(found)
    published = ""
    if board in _BOARDS_WITH_A_PUBLISHED_NAME and found:
        published = str(getattr(found[0], "company", "") or "").strip()
    return BoardProbe(
        board=board,
        slug=slug,
        status=PROBE_FOUND if count else PROBE_EMPTY,
        count=count,
        message=f"{count} posting{'s' if count != 1 else ''}",
        company_name=published,
        name_mismatch=bool(expect and published and _names_disagree(expect, published)),
    )


def _check(board: str, slug: str, *, session: Any = None) -> tuple[bool, str, int | None]:
    """`check_slug` plus the posting count, which `--json` reports separately."""
    board_name = str(board or "").strip().lower()
    if board_name not in BOARDS:
        return False, f"unknown board {board!r} (expected one of {', '.join(BOARDS)})", None
    clean = _slug_for(board_name, slug)
    if not clean:
        return False, "empty slug", None

    # Descriptions are not needed to prove a board exists, and they are the
    # expensive half of every fetch — skip them where the vendor allows it.
    probe = _probe(
        board_name, clean, session=session,
        kwargs=_CHEAP_CHECK_KWARGS.get(board_name, {}),
    )
    if not probe.answered:
        return False, probe.message, None
    if probe.count == 0:
        return True, "0 postings (board reachable but empty)", 0
    return True, probe.message, probe.count


def check_slug(board: str, slug: str, *, session: Any = None) -> tuple[bool, str]:
    """Verify one board slug. Returns `(ok, human message)`, never raises.

    A reachable board with zero postings counts as OK: that is a real state
    (nobody is hiring today), not a broken slug.
    """
    ok, message, _count = _check(board, slug, session=session)
    return ok, message


# --------------------------------------------------------------------------
# discovery: company name -> board + slug
# --------------------------------------------------------------------------

#: How many spellings of one company name may be tried.
#:
#: Six boards times this many candidates is the worst case for one company that
#: is on none of them — 24 requests, spread one per host per round, which is a
#: sweep rather than a hammering. Four covers every shape the derivation
#: produces for a real name ("factorialhr", "factorial-hr", "factorial", plus
#: one expansion) and stops well short of enumerating spellings nobody uses.
#: What it drops is logged and printed, never silently discarded: a company the
#: sweep gave up on must not look like a company that is not on any board.
DISCOVER_MAX_SLUGS_PER_COMPANY = 4

#: How many probes one `--discover` invocation may make in total.
#:
#: The per-company cap bounds one name; this bounds the run, which is what the
#: boards actually see. 120 is five companies' worth of complete misses, or
#: roughly twenty companies at the ~6 probes a company that *is* found costs.
#: Past that the traffic stops looking like someone filling in a watchlist and
#: starts looking like someone enumerating a vendor's tenants, and the cost of
#: being wrong about that is losing access to the boards the daily run needs.
#:
#: One probe is one HTTP request, with one exception worth naming: a bare
#: Personio slug that 404s on `.jobs.personio.de` is retried once on
#: `.jobs.personio.com`, so a Personio miss is two requests against this budget's
#: one. The bound is therefore approximate in the safe direction only for the
#: five other boards, and understates Personio by at most one request per probe.
DISCOVER_MAX_REQUESTS = 120

#: Discovery only needs to know whether a board answers and roughly how big it
#: is, so every expensive half of a fetch is off. `max_pages: 1` is on top of
#: `--check`'s settings and matters most: without it a single probe against a
#: large SmartRecruiters tenant would quietly spend twenty requests of a budget
#: that thinks it spent one.
_DISCOVER_PROBE_KWARGS: dict[str, dict[str, Any]] = {
    **_CHEAP_CHECK_KWARGS,
    "smartrecruiters": {"details": False, "max_pages": 1},
}

def probe_board(board: str, slug: str, *, session: Any = None, expect: str = "") -> BoardProbe:
    """Ask one board about one slug exactly the way `--discover` does.

    The unit of a discovery sweep, public because it is also the unit the live
    contract tests need: "a slug nobody owns still answers 404, rather than
    answering 200 with an empty board" is the assumption every confidence in
    this module rests on, and it can only be settled against the real internet.

    `expect` is the company name the user asked for, used only to notice that a
    board is publishing somebody else's postings under this slug.
    """
    return _probe(
        board, slug, session=session,
        kwargs=_DISCOVER_PROBE_KWARGS.get(board, {}), expect=expect,
    )


#: Confidence, not verdict. A wrong slug that looks right is the expensive
#: outcome: it goes into `watchlist.yaml`, and from then on produces an empty
#: board every morning that is indistinguishable from a quiet market — the exact
#: failure `src/health.py` exists to catch, arriving with no error attached.
CONFIDENCE_HIGH = "high"      #: one board answered with postings, nothing qualifies it
CONFIDENCE_MEDIUM = "medium"  #: one clear answer, but the sweep was incomplete
CONFIDENCE_LOW = "low"        #: answered, and something is wrong with the answer
CONFIDENCE_NONE = "none"      #: nothing answered anywhere

#: `high` and `medium` are pasteable; `low` and ambiguous results are printed
#: commented out, so that pasting the block can never install a guess.
_PASTEABLE = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM)

#: "&" is punctuation to `normalize_text`, so "H&M" tokenises to ["h", "m"] and
#: the joined spelling "hm" comes out right. The written-out form is a genuinely
#: different slug ("h-and-m") and cheap to add as one extra candidate.
_AMPERSAND_RE = re.compile(r"\s*&\s*")

#: German expands its umlauts rather than dropping them — "Bücher" is spelled
#: "buecher" in a URL at least as often as "bucher", and NFKD only ever produces
#: the latter. Both are tried; neither is guessed at silently.
_GERMAN_EXPANSIONS = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
}

#: A slug this short is never right and costs a whole round of six probes to
#: prove it: "H&M" would otherwise contribute the first-token candidate "h".
_MIN_SLUG_CHARS = 2


def _company_tokens(name: str) -> tuple[list[str], list[str]]:
    """`(folded tokens, the same tokens with their original capitals)`.

    Both come from `models.normalize_company` / `normalize_text` — the
    normaliser the tracker already uses to decide that "Spotify AB" and
    "spotify" are one company. That is the same question discovery asks, so it
    gets the same answer: legal suffixes dropped ("Adyen N.V." -> "adyen"),
    dotted initialisms collapsed before the dots become spaces, accents folded
    and the letters NFKD refuses to decompose transliterated ("Æther" ->
    "aether", "Bücher" -> "bucher").

    The cased list exists only because SmartRecruiters addresses a company by a
    case-sensitive slug spelled the way the company spells itself. It is the
    *same* tokenisation with the fold turned off, truncated to the folded list's
    length so that the suffix `normalize_company` dropped is dropped from both;
    if the two ever fail to line up, the folded list is used for both rather
    than risking a mismatched pairing.
    """
    folded = normalize_company(name).split()
    capitals = normalize_text(collapse_initialisms(name), casefold=False).split()
    capitals = capitals[:len(folded)]
    if [t.lower() for t in capitals] != folded:
        capitals = list(folded)
    return folded, capitals


def _slug_forms(tokens: Sequence[str], *, hyphen_first: bool = True) -> list[str]:
    """The spellings a multi-word company name is plausibly a slug under.

    "Factorial HR" -> "factorialhr", "factorial-hr", "factorial", in that order.

    `hyphen_first=False` is for SmartRecruiters, whose slugs are the company's
    own spelling with the spaces closed up ("FactorialHR", "SopraSteria") and
    where a hyphen is rare — so with a cap in play the bare first token is the
    better third guess there and the hyphenated form is the better one anywhere
    else.
    """
    if not tokens:
        return []
    forms = ["".join(tokens)]
    if len(tokens) > 1:
        hyphenated = "-".join(tokens)
        first = tokens[0]
        forms.extend([hyphenated, first] if hyphen_first else [first, hyphenated])
    return forms


def _name_variants(name: str) -> list[str]:
    """Rewrites of the raw name that produce a genuinely different slug."""
    text = str(name or "")
    variants: list[str] = []
    if "&" in text:
        variants.append(_AMPERSAND_RE.sub(" and ", text))
    if any(ch in text for ch in _GERMAN_EXPANSIONS):
        variants.append("".join(_GERMAN_EXPANSIONS.get(ch, ch) for ch in text))
    return variants


def slug_candidates(
    name: str,
    *,
    cased: bool = False,
    limit: int | None = DISCOVER_MAX_SLUGS_PER_COMPANY,
) -> list[str]:
    """Slugs `name` might be published under, most likely first.

    `cased=True` is SmartRecruiters, and only SmartRecruiters: its slug is
    case-sensitive and spelled as the company spells itself, so the folded form
    alone would miss "FactorialHR" entirely. Both spellings are tried, cased
    first, interleaved form by form so that the cap cuts the *least likely
    shape* rather than every capitalised candidate at once. For a name the user
    typed in lower case the two coincide and dedupe to one, costing nothing.

    `limit=None` returns the whole ordered list, which is how the caller sees
    what a cap dropped in order to say so.
    """
    folded, capitals = _company_tokens(name)
    ordered: list[str] = []
    if cased:
        for cased_form, folded_form in zip(
            _slug_forms(capitals, hyphen_first=False),
            _slug_forms(folded, hyphen_first=False),
        ):
            ordered.extend([cased_form, folded_form])
    else:
        ordered.extend(_slug_forms(folded))

    for variant in _name_variants(name):
        variant_folded, variant_capitals = _company_tokens(variant)
        if cased:
            for cased_form, folded_form in zip(
                _slug_forms(variant_capitals, hyphen_first=False),
                _slug_forms(variant_folded, hyphen_first=False),
            ):
                ordered.extend([cased_form, folded_form])
        else:
            ordered.extend(_slug_forms(variant_folded))

    # Exact-string dedupe, never case-insensitive: "Glovo" and "glovo" are two
    # different SmartRecruiters tenants and folding them together here would
    # silently delete the candidate the cased list exists to produce.
    seen: set[str] = set()
    kept: list[str] = []
    for candidate in ordered:
        if len(candidate) < _MIN_SLUG_CHARS or candidate in seen:
            continue
        seen.add(candidate)
        kept.append(candidate)
    return kept if limit is None else kept[:max(0, int(limit))]


class RequestBudget:
    """The total-request cap, and a record of what it stopped us asking.

    Kept as an object rather than a counter so that "the sweep was cut short" is
    a fact the report can state. A discovery run that quietly stops probing
    looks exactly like a company that is on no board, and that is the one
    reading it must never be given.
    """

    def __init__(self, limit: int = DISCOVER_MAX_REQUESTS) -> None:
        self.limit = max(0, int(limit))
        self.spent = 0
        #: `(company, board, slug)` for every probe the cap prevented.
        self.skipped: list[tuple[str, str, str]] = []

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    @property
    def companies_cut_short(self) -> list[str]:
        out: list[str] = []
        for company, _board, _slug in self.skipped:
            if company not in out:
                out.append(company)
        return out

    def take(self, company: str, board: str, slug: str) -> bool:
        """Claim one request, or record that it could not be made."""
        if self.exhausted:
            self.skipped.append((company, board, slug))
            return False
        self.spent += 1
        return True


@dataclass
class DiscoveryResult:
    """What one company name turned into, evidence included."""

    company: str
    probes: list[BoardProbe] = field(default_factory=list)
    candidates: dict[str, list[str]] = field(default_factory=dict)
    dropped_candidates: dict[str, list[str]] = field(default_factory=dict)
    #: The per-company cap actually in force, so the report can print the bound
    #: it applied rather than the default it might not have been run with.
    max_slugs: int = DISCOVER_MAX_SLUGS_PER_COMPANY
    confidence: str = CONFIDENCE_NONE
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)
    #: True when a hit stopped the sweep before every candidate was tried.
    stopped_early: bool = False
    #: True when the request cap, not the evidence, ended this company's sweep.
    capped: bool = False

    @property
    def matches(self) -> list[BoardProbe]:
        """Boards that answered with at least one open posting."""
        return [p for p in self.probes if p.status == PROBE_FOUND]

    @property
    def empties(self) -> list[BoardProbe]:
        """Boards that exist under this slug but have nothing open today."""
        return [p for p in self.probes if p.status == PROBE_EMPTY]

    @property
    def untried(self) -> list[tuple[str, str]]:
        """`(board, slug)` pairs the sweep stopped before asking about.

        A `(board, slug)` pair rather than a bare slug: a hit in round one
        leaves SmartRecruiters' second, case-folded spelling unasked even though
        every other board was asked about that exact string, and a report that
        says "nothing was skipped" there is wrong.
        """
        asked = {(p.board, p.slug) for p in self.probes}
        return [
            (board, slug)
            for board, slugs in self.candidates.items()
            for slug in slugs
            if (board, slug) not in asked
        ]

    @property
    def suggestion(self) -> BoardProbe | None:
        """The single board worth writing down, or None when there isn't one.

        None whenever two boards answered: picking one of two would be the tool
        inventing the confidence it was asked not to have.
        """
        if self.ambiguous:
            return None
        if len(self.matches) == 1:
            return self.matches[0]
        if not self.matches and len(self.empties) == 1:
            return self.empties[0]
        return None

    @property
    def pasteable(self) -> bool:
        return self.confidence in _PASTEABLE and self.suggestion is not None

    def to_dict(self) -> dict[str, Any]:
        suggestion = self.suggestion
        return {
            "company": self.company,
            "confidence": self.confidence,
            "max_slugs": self.max_slugs,
            "ambiguous": self.ambiguous,
            "pasteable": self.pasteable,
            "stopped_early": self.stopped_early,
            "capped": self.capped,
            "candidates": {b: list(c) for b, c in self.candidates.items()},
            "dropped_candidates": {
                b: list(c) for b, c in self.dropped_candidates.items() if c
            },
            "untried": [{"board": b, "slug": s} for b, s in self.untried],
            "suggestion": (
                {"board": suggestion.board, "slug": suggestion.slug,
                 "count": suggestion.count}
                if suggestion else None
            ),
            "matches": [p.to_dict() for p in self.matches],
            "probes": [p.to_dict() for p in self.probes],
            "notes": list(self.notes),
        }


def _grade(result: DiscoveryResult) -> None:
    """Fill in `confidence`, `ambiguous` and `notes` from the probes.

    The whole point of this function is to make an ambiguous result *look*
    ambiguous. Two boards answering is not a tie to be broken; it is the finding.
    """
    matches = result.matches
    empties = result.empties
    unknown = [p for p in result.probes
               if p.status in (PROBE_ERROR, PROBE_UNREACHABLE)]

    if len(matches) > 1:
        result.ambiguous = True
        result.confidence = CONFIDENCE_LOW
        result.notes.append(
            "two or more boards answered — "
            + " and ".join(f"{p.board}/{p.slug} ({p.count} postings)" for p in matches)
            + "; one of them is probably a different company that happens to "
              "share the slug, so this needs a human eye rather than a guess"
        )
    elif len(matches) == 1:
        hit = matches[0]
        result.confidence = CONFIDENCE_HIGH
        if hit.name_mismatch:
            result.confidence = CONFIDENCE_LOW
            result.notes.append(
                f"{hit.board}/{hit.slug} answered, but it calls itself "
                f"{hit.company_name!r} and you asked for {result.company!r} — "
                "very likely a different company on the same slug"
            )
        if empties:
            result.confidence = min(result.confidence, CONFIDENCE_MEDIUM, key=_rank)
            result.notes.append(
                "also reachable but with nothing open: "
                + ", ".join(f"{p.board}/{p.slug}" for p in empties)
            )
    elif len(empties) == 1:
        result.confidence = CONFIDENCE_LOW
        result.notes.append(
            f"{empties[0].board}/{empties[0].slug} exists but has no open "
            "postings, so nothing confirms it is the right company — a board "
            "with no jobs on it looks the same either way"
        )
    elif len(empties) > 1:
        result.ambiguous = True
        result.confidence = CONFIDENCE_LOW
        result.notes.append(
            "several boards exist under this name but none has an open posting: "
            + ", ".join(f"{p.board}/{p.slug}" for p in empties)
        )
    else:
        result.confidence = CONFIDENCE_NONE

    if unknown and result.confidence != CONFIDENCE_NONE:
        result.confidence = min(result.confidence, CONFIDENCE_MEDIUM, key=_rank)
    if unknown:
        # Short form on purpose: a transport failure's message is a paragraph of
        # urllib3, it is already printed in full against its own probe line
        # above, and repeating six of them here would bury the sentence that
        # matters — that these boards were not ruled out.
        result.notes.append(
            "no answer either way from: "
            + ", ".join(
                f"{p.board}/{p.slug} ({truncate(p.message, 60, suffix=' …')})"
                for p in unknown
            )
            + " — these boards were neither ruled in nor out"
        )
    if result.capped:
        result.confidence = min(result.confidence, CONFIDENCE_LOW, key=_rank)
        result.notes.append(
            "the request cap stopped this company's sweep early, so the boards "
            "below it were never asked"
        )


_CONFIDENCE_ORDER = (CONFIDENCE_NONE, CONFIDENCE_LOW, CONFIDENCE_MEDIUM, CONFIDENCE_HIGH)


def _rank(confidence: str) -> int:
    """Order confidences so `min()` can only ever move one downwards."""
    return _CONFIDENCE_ORDER.index(confidence)


def discover_company(
    name: str,
    *,
    session: Any = None,
    boards: Sequence[str] = BOARDS,
    max_slugs: int = DISCOVER_MAX_SLUGS_PER_COMPANY,
    budget: RequestBudget | None = None,
) -> DiscoveryResult:
    """Work out which board — if any — publishes `name`, and how sure we are.

    The sweep is **candidate-major**: round one asks all six boards about the
    best slug spelling, round two asks about the second-best, and so on. Two
    reasons, both about not looking like a scanner. Consecutive requests go to
    six different hosts rather than six in a row to one, and the round that
    matters — the first — is the one that answers in the ordinary case, so a
    company that is where you would expect costs six requests and stops.

    **A board answering with real postings ends the sweep.** Later candidates
    are not tried; the ones that were skipped are recorded and reported, because
    "we stopped looking" and "there was nothing to find" are different claims.

    The one thing the sweep deliberately does *not* cut short is the round it is
    in. Stopping at the first hit mid-round would make the result depend on the
    order of `BOARDS`, which is arbitrary, and would hide the case this whole
    module reports rather than resolves: two vendors answering for one name,
    where one of them is a different company sharing a slug. Six requests, one
    per host, is the price of being able to see that at all.
    """
    cap = max(1, int(max_slugs))
    result = DiscoveryResult(company=str(name or "").strip(), max_slugs=cap)
    if not result.company:
        result.notes.append("empty company name")
        return result

    budget = budget if budget is not None else RequestBudget()
    board_list = [b for b in boards if b in BOARDS]

    for board in board_list:
        every = slug_candidates(name, cased=(board == "smartrecruiters"), limit=None)
        result.candidates[board] = every[:cap]
        result.dropped_candidates[board] = every[cap:]

    dropped = sorted({s for values in result.dropped_candidates.values() for s in values})
    if dropped:
        logger.info(
            "discover/%s: %d slug candidate(s) dropped by the per-company cap "
            "(DISCOVER_MAX_SLUGS_PER_COMPANY=%d) and never tried: %s — check "
            "one directly with --check if you think it is the right spelling",
            result.company, len(dropped), cap, ", ".join(dropped),
        )

    rounds = max((len(c) for c in result.candidates.values()), default=0)
    for index in range(rounds):
        hit = False
        for board in board_list:
            candidates = result.candidates.get(board, [])
            if index >= len(candidates):
                continue
            slug = candidates[index]
            if not budget.take(result.company, board, slug):
                # Deliberately keep walking rather than breaking out: the
                # remaining rounds cost nothing (no request is made) and the
                # walk is what makes "N probes were not made" a real count
                # rather than however many happened to be left in this round.
                result.capped = True
                continue
            probe = probe_board(board, slug, session=session, expect=result.company)
            result.probes.append(probe)
            hit = hit or probe.status == PROBE_FOUND
        if hit and not result.capped:
            result.stopped_early = index + 1 < rounds
            break

    _grade(result)
    return result


def discover(
    names: Sequence[str],
    *,
    session: Any = None,
    boards: Sequence[str] = BOARDS,
    max_slugs: int = DISCOVER_MAX_SLUGS_PER_COMPANY,
    max_requests: int = DISCOVER_MAX_REQUESTS,
    budget: RequestBudget | None = None,
) -> tuple[list[DiscoveryResult], RequestBudget]:
    """Run `discover_company` over several names against one shared budget."""
    budget = budget if budget is not None else RequestBudget(max_requests)
    results = [
        discover_company(name, session=session, boards=boards,
                         max_slugs=max_slugs, budget=budget)
        for name in names
    ]
    if budget.skipped:
        logger.warning(
            "discover: the request cap stopped the sweep after %d probe(s) "
            "(DISCOVER_MAX_REQUESTS=%d). %d probe(s) were NOT made and these "
            "companies are unfinished: %s — nothing below is evidence that they "
            "are on no board, only that they were not asked",
            budget.spent, budget.limit, len(budget.skipped),
            ", ".join(budget.companies_cut_short),
        )
    return results, budget


# --------------------------------------------------------------------------
# discovery output
# --------------------------------------------------------------------------

_STATUS_LABELS = {
    PROBE_FOUND: "postings",
    PROBE_EMPTY: "reachable, nothing open",
    PROBE_ABSENT: "no such slug",
    PROBE_ERROR: "answered, but not with a board",
    PROBE_UNREACHABLE: "no answer",
}


def _probe_line(probe: BoardProbe) -> str:
    if probe.status == PROBE_FOUND:
        detail = f"{probe.count} posting{'s' if probe.count != 1 else ''}"
        if probe.company_name:
            detail += f" — board calls itself {probe.company_name!r}"
    elif probe.status == PROBE_EMPTY:
        detail = "0 postings (reachable, nothing open)"
    else:
        detail = f"{_STATUS_LABELS.get(probe.status, probe.status)} — {probe.message}"
    return f"  {probe.board:<16} {probe.slug:<22} {detail}"


def _paste_block(result: DiscoveryResult) -> list[str]:
    """The YAML to paste — or the reason there is none, as a comment.

    Anything below `medium`, and anything ambiguous, is emitted **commented
    out**. Pasting this block must never be able to install a guess: a wrong
    slug produces an empty board every morning and reads as a quiet market
    rather than as a mistake, which is the expensive way to be wrong here.
    """
    lines: list[str] = []
    hit = result.suggestion
    if result.pasteable and hit is not None:
        lines.append(f"{hit.board}:")
        lines.append(
            f"  - {hit.slug}"
            f"{' ' * max(1, 22 - len(hit.slug))}"
            f"# {result.company} — {hit.count} posting"
            f"{'s' if hit.count != 1 else ''} ({result.confidence} confidence)"
        )
        for note in result.notes:
            lines.append(f"  # note: {note}")
        return lines

    if result.ambiguous:
        lines.append(
            f"# {result.company} — AMBIGUOUS, nothing pasted. Two or more boards "
            "answered:"
        )
        for probe in (result.matches or result.empties):
            lines.append(f"#   {probe.board}: [{probe.slug}]   "
                         f"# {probe.count} posting"
                         f"{'s' if probe.count != 1 else ''}")
        lines.append("# Open the careers page of the company you mean, look at where "
                     "the Apply")
        lines.append("# button points, then --check that one board.")
        return lines

    if hit is not None:
        lines.append(f"# {result.company} — {result.confidence} confidence, "
                     "so this is commented out on purpose:")
        lines.append(f"#   {hit.board}: [{hit.slug}]")
    elif result.capped:
        # Never the "not on any board" wording: this company was not asked, and
        # printing the two as the same sentence is the exact confusion the cap's
        # reporting exists to prevent.
        lines.append(
            f"# {result.company} — UNFINISHED. The request cap stopped the run "
            f"after {len(result.probes)} of {len(result.probes) + len(result.untried)} "
            "board(s) had been asked,"
        )
        lines.append("# so this is not a result: re-run this company on its own.")
    else:
        tried = sorted({p.slug for p in result.probes})
        lines.append(
            f"# {result.company} — no board answered for "
            f"{', '.join(tried) if tried else 'any candidate'}."
        )
        lines.append("# Either they are not on one of the six public boards this "
                     "tool reads,")
        lines.append("# or the slug is spelled differently: open their careers page, "
                     "copy the")
        lines.append("# Apply URL, and paste it into --check.")
    for note in result.notes:
        lines.append(f"#   note: {note}")
    return lines


def format_discovery(results: Sequence[DiscoveryResult], budget: RequestBudget) -> str:
    """The whole human report: evidence per company, then one paste block."""
    lines: list[str] = []
    for result in results:
        lines.append(f"{result.company} — confidence: {result.confidence.upper()}"
                     + ("  [AMBIGUOUS]" if result.ambiguous else ""))
        for probe in result.probes:
            lines.append(_probe_line(probe))
        untried = result.untried
        if not result.probes:
            lines.append(
                f"  nothing was asked — the request cap stopped the run first, so "
                f"all {len(untried)} board/slug pairs are unasked"
                if result.capped else "  (nothing was probed)"
            )
        elif untried:
            reason = ("the request cap stopped this sweep" if result.capped
                      else "stopped here: a board answered")
            shown = ", ".join(f"{board}/{slug}" for board, slug in untried[:8])
            if len(untried) > 8:
                shown += f", and {len(untried) - 8} more"
            lines.append(f"  {reason}, so these were never asked: {shown}")
        dropped = sorted({s for v in result.dropped_candidates.values() for s in v})
        if dropped:
            lines.append(
                "  the per-company cap dropped these spellings untried "
                f"(DISCOVER_MAX_SLUGS_PER_COMPANY={result.max_slugs}): "
                + ", ".join(dropped)
            )
        for note in result.notes:
            lines.append(f"  note: {note}")
        lines.append("")

    lines.append("# " + "-" * 70)
    lines.append("# paste into watchlist.yaml — check anything commented out by hand")
    lines.append("# " + "-" * 70)
    for result in results:
        lines.extend(_paste_block(result))
        lines.append("")

    lines.append(
        f"{budget.spent} probe(s) for {len(results)} compan"
        f"{'y' if len(results) == 1 else 'ies'} "
        f"(cap DISCOVER_MAX_REQUESTS={budget.limit})."
    )
    if budget.skipped:
        lines.append(
            f"REQUEST CAP HIT after {budget.spent} probe(s): {len(budget.skipped)} "
            f"probe(s) were NOT made, and these companies are unfinished: "
            + ", ".join(budget.companies_cut_short)
            + ". Nothing above is evidence that they are on no board — they were "
              "not asked. Re-run them separately, or raise --max-requests "
              "deliberately."
        )
    return "\n".join(lines)


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
            '  python -m src.sources.ats_boards --discover "Glovo" "Factorial HR"\n'
            "\n"
            "--discover guesses slugs and asks boards that were never told to "
            "expect us,\n"
            "so it is bounded on purpose: at most "
            f"{DISCOVER_MAX_SLUGS_PER_COMPANY} spellings per company and "
            f"{DISCOVER_MAX_REQUESTS} requests\n"
            "in total, it stops as soon as a board answers with real postings, "
            "and it\n"
            "prints what to paste rather than editing watchlist.yaml for you.\n"
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
        "--discover",
        nargs="+",
        metavar="COMPANY",
        help="given company NAMES, find which board publishes them and print "
             "the watchlist.yaml lines to paste (never writes the file)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=DISCOVER_MAX_REQUESTS,
        metavar="N",
        help="total probes one --discover run may make (default: %(default)s). "
             "Raise it deliberately and for a reason: these are unsolicited "
             "requests to third-party boards, and traffic that looks like a "
             "scanner gets you blocked from the boards the daily run needs",
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


def _run_discovery(args: argparse.Namespace) -> int:
    """`--discover NAME...`: print the watchlist lines, never write them.

    Exit code 0 only when every name produced something that can be pasted
    without a decision — so an ambiguous result, a low-confidence one, a company
    found nowhere and a run the cap cut short all exit 1. The alternative is a
    green exit on a report whose whole content is "you need to look at this".
    """
    results, budget = discover(
        args.discover, max_requests=args.max_requests,
    )
    if args.json:
        print(json_lib.dumps(
            {
                "ok": all(r.pasteable for r in results) and not budget.skipped,
                "companies": len(results),
                "requests": budget.spent,
                "request_cap": budget.limit,
                "capped": bool(budget.skipped),
                "skipped_probes": [
                    {"company": c, "board": b, "slug": s} for c, b, s in budget.skipped
                ],
                "results": [r.to_dict() for r in results],
            },
            indent=2,
        ))
    else:
        print(format_discovery(results, budget))
    return 0 if all(r.pasteable for r in results) and not budget.skipped else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `--check BOARD SLUG` / `--check-all` / `--discover NAME`.

    Exit code 0 when every checked slug answered, 1 on any failure (including
    "you gave me nothing to check", so a typo in CI is never mistaken for a pass).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Keep stdout to the OK/FAIL lines; library logging goes to stderr only
    # when something is genuinely wrong.
    setup_logging("WARNING")

    if args.discover:
        return _run_discovery(args)

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
                (board, slug) for slug, _company, _options in _watchlist_entries(
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
