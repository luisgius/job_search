"""Markdown review digest for the public Singapore monitor."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..models import ScoredJob

WARSAW = ZoneInfo("Europe/Warsaw")


def _text(value: Any) -> str:
    return " ".join(str(value or "").replace("[", "\\[").replace("]", "\\]").split())


def _link(url: str, label: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        return _text(label)
    return f"[{_text(label)}](<{url.replace('>', '%3E').replace(chr(10), '')}>)"


def render_digest(
    scored: Iterable[ScoredJob], *, now: datetime, weekly: bool,
    stats: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]],
    companies: Iterable[Mapping[str, Any]], sources: Iterable[Mapping[str, Any]],
) -> str:
    items = sorted(scored, key=lambda item: (
        "small_company" in item.job.flags, -item.score.value,
        item.job.company.casefold(), item.job.title.casefold(),
    ))
    day = now.astimezone(WARSAW).date().isoformat()
    lines = [f"# Singapore data science digest — {day}", "",
             "Weekly review." if weekly else "New postings since the last run.", "",
             f"Checked: {len(stats.get('companies_checked', []))} companies; "
             f"found: {stats.get('jobs_found', 0)}; "
             f"excluded: {stats.get('jobs_filtered', 0)}; "
             f"new: {stats.get('new_jobs', 0)}.", ""]
    if not items:
        lines += ["No eligible new postings were found in the sources checked.", ""]
    for item in items:
        job, score = item.job, item.score
        status = job.raw.get("status", "new")
        score_text = str(score.value) + "/100" if score.ok else "unscored"
        flags = ", ".join(job.flags) or "none"
        fit = score.verdict or (score.reasons[0] if score.reasons else "")
        if score.error:
            fit = f"Scoring unavailable: {score.error}"
        if not fit:
            fit = "Fit assessment pending."
        posted = job.posted_at.date().isoformat() if job.posted_at else "not published"
        lines += [f"## {_text(job.company)} — {_text(job.title)}", "",
                  f"{_link(job.apply_url or job.url, 'Apply directly')} · "
                  f"{_text(job.location)} · **{score_text}** · {_text(status)}", "",
                  f"Sources: {_text(', '.join(job.sources or [job.source]))}  ",
                  f"Posted: {posted}  ", f"Fit: {_text(fit)}  ",
                  f"Flags: {_text(flags)}", ""]
        if job.raw.get("company_size") is not None:
            lines += [f"Company size: {_text(job.raw['company_size'])} employees.", ""]
    lines += ["## New companies seen on MCF", "",
              "An MCF advertisement is a discovery signal; it does not establish "
              "Employment Pass sponsorship or a willingness to hire foreigners.", ""]
    reviewed = list(candidates)
    for row in reviewed:
        lines.append(f"- **{_text(row.get('company', row.get('name')))}** — candidate for review; "
                     f"first seen {_text(row.get('first_seen', row.get('observed_at')))}; "
                     f"last seen {_text(row.get('last_seen', row.get('observed_at')))}.")
    if not reviewed:
        lines.append("No new established-company candidates observed.")
    lines += ["", "## Run details", ""]
    for reason, count in sorted(stats.get("filter_counts", {}).items()):
        lines.append(f"- {_text(reason)}: {count}")
    for error in stats.get("errors", []):
        lines.append(f"- Error: {_text(error)}")
    if not stats.get("errors") and not stats.get("filter_counts"):
        lines.append("No source errors or exclusions.")
    lines += ["", "## Manual checks", ""]
    manual = list(sources) + list(companies)
    if not any(str(row.get("name", "")).casefold().startswith("linkedin") for row in manual):
        manual.insert(0, {"name": "LinkedIn", "status": "manual_check",
                          "verified_url": "https://www.linkedin.com/jobs/",
                          "manual_alert": "Daily Singapore Data Scientist and ML/AI Engineer job alerts."})
    for row in manual:
        if row.get("status") == "verified" and row.get("automation_allowed") is True:
            continue
        name = str(row.get("name", "Unknown source"))
        note = row.get("manual_alert") or row.get("notes") or row.get("terms_notes") or "Check manually."
        label = "unverified" if row.get("status") == "unverified" else "manual_check"
        lines.append(f"- {_link(str(row.get('verified_url') or row.get('url') or ''), name)} "
                     f"({_text(label)}): {_text(note)}")
    return "\n".join(lines) + "\n"


def write_digest(content: str, output_dir: Path, now: datetime) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"digest_singapore_{now.astimezone(WARSAW).date().isoformat()}.md"
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path
