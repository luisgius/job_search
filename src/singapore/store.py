"""Persistent state for the public Singapore monitor.

Discovery (new vs seen) is independent of scoring success. Failed or missing
scores stay pending for retry; rejected postings are never written here.
MCF observations record first/last sighting dates only — never sponsorship.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    Job,
    Score,
    ensure_utc,
    normalize_company,
    normalize_title,
    utcnow,
)

_PTE_LTD_RE = re.compile(
    r"\b(?:pte\.?|private)\s*(?:ltd\.?|limited)\b",
    re.IGNORECASE,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    identity        TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    company_norm    TEXT NOT NULL,
    title_norm      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    sources_json    TEXT NOT NULL DEFAULT '[]',
    source_type     TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    apply_url       TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    salary          TEXT,
    country         TEXT,
    remote          INTEGER,
    ats             TEXT,
    ats_job_id      TEXT,
    posted_at       TEXT,
    flags_json      TEXT NOT NULL DEFAULT '[]',
    raw_json        TEXT NOT NULL DEFAULT '{}',
    fs              INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    identity        TEXT PRIMARY KEY,
    value           INTEGER,
    reasons_json    TEXT NOT NULL DEFAULT '[]',
    strengths_json  TEXT NOT NULL DEFAULT '[]',
    gaps_json       TEXT NOT NULL DEFAULT '[]',
    verdict         TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    error           TEXT,
    ok              INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (identity) REFERENCES jobs (identity)
);

CREATE TABLE IF NOT EXISTS mcf_companies (
    company_norm    TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    established     INTEGER NOT NULL DEFAULT 0
);
"""


def _iso(value: datetime | None) -> str | None:
    dt = ensure_utc(value)
    return dt.isoformat() if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def strip_pte_ltd(company: str) -> str:
    """Drop Singapore Pte Ltd / Private Limited noise before normalisation."""
    return _PTE_LTD_RE.sub("", company or "").strip(" ,.-")


def job_identity(job: Job) -> str:
    company = normalize_company(strip_pte_ltd(job.company))
    title = normalize_title(job.title)
    return f"{company}|{title}"


def _source_rank(source_type: str, source: str) -> int:
    st = (source_type or "").strip().lower()
    src = (source or "").strip().lower()
    if st == "company_site" or src in {"greenhouse", "lever", "ashby", "workday", "smartrecruiters"}:
        return 3
    if st in {"board", "mcf"}:
        return 2
    if st == "recruiter":
        return 1
    return 0


def _richer(incoming: Job, existing: Job) -> dict[str, Any]:
    """Field-level merge preferring company_site apply URLs and richer text/date/fs."""
    prefer_new_apply = _source_rank(incoming.source_type, incoming.source) >= _source_rank(
        existing.source_type, existing.source
    )
    apply_url = (
        (incoming.apply_url or incoming.url)
        if prefer_new_apply and (incoming.apply_url or incoming.url)
        else (existing.apply_url or existing.url or incoming.apply_url or incoming.url)
    )
    if prefer_new_apply and (incoming.apply_url or incoming.url):
        apply_url = incoming.apply_url or incoming.url
    elif existing.apply_url or existing.url:
        apply_url = existing.apply_url or existing.url
    else:
        apply_url = incoming.apply_url or incoming.url

    # If the incoming record is explicitly a company site, always take its apply URL.
    if (incoming.source_type or "").lower() == "company_site" and (incoming.apply_url or incoming.url):
        apply_url = incoming.apply_url or incoming.url
    elif (existing.source_type or "").lower() == "company_site" and (existing.apply_url or existing.url):
        apply_url = existing.apply_url or existing.url

    description = incoming.description or ""
    if len(existing.description or "") >= len(description):
        description = existing.description or description

    posted_at = incoming.posted_at or existing.posted_at
    if incoming.posted_at and existing.posted_at:
        # Prefer the earlier employer publication date when both exist.
        posted_at = min(incoming.posted_at, existing.posted_at)

    fs = bool((incoming.raw or {}).get("fs") or (existing.raw or {}).get("fs"))
    sources = list(dict.fromkeys(
        list(existing.sources or []) + list(incoming.sources or [incoming.source])
    ))
    flags = list(dict.fromkeys(list(existing.flags or []) + list(incoming.flags or [])))

    raw = dict(existing.raw or {})
    raw.update({k: v for k, v in (incoming.raw or {}).items() if v is not None})
    raw["fs"] = fs

    source_type = incoming.source_type or existing.source_type
    if (existing.source_type or "").lower() == "company_site":
        source_type = existing.source_type
    if (incoming.source_type or "").lower() == "company_site":
        source_type = incoming.source_type

    salary = incoming.salary or existing.salary
    location = incoming.location or existing.location
    url = incoming.url or existing.url
    if (incoming.source_type or "").lower() != "company_site" and (
        existing.source_type or ""
    ).lower() == "company_site":
        url = existing.url or url

    return {
        "apply_url": apply_url,
        "description": description,
        "posted_at": posted_at,
        "fs": fs,
        "sources": sources,
        "flags": flags,
        "raw": raw,
        "source_type": source_type,
        "salary": salary,
        "location": location,
        "url": url,
        "source": incoming.source or existing.source,
        "company": incoming.company or existing.company,
        "title": incoming.title or existing.title,
        "country": incoming.country or existing.country,
        "remote": incoming.remote if incoming.remote is not None else existing.remote,
        "ats": incoming.ats or existing.ats,
        "ats_job_id": incoming.ats_job_id or existing.ats_job_id,
    }


class SingaporeStore:
    """SQLite-backed merge / score / MCF candidate store."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "SingaporeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identity / row helpers -------------------------------------------

    def _row_to_job(self, row: sqlite3.Row, *, status: str | None = None) -> Job:
        raw = json.loads(row["raw_json"] or "{}")
        raw["fs"] = bool(row["fs"] or raw.get("fs"))
        if status is not None:
            raw["status"] = status
        sources = json.loads(row["sources_json"] or "[]")
        flags = json.loads(row["flags_json"] or "[]")
        remote = row["remote"]
        return Job(
            source=row["source"] or (sources[0] if sources else ""),
            company=row["company"],
            title=row["title"],
            url=row["url"] or "",
            location=row["location"] or "",
            description=row["description"] or "",
            posted_at=_parse_iso(row["posted_at"]),
            remote=None if remote is None else bool(remote),
            salary=row["salary"],
            country=row["country"],
            ats=row["ats"],
            ats_job_id=row["ats_job_id"],
            raw=raw,
            apply_url=row["apply_url"] or row["url"] or "",
            source_type=row["source_type"] or "",
            sources=sources,
            flags=flags,
        )

    def _fetch(self, identity: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE identity = ?", (identity,)
        ).fetchone()

    # -- merge ------------------------------------------------------------

    def merge(self, job: Job, now: datetime | None = None) -> Job:
        """Upsert by company + normalised title. Sets ``raw['status']`` to new/seen."""
        moment = ensure_utc(now) or utcnow()
        stamp = moment.isoformat()
        identity = job_identity(job)
        existing_row = self._fetch(identity)
        company_norm = normalize_company(strip_pte_ltd(job.company))
        title_norm = normalize_title(job.title)

        if existing_row is None:
            sources = list(dict.fromkeys(job.sources or [job.source]))
            flags = list(dict.fromkeys(job.flags or []))
            raw = dict(job.raw or {})
            fs = bool(raw.get("fs"))
            raw["status"] = "new"
            raw["fs"] = fs
            self.conn.execute(
                """
                INSERT INTO jobs (
                    identity, company, title, company_norm, title_norm,
                    source, sources_json, source_type, url, apply_url,
                    location, description, salary, country, remote,
                    ats, ats_job_id, posted_at, flags_json, raw_json, fs,
                    first_seen_at, last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    identity, job.company, job.title, company_norm, title_norm,
                    job.source, json.dumps(sources), job.source_type or "",
                    job.url or "", job.apply_url or job.url or "",
                    job.location or "", job.description or "", job.salary,
                    job.country, None if job.remote is None else int(bool(job.remote)),
                    job.ats, str(job.ats_job_id) if job.ats_job_id is not None else None,
                    _iso(job.posted_at), json.dumps(flags), json.dumps(raw),
                    int(fs), stamp, stamp,
                ),
            )
            self.conn.commit()
            out = Job(
                source=job.source,
                company=job.company,
                title=job.title,
                url=job.url,
                location=job.location,
                description=job.description,
                posted_at=job.posted_at,
                remote=job.remote,
                salary=job.salary,
                country=job.country,
                ats=job.ats,
                ats_job_id=job.ats_job_id,
                raw=raw,
                apply_url=job.apply_url or job.url,
                source_type=job.source_type,
                sources=sources,
                flags=flags,
            )
            return out

        existing = self._row_to_job(existing_row)
        merged = _richer(job, existing)
        raw = dict(merged["raw"])
        raw["status"] = "seen"
        raw["fs"] = merged["fs"]
        self.conn.execute(
            """
            UPDATE jobs SET
                company = ?, title = ?, source = ?, sources_json = ?,
                source_type = ?, url = ?, apply_url = ?, location = ?,
                description = ?, salary = ?, country = ?, remote = ?,
                ats = ?, ats_job_id = ?, posted_at = ?, flags_json = ?,
                raw_json = ?, fs = ?, last_seen_at = ?
            WHERE identity = ?
            """,
            (
                merged["company"], merged["title"], merged["source"],
                json.dumps(merged["sources"]), merged["source_type"] or "",
                merged["url"] or "", merged["apply_url"] or "",
                merged["location"] or "", merged["description"] or "",
                merged["salary"], merged["country"],
                None if merged["remote"] is None else int(bool(merged["remote"])),
                merged["ats"],
                str(merged["ats_job_id"]) if merged["ats_job_id"] is not None else None,
                _iso(merged["posted_at"]), json.dumps(merged["flags"]),
                json.dumps(raw), int(bool(merged["fs"])), stamp, identity,
            ),
        )
        self.conn.commit()
        return Job(
            source=merged["source"],
            company=merged["company"],
            title=merged["title"],
            url=merged["url"] or "",
            location=merged["location"] or "",
            description=merged["description"] or "",
            posted_at=merged["posted_at"],
            remote=merged["remote"],
            salary=merged["salary"],
            country=merged["country"],
            ats=merged["ats"],
            ats_job_id=merged["ats_job_id"],
            raw=raw,
            apply_url=merged["apply_url"] or "",
            source_type=merged["source_type"] or "",
            sources=list(merged["sources"]),
            flags=list(merged["flags"]),
        )

    # -- scoring ----------------------------------------------------------

    def save_score(self, job: Job, score: Score) -> None:
        """Persist a score. Failures stay non-ok so ``pending_jobs`` retries them."""
        identity = job_identity(job)
        if self._fetch(identity) is None:
            # Scoring should only follow a merge; still record nothing for orphans.
            return
        stamp = utcnow().isoformat()
        self.conn.execute(
            """
            INSERT INTO scores (
                identity, value, reasons_json, strengths_json, gaps_json,
                verdict, model, error, ok, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET
                value = excluded.value,
                reasons_json = excluded.reasons_json,
                strengths_json = excluded.strengths_json,
                gaps_json = excluded.gaps_json,
                verdict = excluded.verdict,
                model = excluded.model,
                error = excluded.error,
                ok = excluded.ok,
                updated_at = excluded.updated_at
            """,
            (
                identity,
                score.value,
                json.dumps(list(score.reasons or [])),
                json.dumps(list(score.strengths or [])),
                json.dumps(list(score.gaps or [])),
                score.verdict or "",
                score.model or "",
                score.error,
                int(bool(score.ok)),
                stamp,
            ),
        )
        self.conn.commit()

    def get_score(self, job: Job) -> Score | None:
        identity = job_identity(job)
        row = self.conn.execute(
            "SELECT * FROM scores WHERE identity = ?", (identity,)
        ).fetchone()
        if row is None:
            return None
        return Score(
            value=int(row["value"] or 0),
            reasons=json.loads(row["reasons_json"] or "[]"),
            strengths=json.loads(row["strengths_json"] or "[]"),
            gaps=json.loads(row["gaps_json"] or "[]"),
            verdict=row["verdict"] or "",
            model=row["model"] or "",
            error=row["error"],
        )

    def pending_jobs(self) -> list[Job]:
        """Jobs awaiting a successful score (never scored, or last score failed)."""
        rows = self.conn.execute(
            """
            SELECT j.* FROM jobs j
            LEFT JOIN scores s ON s.identity = j.identity
            WHERE s.identity IS NULL OR s.ok = 0
            ORDER BY j.first_seen_at ASC
            """
        ).fetchall()
        return [self._row_to_job(row, status=(json.loads(row["raw_json"] or "{}").get("status")))
                for row in rows]

    def recent_jobs(self, since: datetime) -> list[Job]:
        """Jobs first discovered at/after ``since`` (Monday weekly window)."""
        stamp = (ensure_utc(since) or utcnow()).isoformat()
        rows = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE first_seen_at >= ?
            ORDER BY first_seen_at DESC
            """,
            (stamp,),
        ).fetchall()
        out: list[Job] = []
        for row in rows:
            raw = json.loads(row["raw_json"] or "{}")
            out.append(self._row_to_job(row, status=raw.get("status")))
        return out

    # -- MCF radar --------------------------------------------------------

    def record_mcf(
        self,
        company: str,
        observed_at: datetime,
        established: bool = False,
    ) -> None:
        """Record an MCF sighting. Never asserts sponsorship or auto-verification."""
        moment = ensure_utc(observed_at) or utcnow()
        stamp = moment.isoformat()
        norm = normalize_company(strip_pte_ltd(company))
        if not norm:
            return
        row = self.conn.execute(
            "SELECT * FROM mcf_companies WHERE company_norm = ?", (norm,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO mcf_companies (company_norm, company, first_seen, last_seen, established)
                VALUES (?,?,?,?,?)
                """,
                (norm, company.strip(), stamp, stamp, int(bool(established))),
            )
        else:
            # established may flip True on later evidence; never silently invent it.
            new_established = bool(row["established"]) or bool(established)
            self.conn.execute(
                """
                UPDATE mcf_companies
                SET company = ?, last_seen = ?, established = ?
                WHERE company_norm = ?
                """,
                (company.strip() or row["company"], stamp, int(new_established), norm),
            )
        self.conn.commit()

    def candidate_companies(
        self, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Established-only MCF candidates for human review (first/last dates)."""
        if since is None:
            rows = self.conn.execute(
                """
                SELECT company, first_seen, last_seen FROM mcf_companies
                WHERE established = 1
                ORDER BY first_seen ASC
                """
            ).fetchall()
        else:
            stamp = (ensure_utc(since) or utcnow()).isoformat()
            rows = self.conn.execute(
                """
                SELECT company, first_seen, last_seen FROM mcf_companies
                WHERE established = 1 AND first_seen >= ?
                ORDER BY first_seen ASC
                """,
                (stamp,),
            ).fetchall()
        return [
            {
                "company": row["company"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                # Explicit: observation only — not sponsorship / verification.
                "status": "candidate",
            }
            for row in rows
        ]
