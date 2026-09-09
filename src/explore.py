"""Bounded, unassessed opportunity browsing; deliberately independent of main."""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import filters, geo
from .config import BOARD_SOURCE_NAMES, SOURCE_NAMES, Config, ConfigError
from .models import Job, ensure_utc, utcnow

MAX_DAYS = 90
MAX_RESULTS = 500
MAX_UNDATED = 50
# Mail is excluded even when enabled in normal configuration.
PUBLIC_SOURCES = tuple(name for name in SOURCE_NAMES if name != "linkedin_email")
NOTICE = (
    "Unassessed opportunities; availability unknown. No fit scoring or availability "
    "checks were performed. Publication age does not establish that a role is open "
    "or recommended. This is a bounded view of the selected configured sources, "
    "not a market completeness assessment. Source dates may describe an update "
    "rather than original publication."
)


def _bounded(name: str, value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _selected(config: Config, sources: Iterable[str] | None) -> tuple[str, ...]:
    wanted = set(PUBLIC_SOURCES) if sources is None else {
        str(name).strip().lower() for name in sources
    }
    unknown = wanted - set(PUBLIC_SOURCES)
    if unknown:
        raise ValueError(f"unsupported exploration sources (mail is excluded): {', '.join(sorted(unknown))}")
    return tuple(name for name in PUBLIC_SOURCES if name in wanted and config.source_enabled(name))


def _fetch_sources(
    config: Config, active: tuple[str, ...], errors: list[str],
    *, fetchers: Mapping[str, Callable[..., list[Job]]] | None = None,
) -> list[Job]:
    """Reuse public source adapters only; injected maps never fall back to network."""
    jobs: list[Job] = []
    boards = set(active) & set(BOARD_SOURCE_NAMES)
    groups = [("ats_boards", boards)] if boards else []
    groups.extend((name, {name}) for name in active if name not in boards)
    for module_name, allowed in groups:
        try:
            fetch = (fetchers[module_name] if fetchers is not None else
                     importlib.import_module(f".sources.{module_name}", __package__).fetch)
            found = fetch(config, errors=errors)
            jobs.extend(job for job in found if job.source.lower() in allowed)
        except Exception as exc:
            errors.append(f"{module_name}: source fetch failed: {exc}")
    return jobs


def _sort_key(job: Job) -> tuple[Any, ...]:
    return (
        -(job.posted_at.timestamp()) if job.posted_at else float("inf"),
        job.company.casefold(), job.title.casefold(), job.key, job.source, job.url,
        json.dumps(job.to_dict(), sort_keys=True, ensure_ascii=False),
        json.dumps(job.raw, sort_keys=True, default=str, ensure_ascii=False),
    )


def _evidence(job: Job) -> str:
    if not job.description.strip():
        return "missing"
    if job.raw.get("snippet_only"):
        return "snippet"
    # Absence of a snippet flag is not proof that the source returned the full ad.
    return "description_completeness_unknown"


def _record(job: Job) -> dict[str, Any]:
    return {
        **job.to_dict(),
        "assessment": "unassessed",
        "availability": "unknown",
        "evidence_kind": _evidence(job),
        "snippet": " ".join(job.description.split())[:320],
        "source_evidence": copy.deepcopy(job.raw),
    }


def explore(
    config: Config, *, days: int = 30, limit: int = 100, undated_limit: int = 0,
    sources: Iterable[str] | None = None, countries: Iterable[str] | None = None,
    now: datetime | None = None,
    fetchers: Mapping[str, Callable[..., list[Job]]] | None = None,
) -> dict[str, Any]:
    """Fetch, dedupe and hard-filter only, returning a bounded report in memory.

    No tracker, scorer, application, CV, mail or digest dependency is imported.
    ``fetchers`` maps adapter module names (including ``ats_boards``) to the
    existing ``fetch(config, errors=...)`` contract, for offline callers/tests.
    """
    _bounded("days", days, 1, MAX_DAYS)
    _bounded("limit", limit, 1, MAX_RESULTS)
    _bounded("undated_limit", undated_limit, 0, MAX_UNDATED)
    moment = ensure_utc(now) or utcnow()
    active = _selected(config, sources)
    country_override = None if countries is None else sorted({str(code).strip().upper() for code in countries})
    if country_override is not None and (not country_override or any(code not in geo.ALL_COUNTRIES for code in country_override)):
        raise ValueError("countries must be supported European ISO2 codes (for example PL or DE)")
    # Existing filters stamp country/level, so both config and records are copies.
    local = copy.deepcopy(config)
    local.data["sources"] = {name: name in active for name in SOURCE_NAMES}
    if country_override is not None:
        local_filters = local.data.setdefault("filters", {})
        local_filters["countries"] = country_override
        # An explicit geography selection must not admit other countries through
        # normal sponsorship exceptions. Keep the nonempty selected allowlist:
        # empty countries disables the existing location gate altogether.
        local_filters["countries_if_sponsorship"] = [
            code for code in (local.get("filters.countries_if_sponsorship", []) or [])
            if str(code).strip().upper() in country_override
        ]
    local.data.setdefault("freshness", {}).update(
        max_age_hours=days * 24, skip_undated=undated_limit == 0,
    )
    errors: list[str] = []
    fetched = _fetch_sources(local, active, errors, fetchers=fetchers)
    jobs = filters.dedupe(sorted(copy.deepcopy(fetched), key=_sort_key))
    result = filters.apply_filters(jobs, local, now=moment)
    dated, undated = [], []
    rejected = list(result.rejected)
    counts = dict(result.counts)
    for job in result.kept:
        if job.posted_at is None:
            undated.append(job)
        elif job.posted_at > moment:
            rejected.append((job, "source posting date is in the future; outside exploration window"))
            counts["future_date"] = counts.get("future_date", 0) + 1
        else:
            dated.append(job)
    dated.sort(key=_sort_key)
    undated.sort(key=_sort_key)
    rejected.sort(key=lambda pair: _sort_key(pair[0]))
    source_counts = Counter(job.source for job in fetched)
    return {
        "schema_version": 1,
        "generated_at": moment.isoformat(),
        "notice": NOTICE,
        "days": days,
        "limit": limit,
        "undated_limit": undated_limit,
        "selected_sources": list(active),
        "countries": copy.deepcopy(local.get("filters.countries", [])),
        "diagnostics": ([] if active else [
            "No enabled public sources selected. Enable a source in config.yaml and check --source selection."
        ]),
        "source_counts": {name: source_counts[name] for name in active},
        "errors": errors,
        "counts": {
            "fetched": len(fetched), "after_dedupe": len(jobs),
            "dated_eligible": len(dated), "undated_eligible": len(undated),
            "dated_displayed": min(len(dated), limit),
            "undated_displayed": min(len(undated), undated_limit),
            "rejected": len(rejected), "rejected_displayed": min(len(rejected), limit),
        },
        "first_rejection_counts": dict(sorted(counts.items())),
        "evidence_counts": dict(sorted(Counter(_evidence(j) for j in dated + undated).items())),
        "opportunities": [_record(job) for job in dated[:limit]],
        "undated_review": [_record(job) for job in undated[:undated_limit]],
        "rejected_examples": [
            {**_record(job), "first_rejection": reason}
            for job, reason in rejected[:limit]
        ],
    }


def render_html(report: dict[str, Any]) -> str:
    def safe(value: Any) -> str:
        return escape(str(value), quote=True)

    def card(row: dict[str, Any]) -> str:
        url = row["url"]
        parsed = urlsplit(url)
        link = (f'<a href="{safe(url)}" rel="noreferrer noopener">Source posting</a>'
                if parsed.scheme in {"http", "https"} and parsed.netloc else safe(url))
        reason = f'<p>First rejection: {safe(row["first_rejection"])}</p>' if "first_rejection" in row else ""
        return (
            f'<article><h3>{safe(row["title"])} — {safe(row["company"])}</h3>'
            '<p><strong>Unassessed · Availability unknown</strong></p>'
            f'<p>{safe(row["location"])} · {safe(row["source"])} · '
            f'Source date: {safe(row["posted_at"] or "unknown")}</p>{link}{reason}'
            f'<p>Evidence: {safe(row["evidence_kind"])}</p><p>{safe(row["snippet"])}</p>'
            f'<details><summary>All available description and source evidence</summary>'
            f'<pre>{safe(row["description"])}</pre><pre>'
            f'{safe(json.dumps(row["source_evidence"], indent=2, ensure_ascii=False, default=str))}'
            '</pre></details></article>'
        )

    def labeled_counts(values: Mapping[str, int], empty: str) -> str:
        if not values:
            return f"<p>{safe(empty)}</p>"
        rows = "".join(f"<tr><th scope='row'>{safe(key.replace('_', ' ').capitalize())}</th><td>{value}</td></tr>"
                       for key, value in values.items())
        return f"<table><tbody>{rows}</tbody></table>"

    messages = report["diagnostics"] + report["errors"]
    status = ("Source collection had errors; missing results cannot be interpreted as no openings."
              if report["errors"] else "No source fetch errors reported.")
    diagnostics = "".join(f"<li>{safe(message)}</li>" for message in messages)
    sections = "".join(
        f'<section><h2>{title}</h2>' + ("".join(card(row) for row in report[key]) or '<p>None in this view.</p>') + '</section>'
        for title, key in (("Dated opportunities", "opportunities"),
                           ("Undated review (date unknown)", "undated_review"),
                           ("Rejected examples (bounded)", "rejected_examples"))
    )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Opportunity explorer — unassessed</title><style>'
        'body{font:16px/1.5 system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#f6f8fb;color:#172238}'
        'article{background:white;border:1px solid #ccd5e2;border-radius:8px;padding:1rem;margin:1rem 0}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#1757a6}'
        'table{border-collapse:collapse;margin:1rem 0}th,td{text-align:left;padding:.3rem 1rem .3rem 0}'
        'th{font-weight:500}td{font-variant-numeric:tabular-nums}</style><body>'
        f'<h1>Opportunity explorer</h1><p>{safe(report["notice"])}</p>'
        f'<p>Last {report["days"]} days through {safe(report["generated_at"])}. '
        f'Dated cap: {report["limit"]}; undated cap: {report["undated_limit"]}.</p>'
        f'<p>Selected sources: {safe(", ".join(report["selected_sources"]) or "none")}</p>'
        f'<p>Country filter: {safe(", ".join(report["countries"]) or "unrestricted")}; existing remote rules apply; sponsorship exceptions cannot expand an explicit country selection.</p>'
        '<h2>Collection status</h2>'
        f'<p>{safe(status)}</p><ul>{diagnostics}</ul>'
        '<h2>Fetched and displayed counts</h2>'
        f'{labeled_counts(report["counts"], "No counts available.")}'
        '<h2>Fetched by source</h2>'
        f'{labeled_counts(report["source_counts"], "No enabled public sources selected.")}'
        '<h2>First rejection counts</h2>'
        f'{labeled_counts(report["first_rejection_counts"], "No hard-filter rejections in the returned batch.")}'
        '<h2>Description evidence</h2>'
        f'{labeled_counts(report["evidence_counts"], "No eligible description evidence in this view.")}'
        f'{sections}</body></html>'
    )


def _output_base(config: Config, output_dir: str | Path | None) -> Path:
    base = Path(output_dir) if output_dir is not None else config.root / "exploration_output"
    if not base.is_absolute():
        base = config.root / base
    base = base.resolve()
    production = config.output_dir.resolve()
    if base == production or base.is_relative_to(production) or production.is_relative_to(base):
        raise ValueError("exploration output directory must be separate from the production output tree")
    return base


def write_report(report: dict[str, Any], config: Config, *, output_dir: str | Path | None = None) -> tuple[Path, Path]:
    """Create a fresh run directory; never overwrite any existing artifact."""
    base = _output_base(config, output_dir)
    html = render_html(report)
    data = json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    base.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="explore_", dir=base))
    html_path, json_path = run_dir / "exploration.html", run_dir / "exploration.json"
    for path, content in ((html_path, html), (json_path, data)):
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    return html_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--watchlist", default="watchlist.yaml")
    parser.add_argument("--days", type=int, default=30, help="lookback in days (1–90; default 30)")
    parser.add_argument("--limit", type=int, default=100, help="maximum dated results and rejected examples (1–500)")
    parser.add_argument("--undated-limit", type=int, default=0, help="explicit undated review cap (0–50; default disabled)")
    parser.add_argument("--source", action="append", choices=PUBLIC_SOURCES, help="repeat to narrow enabled sources; never enables a disabled source")
    parser.add_argument("--country", action="append", type=str.upper, choices=sorted(geo.ALL_COUNTRIES), help="repeat to override country filters for this run (European ISO2); existing remote rules apply; sponsorship cannot expand this selection")
    parser.add_argument("--output-dir", help="separate exploration directory (default: exploration_output)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config, args.watchlist)
        _output_base(config, args.output_dir)  # Fail before any source requests.
        report = explore(config, days=args.days, limit=args.limit,
                         undated_limit=args.undated_limit, sources=args.source, countries=args.country)
        html_path, json_path = write_report(report, config, output_dir=args.output_dir)
    except (ConfigError, ValueError, OSError) as exc:
        parser.error(str(exc))
    for message in report["diagnostics"]:
        print(message)
    if report["errors"]:
        print("Source collection had errors; inspect Collection status in the report.")
    print(f"Unassessed exploration HTML: {html_path}\nExploration JSON: {json_path}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
