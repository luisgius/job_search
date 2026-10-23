"""schema.org JobPosting out of a page's JSON-LD.

Career sites, boards and ATS detail pages publish the same structured block
for search engines: `<script type="application/ld+json">` holding a
JobPosting alone, in a list, or inside an `@graph` beside the Organization,
WebPage and BreadcrumbList nodes. Pages often carry several such scripts, and
one of them is sometimes malformed, so each script is parsed on its own.

This module only finds and reads that block. Whether the page is a live job,
which identity it has and which date policy applies stay with each source.
The page is untrusted data: odd values become empty fields, never exceptions.

The search over scripts, lists and `@graph` is adapted from ai-job-search
(https://github.com/ai-job-search-dev/ai-job-search),
.agents/skills/jobbank-search/cli/src/helpers.ts, MIT License,
© 2026 Mads Lorentzen.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from .util import html_to_text

_JOB_POSTING_TYPES = {
    "JobPosting", "schema:JobPosting",
    "http://schema.org/JobPosting", "https://schema.org/JobPosting",
}
#: Lists and `@graph` levels searched; real pages use two or three.
_MAX_DEPTH = 8
#: A full calendar date. "Aug 28" or "2026-08" would borrow a year or a day.
_FULL_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:$|[T ])")


class _JsonLdScripts(HTMLParser):
    """The text of every JSON-LD script, one string per script.

    A parser rather than a regex, so commented-out scripts stay out and the
    type attribute is read however it is quoted or cased.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        kind = next((value for name, value in attrs if name == "type"), None) or ""
        self._inside = kind.split(";")[0].strip().lower() == "application/ld+json"
        if self._inside:
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.scripts[-1] += data


def _is_job_posting(node: Mapping) -> bool:
    kinds = node.get("@type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return any(isinstance(kind, str) and kind.strip() in _JOB_POSTING_TYPES for kind in kinds)


def _find(value: Any, depth: int = 0) -> dict | None:
    """First JobPosting in a JSON-LD value, through lists and `@graph`."""
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, list):
        for item in value:
            found = _find(item, depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(value, dict):
        return None
    if _is_job_posting(value):
        return value
    return _find(value.get("@graph"), depth + 1)


def extract_job_posting(html: str) -> dict | None:
    """The first JobPosting node in the page's JSON-LD, as published, or None.

    Each script is parsed separately and a malformed one is skipped, so an
    Organization block or a broken block ahead of the posting cannot hide it.
    `@type` may be a string or a list.
    """
    if not isinstance(html, str) or not html:
        return None
    parser = _JsonLdScripts()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # keep whatever scripts were read before the breakage
        pass
    for text in parser.scripts:
        try:
            # strict=False: literal newlines inside description strings are
            # the most common defect in published JSON-LD.
            value = json.loads(text, strict=False)
        except (ValueError, RecursionError):
            continue
        found = _find(value)
        if found is not None:
            return found
    return None


def _text(value: Any) -> str:
    # Descriptions arrive as HTML, sometimes entity-encoded HTML.
    return html_to_text(unescape(value)).strip() if isinstance(value, str) else ""


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return _unique([_text(item) for item in values])


def _names(value: Any) -> list[str]:
    """Place names from Country/AdministrativeArea nodes or bare strings."""
    values = value if isinstance(value, list) else [value]
    return _unique([
        _text(item.get("name")) if isinstance(item, Mapping) else _text(item)
        for item in values
    ])


def _date(value: Any) -> datetime | None:
    """A full ISO 8601 date or datetime as UTC; anything else stays unknown."""
    text = value.strip() if isinstance(value, str) else ""
    if not _FULL_DATE_RE.match(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _url(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    return text if parts.scheme in ("http", "https") and parts.netloc else ""


def job_posting_fields(node: Any) -> dict[str, Any]:
    """The commonly used JobPosting fields as plain Python values.

    Text is rendered to plain text ("" when absent), dates are UTC datetimes
    (None when absent or partial), lists are lists of strings. `remote` is
    True only for `jobLocationType` TELECOMMUTE and otherwise None: silence
    is not evidence of an on-site role.
    """
    node = node if isinstance(node, Mapping) else {}
    org = node.get("hiringOrganization")
    location_types = [kind.upper() for kind in _strings(node.get("jobLocationType"))]
    return {
        "title": _text(node.get("title")),
        "description": _text(node.get("description")),
        "date_posted": _date(node.get("datePosted")),
        "valid_through": _date(node.get("validThrough")),
        "remote": True if "TELECOMMUTE" in location_types else None,
        "applicant_location_requirements": _names(node.get("applicantLocationRequirements")),
        "employment_type": _strings(node.get("employmentType")),
        "hiring_organization": _text(org.get("name") if isinstance(org, Mapping) else org),
        "url": _url(node.get("url")),
    }
