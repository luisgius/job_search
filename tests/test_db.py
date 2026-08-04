"""Tests for src/db.py — the tracker.

This module carries the product's one hard promise: *never apply twice*. The
tests below are written as adversarially as I could manage against that
promise, because a silent regression here means real duplicate applications
sent under the user's name.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from src.db import HANDLED_STATUSES, TERMINAL_APPLY_STATUSES, MIGRATIONS, Tracker
from src.models import ApplyStatus
from tests.conftest import NOW, make_job


# ==========================================================================
# schema
# ==========================================================================


def test_migrate_is_idempotent(tmp_path: Path):
    path = tmp_path / "t.sqlite3"
    with Tracker(path) as t:
        assert t.migrate() == len(MIGRATIONS)
        assert t.migrate() == len(MIGRATIONS)
    # Re-opening an existing file must not re-run migrations destructively.
    with Tracker(path) as t2:
        assert t2.migrate() == len(MIGRATIONS)


def test_reopening_preserves_data(tmp_path: Path):
    path = tmp_path / "t.sqlite3"
    job = make_job()
    with Tracker(path) as t:
        t.record_job(job, now=NOW)
        t.record_status(job.key, ApplyStatus.APPLIED, now=NOW)
    with Tracker(path) as t:
        assert t.has_applied(job.key) is True


def test_creates_parent_directories(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "t.sqlite3"
    with Tracker(path):
        pass
    assert path.exists()


def test_memory_tracker_works(memory_tracker):
    job = make_job()
    assert memory_tracker.record_job(job, now=NOW) is True


# ==========================================================================
# jobs
# ==========================================================================


def test_record_job_reports_first_sighting_only_once(memory_tracker):
    job = make_job()
    assert memory_tracker.record_job(job, now=NOW) is True
    assert memory_tracker.record_job(job, now=NOW) is False
    assert memory_tracker.has_job(job.key) is True


def test_record_jobs_counts_new_ones(memory_tracker):
    batch = [make_job(ats_job_id=str(i)) for i in range(3)]
    assert memory_tracker.record_jobs(batch, now=NOW) == 3
    assert memory_tracker.record_jobs(batch, now=NOW) == 0


def test_repeat_sighting_updates_last_seen_but_keeps_first_seen(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    later = NOW + timedelta(days=2)
    memory_tracker.record_job(job, now=later)

    row = memory_tracker.get_job(job.key)
    assert row["first_seen_at"] == NOW.isoformat()
    assert row["last_seen_at"] == later.isoformat()
    assert memory_tracker.first_seen(job.key) == NOW


def test_upsert_never_overwrites_a_known_posted_at_with_null(memory_tracker):
    """A later source that lost the date must not erase the date we had.

    Greenhouse gives a real `first_published`; a LinkedIn alert for the same
    role may not. Losing the date would make the job look undated and get it
    dropped by the freshness filter forever after.
    """
    dated = make_job(hours_old=3)
    memory_tracker.record_job(dated, now=NOW)
    undated = make_job(hours_old=None)
    assert undated.key == dated.key
    memory_tracker.record_job(undated, now=NOW)

    assert memory_tracker.get_job(dated.key)["posted_at"] == dated.posted_at.isoformat()


def test_upsert_fills_in_a_missing_country(memory_tracker):
    memory_tracker.record_job(make_job(country=None), now=NOW)
    memory_tracker.record_job(make_job(country="DE"), now=NOW)
    assert memory_tracker.get_job(make_job().key)["country"] == "DE"


def test_upsert_does_not_blank_an_existing_location(memory_tracker):
    memory_tracker.record_job(make_job(location="Berlin, Germany"), now=NOW)
    memory_tracker.record_job(make_job(location=""), now=NOW)
    assert memory_tracker.get_job(make_job().key)["location"] == "Berlin, Germany"


def test_get_job_returns_none_for_unknown_key(memory_tracker):
    assert memory_tracker.get_job("nope") is None
    assert memory_tracker.first_seen("nope") is None


# ==========================================================================
# the double-apply guarantee
# ==========================================================================


def test_has_applied_is_false_before_anything_happens(memory_tracker):
    assert memory_tracker.has_applied(make_job().key) is False


def test_has_applied_is_true_only_after_a_real_submission(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.APPLIED, now=NOW)
    assert memory_tracker.has_applied(job.key) is True


@pytest.mark.parametrize(
    "status",
    [ApplyStatus.NEW, ApplyStatus.FILTERED, ApplyStatus.SCORED_BELOW,
     ApplyStatus.DIGEST, ApplyStatus.DRY_RUN, ApplyStatus.APPLY_FAILED],
)
def test_non_submitting_statuses_never_block_a_future_application(memory_tracker, status):
    """Only `applied` may block. A dry run submits nothing, so blocking on it
    would silently prevent the real application the user is preparing for."""
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, status, now=NOW)
    assert memory_tracker.has_applied(job.key) is False


def test_applied_is_never_downgraded(memory_tracker):
    """The regression that would cause a duplicate application.

    Once a job is `applied`, a later run recording `digest` (because the
    tailoring failed, say) must not reopen it for auto-apply.
    """
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.APPLIED, now=NOW)

    for later in (ApplyStatus.DIGEST, ApplyStatus.DRY_RUN,
                  ApplyStatus.SCORED_BELOW, ApplyStatus.APPLY_FAILED,
                  ApplyStatus.NEW, ApplyStatus.FILTERED):
        memory_tracker.record_status(job.key, later, now=NOW + timedelta(days=1))
        assert memory_tracker.get_status(job.key) == ApplyStatus.APPLIED.value
        assert memory_tracker.has_applied(job.key) is True


def test_applied_can_be_re_recorded_as_applied(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.APPLIED, detail="first", now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.APPLIED, detail="second",
                                 now=NOW + timedelta(days=1))
    assert memory_tracker.get_application(job.key)["detail"] == "second"


def test_terminal_statuses_are_exactly_the_two_that_may_have_been_sent():
    """Widening this set silently changes what "already handled" means, so it
    is pinned by name rather than by count.

    `submitted_unconfirmed` belongs here: the submit click happened and the
    page could not be read afterwards, so the employer may well have received
    it. Blocking a possible duplicate beats sending one. `dry_run` never
    belongs — a dry run submits nothing, and blocking on it would prevent the
    real application the user is rehearsing for.
    """
    assert TERMINAL_APPLY_STATUSES == {
        ApplyStatus.APPLIED.value,
        ApplyStatus.SUBMITTED_UNCONFIRMED.value,
    }
    assert ApplyStatus.DRY_RUN.value not in TERMINAL_APPLY_STATUSES
    assert ApplyStatus.APPLY_FAILED.value not in TERMINAL_APPLY_STATUSES


def test_an_unconfirmed_submission_blocks_a_second_one(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.SUBMITTED_UNCONFIRMED, now=NOW)
    assert memory_tracker.has_applied(job.key) is True


# ==========================================================================
# status bookkeeping
# ==========================================================================


def test_record_status_stores_the_details(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.DRY_RUN, detail="form filled",
                                 score=88, method="greenhouse",
                                 artifacts_dir="/tmp/x", now=NOW)
    row = memory_tracker.get_application(job.key)
    assert row["status"] == "dry_run"
    assert row["detail"] == "form filled"
    assert row["score"] == 88
    assert row["method"] == "greenhouse"
    assert row["artifacts_dir"] == "/tmp/x"


def test_record_status_accepts_a_plain_string(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, "digest", now=NOW)
    assert memory_tracker.get_status(job.key) == "digest"


def test_a_later_update_keeps_the_score_and_artifacts_it_is_not_given(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.DIGEST, score=88,
                                 artifacts_dir="/tmp/x", method="lever", now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.DRY_RUN, now=NOW)
    row = memory_tracker.get_application(job.key)
    assert row["score"] == 88
    assert row["artifacts_dir"] == "/tmp/x"
    assert row["method"] == "lever"


def test_get_status_is_none_for_an_untouched_job(memory_tracker):
    assert memory_tracker.get_status("unknown") is None
    assert memory_tracker.get_application("unknown") is None


def test_counts_by_status(memory_tracker):
    for i, status in enumerate([ApplyStatus.DIGEST, ApplyStatus.DIGEST,
                                ApplyStatus.APPLIED]):
        job = make_job(ats_job_id=str(i))
        memory_tracker.record_job(job, now=NOW)
        memory_tracker.record_status(job.key, status, now=NOW)
    assert memory_tracker.counts_by_status() == {"digest": 2, "applied": 1}


def test_applications_by_status_joins_the_job_row(memory_tracker):
    job = make_job(company="Acme", title="Backend Engineer")
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.APPLIED, now=NOW)
    rows = memory_tracker.applications_by_status(ApplyStatus.APPLIED)
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["title"] == "Backend Engineer"


def test_foreign_key_is_enforced(memory_tracker):
    """A status for a job we never recorded is a bug, not data."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        memory_tracker.record_status("never-seen", ApplyStatus.DIGEST, now=NOW)


# ==========================================================================
# should_surface — the digest de-noiser
# ==========================================================================


def test_unknown_job_is_surfaced(memory_tracker):
    assert memory_tracker.should_surface("brand-new", within_days=30, now=NOW) is True


def test_a_job_shown_today_is_not_shown_again_tomorrow(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.DIGEST, now=NOW)
    assert memory_tracker.should_surface(job.key, within_days=30,
                                         now=NOW + timedelta(days=1)) is False


def test_a_job_resurfaces_once_the_window_expires(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.DIGEST, now=NOW)
    assert memory_tracker.should_surface(job.key, within_days=30,
                                         now=NOW + timedelta(days=31)) is True


def test_an_applied_job_never_resurfaces_however_long_you_wait(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_status(job.key, ApplyStatus.APPLIED, now=NOW)
    assert memory_tracker.should_surface(job.key, within_days=30,
                                         now=NOW + timedelta(days=3650)) is False


def test_zero_window_disables_suppression_except_for_applied(memory_tracker):
    shown = make_job(ats_job_id="a")
    applied = make_job(ats_job_id="b")
    for job, status in ((shown, ApplyStatus.DIGEST), (applied, ApplyStatus.APPLIED)):
        memory_tracker.record_job(job, now=NOW)
        memory_tracker.record_status(job.key, status, now=NOW)
    assert memory_tracker.should_surface(shown.key, within_days=0, now=NOW) is True
    assert memory_tracker.should_surface(applied.key, within_days=0, now=NOW) is False


def test_handled_statuses_cover_every_non_new_outcome():
    # If a new ApplyStatus is added and not classified, the digest starts
    # re-showing jobs forever. This test is the tripwire.
    unclassified = {
        s.value for s in ApplyStatus
        if s not in (ApplyStatus.NEW, ApplyStatus.SKIPPED_DUPLICATE)
    } - set(HANDLED_STATUSES)
    assert unclassified == set(), f"unclassified statuses: {unclassified}"


# ==========================================================================
# runs
# ==========================================================================


def test_run_lifecycle(memory_tracker):
    run_id = memory_tracker.start_run(now=NOW)
    assert run_id > 0
    memory_tracker.finish_run(run_id, {"fetched": 10, "matches": 2},
                              now=NOW + timedelta(minutes=3))
    runs = memory_tracker.recent_runs()
    assert len(runs) == 1
    assert runs[0]["finished_at"] is not None
    import json

    assert json.loads(runs[0]["stats_json"])["fetched"] == 10


def test_recent_runs_is_newest_first(memory_tracker):
    first = memory_tracker.start_run(now=NOW)
    second = memory_tracker.start_run(now=NOW + timedelta(hours=24))
    assert [r["id"] for r in memory_tracker.recent_runs()] == [second, first]


def test_recent_runs_honours_the_limit(memory_tracker):
    for i in range(5):
        memory_tracker.start_run(now=NOW + timedelta(hours=i))
    assert len(memory_tracker.recent_runs(limit=2)) == 2


def test_finish_run_serialises_non_json_values(memory_tracker):
    run_id = memory_tracker.start_run(now=NOW)
    # RunStats can carry datetimes; `default=str` must keep this from raising.
    memory_tracker.finish_run(run_id, {"when": NOW}, now=NOW)
    assert "2026-08-04" in memory_tracker.recent_runs()[0]["stats_json"]


# ==========================================================================
# transactions
# ==========================================================================


def test_transaction_rolls_back_on_error(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    with pytest.raises(RuntimeError):
        with memory_tracker.transaction() as conn:
            conn.execute("DELETE FROM jobs WHERE key = ?", (job.key,))
            raise RuntimeError("boom")
    assert memory_tracker.has_job(job.key) is True
