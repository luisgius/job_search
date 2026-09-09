"""Bounded reader for Allegro's observed employer feed and public career pages.

No SAP administrative API, guessed search endpoint, or application requests.
See docs/ALLEGRO_SOURCE.md for evidence and the deliberately separate ID domains.
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
_MIGRATION = re.compile(r"\b(?:we (?:have |are )?mov(?:ed|ing)|new career(?:s)? (?:site|website))\b|^test(?: job)?$", re.I)


def _text(value: Any) -> str:
    return html_to_text(unescape(value)).strip() if isinstance(value, str) else ""


def _date(value: Any):
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
        if p.scheme != "https" or p.netloc != origin.netloc or p.username or p.password:
            return None
        if kind == "listing":
            valid = p.path == urlsplit(LISTING_URL).path
        elif p.netloc == "careers.allegro.eu":
            valid = bool(re.fullmatch(r"/job/[^/]+/\d+/", p.path))
        else:
            valid = bool(re.fullmatch(r"/offer/[^/]+/", p.path))
        if not valid:
            return None
        return urlunsplit((p.scheme, p.netloc, p.path, p.query if kind == "listing" else "", ""))
    except ValueError:
        return None


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
    job.posted_at = _date(data.get("datePosted") or page.value("datePosted")) or job.posted_at
    # Occasional remote days and hybrid wording do not establish a remote role.
    job.remote = True if (data.get("jobLocationType") or page.value("jobLocationType")) == "TELECOMMUTE" else None
    job.raw.update({"description_status": "full" if description else "missing",
                    "snippet_only": not bool(description), "detail_status": "verified",
                    "datePosted": data.get("datePosted") or page.value("datePosted") or None,
                    "employment_type": _text(data.get("employmentType")) or page.value("shift") or job.raw.get("employment_type"),
                    "platform": "SAP SuccessFactors" if sap_page else None})
    return job


def parse_offer(value: Any) -> Job | None:
    if not isinstance(value, Mapping):
        return None
    title, company = _text(value.get("name")), _text(value.get("brand"))
    uid = value.get("uid")
    uid = str(uid) if isinstance(uid, (str, int)) and not isinstance(uid, bool) else ""
    url = _safe_url(value.get("url"), LISTING_URL, "detail")
    if not title or not company or not uid.isdigit() or not url or _MIGRATION.search(title):
        return None
    if _CLOSED.search(title):
        return None
    return Job(source="allegro", company=company, title=title, url=url,
               location=_text(value.get("location")), posted_at=_date(value.get("releaseDate")),
               # Source-owned identity namespace; the feed vendor is unverified.
               ats="allegro_public", ats_job_id="allegro:uid:" + uid,
               raw={"uid": uid, "platform": None, "wordpress_id": value.get("id"), "team": _text(value.get("team")),
                    "employment_type": _text(value.get("contract")), "releaseDate": value.get("releaseDate"),
                    "description_status": "missing", "snippet_only": True, "detail_status": "not_fetched"})


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
    pages = _cap(settings, "max_pages", 2, 5)
    details = _cap(settings, "max_details", 8, 20, 0)
    limit = _cap(settings, "max_requests", 12, 30)
    timeout = _cap(settings, "timeout_seconds", 20, 30)
    client = session if session is not None else requests.Session()
    requests_used = 0
    jobs: dict[str, Job] = {}
    raw_count = 0
    listing_uids: set[str] = set()
    page_fingerprints: set[str] = set()
    first_metadata = None

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
                if kind == "detail" and urlsplit(original).netloc == "careers.allegro.eu" and target.split("/")[-2] != original.split("/")[-2]:
                    raise ValueError("redirect changed original career job ID")
                if kind == "detail" and urlsplit(original).netloc == "jobs.allegro.eu" and urlsplit(target).path != urlsplit(original).path:
                    raise ValueError("redirect changed original offer path")
                url, params = target, None
                continue
            if kind == "detail" and response.status_code in {404, 410}:
                return None
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"unexpected HTTP {response.status_code}")
            if len(response.text) > _MAX_BODY:
                raise ValueError("response body limit exceeded")
            return response.text
        raise ValueError("redirect cap reached")

    try:
        for page in range(1, pages + 1):
            try:
                body = read(LISTING_URL, "listing", {"lang": "en", "page": page})
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
                    uid = entry.get("uid") if isinstance(entry, Mapping) else None
                    if isinstance(uid, (str, int)) and not isinstance(uid, bool) and str(uid).isdigit():
                        listing_uids.add(str(uid))
                malformed = 0
                for entry in offers[:100]:
                    job = parse_offer(entry)
                    if job:
                        jobs.setdefault(job.ats_job_id, job)
                    else:
                        malformed += 1
                if malformed:
                    _report(f"page {page}: skipped {malformed} malformed/non-job records", errors)
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
        for seed in seeds[:20]:
            safe = _safe_url(seed, CAREERS_ORIGIN, "detail")
            if safe and safe not in seen_urls:
                pending.append((safe, None))
                seen_urls.add(safe)
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
        for url, listing in pending[:details]:
            try:
                body = read(url, "detail")
                job = parse_detail(body, url, listing=listing, now=now) if body is not None else None
                if job is None:
                    if listing:
                        jobs.pop(listing.ats_job_id, None)
                    logger.info("allegro: closed/unavailable job removed: %s", url)
                else:
                    jobs[job.ats_job_id] = job
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
    logger.info("allegro: %d jobs, %d full, %d missing; %d requests", len(jobs),
                sum(j.raw.get("description_status") == "full" for j in jobs.values()),
                sum(not j.description for j in jobs.values()), requests_used)
    return list(jobs.values())
