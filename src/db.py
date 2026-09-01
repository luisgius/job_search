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

SCHEMA_VERSION = 3

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
    # v2 — write-ahead record of submit clicks.
    #
    # The outcome row is written *after* the click, so a browser that dies
    # between "the POST left the machine" and "we read the confirmation"
    # leaves behind `apply_failed`, which deliberately does not block — and
    # tomorrow's run submits a second application. This table records the
    # intent *before* the click instead, and is cleared again only when the
    # run can see for itself that the form rejected the submission.
    #
    # Deliberately no foreign key onto `jobs`: this row is the safety record,
    # and it must be writable even when everything else about the job is
    # missing or inconsistent.
    """
    CREATE TABLE IF NOT EXISTS submit_attempts (
        key           TEXT PRIMARY KEY,
        url           TEXT NOT NULL DEFAULT '',
        method        TEXT NOT NULL DEFAULT '',
        attempted_at  TEXT NOT NULL
    );
    """,
    # v3 — the scorer's reasons, kept. The score alone cannot calibrate the
    # threshold ("was 63 really worse than 67?"); the reasons are the
    # calibration data, and until this column they were printed once in the
    # digest and thrown away. JSON-encoded list, NULL when never scored.
    """
    ALTER TABLE applications ADD COLUMN score_reasons TEXT;
    """,
]

# Statuses that mean "this job has been sent somewhere on your behalf".
# `dry_run` is deliberately NOT here: a dry run submits nothing, so the job
# must stay eligible for a real application later.
TERMINAL_APPLY_STATUSES: frozenset[str] = frozenset(
    {ApplyStatus.APPLIED.value, ApplyStatus.SUBMITTED_UNCONFIRMED.value}
)

# Statuses that mean "the user has already been shown / handled this".
HANDLED_STATUSES: frozenset[str] = frozenset(
    {
        ApplyStatus.APPLIED.value,
        ApplyStatus.SUBMITTED_UNCONFIRMED.value,
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


def _source_key(value: Any) -> str:
    """A `source` name in comparable form — trimmed and case-folded."""
    return str(value or "").strip().lower()


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

    def backup(self, *, keep: int = 14, now: datetime | None = None) -> Path | None:
        """One dated copy per day into `<db dir>/backups/`, oldest pruned.

        The tracker is the single record of what was applied to — the one
        file in `output/` that cannot be regenerated. sqlite3's backup API
        copies a *consistent* snapshot even mid-transaction, which a plain
        file copy of a WAL database does not guarantee. Same-day re-runs
        reuse the day's file (the daily granularity is the point: yesterday's
        state survives today's mistake). Returns the path, or None when
        nothing was written (in-memory tracker, or today's copy exists).
        """
        if self.path == ":memory:":
            return None
        stamp = (ensure_utc(now) or utcnow()).strftime("%Y-%m-%d")
        directory = Path(self.path).parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"tracker-{stamp}.sqlite3"
        if target.exists():
            return None
        # `Connection.backup` retries BUSY in an unbounded loop, and this
        # connection's own open write transaction (a failed statement leaves
        # its implicit one dangling) holds that lock forever — the copy would
        # hang the run, not fail it. Every Tracker write commits on success,
        # so there is nothing here a caller meant to roll back.
        self.conn.commit()
        # No context manager: `with` on a sqlite3 connection commits but does
        # not close, and a torn file left behind by a failed copy would claim
        # the day (`target.exists()` above) — a same-day retry would then keep
        # unreadable garbage as the backup of the one unregenerable file.
        copy = sqlite3.connect(target)
        try:
            self.conn.backup(copy)
        except BaseException:
            copy.close()
            try:
                target.unlink()
            except OSError:
                pass  # the caller still hears the original failure
            raise
        copy.close()
        backups = sorted(directory.glob("tracker-*.sqlite3"))
        for stale in backups[:-max(1, int(keep))]:
            try:
                stale.unlink()
            except OSError:
                pass  # a survivor costs disk, not correctness
        return target

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
                -- Refreshed on every sighting, not frozen at the first one.
                -- `dedupe_key` is derived from company/title/city, so a
                -- location filled in on day two — or a relocation — changes
                -- it. `has_applied_similar` reads this column, so a stale
                -- value meant a repost under a new ATS id sailed through the
                -- gate and a second application went out.
                dedupe_key   = CASE WHEN excluded.dedupe_key != ''
                                    THEN excluded.dedupe_key ELSE jobs.dedupe_key END,
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

    def repost_gap_days(
        self,
        dedupe_key: str,
        *,
        key: str,
        source: str | None = None,
        posted_at: datetime | None = None,
        now: datetime | None = None,
    ) -> float | None:
        """How long this role had already been listed **on this same board**
        before *this* listing.

        A recruiter who closes a requisition and opens it again gets a new
        `ats_job_id`, therefore a new `Job.key` — but `dedupe_key` (company +
        title + city) does not move. So a row carrying this `dedupe_key` under
        a **different** `key` is the same role, listed before. This is the only
        ghost-job signal the pipeline has: posting age cannot be one, because
        every posting that reaches a card came through `filters.is_fresh` and
        is younger than `freshness.max_age_hours` by construction.

        Same shape as `has_applied_similar`: one indexed lookup on
        `dedupe_key`, no network, no cost. It reads only the `jobs` table and
        **nothing acts on the answer except a flag on a digest card.**

        Returns the days between the earliest qualifying earlier sighting and
        this one, or `None` when there is none. The value is signed: negative
        means the only other rows are *newer* than this listing, which is
        evidence of nothing. **The caller applies the threshold** — the gap is
        a measurement, whether it is wide enough to call a repost is policy,
        and policy lives in the config (`freshness.repost_min_gap_days`).

        **Only same-source sightings count**, and that is what keeps this
        usable. A different `source` on the same `dedupe_key` has at least four
        readings and only one of them is a repost:

        * an **ATS migration** — Greenhouse to Ashby — re-lists a company's
          entire board under new ids on one day. Counting those accused every
          open role at that employer at once, which is not a heuristic, it is
          a slander generator;
        * an **aggregator re-dating a live posting**: Adzuna's `created` is its
          own ingest time, so the same job can arrive months "after" itself;
        * a plain **cross-source duplicate**, one live job reaching us from two
          places. The gap alone was supposed to separate this one, and it does
          when both sources agree on the date — but that is exactly what an
          aggregator does not do;
        * and, occasionally, a real repost we now miss. That is the trade, and
          it is the right way round: a missed flag costs a glance, a wrong one
          accuses a named employer.

        `source=` names this listing's board. Pass it — the caller is holding
        the `Job`. Omitting it falls back to the stored row for `key`, which is
        right for a job the tracker already knows and silently wrong for one it
        does not.

        `now=` is accepted and, since the undated case above returns early,
        no longer read. It stays on the signature deliberately: every
        time-dependent function in this codebase takes an injectable clock
        (see `docs/TESTING.md`), and one method quietly opting out of that
        convention is how the next change reaches for `utcnow()` instead.

        Which timestamps, and why. This is the part worth reading:

        * A **prior** row contributes `posted_at` when it has one and
          `first_seen_at` when it does not. `posted_at` is the employer's own
          claim about when the listing went up, which is exactly the quantity
          being compared. `first_seen_at` is *our* first sight of it, which can
          never be earlier than the posting itself — so substituting it can
          only shrink the gap, never inflate it. Shrinking is the safe
          direction for a flag that would otherwise fire on a healthy posting.
        * The **current** listing gets no such substitution, because there the
          same trick runs the other way: replacing this listing's missing date
          with `first_seen_at` — or worse, with `now()` — moves the reference
          *later* and inflates the gap. An undated posting that has simply been
          open for 200 days then reads identically to a role genuinely
          re-listed today, opposite ground truths, same number. So an undated
          current listing returns `None`: no date, no measurement. Reachable
          whenever `freshness.skip_undated` is false, which is a documented
          setting.
        * Both stored values are stable per row. `record_job` freezes
          `posted_at` with COALESCE and never rewrites `first_seen_at`, so both
          mean "when this listing first appeared" and neither drifts as the
          same row is re-fetched every morning. That stability is what makes
          two rows comparable at all.
        * `last_seen_at` is deliberately **not** used, though "was the old
          listing still live?" sounds like the sharper question. It moves for
          reasons that have nothing to do with the employer: a weekend with no
          cron run, a watchlist edit, a board outage or a narrowed country
          filter all freeze it. Our own gaps in observation would then read as
          the employer closing the requisition.

        What is left over is irreducible: a company that failed to fill a role
        in six months and honestly re-advertises it looks, from outside,
        exactly like a ghost job. The card's wording carries that hedge rather
        than pretending the data settles it.
        """
        clean = str(dedupe_key or "").strip()
        own = str(key or "")
        if not clean:
            return None

        # Reference for *this* listing: what the caller knows, else what the
        # tracker froze on the row for this key. No third fallback — see the
        # docstring; the ones on offer inflate.
        reference = ensure_utc(posted_at)
        if reference is None:
            row = self.conn.execute(
                "SELECT posted_at FROM jobs WHERE key = ?", (own,)
            ).fetchone()
            reference = _parse_iso(row["posted_at"]) if row else None
        if reference is None:
            return None

        rows = self.conn.execute(
            "SELECT key, source, posted_at, first_seen_at FROM jobs WHERE dedupe_key = ?",
            (clean,),
        ).fetchall()

        def appeared(row: sqlite3.Row) -> datetime | None:
            return _parse_iso(row["posted_at"]) or _parse_iso(row["first_seen_at"])

        # Which board this listing is on. The caller normally knows (it is
        # holding the `Job`); falling back to the stored row covers a caller
        # that does not, and `record_job` never rewrites `source` on conflict,
        # so the stored value names the board this key was first seen on and
        # does not drift underneath us.
        own_source = _source_key(source)
        if not own_source:
            mine = next((r for r in rows if r["key"] == own), None)
            own_source = _source_key(mine["source"]) if mine is not None else ""

        # `min()` in Python rather than `MIN()` in SQL: ISO strings with and
        # without microseconds do not sort chronologically as text.
        earlier = [
            appeared(row)
            for row in rows
            if row["key"] != own and _source_key(row["source"]) == own_source
        ]
        earlier = [moment for moment in earlier if moment is not None]
        if not earlier:
            return None

        return (reference - min(earlier)).total_seconds() / 86400.0

    # -- applications -----------------------------------------------------

    def record_status(
        self,
        key: str,
        status: ApplyStatus | str,
        *,
        detail: str = "",
        score: int | None = None,
        score_reasons: list[str] | None = None,
        method: str = "",
        artifacts_dir: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write the outcome for a job.

        An `applied` row is never downgraded: once something has really been
        submitted, a later run recording `digest` must not erase that fact.
        `score_reasons` is the calibration data — JSON-encoded, and like the
        score itself never erased by a later reason-less recording.
        """
        status_value = status.value if isinstance(status, ApplyStatus) else str(status)
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        existing = self.get_status(key)
        if existing in TERMINAL_APPLY_STATUSES and status_value not in TERMINAL_APPLY_STATUSES:
            return
        reasons_json = (
            json.dumps([str(r) for r in score_reasons], ensure_ascii=False)
            if score_reasons else None
        )
        self.conn.execute(
            """
            INSERT INTO applications (key, status, detail, score, score_reasons,
                                      method, artifacts_dir, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                status        = excluded.status,
                detail        = excluded.detail,
                score         = COALESCE(excluded.score, applications.score),
                score_reasons = COALESCE(excluded.score_reasons,
                                         applications.score_reasons),
                method        = CASE WHEN excluded.method != ''
                                     THEN excluded.method ELSE applications.method END,
                artifacts_dir = COALESCE(excluded.artifacts_dir,
                                         applications.artifacts_dir),
                updated_at    = excluded.updated_at
            """,
            (key, status_value, detail, score, reasons_json, method,
             artifacts_dir, stamp, stamp),
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

    def has_applied_similar(self, dedupe_key: str) -> bool:
        """True when *some* posting of the same role was already applied to.

        `has_applied` is keyed on the ATS id, so a recruiter closing and
        re-opening a requisition — same company, same title, same city, new id
        — reads as a brand new job and earns the employer a second application
        a week after the first. `dedupe_key` is the identity that survives
        that, and the tracker already stores and indexes it.

        Blunter than `has_applied` on purpose, and blunt in the safe
        direction: the false positive is one job the user applies to by hand,
        the false negative is a duplicate application they cannot unsend.
        """
        key = str(dedupe_key or "").strip()
        if not key:
            return False
        placeholders = ",".join("?" * len(TERMINAL_APPLY_STATUSES))
        row = self.conn.execute(
            f"""
            SELECT 1 FROM applications a JOIN jobs j ON j.key = a.key
            WHERE j.dedupe_key = ? AND a.status IN ({placeholders})
            LIMIT 1
            """,
            (key, *sorted(TERMINAL_APPLY_STATUSES)),
        ).fetchone()
        return row is not None

    # -- submit attempts --------------------------------------------------

    def record_submit_attempt(
        self,
        key: str,
        *,
        url: str = "",
        method: str = "",
        now: datetime | None = None,
    ) -> None:
        """Note that submit is about to be clicked, before it is.

        Written ahead of the click precisely so it survives the click failing
        to come back. `clear_submit_attempt` is the only thing that removes
        it, and only on positive evidence that nothing was sent.
        """
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        self.conn.execute(
            """
            INSERT INTO submit_attempts (key, url, method, attempted_at)
            VALUES (?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                url          = excluded.url,
                method       = excluded.method,
                attempted_at = excluded.attempted_at
            """,
            (key, str(url or ""), str(method or ""), stamp),
        )
        self.conn.commit()

    def clear_submit_attempt(self, key: str) -> None:
        """Forget a submit click that demonstrably sent nothing."""
        self.conn.execute("DELETE FROM submit_attempts WHERE key = ?", (key,))
        self.conn.commit()

    def submit_attempted(self, key: str) -> bool:
        """True when a previous run clicked submit and never cleared it."""
        row = self.conn.execute(
            "SELECT 1 FROM submit_attempts WHERE key = ?", (key,)
        ).fetchone()
        return row is not None

    def submit_attempt(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM submit_attempts WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

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
