"""Bounded reader for Allegro's observed employer feed and public career pages.

No SAP administrative API, guessed search endpoint, or application requests.
See docs/ALLEGRO_SOURCE.md for evidence and the deliberately separate ID domains.

Feed shape observed 2026-10-05 (frontend build 2026-09-10): the original
requisition number moved from `uid` to a string `id`, `url` became the
cross-origin `careers.allegro.eu/job-invite/<id>` link, and `releaseDate`
became epoch seconds. Both shapes are read; nothing else is inferred.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from ..models import Job
from ..util import html_to_text, parse_datetime

logger = logging.getLogger(__name__)
LISTING_URL = "https://jobs.allegro.eu//wp-json/api/v1/offer"
CAREERS_ORIGIN = "https://careers.allegro.eu"
_MAX_BODY = 2_000_000
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_CLOSED = re.compile(r"(?:this (?:job|position|vacancy|requisition) (?:is |has been )?(?:no longer available|closed|filled|expired)|job (?:is )?no longer available|position has been filled)", re.I)
_INVITE = re.compile(r"/job-invite/(\d+)/?")
_CAREER = re.compile(r"/job/[^/]+/(\d+)/")
_MIGRATION = re.compile(r"\b(?:we (?:have |are )?mov(?:ed|ing)|new career(?:s)? (?:site|website))\b|^test(?: job)?$", re.I)


def _text(value: Any) -> str:
    return html_to_text(unescape(value)).strip() if isinstance(value, str) else ""


def _date(value: Any):
    # Epoch seconds, as the feed has published `releaseDate` since 2026-09-10.
    # Milliseconds, numeric strings and implausible years stay unknown.
    if type(value) is int:
        return datetime.fromtimestamp(value, timezone.utc) if 946684800 <= value < 4102444800 else None
    # Partial dates parsed by dateutil borrow today's year: never allow that.
    if not isinstance(value, str) or not re.search(
        r"\b(?:19|20)\d{2}-\d{2}-\d{2}(?=T|\b)|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b.*\b(?:19|20)\d{2}\b", value, re.I
    ):
        return None
    parsed = parse_datetime(value)
    return parsed.astimezone(timezone.utc) if parsed else None


def _safe_url(value: Any, base: str, kind: str) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        return None
    try:
        p = urlsplit(urljoin(base, value))
        origin = urlsplit(base)
        if p.scheme != "https" or p.username or p.password:
            return None
        if kind == "offer" and p.netloc == "careers.allegro.eu":
            # The only cross-origin link the feed may supply: the employer's
            # own numeric invite route, with at most its published locale.
            if not _INVITE.fullmatch(p.path):
                return None
            query = p.query if re.fullmatch(r"locale=[a-z]{2}_[A-Z]{2}", p.query) else ""
            return urlunsplit((p.scheme, p.netloc, p.path, query, ""))
        if p.netloc != origin.netloc:
            return None
        if kind == "listing":
            valid = p.path == urlsplit(LISTING_URL).path
        elif p.netloc == "careers.allegro.eu":
            valid = bool(_CAREER.fullmatch(p.path))
        else:
            valid = bool(re.fullmatch(r"/offer/[^/]+/", p.path))
        if not valid:
            return None
        return urlunsplit((p.scheme, p.netloc, p.path, p.query if kind == "listing" else "", ""))
    except ValueError:
        return None


def _settle_dates(job: Job) -> None:
    """Pick the freshness date from the two employer date fields, keeping both.

    The feed's `releaseDate` and the career page's `datePosted` disagreed on
    every posting where both were read (page later by weeks), and one page's
    `datePosted` changed between two captures. What either field means is not
    established, so the earliest complete one is used as a conservative
    freshness date, not as a proven first publication, and the other is kept
    as a conflicting source date, never as an update time.
    """
    stated = {"feed releaseDate": _date(job.raw.get("releaseDate")),
              "page datePosted": _date(job.raw.get("datePosted"))}
    known = sorted((moment, name) for name, moment in stated.items() if moment)
    job.posted_at = known[0][0] if known else None
    job.raw["posted_at_source"] = known[0][1] if known else None
    job.raw["source_dates"] = {name: moment.isoformat() for moment, name in known}
    job.raw["date_conflict"] = len(known) == 2 and known[0][0] != known[1][0]


class _Page(HTMLParser):
    """Read observed SAP microdata, plus standard JobPosting JSON-LD."""
    def __init__(self, body: str):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, list[str]]] = []
        self.fields: dict[str, list[str]] = {}
        self.links: list[str] = []
        self.visible: list[str] = []
        self.structured = False
        self.refresh = False
        self.feed(body)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        keys = []
        if tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            self.refresh = True
        if a.get("itemtype", "").rstrip("/").endswith("/JobPosting"):
            self.structured = True
        prop = a.get("itemprop", "")
        if prop in {"title", "description", "datePosted", "validThrough", "streetAddress", "jobLocationType"}:
            key = prop
            if tag == "meta":
                self.fields.setdefault(key, []).append(a.get("content", ""))
            else:
                keys.append(key)
        prop = a.get("data-careersite-propertyid", "")
        if prop in {"dept", "shift", "location"}:
            keys.append(prop)
        if tag == "script" and a.get("type") == "application/ld+json":
            keys.append("jsonld")
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag not in _VOID:
            if len(self.stack) > 200:
                raise ValueError("HTML nesting limit exceeded")
            self.stack.append((tag, keys))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        hidden = any(t in {"script", "style"} for t, _ in self.stack)
        if not hidden:
            self.visible.append(data)
        for _, keys in self.stack:
            for key in keys:
                if not hidden or key == "jsonld":
                    self.fields.setdefault(key, []).append(data)

    def value(self, key):
        return " ".join(" ".join(self.fields.get(key, [])).split())


def _json_job(page: _Page) -> Mapping:
    try:
        value = json.loads("".join(page.fields.get("jsonld", [])))
    except (ValueError, TypeError):
        return {}
    nodes = value if isinstance(value, list) else [value]
    if isinstance(value, Mapping) and isinstance(value.get("@graph"), list):
        nodes = value["@graph"]
    for node in nodes:
        if isinstance(node, Mapping) and node.get("@type") == "JobPosting":
            return node
    return {}


def parse_detail(body: str, url: str, *, listing: Job | None = None,
                 now: datetime | None = None) -> Job | None:
    """Return full/partial job, None for explicit closure; reject non-job pages."""
    page = _Page(body)
    visible = " ".join(" ".join(page.visible).split())
    data = _json_job(page)
    if _CLOSED.search(visible):
        return None
    if page.refresh:
        raise ValueError("migration/redirect page, not a job description")
    expires = _date(data.get("validThrough") or page.value("validThrough"))
    if expires:
        moment = now or datetime.now(timezone.utc)
        moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        if expires < moment:
            return None
    if not (page.structured or data):
        raise ValueError("unrecognized non-job detail page")
    title = _text(data.get("title")) or page.value("title")
    description = _text(data.get("description")) or page.value("description")
    if not title or _MIGRATION.fullmatch(title):
        raise ValueError("missing or non-job title")
    safe = _safe_url(url, url if url.startswith(CAREERS_ORIGIN + "/") else LISTING_URL, "detail")
    if not safe:
        raise ValueError("unsafe detail URL")
    sap_page = urlsplit(safe).netloc == "careers.allegro.eu"
    if listing is None and not sap_page:
        raise ValueError("employer detail requires original listing identity")
    org = data.get("hiringOrganization")
    company = _text(org.get("name")) if isinstance(org, Mapping) else ""
    location = page.value("location") or page.value("streetAddress")
    loc = data.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    if isinstance(loc, Mapping) and isinstance(loc.get("address"), Mapping):
        address = loc["address"]
        location = ", ".join(_text(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if _text(address.get(k))) or location
    job = listing or Job(source="allegro", company="Allegro", title=title, url=safe,
                         ats="successfactors", ats_job_id="allegro:career:" + urlsplit(safe).path.rstrip("/").split("/")[-1])
    job.title = title
    job.company = company or page.value("dept") or job.company
    job.location = location or job.location
    job.description = description
    # Occasional remote days and hybrid wording do not establish a remote role.
    job.remote = True if (data.get("jobLocationType") or page.value("jobLocationType")) == "TELECOMMUTE" else None
    job.raw.update({"description_status": "full" if description else "missing",
                    "snippet_only": not bool(description), "detail_status": "verified",
                    "datePosted": data.get("datePosted") or page.value("datePosted") or None,
                    "employment_type": _text(data.get("employmentType")) or page.value("shift") or job.raw.get("employment_type"),
                    "platform": "SAP SuccessFactors" if sap_page else None})
    _settle_dates(job)
    return job


def _uid(value: Mapping) -> tuple[str, str]:
    """Original requisition number and the field that carried it."""
    field = "uid" if value.get("uid") is not None else "id"
    uid = value.get(field)
    # A bare integer `id` was the WordPress post number, a different domain.
    if field == "id" and not isinstance(uid, str):
        return "", field
    uid = str(uid) if isinstance(uid, (str, int)) and not isinstance(uid, bool) else ""
    return (uid if uid.isdigit() else ""), field


def _offer(value: Any) -> tuple[Job | None, str]:
    """A listing record as a job, or the reason it is not one."""
    if not isinstance(value, Mapping):
        return None, "not an object"
    title, company = _text(value.get("name")), _text(value.get("brand"))
    uid, field = _uid(value)
    url = _safe_url(value.get("url"), LISTING_URL, "offer")
    if not title or not company:
        return None, "missing title or brand"
    if not uid:
        return None, "missing original id"
    if not url:
        return None, "unsafe or unrecognized url"
    invite = _INVITE.fullmatch(urlsplit(url).path)
    if invite and invite.group(1) != uid:
        return None, "invite url disagrees with original id"
    # A string `id` is only known to be the requisition number where the
    # invite link repeats it.
    if field == "id" and not invite:
        return None, "missing original id"
    if _MIGRATION.search(title) or _CLOSED.search(title):
        return None, "non-job or closed notice"
    job = Job(source="allegro", company=company, title=title, url=url,
              location=_text(value.get("location")),
              # Source-owned identity namespace; the feed vendor is unverified.
              ats="allegro_public", ats_job_id="allegro:uid:" + uid,
              raw={"uid": uid, "uid_field": field, "platform": None,
                   "wordpress_id": value.get("id") if field == "uid" else None, "team": _text(value.get("team")),
                   "employment_type": _text(value.get("contract")), "releaseDate": value.get("releaseDate"),
                   "listing_url": url,
                   "description_status": "missing", "snippet_only": True, "detail_status": "not_fetched"})
    _settle_dates(job)
    return job, ""


def parse_offer(value: Any) -> Job | None:
    return _offer(value)[0]


def _report(message, errors):
    message = "allegro: " + message
    logger.warning(message)
    if errors is not None:
        errors.append(message)


def _cap(settings, key, default, maximum, minimum=1):
    try:
        value = int(settings.get(key, default))
    except (ValueError, TypeError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def fetch(config: Any, *, session: Any = None, errors: list[str] | None = None,
          now: datetime | None = None) -> list[Job]:
    """Fetch bounded pages and details, retaining attributed partial results."""
    if not config.source_enabled("allegro"):
        return []
    settings = config.watchlist.get("allegro", {})
    settings = settings if isinstance(settings, Mapping) else {}
    # Ceilings sized for the 16-page feed observed on 2026-10-05: the whole
    # listing plus the detail slice, every redirect hop charged to `limit`.
    pages = _cap(settings, "max_pages", 2, 20)
    details = _cap(settings, "max_details", 8, 20, 0)
    limit = _cap(settings, "max_requests", 12, 40)
    timeout = _cap(settings, "timeout_seconds", 20, 30)
    # Optional `team[]` values, exactly as the public listing page offers and
    # sends them. Unset keeps the unfiltered walk.
    teams = settings.get("teams") or []
    if not isinstance(teams, list) or not all(isinstance(t, str) and 0 < len(t.strip()) <= 80 for t in teams):
        _report("teams must be a list of team names; filter ignored", errors)
        teams = []
    teams = [t.strip() for t in teams[:10]]
    client = session if session is not None else requests.Session()
    requests_used = 0
    jobs: dict[str, Job] = {}
    raw_count = 0
    listing_uids: set[str] = set()
    page_fingerprints: set[str] = set()
    first_metadata = None
    pages_read: dict[str, str] = {}

    def read(url, kind, params=None):
        nonlocal requests_used
        original = url
        for _ in range(4):
            if requests_used >= limit:
                raise ValueError("request cap reached; coverage partial")
            requests_used += 1
            response = client.get(url, params=params, timeout=timeout, allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                target = _safe_url(response.headers.get("Location"), original, kind)
                if not target or target == url:
                    raise ValueError("unsafe or looping redirect")
                # An invite link resolves once to its career page (already
                # route-checked above); after that the page number is fixed.
                if kind == "detail" and _CAREER.fullmatch(urlsplit(url).path) and target.split("/")[-2] != url.split("/")[-2]:
                    raise ValueError("redirect changed original career job ID")
                if kind == "detail" and urlsplit(original).netloc == "jobs.allegro.eu" and urlsplit(target).path != urlsplit(original).path:
                    raise ValueError("redirect changed original offer path")
                url, params = target, None
                if kind == "detail" and url in pages_read:
                    return pages_read[url], url
                continue
            if kind == "detail" and response.status_code in {404, 410}:
                return None
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"unexpected HTTP {response.status_code}")
            if len(response.text) > _MAX_BODY:
                raise ValueError("response body limit exceeded")
            if kind == "detail":
                pages_read[url] = response.text
            return response.text, url
        raise ValueError("redirect cap reached")

    try:
        for page in range(1, pages + 1):
            try:
                query: dict[str, Any] = {"lang": "en", "page": page}
                if teams:
                    query["team[]"] = teams
                body, _ = read(LISTING_URL, "listing", query)
                data = json.loads(body)
                if not isinstance(data, Mapping) or not isinstance(data.get("offers"), list):
                    raise ValueError("not the offers envelope (migration/non-listing page)")
                total, count = data.get("max_pages"), data.get("results_count")
                if type(total) is not int or type(count) is not int or total < 0 or count < 0:
                    raise ValueError("malformed pagination metadata")
                offers = data["offers"]
                if (not offers and (total or count)) or (offers and (total < page or count < len(offers))):
                    raise ValueError("inconsistent empty feed/pagination metadata")
                metadata = (total, count)
                if first_metadata is None:
                    first_metadata = metadata
                elif metadata != first_metadata:
                    _report(f"page {page}: pagination metadata changed; coverage partial", errors)
                fingerprint = json.dumps(offers, sort_keys=True)
                if offers and fingerprint in page_fingerprints:
                    _report(f"page {page}: repeated listing page; coverage partial", errors)
                page_fingerprints.add(fingerprint)
                raw_count += len(offers)
                for entry in offers:
                    uid = _uid(entry)[0] if isinstance(entry, Mapping) else ""
                    if uid:
                        listing_uids.add(uid)
                reasons: dict[str, int] = {}
                for entry in offers[:100]:
                    job, reason = _offer(entry)
                    if job:
                        jobs.setdefault(job.ats_job_id, job)
                    else:
                        reasons[reason] = reasons.get(reason, 0) + 1
                malformed = sum(reasons.values())
                if malformed:
                    why = ", ".join(f"{reason}: {n}" for reason, n in sorted(reasons.items()))
                    # A page where nothing parses is a changed feed, not ten bad rows.
                    drift = "; every record rejected, feed shape drift suspected" if malformed == len(offers[:100]) and malformed > 1 else ""
                    _report(f"page {page}: skipped {malformed} malformed/non-job records ({why}){drift}", errors)
                if teams and any(isinstance(e, Mapping) and _text(e.get("team")) not in teams for e in offers):
                    _report(f"page {page}: team filter not honoured by the feed; unfiltered records retained", errors)
                if len(offers) > 100:
                    _report(f"page {page}: record cap reached; coverage partial", errors)
                if page >= total:
                    if raw_count != count or len(listing_uids) != count:
                        _report(f"listing count mismatch: {raw_count} raw / {len(listing_uids)} unique UID records versus {count} advertised; coverage partial", errors)
                    break
                if page == pages:
                    _report(f"page cap reached ({pages}/{total}); coverage partial", errors)
            except Exception as exc:
                _report(f"listing page {page}: {exc}", errors)
                break

        seeds = settings.get("detail_urls", [])
        if not isinstance(seeds, list):
            _report("detail_urls must be a list", errors)
            seeds = []
        pending: list[tuple[str, Job | None]] = []
        seen_urls = set()
        seed_pages: set[str] = set()
        for seed in seeds[:20]:
            safe = _safe_url(seed, CAREERS_ORIGIN, "detail")
            if safe and safe not in seen_urls:
                pending.append((safe, None))
                seen_urls.add(safe)
                seed_pages.add(_CAREER.fullmatch(urlsplit(safe).path).group(1))
            elif not safe:
                _report("unsafe career detail seed skipped", errors)
        ranked = sorted(jobs.values(), key=lambda j: (
            0 if re.search(r"data scientist|machine learning|\bML engineer", j.title, re.I) else
            1 if re.search(r"data analyst|data engineer|analytics", j.title, re.I) else 2
        ))
        for job in ranked:
            if job.url not in seen_urls:
                pending.append((job.url, job))
                seen_urls.add(job.url)
        if len(pending) > details or len(seeds) > 20:
            _report("detail cap reached; description coverage partial", errors)
        page_owner: dict[str, str] = {}
        for url, listing in pending[:details]:
            try:
                found = read(url, "detail")
                body, final = found if found is not None else (None, url)
                career = _CAREER.fullmatch(urlsplit(final).path)
                page = career.group(1) if career else final
                owner = jobs.get(page_owner.get(page, ""))
                # A configured seed owns its page's identity whenever the
                # employer's redirect proves a listing record is that page,
                # whether or not the seed's own request succeeded this run.
                if listing and body is not None and owner is None and page in seed_pages:
                    owner = parse_detail(body, final, now=now)
                    if owner is not None:
                        owner.raw["career_page_id"] = page
                        jobs[owner.ats_job_id] = owner
                        page_owner[page] = owner.ats_job_id
                        body = None  # already consumed; the listing only adds its facts below
                if listing and owner is not None and owner is not listing:
                    # One posting: the identity emitted for the page is kept
                    # and the listing record contributes what only it knows.
                    jobs.pop(listing.ats_job_id, None)
                    for key in ("uid", "uid_field", "team", "releaseDate", "listing_url"):
                        owner.raw.setdefault(key, listing.raw[key])
                    _settle_dates(owner)
                    logger.info("allegro: listing %s is career page %s; one job kept", listing.ats_job_id, final)
                    continue
                job = parse_detail(body, final, listing=listing, now=now) if body is not None else None
                if job is None:
                    if listing:
                        jobs.pop(listing.ats_job_id, None)
                    logger.info("allegro: closed/unavailable job removed: %s", url)
                else:
                    if final != url:
                        job.url = final
                    if career:
                        job.raw["career_page_id"] = page
                    jobs[job.ats_job_id] = job
                    page_owner[page] = job.ats_job_id
                    if not job.description:
                        _report(f"detail {url}: missing description", errors)
            except Exception as exc:
                if listing:
                    listing.raw["detail_status"] = "failed"
                _report(f"detail {url}: {exc}", errors)
                if requests_used >= limit:
                    break
    finally:
        if session is None:
            client.close()
    logger.info("allegro: %d jobs, %d full, %d missing, %d dated; %d requests%s", len(jobs),
                sum(j.raw.get("description_status") == "full" for j in jobs.values()),
                sum(not j.description for j in jobs.values()),
                sum(j.posted_at is not None for j in jobs.values()), requests_used,
                f"; team filter {teams}" if teams else "")
    return list(jobs.values())
