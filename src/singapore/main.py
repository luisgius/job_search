"""Public-only Singapore monitoring, separate from tailoring and application."""
from __future__ import annotations

import argparse
import copy
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import yaml

from ..config import Config, deep_merge
from ..models import Job, Score, ScoredJob, normalize_company, normalize_title, utcnow
from ..scoring import score_jobs
from .config import enabled, load_yaml, public_api_robots, validate_entries
from .digest import render_digest, write_digest

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
WARSAW = ZoneInfo("Europe/Warsaw")


def mode_for_date(now: datetime) -> str:
    weekday = now.astimezone(WARSAW).weekday()
    return "weekly" if weekday == 0 else "boards" if weekday in {2, 4} else "daily"


def due(frequency: str, mode: str) -> bool:
    return frequency == "daily" or mode == "weekly" or (frequency == "mwf" and mode == "boards")


def _identity(job: Job) -> tuple[str, str]:
    import re
    company = re.sub(r"\b(?:pte\.?|private)\s*(?:ltd\.?|limited)\b", "", job.company, flags=re.I)
    return normalize_company(company), normalize_title(job.title)


def _coalesce(jobs: Iterable[Job]) -> list[Job]:
    """Merge cross-source requirements before filtering or sending to the scorer."""
    combined: dict[tuple[str, str], Job] = {}
    for candidate in jobs:
        key = _identity(candidate)
        if key not in combined:
            combined[key] = copy.deepcopy(candidate)
            continue
        job = combined[key]
        job.sources = list(dict.fromkeys(job.sources + candidate.sources))
        job.flags = list(dict.fromkeys(job.flags + candidate.flags))
        if candidate.description and candidate.description not in job.description:
            job.description = "\n\n".join(filter(None, [job.description, candidate.description]))
        company_site = candidate.source_type == "company_site"
        if company_site and job.source_type != "company_site":
            job.url = candidate.url
            job.apply_url = candidate.apply_url or candidate.url
            job.source_type = "company_site"
            job.source = candidate.source
            job.location = candidate.location or job.location
            job.country = candidate.country or job.country
        job.posted_at = job.posted_at or candidate.posted_at
        job.salary = job.salary or candidate.salary
        financial = bool(job.raw.get("fs") or candidate.raw.get("fs"))
        job.raw.update(candidate.raw)
        job.raw["fs"] = financial
    return list(combined.values())


def _source_rules(jobs: Iterable[Job], entry: Mapping[str, Any]) -> tuple[list[Job], list[tuple[Job, str]]]:
    kept: list[Job] = []
    rejected: list[tuple[Job, str]] = []
    for job in jobs:
        job.source_type = str(entry.get("source_type") or "board")
        job.raw["fs"] = bool(entry.get("fs") or job.raw.get("fs"))
        if not job.company or job.company.strip().casefold() in {"confidential", "anonymous", "undisclosed", "unknown", "n/a"}:
            rejected.append((job, "unnamed_company"))
            continue
        if job.source_type == "recruiter":
            if not job.raw.get("end_client_named") and not job.raw.get("end_client_established"):
                rejected.append((job, "unverified_recruiter_client"))
                continue
            job.flags = list(dict.fromkeys(job.flags + ["recruiter"]))
        if str(entry.get("id")) in {"glints", "tech_in_asia", "techinasia"}:
            size = job.raw.get("company_size")
            job.raw["company_size"] = size  # preserve any disclosed range for review
            if not isinstance(size, int) or size < 50:
                job.flags = list(dict.fromkeys(job.flags + ["small_company"]))
        kept.append(job)
    return kept, rejected


def run_monitor(
    config: Config, companies: list[dict[str, Any]], sources: list[dict[str, Any]],
    settings: dict[str, Any], *, now: datetime | None = None, mode: str = "auto",
    client: Any = None, store: Any = None, llm_client: Any = None,
    cv_path: Path | None = None, fetch_only: bool = False, limit: int | None = None,
) -> dict[str, Any]:
    # Imports are deliberately limited to public acquisition, pure filtering,
    # persistence and the existing scorer. Nothing invokes browser/apply/Gmail.
    from .adapters import get_adapter
    from .filters import apply_filters, located_in_singapore
    from .http import PublicClient
    from .store import SingaporeStore

    moment = now or utcnow()
    # Monitor-local provider/model overrides reuse the existing Config and LLM
    # factory while preserving the separately configured EU pipeline.
    config = copy.deepcopy(config)
    config.data = deep_merge(config.data, {
        name: settings[name] for name in ("llm", "scoring")
        if isinstance(settings.get(name), dict)
    })
    active_mode = mode_for_date(moment) if mode == "auto" else mode
    problems = validate_entries(companies, sources)
    if problems:
        raise ValueError("; ".join(problems))
    output = config.output_dir
    output.mkdir(parents=True, exist_ok=True)
    transport = client if client is not None else PublicClient(public_api_robots=public_api_robots(companies + sources))
    owned_transport = client is None
    owned_store = store is None
    tracker = store if store is not None else SingaporeStore(output / "singapore.sqlite3")
    stats: dict[str, Any] = {
        "started_at": moment.isoformat(), "mode": active_mode,
        "companies_checked": [], "sources_checked": [], "jobs_found": 0,
        "jobs_filtered": 0, "new_jobs": 0, "seen_jobs": 0,
        "filter_counts": {}, "errors": [], "checks": [], "rejected": [],
    }
    collected: list[Job] = []
    try:
        for entry in companies + sources:
            is_company = entry in companies
            if is_company and active_mode != "weekly":
                continue
            if not is_company and not due(str(entry.get("frequency", "weekly")), active_mode):
                continue
            if not enabled(entry):
                logger.info("manual/disabled source %s: %s", entry.get("name"), entry.get("status"))
                continue
            name = str(entry["name"])
            stats["companies_checked" if is_company else "sources_checked"].append(name)
            try:
                adapter = get_adapter(str(entry.get("ats") or entry.get("id") or entry.get("adapter")))
                found = adapter.fetch(entry, transport)
                stats["jobs_found"] += len(found)
                stats["checks"].append({"name": name, "jobs_found": len(found)})
                for job in found:
                    if is_company:
                        job.company = name
                        job.source_type = "company_site"
                        job.raw["fs"] = bool(entry.get("fs"))
                        job.raw["established"] = True
                    else:
                        job.raw["fs"] = bool(entry.get("fs") or job.raw.get("fs"))
                if not is_company:
                    found, rejected = _source_rules(found, entry)
                    for job, reason in rejected:
                        _reject(stats, job, reason)
                if entry.get("source_type") == "mcf":
                    fresh_mcf: list[Job] = []
                    for job in found:
                        if not job.posted_at or not moment - timedelta(days=7) <= job.posted_at <= moment:
                            _reject(stats, job, "mcf_outside_last_7_days_or_undated")
                        else:
                            fresh_mcf.append(job)
                    found = fresh_mcf
                    # The company radar covers DS advertisements even when a
                    # seniority/degree requirement knocks out the individual role.
                    import re
                    known = {normalize_company(row["name"]) for row in companies if row.get("status") != "candidate"}
                    for job in found:
                        if (re.search(r"\bSingapore\b", job.location, re.I) or job.country == "SG") and re.search(
                            r"\b(?:data\s+scientist|ML\s+scientist|machine\s+learning\s+scientist)\b", job.title, re.I
                        ):
                            tracker.record_mcf(job.company, moment, established=(
                                bool(job.raw.get("established")) and normalize_company(job.company) not in known
                            ))
                collected.extend(found)
                logger.info("checked %s: %d jobs", name, len(found))
            except Exception as exc:
                message = f"{name}: {type(exc).__name__}: {exc}"
                stats["errors"].append(message)
                stats["checks"].append({"name": name, "error": message})
                logger.error("%s", message)
        local_jobs: list[Job] = []
        for job in collected:
            if located_in_singapore(job):
                local_jobs.append(job)
            else:
                _reject(stats, job, "location_not_singapore")
        filtered = apply_filters(_coalesce(local_jobs), settings, now=moment)
        rejected_keys = {_identity(job) for job, _ in filtered.rejected}
        for job, reason in filtered.rejected:
            _reject(stats, job, reason)
        merged: dict[tuple[str, str], Job] = {}
        new_keys: set[tuple[str, str]] = set()
        for job in filtered.kept:
            item = tracker.merge(job, now=moment)
            identity = _identity(item)
            if item.raw.get("status") == "new":
                new_keys.add(identity)
            merged[identity] = item
        stats["new_jobs"] = len(new_keys)
        stats["seen_jobs"] = len(merged) - len(new_keys)
        for key, job in merged.items():
            job.raw["status"] = "new" if key in new_keys else "seen"
        pending = tracker.pending_jobs()
        # A previously pending role may have acquired a stronger description;
        # recheck all knockouts before retrying the scorer.
        pending = apply_filters((job for job in pending if _identity(job) not in rejected_keys), settings, now=moment).kept
        batch = pending[:limit] if limit is not None else pending
        assessed: list[ScoredJob] = []
        profile_error = ""
        if batch and not fetch_only:
            profile_file = cv_path or config.cv_path
            try:
                profile = profile_file.read_text(encoding="utf-8")
                if not profile.strip() or "# YOUR NAME" in profile or "REPLACE EVERYTHING IN THIS FILE" in profile:
                    raise ValueError("Configure a real CV; the repository CV is a placeholder")
                assessed = score_jobs(batch, profile, config, client=llm_client, errors=stats["errors"])
                for item in assessed:
                    tracker.save_score(item.job, item.score)
            except (OSError, ValueError) as exc:
                profile_error = str(exc)
                stats["errors"].append(profile_error)
        if fetch_only:
            profile_error = "Fetch-only run; fit scoring has not been performed"
        for item in assessed:
            if item.score.error:
                logger.error("scoring %s: %s", item.job.label, item.score.error)
        display = (tracker.recent_jobs(moment - timedelta(days=7))
                   if active_mode == "weekly" else list(merged.values()))
        display = apply_filters((job for job in display if _identity(job) not in rejected_keys), settings, now=moment).kept
        display_items: list[ScoredJob] = []
        for job in display:
            identity = _identity(job)
            job.raw["status"] = "new" if identity in new_keys else "seen"
            if active_mode != "weekly" and identity not in new_keys:
                continue
            cached = tracker.get_score(job)
            score = cached or Score(value=0, error=profile_error or "Scoring pending; retained for the next run")
            display_items.append(ScoredJob(job=job, score=score))
        candidates = tracker.candidate_companies(since=moment - timedelta(days=7))
        stats["digest_path"] = None
        if active_mode == "weekly" or stats["new_jobs"]:
            content = render_digest(display_items, now=moment, weekly=active_mode == "weekly",
                                    stats=stats, candidates=candidates, companies=companies, sources=sources)
            stats["digest_path"] = str(write_digest(content, output, moment))
        # Candidates are separate review data; they never change the verified list.
        (output / "sg_candidates.yaml").write_text(yaml.safe_dump({"companies": [
            {**row, "status": "candidate"} for row in candidates
        ]}, sort_keys=False), encoding="utf-8")
        stats["finished_at"] = utcnow().isoformat()
        log_path = output / "singapore_runs.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stats, ensure_ascii=False) + "\n")
        return stats
    finally:
        if owned_store:
            tracker.close()
        if owned_transport:
            transport.close()


def _reject(stats: dict[str, Any], job: Job, reason: str) -> None:
    stats["jobs_filtered"] += 1
    counts = stats["filter_counts"]
    counts[reason] = counts.get(reason, 0) + 1
    stats["rejected"].append({"company": job.company, "title": job.title, "reason": reason})
    logger.info("excluded %s: %s", job.label, reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--companies", type=Path, default=ROOT / "config/sg_companies.yaml")
    parser.add_argument("--sources", type=Path, default=ROOT / "config/sg_sources.yaml")
    parser.add_argument("--settings", type=Path, default=ROOT / "config/sg_monitor.yaml")
    parser.add_argument("--mode", choices=["auto", "weekly", "daily", "boards"], default="auto")
    parser.add_argument("--cv", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.limit is not None and args.limit < 0:
            raise ValueError("--limit must be nonnegative")
        companies = load_yaml(args.companies, "companies")
        sources = load_yaml(args.sources, "sources")
        settings = load_yaml(args.settings)
        config = Config.load(args.config, ROOT / "watchlist.yaml")
        if args.output_dir:
            config.data["output"]["dir"] = str(args.output_dir.resolve())
        issues = validate_entries(companies, sources)
        if issues:
            raise ValueError("; ".join(issues))
        if args.validate_only:
            print(json.dumps({"valid": True, "verified_companies": sum(row.get("status") == "verified" for row in companies),
                              "automated_companies": sum(enabled(row) for row in companies)}))
            return 0
        result = run_monitor(config, companies, sources, settings, mode=args.mode,
                             cv_path=args.cv, fetch_only=args.fetch_only, limit=args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2 if result["errors"] else 0
    except (OSError, ValueError) as exc:
        logger.error("Configuration error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
