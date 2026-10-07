"""Validate the independent monitor without changing the EU pipeline config."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from datetime import date

import yaml


def load_yaml(path: Path, key: str | None = None) -> Any:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    if key is None:
        return data
    rows = data.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected a '{key}' list of mappings")
    return rows


def enabled(row: dict[str, Any]) -> bool:
    """A reachable endpoint alone does not approve automated access."""
    return row.get("status") == "verified" and row.get("automation_allowed") is True and not row.get("policy_hold")


def public_api_robots(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Only Ashby's documented public posting API has an evidenced 401 policy.

    RFC 9309 section 2.3.1.3 treats 4xx robots as unavailable. This narrower
    researched policy applies only to that API's absent robots file; successful
    robots directives, forbidden responses and authentication still win.
    """
    approved: dict[str, dict[str, Any]] = {}
    for row in entries:
        exception = row.get("robots_unavailable")
        if not enabled(row) or exception is None:
            continue
        if not isinstance(exception, dict):
            raise ValueError(f"{row.get('name')}: invalid public API robots evidence")
        robots = urlsplit(str(row.get("robots_url") or ""))
        if (row.get("ats") != "ashby" or robots.scheme != "https" or robots.netloc != "api.ashbyhq.com"
                or robots.path != "/robots.txt" or exception.get("http_status") != 401
                or exception.get("policy") != "documented_public_api"
                or exception.get("path_prefix") != "/posting-api/job-board/"
                or exception.get("documentation_url") != "https://developers.ashbyhq.com/docs/public-job-posting-api"
                or exception.get("reference_url") != "https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3"):
            raise ValueError(f"{row.get('name')}: unsupported public API robots exception")
        try:
            date.fromisoformat(str(exception["verified_at"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{row.get('name')}: public API robots policy needs a verification date") from exc
        approved["https://api.ashbyhq.com"] = dict(exception)
    return approved


def validate_entries(companies: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    names: set[str] = set()
    for row in companies:
        name = str(row.get("name") or "").strip()
        if not name or name.casefold() in names:
            errors.append(f"Company name missing or duplicated: {name!r}")
        names.add(name.casefold())
        if row.get("ats") not in {"workday", "greenhouse", "lever", "smartrecruiters", "ashby", "custom"}:
            errors.append(f"{name}: unsupported ATS")
        if not isinstance(row.get("fs"), bool):
            errors.append(f"{name}: fs must be a boolean")
    for row in companies + sources:
        name = str(row.get("name") or "unknown")
        if row.get("status") not in {"verified", "unverified", "manual_check", "candidate"}:
            errors.append(f"{name}: invalid status")
        if enabled(row):
            for key in ("verified_url", "robots_url", "terms_url"):
                value = urlsplit(str(row.get(key) or ""))
                if value.scheme != "https" or not value.hostname or value.username:
                    errors.append(f"{name}: enabled entry requires public HTTPS {key}")
            if not row.get("evidence"):
                errors.append(f"{name}: enabled entry requires dated verification evidence")
        if "fs" in row and not isinstance(row["fs"], bool):
            errors.append(f"{name}: fs must be a boolean")
    try:
        public_api_robots(companies + sources)
    except ValueError as exc:
        errors.append(str(exc))
    return errors
