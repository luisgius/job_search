"""SQLite tracker — the thing that stops you applying to the same job twice.

Two tables carry the load:

  jobs          every posting we have ever normalised, keyed by `Job.key`
  applications  the outcome per job (digest / dry-run / applied / failed)

`has_applied()` is the hard gate in front of the auto-apply stage, and
`should_surface()` is the softer gate in front of the digest.

Schema changes go through `MIGRATIONS`; `PRAGMA user_version` tracks where a
database is, so an existing tracker upgrades in place instead of exploding.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import ApplyStatus, Job, ensure_utc, utcnow

SCHEMA_VERSION = 1

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS jobs (
        key            TEXT PRIMARY KEY,
        dedupe_key     TEXT NOT NULL DEFAULT '',
        source         TEXT NOT NULL DEFAULT '',
        company        TEXT NOT NULL DEFAULT '',
        title          TEXT NOT NULL DEFAULT '',
        location       TEXT NOT NULL DEFAULT '',
        country        TEXT,
        url            TEXT NOT NULL DEFAULT '',
        ats            TEXT,
        ats_job_id     TEXT,
        posted_at      TEXT,
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs (dedupe_key);
    CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs (last_seen_at);

    CREATE TABLE IF NOT EXISTS applications (
        key            TEXT PRIMARY KEY,
        status         TEXT NOT NULL,
        detail         TEXT NOT NULL DEFAULT '',
        score          INTEGER,
        method         TEXT NOT NULL DEFAULT '',
        artifacts_dir  TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        FOREIGN KEY (key) REFERENCES jobs (key)
    );

    CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (status);

    CREATE TABLE IF NOT EXISTS runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        stats_json   TEXT NOT NULL DEFAULT '{}'
    );
    """,
]

# Statuses that mean "this job has been sent somewhere on your behalf".
# `dry_run` is deliberately NOT here: a dry run submits nothing, so the job
# must stay eligible for a real application later.
TERMINAL_APPLY_STATUSES: frozenset[str] = frozenset(
    {ApplyStatus.APPLIED.value}
)

# Statuses that mean "the user has already been shown / handled this".
HANDLED_STATUSES: frozenset[str] = frozenset(
    {
        ApplyStatus.APPLIED.value,
        ApplyStatus.DIGEST.value,
        ApplyStatus.DRY_RUN.value,
        ApplyStatus.APPLY_FAILED.value,
        ApplyStatus.SCORED_BELOW.value,
        ApplyStatus.FILTERED.value,
    }
)


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


class Tracker:
    """Thin, explicit wrapper over sqlite3. Not thread-safe by design."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # -- lifecycle --------------------------------------------------------

    def migrate(self) -> int:
        """Apply any migrations this database has not seen. Idempotent."""
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(current, len(MIGRATIONS)):
            self.conn.executescript(MIGRATIONS[index])
            self.conn.execute(f"PRAGMA user_version = {index + 1}")
        self.conn.commit()
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Tracker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- jobs -------------------------------------------------------------

    def record_job(self, job: Job, *, now: datetime | None = None) -> bool:
        """Upsert a posting. Returns True when this is the first sighting."""
        now = ensure_utc(now) or utcnow()
        stamp = now.isoformat()
        is_new = not self.has_job(job.key)
        self.conn.execute(
            """
            INSERT INTO jobs (key, dedupe_key, source, company, title, location,
                              country, url, ats, ats_job_id, posted_at,
                              first_seen_at, last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                url          = excluded.url,
                location     = CASE WHEN excluded.location != ''
                                    THEN excluded.location ELSE jobs.location END,
                country      = COALESCE(excluded.country, jobs.country),
                posted_at    = COALESCE(jobs.posted_at, excluded.posted_at)
            """,
            (
                job.key, job.dedupe_key, job.source, job.company, job.title,
                job.location, job.country, job.url, job.ats,
                str(job.ats_job_id) if job.ats_job_id is not None else None,
                _iso(job.posted_at), stamp, stamp,
            ),
        )
        self.conn.commit()
        return is_new

    def record_jobs(self, jobs: Iterable[Job], *, now: datetime | None = None) -> int:
        """Bulk `record_job`. Returns how many were new."""
        return sum(1 for job in jobs if self.record_job(job, now=now))

    def has_job(self, key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM jobs WHERE key = ?", (key,)).fetchone()
        return row is not None

    def get_job(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def first_seen(self, key: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT first_seen_at FROM jobs WHERE key = ?", (key,)
        ).fetchone()
        return _parse_iso(row["first_seen_at"]) if row else None

    # -- applications -----------------------------------------------------

    def record_status(
        self,
        key: str,
        status: ApplyStatus | str,
        *,
        detail: str = "",
        score: int | None = None,
        method: str = "",
        artifacts_dir: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write the outcome for a job.

        An `applied` row is never downgraded: once something has really been
        submitted, a later run recording `digest` must not erase that fact.
        """
        status_value = status.value if isinstance(status, ApplyStatus) else str(status)
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        existing = self.get_status(key)
        if existing in TERMINAL_APPLY_STATUSES and status_value not in TERMINAL_APPLY_STATUSES:
            return
        self.conn.execute(
            """
            INSERT INTO applications (key, status, detail, score, method,
                                      artifacts_dir, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                status        = excluded.status,
                detail        = excluded.detail,
                score         = COALESCE(excluded.score, applications.score),
                method        = CASE WHEN excluded.method != ''
                                     THEN excluded.method ELSE applications.method END,
                artifacts_dir = COALESCE(excluded.artifacts_dir,
                                         applications.artifacts_dir),
                updated_at    = excluded.updated_at
            """,
            (key, status_value, detail, score, method, artifacts_dir, stamp, stamp),
        )
        self.conn.commit()

    def get_status(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM applications WHERE key = ?", (key,)
        ).fetchone()
        return row["status"] if row else None

    def get_application(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def has_applied(self, key: str) -> bool:
        """The hard double-apply gate. True == never submit again."""
        return (self.get_status(key) or "") in TERMINAL_APPLY_STATUSES

    def should_surface(self, key: str, *, within_days: int = 30,
                       now: datetime | None = None) -> bool:
        """False when this job was already handled inside the reminder window.

        Applied jobs are never surfaced again, regardless of the window.
        """
        status = self.get_status(key)
        if status is None:
            return True
        if status in TERMINAL_APPLY_STATUSES:
            return False
        if status not in HANDLED_STATUSES:
            return True
        if within_days <= 0:
            return True
        row = self.conn.execute(
            "SELECT updated_at FROM applications WHERE key = ?", (key,)
        ).fetchone()
        updated = _parse_iso(row["updated_at"]) if row else None
        if updated is None:
            return True
        cutoff = (ensure_utc(now) or utcnow()) - timedelta(days=within_days)
        return updated < cutoff

    def applications_by_status(self, status: ApplyStatus | str) -> list[dict[str, Any]]:
        value = status.value if isinstance(status, ApplyStatus) else str(status)
        rows = self.conn.execute(
            """
            SELECT a.*, j.company, j.title, j.url, j.location
            FROM applications a LEFT JOIN jobs j ON j.key = a.key
            WHERE a.status = ? ORDER BY a.updated_at DESC
            """,
            (value,),
        ).fetchall()
        return [dict(r) for r in rows]

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- runs -------------------------------------------------------------

    def start_run(self, *, now: datetime | None = None) -> int:
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (stamp,))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, stats: dict[str, Any],
                   *, now: datetime | None = None) -> None:
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE id = ?",
            (stamp, json.dumps(stats, default=str), run_id),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
