"""SQLite tracker — the thing that stops you applying to the same job twice.

The tracker separates sightings, application history and processing work:

  jobs          every posting we have ever normalised, keyed by `Job.key`
  applications  the outcome per job (digest / dry-run / applied / failed)
  scoring_backlog  bounded pending evidence and durable retry/stop reasons
  submit_attempts  write-ahead protection against repeated submissions
  application_events  what happened after the click (interview, rejection…)

`has_applied()` is the hard gate in front of the auto-apply stage, and
`should_surface()` is the softer gate in front of the digest. Both read
`applications.status` only; the outcome log never moves either gate.

Schema changes go through `MIGRATIONS`; `PRAGMA user_version` tracks where a
database is, so an existing tracker upgrades in place instead of exploding.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urlsplit

from .models import ApplyStatus, Job, ensure_utc, utcnow
from .util import canonical_url, get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 7

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
    # v4 — processing evidence is independent of application outcomes.
    """
    CREATE TABLE scoring_backlog (
        key TEXT PRIMARY KEY REFERENCES jobs(key),
        job_json TEXT,
        state TEXT NOT NULL DEFAULT 'pending',
        queued_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        application_status TEXT,
        application_updated_at TEXT,
        detail TEXT NOT NULL DEFAULT 'pending scoring'
    );
    CREATE INDEX idx_scoring_backlog_due ON scoring_backlog(state, next_attempt_at);
    """,
    # v5 — what happened after the click. `applications.status` stays the
    # apply gate; this is an append-only log of the outcomes recorded by hand
    # (`python -m src.track`) plus the `applied` event `record_status` writes
    # when a row first becomes applied. Rows that were already applied get
    # that event backfilled, dated from `updated_at` — the last time the
    # submission was recorded — so their follow-up clock has a start.
    #
    # Idempotent (IF NOT EXISTS / NOT EXISTS) because the INSERT makes this
    # the first migration that moves data, and `migrate` bumps user_version
    # in a separate statement: a crash between the two must re-run cleanly.
    # The JOIN keeps an orphaned application row from failing the foreign key.
    """
    CREATE TABLE IF NOT EXISTS application_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        key          TEXT NOT NULL REFERENCES jobs(key),
        stage        TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        occurred_on  TEXT NOT NULL,
        recorded_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_application_events_key
        ON application_events (key, occurred_on);
    INSERT INTO application_events (key, stage, note, occurred_on, recorded_at)
    SELECT a.key, 'applied', 'backfilled from status ' || a.status,
           substr(a.updated_at, 1, 10), a.updated_at
    FROM applications a JOIN jobs j ON j.key = a.key
    WHERE a.status IN ('applied', 'submitted_unconfirmed')
      AND NOT EXISTS (SELECT 1 FROM application_events e
                      WHERE e.key = a.key AND e.stage = 'applied');
    """,
    # v6 — was the outcome judged on the full posting (1) or on a snippet
    # (0)? NULL, unknown, for every row recorded before this column, and
    # `handled_twin` reads unknown as "maybe only a snippet".
    """
    ALTER TABLE applications ADD COLUMN full_text INTEGER;
    """,
    # v7 — the scorer's gaps, kept beside its reasons. `python -m src.track
    # gaps` counts which missing skills cost the most jobs; until this column
    # the gaps list was printed in the digest and thrown away. JSON-encoded
    # list, NULL when never scored or scored before v7.
    """
    ALTER TABLE applications ADD COLUMN score_gaps TEXT;
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

# What a *twin* on another board must hold for `handled_twin` to call this
# posting handled: `HANDLED_STATUSES` minus `filtered`. A filter verdict is
# about one copy's data — its date, its location string, its body — and the
# copy in hand passed the filters today, so another board's rejection is no
# evidence anyone has looked at the job. Counting it would drop a passing
# posting unscored; a dropped posting is invisible, a duplicate card costs a
# glance. `scored_below` counts only under the richness rule in
# `handled_twin`: a verdict on a snippet says nothing about the full ad.
TWIN_HANDLED_STATUSES: frozenset[str] = HANDLED_STATUSES - {ApplyStatus.FILTERED.value}

#: What happened after the click, recorded in `application_events`.
#: Deliberately NOT `ApplyStatus` values: `applications.status` stays the one
#: apply gate, and ten outcome words in that column would have every gate
#: (`has_applied`, `should_surface`, the no-downgrade guard) to re-learn.
OUTCOME_STAGES: tuple[str, ...] = (
    "applied", "followed_up", "interview", "offer", "hired",
    "rejected", "no_response", "offer_declined", "withdrawn", "dismissed",
)

#: Stages that close an application. Anything recorded after one needs
#: `force=True`, so a key typed from memory cannot quietly reopen it.
FINAL_STAGES: frozenset[str] = frozenset(
    {"hired", "rejected", "no_response", "offer_declined", "withdrawn", "dismissed"}
)

#: Relevance labels for evaluating the ranking (docs/ROADMAP.md asks for
#: pursue / stretch / dismiss). They judge the posting, not an application:
#: they never imply one, never close one, and are accepted after a final stage.
LABEL_STAGES: tuple[str, ...] = ("label_pursue", "label_stretch", "label_dismiss")

#: Spellings accepted on input; only the right-hand side is ever stored.
#: No bare "dismiss": it could mean the final stage or the label.
STAGE_ALIASES: dict[str, str] = {
    "ghosted": "no_response",
    "no response": "no_response",
    "declined": "offer_declined",
    "followed up": "followed_up",
    "follow up": "followed_up",
    "followup": "followed_up",
    "interviewing": "interview",
    "pursue": "label_pursue",
    "stretch": "label_stretch",
}

#: Stages that do not mean an application went out. Every other stage does —
#: an interview, a rejection or a withdrawal (only a sent application can be
#: withdrawn) is evidence of a submission — so recording one on a job the
#: tracker never saw applied marks it applied, and on an unconfirmed
#: submission confirms it.
NON_SUBMITTING_STAGES: frozenset[str] = frozenset({"dismissed", *LABEL_STAGES})

#: How `filters.collapse_variants` begins the reason recorded on a copy held
#: back for another copy of the same posting, whose key's first eight
#: characters follow in parentheses — a `filtered` row, or a `protected`
#: scoring tombstone.
VARIANT_HOLD = "same_posting_other_city:"
_HOLDER_RE = re.compile(r"\(([0-9a-f]{8})\)")

#: Shortest key prefix `jobs_matching` resolves. Keys are 16 hex characters;
#: with six, a given prefix matching a second job among tens of thousands is
#: already unlikely, and an ambiguous prefix is refused rather than guessed.
MIN_KEY_PREFIX = 6


class OutcomeClosedError(ValueError):
    """An event was refused because the application already has a final stage."""


def normalize_stage(value: Any) -> str:
    """`" Ghosted "` -> `"no_response"`. ValueError for an unknown stage."""
    text = " ".join(str(value or "").strip().lower().replace("-", " ").replace("_", " ").split())
    stage = STAGE_ALIASES.get(text, text.replace(" ", "_"))
    if stage not in OUTCOME_STAGES and stage not in LABEL_STAGES:
        raise ValueError(
            f"unknown stage {value!r} — one of: {', '.join(OUTCOME_STAGES + LABEL_STAGES)}"
        )
    return stage


def _event_day(value: date | str) -> str:
    """A calendar day as `YYYY-MM-DD`. ValueError for anything else."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"expected a YYYY-MM-DD date, got {value!r}") from None


def _summarise(events: list[Mapping[str, Any]], status: str | None,
               updated_at: str | None) -> dict[str, Any]:
    """Where one application stands, from its events in (occurred_on, id) order.

    `followed_up` counts the follow-ups since the current stage began, so an
    interview restarts the budget: a nudge after an interview is a different
    email from the two already sent after applying. Labels are not activity.
    """
    lifecycle = [e for e in events if e["stage"] not in LABEL_STAGES]
    labels = [e["stage"] for e in events if e["stage"] in LABEL_STAGES]
    stage, followed_up = "", 0
    for event in lifecycle:
        if event["stage"] == "followed_up":
            followed_up += 1
        else:
            stage, followed_up = event["stage"], 0
    if not stage and status in TERMINAL_APPLY_STATUSES:
        stage = ApplyStatus.APPLIED.value
    return {
        "stage": stage,
        "followed_up": followed_up,
        "last_activity": (lifecycle[-1]["occurred_on"] if lifecycle
                          else str(updated_at or "")[:10]),
        "closed": bool(lifecycle) and lifecycle[-1]["stage"] in FINAL_STAGES,
        "applied_on": next((e["occurred_on"] for e in lifecycle
                            if e["stage"] == "applied"), ""),
        "label": labels[-1] if labels else "",
    }


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

    # -- scoring work (never writes application or submission state) ------

    def get_scoring(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM scoring_backlog WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def pending_scoring(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM scoring_backlog WHERE state = 'pending' "
            "ORDER BY queued_at, rowid"
        )]

    def enqueue_scoring(self, job: Job, *, max_pending: int = 1000,
                        max_age_days: int = 7, now: datetime | None = None) -> bool:
        """Retain admitted evidence before a cap; never reset an existing retry.

        Stopped rows keep a small reason/timestamp tombstone, but release their
        evidence. This prevents an exhausted job being automatically requeued.
        """
        moment = ensure_utc(now) or utcnow()
        if self.get_scoring(job.key) is not None:
            return False
        count = self.conn.execute(
            "SELECT COUNT(*) FROM scoring_backlog WHERE state = 'pending'"
        ).fetchone()[0]
        if count >= max(0, max_pending):
            return False
        data = job.to_dict()
        data.pop("key")
        data["raw"] = job.raw
        snapshot = json.dumps(data, ensure_ascii=False, default=str)
        # Bound evidence bytes as well as row count; never silently truncate
        # descriptions or requirement metadata before scoring.
        if len(snapshot.encode("utf-8")) > 256 * 1024:
            raise ValueError("scoring evidence exceeds 256 KiB")
        self.record_job(job, now=moment)
        application = self.get_application(job.key) or {}
        self.conn.execute(
            "INSERT INTO scoring_backlog "
            "(key, job_json, queued_at, last_observed_at, expires_at, next_attempt_at, "
            "application_status, application_updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (job.key, snapshot, _iso(moment), _iso(moment),
             _iso(moment + timedelta(days=max(0, max_age_days))), _iso(moment),
             application.get("status"), application.get("updated_at")),
        )
        self.conn.commit()
        return True

    def observe_scoring(self, job: Job, *, now: datetime) -> Job:
        """Refresh evidence, preserving the posting date at queue admission.

        A full description already queued outlives a thinner sighting. When
        today's copy is empty or snippet-only — the job fell out of its
        source's detail-fetch budget this morning — and the stored copy is a
        real body, the stored body and its `snippet_only=False` state are
        kept, so the job is not scored on a teaser it was admitted without.
        """
        row = self.get_scoring(job.key)
        if row is None or row["state"] != "pending":
            raise ValueError("job has no pending scoring work")
        original = json.loads(row["job_json"])
        data = job.to_dict()
        data.pop("key")
        data["posted_at"] = original["posted_at"]
        data["raw"] = job.raw
        stored_raw = original.get("raw") if isinstance(original.get("raw"), dict) else {}
        thin = bool(job.raw.get("snippet_only")) or not job.description.strip()
        full = bool(str(original.get("description") or "").strip()) \
            and not stored_raw.get("snippet_only")
        if thin and full:
            data["description"] = original["description"]
            data["raw"] = {**job.raw, "snippet_only": False}
            if "description_status" in stored_raw:
                data["raw"]["description_status"] = stored_raw["description_status"]
        snapshot = json.dumps(data, ensure_ascii=False, default=str)
        if len(snapshot.encode("utf-8")) > 256 * 1024:
            raise ValueError("scoring evidence exceeds 256 KiB")
        self.conn.execute(
            "UPDATE scoring_backlog SET job_json = ?, last_observed_at = ? "
            "WHERE key = ?", (snapshot, _iso(now), job.key),
        )
        self.conn.commit()
        data["posted_at"] = _parse_iso(data["posted_at"])
        return Job(**data)

    def scoring_detail(self, key: str, detail: str) -> None:
        self.conn.execute("UPDATE scoring_backlog SET detail = ? WHERE key = ?",
                          (detail[:2000], key))
        self.conn.commit()

    def stop_scoring(self, key: str, state: str, detail: str) -> None:
        if state not in {"expired", "exhausted", "ineligible", "protected"}:
            raise ValueError(f"invalid scoring stop state: {state}")
        self.conn.execute(
            "UPDATE scoring_backlog SET state = ?, detail = ?, "
            "job_json = CASE WHEN ? IN ('expired', 'exhausted') THEN job_json ELSE NULL END "
            "WHERE key = ?", (state, detail[:2000], state, key),
        )
        self.conn.commit()

    def begin_scoring(self, key: str, *, retry_hours: int, now: datetime) -> None:
        """Write ahead of the model call so crashes consume a bounded attempt."""
        row = self.get_scoring(key)
        if row is None or row["state"] != "pending":
            raise ValueError("job has no pending scoring work")
        delay = max(1, retry_hours) * 2 ** min(row["attempts"], 10)
        self.conn.execute(
            "UPDATE scoring_backlog SET attempts = attempts + 1, "
            "next_attempt_at = ?, detail = ? WHERE key = ?",
            (_iso(now + timedelta(hours=delay)),
             "scoring attempt started; retry if interrupted", key),
        )
        self.conn.commit()

    def complete_scoring(self, key: str) -> None:
        self.conn.execute("DELETE FROM scoring_backlog WHERE key = ?", (key,))
        self.conn.commit()

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
        score_gaps: list[str] | None = None,
        method: str = "",
        artifacts_dir: str | None = None,
        full_text: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write the outcome for a job.

        An `applied` row is never downgraded: once something has really been
        submitted, a later run recording `digest` must not erase that fact.
        `score_reasons` is the calibration data — JSON-encoded, and like the
        score itself never erased by a later reason-less recording; the same
        holds for `score_gaps`.
        `full_text` says whether the copy judged had its full description;
        None keeps what is stored.

        A row that becomes applied (or unconfirmed) for the first time also
        starts its outcome log with an `applied` event, so an auto-applied
        job gets a follow-up clock without anyone typing a command.
        """
        status_value = status.value if isinstance(status, ApplyStatus) else str(status)
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        existing = self.get_status(key)
        if existing in TERMINAL_APPLY_STATUSES and status_value not in TERMINAL_APPLY_STATUSES:
            return
        self._write_status(key, status_value, detail=detail, score=score,
                           score_reasons=score_reasons, score_gaps=score_gaps,
                           method=method,
                           artifacts_dir=artifacts_dir, full_text=full_text, stamp=stamp)
        self.conn.commit()
        if status_value in TERMINAL_APPLY_STATUSES and existing not in TERMINAL_APPLY_STATUSES:
            # After the commit on purpose: the row above is the apply gate,
            # and nothing about the outcome log may ever cost it.
            try:
                if not self._has_event(key, "applied"):
                    note = f"{status_value} via {method}" if method else status_value
                    self._insert_event(key, "applied", note, stamp[:10], stamp)
                    self.conn.commit()
            except sqlite3.Error as exc:
                self.conn.rollback()
                logger.warning("could not start the outcome log for %s: %s", key, exc)

    def _write_status(self, key: str, status_value: str, *, detail: str = "",
                      score: int | None = None, score_reasons: list[str] | None = None,
                      score_gaps: list[str] | None = None,
                      method: str = "", artifacts_dir: str | None = None,
                      full_text: bool | None = None, stamp: str) -> None:
        """The `applications` upsert alone: no downgrade check, no commit."""
        reasons_json = (
            json.dumps([str(r) for r in score_reasons], ensure_ascii=False)
            if score_reasons else None
        )
        gaps_json = (
            json.dumps([str(g) for g in score_gaps], ensure_ascii=False)
            if score_gaps else None
        )
        self.conn.execute(
            """
            INSERT INTO applications (key, status, detail, score, score_reasons,
                                      score_gaps, method, artifacts_dir, full_text,
                                      created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                status        = excluded.status,
                detail        = excluded.detail,
                score         = COALESCE(excluded.score, applications.score),
                score_reasons = COALESCE(excluded.score_reasons,
                                         applications.score_reasons),
                score_gaps    = COALESCE(excluded.score_gaps, applications.score_gaps),
                method        = CASE WHEN excluded.method != ''
                                     THEN excluded.method ELSE applications.method END,
                artifacts_dir = COALESCE(excluded.artifacts_dir,
                                         applications.artifacts_dir),
                full_text     = COALESCE(excluded.full_text, applications.full_text),
                updated_at    = excluded.updated_at
            """,
            (key, status_value, detail, score, reasons_json, gaps_json, method,
             artifacts_dir,
             None if full_text is None else int(bool(full_text)), stamp, stamp),
        )

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

    def has_applied_url(self, url: str) -> bool:
        """True when a posting at this URL was applied to, or had submit
        clicked, under **any** key.

        URLs compare as `canonical_url`, so `?gh_src=` and other tracking
        clutter do not matter. This is what stops the board's copy of a job
        recorded by hand (`python -m src.track add`) under a key the board
        never produces — the company typed differently from the board slug,
        "Warszawa" for "Warsaw".
        """
        target = canonical_url(url)
        if not target:
            return False
        placeholders = ",".join("?" * len(TERMINAL_APPLY_STATUSES))
        rows = self.conn.execute(
            f"""
            SELECT j.url FROM applications a JOIN jobs j ON j.key = a.key
            WHERE a.status IN ({placeholders})
            UNION ALL
            SELECT j.url FROM submit_attempts s JOIN jobs j ON j.key = s.key
            UNION ALL
            SELECT url FROM submit_attempts
            """,
            tuple(sorted(TERMINAL_APPLY_STATUSES)),
        ).fetchall()
        return any(canonical_url(row[0]) == target for row in rows)

    def handled_twin(
        self,
        dedupe_key: str,
        *,
        key: str,
        source: str | None = None,
        within_days: int = 30,
        now: datetime | None = None,
        full_text: bool | None = None,
    ) -> dict[str, Any] | None:
        """The same role, already handled through **another board**.

        `should_surface` is keyed on `Job.key`, and in-run `filters.dedupe`
        only sees one morning. So a role scored from Greenhouse on Monday came
        back on Tuesday as the Arbeitnow copy — new key, longer description,
        so it won the dedupe — and was scored and put in the digest again.

        A row counts when it shares this non-empty `dedupe_key` under a
        different `key` **and a different `source`**, and holds a
        `TWIN_HANDLED_STATUSES` status inside the window — same window rule as
        `should_surface`: a submission counts forever, anything else only
        within `within_days`, and `within_days <= 0` leaves only submissions.
        Same-source rows are deliberately ignored: a new id on the same board
        is a re-listing, which `repost_gap_days` exists to flag, not to hide.

        A `scored_below` twin counts only when the copy in hand is no richer:
        it is itself a snippet (`full_text=False`), or the twin was scored on
        its full description. A below-threshold verdict on an aggregator's
        teaser must not keep the full ATS record from ever being read, and a
        twin of unknown richness (recorded before that was stored) is
        treated as a teaser. `full_text=None` means the copy in hand is
        unknown, so it may be the richer one.

        `source=` falls back to the stored row for `key`, as in
        `repost_gap_days`. Returns the twin's `key`, `source`, `status` and
        `updated_at` (a submission first, then the most recent), or None.
        """
        clean = str(dedupe_key or "").strip()
        own = str(key or "")
        if not clean:
            return None
        placeholders = ",".join("?" * len(TWIN_HANDLED_STATUSES))
        rows = self.conn.execute(
            f"""
            SELECT j.key, j.source, a.status, a.updated_at, a.full_text
            FROM jobs j JOIN applications a ON a.key = j.key
            WHERE j.dedupe_key = ? AND j.key != ? AND a.status IN ({placeholders})
            """,
            (clean, own, *sorted(TWIN_HANDLED_STATUSES)),
        ).fetchall()

        own_source = _source_key(source)
        if not own_source:
            mine = self.get_job(own)
            own_source = _source_key(mine["source"]) if mine is not None else ""
        cutoff = (ensure_utc(now) or utcnow()) - timedelta(days=within_days)

        best: tuple[bool, datetime] | None = None
        found: dict[str, Any] | None = None
        for row in rows:
            if _source_key(row["source"]) == own_source:
                continue
            if row["status"] == ApplyStatus.SCORED_BELOW.value \
                    and full_text is not False and row["full_text"] != 1:
                continue  # judged on less than the copy in hand may hold
            terminal = row["status"] in TERMINAL_APPLY_STATUSES
            updated = _parse_iso(row["updated_at"])
            # Unparseable or out-of-window rows do not block, exactly as
            # `should_surface` treats its own row.
            if not terminal and (within_days <= 0 or updated is None or updated < cutoff):
                continue
            # `max` in Python, not SQL: ISO strings with and without
            # microseconds do not sort chronologically as text.
            rank = (terminal, updated or datetime.min.replace(tzinfo=cutoff.tzinfo))
            if best is None or rank > best:
                best, found = rank, dict(row)
        return found

    def _spoken_for(self, key: str, dedupe_key: str) -> bool:
        """Applied to, submit clicked, or the same role applied to."""
        return (self.has_applied(key) or self.submit_attempted(key)
                or self.has_applied_similar(dedupe_key))

    def release_variant_copies(self, key: str) -> int:
        """Let the copies held back as `key`'s city variants be judged again.

        `filters.collapse_variants` keeps one copy of a posting listed per
        city and holds the others back (`VARIANT_HOLD` naming `key`). That
        is only right while `key` is going to be scored. When its scoring
        work stops without a score (expired, exhausted, ineligible), those
        holds are deleted, so each copy's next sighting is new work instead
        of waiting out the reminder window.

        Never when `key` was applied to, had submit clicked, or shares its
        role with an application — then the copies stay held back for good
        reason — and never for a copy with a submit attempt of its own.
        Returns how many copies were released.
        """
        job = self.get_job(key)
        if job is None or self._spoken_for(key, job["dedupe_key"]):
            return 0
        held = (f"FROM applications WHERE status = '{ApplyStatus.FILTERED.value}' "
                "AND detail LIKE :pattern AND key != :key "
                "AND key NOT IN (SELECT key FROM submit_attempts)")
        stopped = ("FROM scoring_backlog WHERE state = 'protected' "
                   "AND detail LIKE :pattern AND key != :key "
                   "AND key NOT IN (SELECT key FROM submit_attempts)")
        params = {"pattern": f"{VARIANT_HOLD}%({key[:8]})%", "key": key}
        with self.transaction() as conn:
            released = {row[0] for row in conn.execute(
                f"SELECT key {held} UNION SELECT key {stopped}", params)}
            conn.execute(f"DELETE {held}", params)
            conn.execute(f"DELETE {stopped}", params)
        return len(released)

    def settle_variant_hold(self, key: str, *, within_days: int = 30,
                            now: datetime | None = None) -> str | None:
        """Re-judge `key`'s own `VARIANT_HOLD` now that it won a collapse.

        The copy it was held back for was not seen today, or no longer
        outranks it. Returns:

        * None — `key` holds no such `filtered` row;
        * "applied" — that copy was applied to (or clicked, or the role):
          the hold must outlive the reminder window, so the caller renews
          it rather than let the gate's window expire it;
        * "held" — that copy was handled inside the window: the posting
          was shown, and the hold stands for the gate to read;
        * "released" — otherwise. The hold is deleted, so the gate judges
          `key` as new, and that copy's pending scoring work becomes a
          `protected` hold naming `key`: one copy of a posting in flight at
          a time, and `release_variant_copies(key)` hands the work back if
          `key` is never scored. Without this the posting waited out the
          window while it aged past the freshness filter.
        """
        application = self.get_application(key)
        if application is None or application["status"] != ApplyStatus.FILTERED.value \
                or not str(application["detail"] or "").startswith(VARIANT_HOLD):
            return None
        prefixes = _HOLDER_RE.findall(application["detail"])
        if not prefixes:
            return None
        holders = [dict(row) for row in self.conn.execute(
            "SELECT key, dedupe_key FROM jobs WHERE substr(key, 1, 8) = ? AND key != ?",
            (prefixes[-1], key),  # the key follows the place, which is free text
        )]
        if any(self._spoken_for(row["key"], row["dedupe_key"]) for row in holders):
            return "applied"
        for row in holders:
            status = self.get_status(row["key"])
            if status not in (None, ApplyStatus.FILTERED.value) and not self.should_surface(
                    row["key"], within_days=within_days, now=now):
                return "held"
        place = str((self.get_job(key) or {}).get("location") or "").split(",")[0].strip()
        detail = f"{VARIANT_HOLD} the copy listed for {place or 'no location'} " \
                 f"({key[:8]}) stands in for it"
        with self.transaction() as conn:
            conn.execute("DELETE FROM applications WHERE key = ? AND status = ?",
                         (key, ApplyStatus.FILTERED.value))
            conn.executemany(
                "UPDATE scoring_backlog SET state = 'protected', detail = ?, job_json = NULL "
                "WHERE key = ? AND state = 'pending'",
                [(detail, row["key"]) for row in holders],
            )
        return "released"

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

    # -- outcomes (what happened after the click) -------------------------

    def record_event(self, key: str, stage: str, *, on: date | str,
                     note: str = "", force: bool = False,
                     now: datetime | None = None) -> str:
        """Append one outcome event for `key`; returns the stage as stored.

        A stage that means an application went out (anything outside
        `NON_SUBMITTING_STAGES`) also tells the apply gate: a row not already
        applied is upgraded through `record_status(APPLIED, method="manual")`,
        so `has_applied`, `has_applied_similar` and `should_surface` block it
        exactly as they block an auto-application. Its `applied` event is
        dated `on` as well — the latest the submission can have happened.
        Any such stage on an unconfirmed submission confirms it (status
        `applied`, no second `applied` event): a recruiter's answer is proof
        of receipt. `applied` on a row already carrying one writes nothing
        new beyond that.

        Events and status change in one transaction: a failed status write
        leaves no event claiming an application the gate does not know.

        After a final stage further events raise `OutcomeClosedError` unless
        `force` (labels excepted). ValueError for an unknown key, stage or date.
        """
        stage_value = normalize_stage(stage)
        day = _event_day(on)
        if not self.has_job(key):
            raise ValueError(f"no job with key {key!r} in the tracker")
        events = self._events(key)
        if stage_value not in LABEL_STAGES and not force:
            summary = _summarise(events, None, None)
            if summary["closed"]:
                raise OutcomeClosedError(
                    f"already closed as {summary['stage']} on "
                    f"{summary['last_activity']}; recording {stage_value} needs force"
                )
        stamp = (ensure_utc(now) or utcnow()).isoformat()
        with self.transaction():
            if stage_value not in NON_SUBMITTING_STAGES:
                implied = "" if stage_value == "applied" else f"implied by {stage_value}"
                if not any(event["stage"] == "applied" for event in events):
                    self._insert_event(key, "applied", implied or note, day, stamp)
                status = self.get_status(key)
                if status not in TERMINAL_APPLY_STATUSES:
                    self._write_status(key, ApplyStatus.APPLIED.value, method="manual",
                                       detail=implied or "recorded by hand", stamp=stamp)
                elif status == ApplyStatus.SUBMITTED_UNCONFIRMED.value:
                    confirmed = "receipt confirmed by hand" + (f" ({implied})" if implied else "")
                    self._write_status(key, ApplyStatus.APPLIED.value, detail=confirmed,
                                       stamp=stamp)
                if stage_value == "applied":
                    return stage_value
            self._insert_event(key, stage_value, note, day, stamp)
        return stage_value

    def _events(self, key: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "SELECT stage, note, occurred_on, recorded_at FROM application_events "
            "WHERE key = ? ORDER BY occurred_on, id", (key,)
        )]

    def _has_event(self, key: str, stage: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM application_events WHERE key = ? AND stage = ? LIMIT 1",
            (key, stage),
        ).fetchone()
        return row is not None

    def _insert_event(self, key: str, stage: str, note: str, day: str, stamp: str) -> None:
        self.conn.execute(
            "INSERT INTO application_events (key, stage, note, occurred_on, recorded_at) "
            "VALUES (?,?,?,?,?)",
            (key, stage, str(note or "")[:2000], day, stamp),
        )

    def scored_applications(self, since: str = "") -> list[dict[str, Any]]:
        """Every job the model scored, recorded on or after the ISO timestamp
        `since`: company, title, source, score, status and the decoded
        `reasons` / `gaps` lists (empty when not stored)."""
        rows = self.conn.execute(
            """
            SELECT j.key, j.company, j.title, j.source, a.status, a.score,
                   a.score_reasons, a.score_gaps, a.updated_at
            FROM applications a JOIN jobs j ON j.key = a.key
            WHERE a.score IS NOT NULL AND a.updated_at >= ?
            ORDER BY a.updated_at
            """,
            (since,),
        ).fetchall()
        scored = []
        for row in rows:
            item = dict(row)
            for column, name in (("score_reasons", "reasons"), ("score_gaps", "gaps")):
                try:
                    decoded = json.loads(item.pop(column) or "[]")
                except (TypeError, ValueError):
                    decoded = []
                item[name] = [str(entry) for entry in decoded] \
                    if isinstance(decoded, list) else []
            scored.append(item)
        return scored

    def tracked_applications(self) -> list[dict[str, Any]]:
        """Every job with an outcome on record, oldest activity first.

        Applied-type rows plus any job with an event (a dismissal or a label
        on a digest card). Each dict carries the job's company/title/location/
        url/source, its application `status`, and the `_summarise` fields:
        `stage`, `followed_up`, `last_activity`, `closed`, `applied_on`, `label`.
        """
        placeholders = ",".join("?" * len(TERMINAL_APPLY_STATUSES))
        rows = self.conn.execute(
            f"""
            SELECT j.key, j.company, j.title, j.location, j.url, j.source,
                   a.status, a.updated_at
            FROM jobs j LEFT JOIN applications a ON a.key = j.key
            WHERE a.status IN ({placeholders})
               OR j.key IN (SELECT key FROM application_events)
            """,
            tuple(sorted(TERMINAL_APPLY_STATUSES)),
        ).fetchall()
        events: dict[str, list[dict[str, Any]]] = {}
        for row in self.conn.execute(
            "SELECT key, stage, note, occurred_on FROM application_events "
            "ORDER BY key, occurred_on, id"
        ):
            events.setdefault(row["key"], []).append(dict(row))
        tracked = []
        for row in rows:
            item = dict(row)
            item.update(_summarise(events.get(row["key"], []), row["status"], row["updated_at"]))
            tracked.append(item)
        tracked.sort(key=lambda item: (item["last_activity"], item["key"]))
        return tracked

    def open_applications(self) -> list[dict[str, Any]]:
        """Applied-type rows whose latest outcome is not final — the
        follow-up list. Read-only; the digest calls it on every run."""
        return [row for row in self.tracked_applications()
                if row["status"] in TERMINAL_APPLY_STATUSES and not row["closed"]]

    def outcome_events(self) -> list[dict[str, Any]]:
        """Every event with its job, in (key, occurred_on) order — the
        funnel's input and the export's rows."""
        return [dict(row) for row in self.conn.execute(
            """
            SELECT e.key, e.stage, e.note, e.occurred_on, e.recorded_at,
                   j.source, j.company, j.title, j.location, j.url, a.status
            FROM application_events e JOIN jobs j ON j.key = e.key
            LEFT JOIN applications a ON a.key = e.key
            ORDER BY e.key, e.occurred_on, e.id
            """
        )]

    def jobs_matching(self, handle: str) -> list[dict[str, Any]]:
        """The jobs a hand-typed handle could mean: an exact key, a posting
        URL, or a key prefix of at least `MIN_KEY_PREFIX` characters. URLs
        compare as `canonical_url`, so a link copied from the browser with
        `?gh_src=` still finds the board's record.
        More than one row is an ambiguity the caller must refuse, not resolve."""
        text = str(handle or "").strip()
        if not text:
            return []
        columns = "key, company, title, location, url, source"
        row = self.conn.execute(f"SELECT {columns} FROM jobs WHERE key = ?", (text,)).fetchone()
        if row is not None:
            return [dict(row)]
        target = canonical_url(text) if "://" in text else ""
        if target:
            # The path survives canonicalisation byte for byte: a cheap prefilter.
            path = urlsplit(target).path
            rows = [r for r in self.conn.execute(
                f"SELECT {columns} FROM jobs WHERE url = ? OR instr(url, ?) > 0 "
                "ORDER BY first_seen_at, key", (text, path),
            ) if r["url"] == text or canonical_url(r["url"]) == target]
        else:
            rows = self.conn.execute(
                f"SELECT {columns} FROM jobs WHERE url = ? ORDER BY first_seen_at, key", (text,)
            ).fetchall()
        if rows or "://" in text or len(text) < MIN_KEY_PREFIX:
            return [dict(r) for r in rows]
        prefix = text.lower()
        return [dict(r) for r in self.conn.execute(
            f"SELECT {columns} FROM jobs WHERE substr(key, 1, ?) = ? ORDER BY key LIMIT 50",
            (len(prefix), prefix),
        )]

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
