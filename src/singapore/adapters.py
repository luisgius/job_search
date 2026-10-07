"""Explicit public ATS adapters; permission is checked again at fetch time.

Unknown schemas, incomplete pagination and missing details raise AdapterError.
Its partial_jobs are diagnostic evidence, never a silently successful fetch.
No board identifiers are guessed from company names or careers redirects.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from ..models import Job
from ..sources.ats_boards import _parse_greenhouse_posting, _parse_lever_posting
from ..util import html_to_text, parse_datetime
from .config import enabled
from .http import validate_url

MAX_PAGES = 200
_WORKDAY_DETAIL_TITLES = re.compile(
    r"\b(?:data\s+scien(?:tist|ce)|machine\s+learning|ML|AI|artificial\s+intelligence|"
    r"(?:applied|decision)\s+scientist)\b", re.I)


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, partial_jobs: list[Job] | None = None,
                 page: int | None = None):
        self.partial_jobs = list(partial_jobs or [])
        self.partial_count = len(self.partial_jobs)
        self.page = page
        super().__init__(f"{message} (partial_jobs={self.partial_count}, page={page})")


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context}: expected object")
    return value


def _rows(payload: Any, key: str | None = None) -> list[Mapping[str, Any]]:
    value = _object(payload, "listing").get(key) if key else payload
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"listing: expected {'list' if key is None else key + ' list of objects'}")
    return value


def _identifier(entry: Mapping[str, Any], *names: str) -> str:
    identifiers = entry.get("identifiers") or {}
    if not isinstance(identifiers, Mapping):
        raise ValueError("identifiers must be an object")
    value = next((identifiers.get(n) or entry.get(n) for n in names
                  if identifiers.get(n) or entry.get(n)), None)
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        raise ValueError(f"Missing or invalid verified identifier: {names[0]}")
    return text


def _url(value: Any, base: str = "") -> str:
    return validate_url(urljoin(base, str(value or "")))


def _date(value: Any) -> Any:
    # Never turn 'Posted 3 Days Ago' or incomplete dates into today's date.
    if isinstance(value, str):
        if value.isdigit() and len(value) >= 10:
            return parse_datetime(value)
        if not re.search(r"\b\d{4}\b", value):
            return None
        if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", value):
            # dateutil normally fills missing month/day from the fetch date.
            # Two defaults prove all calendar fields came from the source.
            from dateutil.parser import parse
            try:
                first = parse(value, default=datetime(2001, 2, 3))
                second = parse(value, default=datetime(2002, 5, 7))
                if first.date() != second.date():
                    return None
            except (ValueError, OverflowError):
                return None
    return parse_datetime(value)


def _range(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, Mapping):
        return None
    nested = value.get("value", value)
    if isinstance(nested, (str, int, float)):
        return " ".join(str(p) for p in (value.get("currency"), nested) if p)
    if not isinstance(nested, Mapping):
        return None
    low = nested.get("minValue", nested.get("min", nested.get("minAmount")))
    high = nested.get("maxValue", nested.get("max", nested.get("maxAmount")))
    amount = nested.get("value")
    if low is not None or high is not None:
        amount = f"{low}-{high}" if low is not None and high is not None else str(low if low is not None else high)
    if amount is None:
        return None
    return " ".join(str(p) for p in (value.get("currency") or value.get("currencyCode"), amount,
                     nested.get("unitText") or nested.get("interval") or value.get("interval")) if p) or None


def _locations(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(dict.fromkeys(filter(None, (_locations(v) for v in value))))
    if isinstance(value, Mapping):
        address = value.get("address", value)
        if isinstance(address, Mapping):
            return ", ".join(str(v) for v in (address.get("addressLocality") or address.get("city"),
                              address.get("addressRegion") or address.get("region"),
                              address.get("addressCountry") or address.get("country")) if v)
    return ""


class _Adapter:
    ats = ""

    def fetch(self, entry: dict[str, Any], client: Any) -> list[Job]:
        if not enabled(entry) or entry.get("policy_hold"):
            return []
        jobs: list[Job] = []
        try:
            # A caller bypassing config validation must still supply terms evidence.
            for key in ("verified_url", "robots_url", "terms_url"):
                _url(entry.get(key))
            if not entry.get("evidence"):
                raise ValueError("Missing dated access-policy verification evidence")
            self._fetch(entry, client, jobs)
            return jobs
        except AdapterError:
            raise
        except Exception as exc:
            # Third-party exception text may contain URL query credentials. Only our
            # schema errors are safe to expose; HTTP failures already sanitize text.
            from .http import PublicHTTPError
            detail = str(exc) if isinstance(exc, (ValueError, PublicHTTPError)) else type(exc).__name__
            raise AdapterError(f"{self.ats} fetch incomplete: {detail}", partial_jobs=jobs) from None

    def _finish(self, job: Job | None, entry: Mapping[str, Any], endpoint: str,
                jobs: list[Job], *, apply_url: Any = None, date_field: str = "") -> None:
        if job is None or not job.title.strip() or not job.url:
            raise ValueError("Posting has no usable title or public URL")
        job.url = _url(job.url)
        job.apply_url = _url(apply_url or job.apply_url or job.url, job.url)
        job.source = self.ats
        job.sources = [f"{self.ats}:{endpoint}"]
        job.source_type = str(entry.get("source_type") or "company_site")
        if job.source_type == "company_site":
            job.company = str(entry.get("name") or job.company)
        job.raw.update({"fs": bool(entry.get("fs")),
                        "established": job.source_type == "company_site",
                        "provenance": {"adapter": self.ats, "endpoint": endpoint,
                                       "verified_url": entry.get("verified_url"),
                                       "evidence": entry.get("evidence"),
                                       "date_field": date_field}})
        if job.posted_at is None:
            job.flags.append("posted_date_unknown")
        identity = (job.ats_job_id, job.url)
        if identity in {(j.ats_job_id, j.url) for j in jobs}:
            raise ValueError("Repeated posting encountered; pagination may be incomplete")
        jobs.append(job)


class GreenhouseAdapter(_Adapter):
    ats = "greenhouse"

    def _fetch(self, entry: dict[str, Any], client: Any, jobs: list[Job]) -> None:
        slug = _identifier(entry, "token", "slug", "board")
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{quote(slug)}/jobs"
        payload = _object(client.get_json(endpoint, params={"content": "true"}), "Greenhouse listing")
        rows = _rows(payload, "jobs")
        for row in rows:
            job = _parse_greenhouse_posting(row, slug, str(entry["name"]))
            if job:
                # Preserve parser reuse while removing its last-edit fallback.
                job.posted_at = _date(row.get("first_published"))
            self._finish(job, entry, endpoint, jobs, date_field="first_published")
        total = _object(payload.get("meta") or {}, "Greenhouse meta").get("total")
        if total is not None and total != len(rows):
            raise ValueError("Greenhouse listing count does not match reported total")


class LeverAdapter(_Adapter):
    ats = "lever"

    def _fetch(self, entry: dict[str, Any], client: Any, jobs: list[Job]) -> None:
        slug = _identifier(entry, "slug", "board", "token")
        endpoint = f"https://api.lever.co/v0/postings/{quote(slug)}"
        offset = 0
        limit = 100
        for page in range(MAX_PAGES):
            rows = _rows(client.get_json(endpoint, params={"mode": "json", "skip": offset, "limit": limit}))
            for row in rows:
                job = _parse_lever_posting(row, slug, str(entry["name"]))
                self._finish(job, entry, endpoint, jobs, apply_url=row.get("applyUrl"),
                             date_field="createdAt/publishedAt")
            if len(rows) < limit:
                return
            offset += len(rows)
        raise AdapterError("lever pagination limit reached", partial_jobs=jobs, page=MAX_PAGES)


class WorkdayAdapter(_Adapter):
    """Paginate all summaries, hydrating only Singapore monitor domain titles.

    Every potential DS/ML/AI match still gets its complete public detail. This
    avoids thousands of unrelated detail requests on broad employer boards.
    Final location, seniority and sponsorship filtering belongs to the runner.
    """
    ats = "workday"

    def _fetch(self, entry: dict[str, Any], client: Any, jobs: list[Job]) -> None:
        host = _identifier(entry, "host")
        if not re.fullmatch(r"[A-Za-z0-9-]+\.wd\d+\.myworkdayjobs\.com", host):
            raise ValueError("Workday host must be a verified full myworkdayjobs hostname")
        tenant, site = _identifier(entry, "tenant"), _identifier(entry, "site")
        origin = f"https://{host}"
        api = f"{origin}/wday/cxs/{quote(tenant)}/{quote(site)}"
        endpoint = api + "/jobs"
        offset = 0
        seen_paths: set[str] = set()
        for page in range(MAX_PAGES):
            payload = _object(client.post_json(endpoint, json={"appliedFacets": entry.get("applied_facets", {}),
                              "limit": 20, "offset": offset,
                              "searchText": str(entry.get("search_text") or "")}), "Workday listing")
            rows = _rows(payload, "jobPostings")
            total = payload.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("Workday listing has no valid total")
            if not rows and offset < total:
                raise ValueError("Workday pagination ended before total")
            for row in rows:
                path = str(row.get("externalPath") or "")
                if not path.startswith("/job/") or "?" in path or "#" in path or "/../" in path:
                    raise ValueError("Workday posting has invalid externalPath")
                if path in seen_paths:
                    raise ValueError("Repeated Workday summary; pagination may be incomplete")
                seen_paths.add(path)
                title = str(row.get("title") or "").strip()
                if not title:
                    raise ValueError("Workday summary has no title")
                if not _WORKDAY_DETAIL_TITLES.search(title):
                    continue
                detail_url = _url(api + path)
                payload_detail = _object(client.get_json(detail_url), "Workday detail")
                info = _object(payload_detail.get("jobPostingInfo"), "Workday jobPostingInfo")
                public_url = info.get("externalUrl") or f"{origin}/{quote(site)}{path}"
                location = _locations(info.get("location")) or str(row.get("locationsText") or "")
                additional = info.get("additionalLocations") or []
                if additional:
                    location = "; ".join(dict.fromkeys(filter(None, [location, _locations(additional)])))
                published = info.get("startDate") or info.get("postedDate")
                job = Job(source=self.ats, company=str(entry["name"]),
                          title=str(info.get("title") or row.get("title") or ""),
                          url=_url(public_url), location=location,
                          description=html_to_text(info.get("jobDescription") or ""),
                          posted_at=_date(published), salary=_range(info.get("salary") or info.get("baseSalary")),
                          ats=self.ats, ats_job_id=str(info.get("jobReqId") or info.get("jobPostingId") or "") or None,
                          raw={"external_path": path, "job_req_id": info.get("jobReqId"),
                               "start_date": info.get("startDate"), "posted_date": info.get("postedDate"),
                               "listing_total": total, "listing_offset": offset,
                               "detail_title_scope": "data science / ML / AI"})
                self._finish(job, entry, detail_url, jobs, apply_url=info.get("applyUrl") or public_url,
                             date_field="startDate/postedDate (absolute only)")
            offset += len(rows)
            if offset >= total:
                return
        raise AdapterError("workday pagination limit reached", partial_jobs=jobs, page=MAX_PAGES)


class SmartRecruitersAdapter(_Adapter):
    ats = "smartrecruiters"

    def _fetch(self, entry: dict[str, Any], client: Any, jobs: list[Job]) -> None:
        company_id = _identifier(entry, "slug", "company_id", "company", "board")
        endpoint = f"https://api.smartrecruiters.com/v1/companies/{quote(company_id)}/postings"
        offset = 0
        for page in range(MAX_PAGES):
            payload = _object(client.get_json(endpoint, params={"limit": 100, "offset": offset}), "SmartRecruiters listing")
            rows = _rows(payload, "content")
            total = payload.get("totalFound")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("SmartRecruiters listing has no valid totalFound")
            if not rows and offset < total:
                raise ValueError("SmartRecruiters pagination ended before total")
            for summary in rows:
                posting_id = str(summary.get("id") or "")
                if not posting_id:
                    raise ValueError("SmartRecruiters posting has no id")
                detail_url = endpoint + "/" + quote(posting_id, safe="")
                row = _object(client.get_json(detail_url), "SmartRecruiters detail")
                sections = _object(row.get("jobAd") or {}, "SmartRecruiters jobAd").get("sections") or {}
                parts = [html_to_text(section.get("text") or "") for section in _object(sections, "jobAd sections").values()
                         if isinstance(section, Mapping)]
                job = Job(source=self.ats, company=str(entry["name"]), title=str(row.get("name") or summary.get("name") or ""),
                          url=_url(row.get("postingUrl") or summary.get("postingUrl") or row.get("applyUrl")),
                          location=_locations(row.get("location") or summary.get("location")),
                          description="\n\n".join(filter(None, parts)),
                          posted_at=_date(row.get("releasedDate") or summary.get("releasedDate")),
                          salary=_range(row.get("salary") or row.get("compensation")),
                          ats=self.ats, ats_job_id=posting_id,
                          remote=True if (row.get("location") or {}).get("remote") else None,
                          raw={"id": posting_id, "released_date": row.get("releasedDate") or summary.get("releasedDate")})
                self._finish(job, entry, detail_url, jobs, apply_url=row.get("applyUrl"), date_field="releasedDate")
            offset += len(rows)
            if offset >= total:
                return
        raise AdapterError("smartrecruiters pagination limit reached", partial_jobs=jobs, page=MAX_PAGES)


class AshbyAdapter(_Adapter):
    ats = "ashby"

    def _fetch(self, entry: dict[str, Any], client: Any, jobs: list[Job]) -> None:
        board = _identifier(entry, "board", "slug")
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board)}"
        payload = _object(client.get_json(endpoint, params={"includeCompensation": "true"}), "Ashby listing")
        for row in _rows(payload, "jobs"):
            if row.get("isListed") is False:
                continue
            job_url = _url(row.get("jobUrl") or row.get("applyUrl"))
            # The actual source URL UUID is a provided posting identity, not a
            # tenant/board guess. Prefer an explicit id when the schema has one.
            posting_id = row.get("id")
            if not posting_id:
                tail = urlsplit(job_url).path.rstrip("/").split("/")[-1]
                posting_id = tail if re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", tail) else None
            compensation = row.get("compensation") or {}
            salary = compensation.get("compensationTierSummary") if isinstance(compensation, Mapping) else None
            salary = salary or _range(row.get("salaryRange") or compensation)
            location = _locations(row.get("location"))
            secondary = row.get("secondaryLocations") or []
            secondary_names = [str(v.get("location") or "") for v in secondary if isinstance(v, Mapping)]
            location = "; ".join(dict.fromkeys(filter(None, [location, *secondary_names])))
            job = Job(source=self.ats, company=str(entry["name"]), title=str(row.get("title") or ""),
                      url=job_url, location=location,
                      description=str(row.get("descriptionPlain") or html_to_text(row.get("descriptionHtml") or "")),
                      posted_at=_date(row.get("publishedAt")), salary=salary,
                      remote=row.get("isRemote") if isinstance(row.get("isRemote"), bool) else None,
                      ats=self.ats, ats_job_id=str(posting_id) if posting_id else None,
                      raw={"board": board, "published_at": row.get("publishedAt"), "employment_type": row.get("employmentType")})
            self._finish(job, entry, endpoint, jobs, apply_url=row.get("applyUrl"), date_field="publishedAt")


class _JSONLD(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts: list[str] = []
        self.documents: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.active = True
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.active:
            self.active = False
            self.documents.append(json.loads("".join(self.parts)))


def _job_nodes(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [node for child in value for node in _job_nodes(child)]
    if isinstance(value, Mapping):
        kind = value.get("@type")
        if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
            return [value]
        return [node for key in ("@graph", "itemListElement", "item", "mainEntity")
                if key in value for node in _job_nodes(value[key])]
    return []


def _path(value: Any, path: str) -> Any:
    for segment in path.split(".") if path else []:
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


class CustomAdapter(_Adapter):
    ats = "custom"

    def _fetch(self, entry: dict[str, Any], client: Any, jobs: list[Job]) -> None:
        endpoint = _url(entry.get("endpoint") or entry.get("verified_url"))
        schema = entry.get("json_schema")
        if schema:
            if entry.get("json_schema_verified") is not True:
                raise ValueError("Custom JSON requires explicit verified schema")
            schema = _object(schema, "custom JSON schema")
            fields = _object(schema.get("fields"), "custom JSON fields")
            if not fields.get("title") or not fields.get("url"):
                raise ValueError("Custom JSON schema requires title and url fields")
            payload = client.get_json(endpoint)
            rows = _rows(_path(payload, str(schema.get("items_path") or "")))
            for row in rows:
                values = {key: _path(row, str(path)) for key, path in fields.items()}
                if not values.get("url"):
                    raise ValueError("Custom JSON posting is missing its mapped URL")
                job = Job(source=self.ats, company=str(values.get("company") or entry["name"]),
                          title=str(values.get("title") or ""), url=_url(values.get("url"), endpoint),
                          description=html_to_text(values.get("description") or ""),
                          location=_locations(values.get("location")), posted_at=_date(values.get("posted_at")),
                          salary=_range(values.get("salary")), ats=self.ats,
                          ats_job_id=str(values["id"]) if values.get("id") is not None else None)
                self._finish(job, entry, endpoint, jobs, apply_url=values.get("apply_url"),
                             date_field=str(fields.get("posted_at") or ""))
            return
        parser = _JSONLD()
        parser.feed(client.get_text(endpoint))
        parser.close()
        nodes = _job_nodes(parser.documents)
        if not nodes:
            raise ValueError("Custom HTML contains no JobPosting JSON-LD; no unverified scraping fallback")
        for row in nodes:
            company = row.get("hiringOrganization") or {}
            identifier = row.get("identifier")
            if isinstance(identifier, Mapping):
                identifier = identifier.get("value")
            job = Job(source=self.ats, company=str(company.get("name") or entry["name"]),
                      title=str(row.get("title") or ""), url=_url(row.get("url") or endpoint, endpoint),
                      description=html_to_text(row.get("description") or ""),
                      location=_locations(row.get("jobLocation")), posted_at=_date(row.get("datePosted")),
                      salary=_range(row.get("baseSalary")), ats=self.ats,
                      ats_job_id=str(identifier) if identifier is not None else None,
                      remote=True if row.get("jobLocationType") == "TELECOMMUTE" else None)
            self._finish(job, entry, endpoint, jobs, apply_url=row.get("applicationUrl") or row.get("url"), date_field="datePosted")


def get_adapter(ats: str) -> _Adapter:
    adapters = {"greenhouse": GreenhouseAdapter, "lever": LeverAdapter,
                "workday": WorkdayAdapter, "smartrecruiters": SmartRecruitersAdapter,
                "ashby": AshbyAdapter, "custom": CustomAdapter}
    selected = adapters.get(str(ats).strip().lower())
    if selected is None:
        raise AdapterError(f"Unsupported/unapproved public adapter: {ats}")
    return selected()
