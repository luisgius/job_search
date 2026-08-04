"""Adzuna search API — breadth, for the companies not on your watchlist.

Greenhouse/Lever cover companies you already know about. Adzuna aggregates
national job boards, so it is how a role at a company you have never heard of
reaches the digest. The trade-offs, all of which shape the code below:

  * it needs a (free) `app_id` / `app_key` pair, and a rejected key looks like
    a normal HTTP error — so 401/403 skips the whole country with a message
    that names the config keys to fix;
  * `description` is a **truncated snippet**, never the full ad, which is
    recorded as `raw["snippet_only"]` so scoring knows what it is reading;
  * a broad query like "engineer" matches tens of thousands of postings, so
    every `(country, query)` pair fetches exactly one page of at most
    `results_per_page` results and logs how much it left behind.

Nothing here raises out of `fetch()`: one dead country or a rejected key must
never take down a run that also has working sources.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..config import Config
from ..models import Job, normalize_text
from ..util import get_logger, html_to_text, http_get_json, parse_datetime

logger = get_logger(__name__)

BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

#: Adzuna caps a single page here; asking for more silently returns 50.
MAX_RESULTS_PER_PAGE = 50
#: Matching `WATCHLIST_DEFAULTS["adzuna"]["results_per_page"]`.
DEFAULT_RESULTS_PER_PAGE = 50

# A rejected key is indistinguishable from a broken query unless you look at
# the status, so these two get their own, louder path.
_AUTH_STATUSES = frozenset({401, 403})

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_REMOTE_RE = re.compile(
    r"\b(remote(?:ly)?|home[- ]?office|homeoffice|work from home|wfh|telearbeit|"
    r"teletrabajo|anywhere)\b",
    re.IGNORECASE,
)

# Adzuna's country index is also the currency of every salary on it.
_CURRENCY_BY_COUNTRY: dict[str, str] = {
    "gb": "£", "us": "$", "ca": "C$", "au": "A$", "nz": "NZ$", "in": "₹",
    "za": "R", "sg": "S$", "br": "R$", "mx": "MX$", "ch": "CHF ", "pl": "zł ",
    "ru": "₽",
}
_DEFAULT_CURRENCY = "€"

# `location.area[0]` is a country *name*; the pipeline speaks ISO alpha-2.
# Keys are `normalize_text`-ed so casing/accents cannot miss.
_ISO_BY_COUNTRY_NAME: dict[str, str] = {
    "austria": "AT", "osterreich": "AT", "belgium": "BE", "belgie": "BE",
    "belgique": "BE", "brazil": "BR", "brasil": "BR", "canada": "CA",
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "czech republic": "CZ",
    "czechia": "CZ", "germany": "DE", "deutschland": "DE", "denmark": "DK",
    "danmark": "DK", "spain": "ES", "espana": "ES", "finland": "FI",
    "suomi": "FI", "france": "FR", "united kingdom": "GB", "uk": "GB",
    "great britain": "GB", "greece": "GR", "hungary": "HU", "ireland": "IE",
    "india": "IN", "italy": "IT", "italia": "IT", "luxembourg": "LU",
    "mexico": "MX", "netherlands": "NL", "nederland": "NL", "norway": "NO",
    "norge": "NO", "new zealand": "NZ", "poland": "PL", "polska": "PL",
    "portugal": "PT", "romania": "RO", "russia": "RU", "sweden": "SE",
    "sverige": "SE", "singapore": "SG", "united states": "US", "usa": "US",
    "australia": "AU", "south africa": "ZA",
}

# People write "uk" far more often than Adzuna's actual index name.
_COUNTRY_ALIASES = {"uk": "gb", "en": "gb", "uae": "ae"}

_EN_DASH = "–"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    """Coerce a watchlist scalar or list into a de-duplicated list of strings."""
    if value is None or isinstance(value, Mapping):
        return []
    items: Sequence[Any] = [value] if isinstance(value, (str, bytes)) else (
        value if isinstance(value, Sequence) else []
    )
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _int(value: Any, default: int) -> int:
    """Read an int out of YAML, rejecting bools (`max_days_old: yes` -> 1)."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _country_code(value: Any) -> str:
    """Normalise a watchlist country into an Adzuna index name ("DE" -> "de")."""
    code = str(value or "").strip().lower()
    return _COUNTRY_ALIASES.get(code, code)


def _status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status out of an exception.

    `util.HttpError` only carries the status in its message, and a raw
    `requests` error carries it on `response`; both shapes show up here.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    match = _HTTP_STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _redact(text: str, *secrets: str) -> str:
    """Strip credentials before a message reaches a log line or the digest."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _mentions_remote(*values: Any) -> bool:
    return any(value and _REMOTE_RE.search(str(value)) for value in values)


def _format_money(value: Any, symbol: str) -> str | None:
    """`65000` -> "€65,000". None for missing / zero / unparseable input."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return f"{symbol}{amount:,.0f}"


def _salary(result: Mapping[str, Any], country: str) -> str | None:
    """Format the pay range, flagging Adzuna's *modelled* figures as estimates.

    `salary_is_predicted == "1"` means Adzuna guessed the number from similar
    ads rather than reading it off the posting — surfacing that verbatim would
    put invented money in the digest.
    """
    symbol = _CURRENCY_BY_COUNTRY.get(country, _DEFAULT_CURRENCY)
    low = _format_money(result.get("salary_min"), symbol)
    high = _format_money(result.get("salary_max"), symbol)
    if not low and not high:
        return None
    if low and high:
        text = low if low == high else f"{low}{_EN_DASH}{high}"
    elif low:
        text = f"from {low}"
    else:
        text = f"up to {high}"
    if str(result.get("salary_is_predicted") or "").strip() == "1":
        text += " (estimated)"
    return text


def _location(result: Mapping[str, Any], country: str) -> tuple[str, list[str], str | None]:
    """Return `(location, area, iso_country)` for one result.

    `location.display_name` is the human string ("Berlin, Berlin") but it very
    often omits the country, which is exactly what `geo` needs to place the
    job. `location.area` is `[country, region, city]`, so `area[0]` is appended
    to the location when it is not already in there, and doubles as the seed
    for `Job.country`.
    """
    node = result.get("location")
    if not isinstance(node, Mapping):
        node = {}
    raw_area = node.get("area")
    area = [str(a).strip() for a in raw_area if str(a).strip()] if isinstance(raw_area, list) else []

    display = str(node.get("display_name") or "").strip()
    if not display and area:
        # area reads country-first; humans (and geo) read it city-first.
        display = ", ".join(reversed(area))

    country_name = area[0] if area else ""
    if country_name and not re.search(
        rf"\b{re.escape(country_name)}\b", display, re.IGNORECASE
    ):
        display = f"{display}, {country_name}" if display else country_name

    iso = _ISO_BY_COUNTRY_NAME.get(normalize_text(country_name))
    if not iso and len(country) == 2:
        # The search index itself is an ISO code, and it is authoritative:
        # a posting on adzuna.de is a German posting.
        iso = country.upper()
    return display, area, iso


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_result(payload: Mapping[str, Any], country: str) -> Job | None:
    """Convert one Adzuna search result into a `Job`.

    Returns None when the result cannot be used: no title (Adzuna does emit
    those) or no `redirect_url` to apply through. A missing company is *not*
    fatal — an anonymous listing with a good title is still worth scoring.
    """
    if not isinstance(payload, Mapping):
        return None

    title = str(payload.get("title") or "").strip()
    url = str(payload.get("redirect_url") or payload.get("url") or "").strip()
    if not title or not url:
        return None

    code = _country_code(country)
    company_node = payload.get("company")
    if isinstance(company_node, Mapping):
        company = str(company_node.get("display_name") or "").strip()
    else:
        company = str(company_node or "").strip()

    location, area, iso = _location(payload, code)

    # Adzuna descriptions are TRUNCATED snippets — they end in an ellipsis and
    # never contain the full ad. Keeping them is still worth it (the keywords
    # that survive drive filtering and scoring), but `raw["snippet_only"]`
    # tells the scorer it is judging a teaser, not a job description.
    description = html_to_text(payload.get("description"))

    category = payload.get("category")
    category_label = (
        str(category.get("label") or "").strip() if isinstance(category, Mapping)
        else str(category or "").strip()
    )

    ident = str(payload.get("id") or "").strip()
    if not company:
        logger.debug("adzuna %s: result %s has no company name", code.upper(), ident or "?")

    return Job(
        source="adzuna",
        company=company,
        title=title,
        url=url,
        location=location,
        description=description,
        posted_at=parse_datetime(payload.get("created")),
        # Only ever assert remote-ness positively: silence is not evidence of
        # an onsite role, and False would wrongly narrow the location filter.
        remote=True if _mentions_remote(title, location, description) else None,
        salary=_salary(payload, code),
        country=iso,
        # Adzuna is an aggregator, not an ATS: the apply link redirects
        # somewhere else entirely, so there is no ATS id to trust here.
        ats=None,
        ats_job_id=None,
        raw={
            "source": "adzuna",
            "id": ident,
            "country": code,
            "snippet_only": True,
            "area": area,
            "category": category_label,
            "contract_time": payload.get("contract_time"),
            "contract_type": payload.get("contract_type"),
            "salary_min": payload.get("salary_min"),
            "salary_max": payload.get("salary_max"),
            "salary_is_predicted": payload.get("salary_is_predicted"),
            "created": payload.get("created"),
        },
    )


# --------------------------------------------------------------------------
# watchlist -> jobs
# --------------------------------------------------------------------------


def _report(message: str, errors: list[str] | None) -> None:
    logger.warning("%s", message)
    if errors is not None:
        errors.append(message)


def _query_params(
    query: str,
    *,
    app_id: str,
    app_key: str,
    per_page: int,
    max_days_old: int,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the query string for one search.

    `content-type` really is a *query parameter* here (hyphen and all), not a
    header — Adzuna returns XML without it.
    """
    params: dict[str, Any] = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": per_page,
        "what": query,
        "content-type": "application/json",
    }
    if max_days_old > 0:
        params["max_days_old"] = max_days_old

    # `distance` is meaningless to Adzuna without a `where`, so both are sent
    # only when the watchlist actually names a place to search around.
    where = str(settings.get("where") or "").strip()
    if where:
        params["where"] = where
        distance = _int(settings.get("distance_km"), 0)
        if distance > 0:
            params["distance"] = distance
    return params


def fetch(
    config: Config, *, session: Any = None, errors: list[str] | None = None
) -> list[Job]:
    """Fetch one page of results for every `(country, query)` in the watchlist.

    Never raises. A rejected key skips the rest of that country (every query
    would fail identically); any other failure skips just that one query.
    Results are de-duplicated by Adzuna id, because the same posting comes
    back for overlapping queries — and, in real payloads, twice within one.
    """
    if not config.source_enabled("adzuna"):
        logger.debug("adzuna disabled in config.sources, skipping")
        return []

    settings = config.watchlist.get("adzuna")
    if not isinstance(settings, Mapping):
        settings = {}

    countries = [c for c in (_country_code(c) for c in _as_str_list(settings.get("countries"))) if c]
    countries = list(dict.fromkeys(countries))
    queries = _as_str_list(settings.get("queries"))
    if not countries or not queries:
        _report(
            "adzuna is enabled but watchlist.adzuna.countries / .queries is empty",
            errors,
        )
        return []

    app_id = str(config.get("keys.adzuna_app_id") or "").strip()
    app_key = str(config.get("keys.adzuna_app_key") or "").strip()
    if not app_id or not app_key:
        _report(
            "adzuna is enabled but keys.adzuna_app_id / keys.adzuna_app_key are "
            "missing — get free keys at developer.adzuna.com",
            errors,
        )
        return []

    per_page = _int(settings.get("results_per_page"), DEFAULT_RESULTS_PER_PAGE)
    if per_page <= 0:
        per_page = DEFAULT_RESULTS_PER_PAGE
    if per_page > MAX_RESULTS_PER_PAGE:
        logger.info(
            "adzuna: results_per_page %d exceeds the API maximum, using %d",
            per_page, MAX_RESULTS_PER_PAGE,
        )
        per_page = MAX_RESULTS_PER_PAGE
    max_days_old = _int(settings.get("max_days_old"), 0)

    jobs: list[Job] = []
    seen: set[str] = set()

    for code in countries:
        url = BASE_URL.format(country=code, page=1)
        for query in queries:
            label = f"adzuna {code.upper()} {query!r}"
            try:
                payload = http_get_json(
                    url,
                    params=_query_params(
                        query,
                        app_id=app_id,
                        app_key=app_key,
                        per_page=per_page,
                        max_days_old=max_days_old,
                        settings=settings,
                    ),
                    session=session,
                )
            except Exception as exc:
                status = _status_code(exc)
                if status in _AUTH_STATUSES:
                    _report(
                        f"adzuna {code.upper()}: HTTP {status} — check "
                        "keys.adzuna_app_id/app_key",
                        errors,
                    )
                    break  # the credentials are wrong; every query would fail
                _report(f"{label}: {_redact(str(exc), app_key, app_id)}", errors)
                continue

            results = payload.get("results") if isinstance(payload, Mapping) else None
            if not isinstance(results, list):
                _report(f"{label}: unexpected payload (no 'results' list)", errors)
                continue

            # Single page, hard-capped: a broad query must not be able to pull
            # thousands of postings into a run.
            page_results = results[:per_page]
            found = 0
            for item in page_results:
                try:
                    job = parse_result(item, code)
                except Exception as exc:  # one bad result must not kill the query
                    logger.debug("%s: skipping malformed result: %s", label, exc)
                    continue
                if job is None:
                    logger.debug("%s: skipping result without title/url", label)
                    continue
                ident = str(job.raw.get("id") or "") or job.dedupe_key
                if ident in seen:
                    continue
                seen.add(ident)
                jobs.append(job)
                found += 1

            total = payload.get("count")
            if isinstance(total, (int, float)) and not isinstance(total, bool) \
                    and total > len(page_results):
                logger.info(
                    "%s: %d new of %d fetched (Adzuna reports %d matches; "
                    "single page of %d)",
                    label, found, len(page_results), int(total), per_page,
                )
            else:
                logger.info("%s: %d new of %d fetched", label, found, len(page_results))

    return jobs
