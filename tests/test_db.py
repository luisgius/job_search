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
# repost_gap_days — "has this role been listed before, and how long ago?"
# ==========================================================================
#
# The measurement behind the digest's repost flag. It is only ever used to
# annotate a card, never to drop one, so every test below is about *accuracy*
# rather than safety — but accuracy here is worth as much as the double-apply
# tests are, in the other direction: this flag accuses a named employer of
# advertising a job it is not filling, and a heuristic that indicts healthy
# postings is worse than no heuristic at all.


def cross_source_twin(**overrides):
    """The same posting as `make_job()`, as an aggregator would hand it over.

    No ATS id, so `Job.key` falls back to company/title/location and differs —
    while `dedupe_key` (company + title + city) is identical. That is exactly
    the shape a re-listing has, which is the whole difficulty.
    """
    return make_job(source="adzuna", ats=None, ats_job_id=None,
                    url="https://www.adzuna.de/details/1", **overrides)


def test_a_role_never_seen_before_has_no_repost_gap(memory_tracker):
    job = make_job()
    memory_tracker.record_job(job, now=NOW)
    assert memory_tracker.repost_gap_days(job.dedupe_key, key=job.key, now=NOW) is None


def test_seeing_the_same_listing_again_is_not_a_repost(memory_tracker):
    """The ordinary case: a board still carrying the same req every morning.
    One key, so there is no *other* sighting to compare against, however many
    times it is recorded."""
    job = make_job(hours_old=2)
    memory_tracker.record_job(job, now=NOW)
    memory_tracker.record_job(job, now=NOW + timedelta(days=40))
    assert memory_tracker.repost_gap_days(
        job.dedupe_key, key=job.key, posted_at=job.posted_at,
        now=NOW + timedelta(days=40),
    ) is None


def test_a_simultaneous_cross_source_duplicate_is_not_a_repost(memory_tracker):
    """The false positive this whole method is shaped around.

    One live job reaching us from Greenhouse and from Adzuna in the same run
    has one `dedupe_key` and two `Job.key`s — structurally identical to a
    re-listing. Flagging it would put a ghost-job warning on a healthy posting,
    every day, until the reader stopped believing the flag.

    Time used to be the only thing separating the two, and this pair *does*
    come out at a gap of zero. But time only works when both sources agree on
    the date, and an aggregator's date is its own ingest time — see the next
    test. So the sightings no longer count across boards at all, and this case
    is now excluded by construction rather than by arithmetic.
    """
    ats = make_job(hours_old=3)
    aggregator = cross_source_twin(hours_old=3)
    assert aggregator.dedupe_key == ats.dedupe_key
    assert aggregator.key != ats.key

    memory_tracker.record_job(ats, now=NOW)
    memory_tracker.record_job(aggregator, now=NOW)

    assert memory_tracker.repost_gap_days(
        aggregator.dedupe_key, key=aggregator.key,
        posted_at=aggregator.posted_at, now=NOW,
    ) is None


def test_an_aggregator_re_dating_a_live_posting_is_not_a_repost(memory_tracker):
    """The cross-source case time could not separate, which is why it is no
    longer asked to.

    Adzuna's `created` is its own ingest date, not the employer's. One live
    Greenhouse posting syndicated there arrives carrying a date months away
    from its twin's, so the gap comes out at 150 days rather than the zero the
    duplicate defence relies on. The docstring's claim — "a duplicate arrives
    beside its twin" — holds only when both sources agree about when, and this
    is the source that routinely does not.
    """
    live = make_job(hours_old=2, ats_job_id="req-1")
    syndicated = cross_source_twin(posted_at=NOW - timedelta(days=150))
    memory_tracker.record_job(syndicated, now=NOW - timedelta(days=150))
    memory_tracker.record_job(live, now=NOW)

    assert memory_tracker.repost_gap_days(
        live.dedupe_key, key=live.key, source=live.source,
        posted_at=live.posted_at, now=NOW,
    ) is None


def test_an_ats_migration_is_not_a_board_full_of_reposts(memory_tracker):
    """The worst false positive available, because it fires on every role at
    one employer on the same morning.

    A company moving Greenhouse -> Ashby re-lists its whole board under new
    ids in one day. Each req then has an older sighting with the same
    `dedupe_key`, a different `Job.key` and a gap of however long they were on
    the old ATS. Four reqs, four accusations, one relocation — and the day
    that happens is precisely the day the user most needs the digest to be
    readable.
    """
    for i in range(4):
        old = make_job(source="greenhouse", ats="greenhouse", ats_job_id=f"gh-{i}",
                       title=f"Engineer {i}", posted_at=NOW - timedelta(days=118))
        memory_tracker.record_job(old, now=NOW - timedelta(days=118))

    for i in range(4):
        moved = make_job(source="ashby", ats="ashby", ats_job_id=f"ashby-{i}",
                         title=f"Engineer {i}", hours_old=2)
        memory_tracker.record_job(moved, now=NOW)
        assert memory_tracker.repost_gap_days(
            moved.dedupe_key, key=moved.key, source=moved.source,
            posted_at=moved.posted_at, now=NOW,
        ) is None


def test_a_repost_on_the_same_board_still_counts(memory_tracker):
    """The neighbouring case for both tests above. Ignoring other boards must
    not quietly ignore everything: a role re-opened on the board it was
    originally advertised on is the signal, and it survives."""
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=120))
    memory_tracker.record_job(old, now=NOW - timedelta(days=120))
    new = make_job(ats_job_id="new", posted_at=NOW)
    memory_tracker.record_job(new, now=NOW)

    gap = memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source,
        posted_at=new.posted_at, now=NOW)
    assert gap == pytest.approx(120, abs=0.01)


def test_the_source_argument_beats_the_stored_row(memory_tracker):
    """`source=` is what the caller is holding; the stored row is the fallback
    for a caller that is not. Passing it is what makes the answer right for a
    job the tracker has never recorded — without it, an unrecorded job has no
    knowable board and gets no measurement rather than a wrong one."""
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=60))
    memory_tracker.record_job(old, now=NOW - timedelta(days=60))
    unrecorded = make_job(ats_job_id="new", posted_at=NOW)

    assert memory_tracker.repost_gap_days(
        unrecorded.dedupe_key, key=unrecorded.key,
        posted_at=unrecorded.posted_at, now=NOW) is None
    assert memory_tracker.repost_gap_days(
        unrecorded.dedupe_key, key=unrecorded.key, source="greenhouse",
        posted_at=unrecorded.posted_at, now=NOW) == pytest.approx(60, abs=0.01)


def test_a_relisting_after_a_gap_is_measured_in_days(memory_tracker):
    """A recruiter closing and re-opening a requisition: same company, same
    title, same city, new ATS id — so a new `Job.key` and the same
    `dedupe_key`. This is the signal."""
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))

    new = make_job(ats_job_id="new", posted_at=NOW - timedelta(days=1))
    assert new.dedupe_key == old.dedupe_key and new.key != old.key
    memory_tracker.record_job(new, now=NOW)

    gap = memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, posted_at=new.posted_at, now=NOW)
    assert gap == pytest.approx(89, abs=0.01)


def test_an_undated_earlier_sighting_falls_back_to_when_we_first_saw_it(memory_tracker):
    """`posted_at` is the employer's claim and `first_seen_at` is ours. Ours
    can never be *earlier* than the posting, so the substitution can only
    shrink the gap — which is the safe direction for a flag that would
    otherwise fire on a healthy posting."""
    old = make_job(ats_job_id="old", hours_old=None)
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))
    assert memory_tracker.get_job(old.key)["posted_at"] is None

    new = make_job(ats_job_id="new", posted_at=NOW)
    gap = memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source,
        posted_at=new.posted_at, now=NOW)
    assert gap == pytest.approx(90, abs=0.01)


def test_an_undated_current_listing_has_no_measurable_gap(memory_tracker):
    """The substitution is only safe on one side, and this is the other one.

    Falling back to `first_seen_at` for a *prior* row can only shrink the gap
    (the test above). Doing it for the listing being judged — or worse, to
    `now()` — moves the reference *later* and inflates. Concretely, these two
    used to produce the identical `gap=200`:

        the same 200-day-old role, still open, still undated  -> NOT a repost
        a 200-day-old first listing, genuinely re-listed today -> a repost

    Opposite ground truths, one number, and the flag accuses somebody either
    way. Reachable whenever `freshness.skip_undated` is false, which is
    documented and supported. With no date for this listing there is no
    measurement to make, so there is none.
    """
    old = make_job(ats_job_id="old", hours_old=None)
    memory_tracker.record_job(old, now=NOW - timedelta(days=200))
    new = make_job(ats_job_id="new", hours_old=None)
    memory_tracker.record_job(new, now=NOW)

    assert memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source,
        posted_at=None, now=NOW) is None


def test_a_stored_posted_at_is_used_when_the_caller_passes_none(memory_tracker):
    """The neighbouring case, and the line between the two.

    Reading this listing's date off its own stored row is not a fabrication —
    it is the employer's claim, frozen by `record_job`'s COALESCE, and it is
    the same value the caller would have passed. Only `first_seen_at` and the
    clock are inventions, and only those are refused.
    """
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))
    new = make_job(ats_job_id="new", posted_at=NOW)
    memory_tracker.record_job(new, now=NOW)

    gap = memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source, posted_at=None, now=NOW)
    assert gap == pytest.approx(90, abs=0.01)


def test_a_cross_source_twin_does_not_mask_a_real_repost(memory_tracker):
    """The earliest *qualifying* sighting decides the gap, not the nearest one.

    A role re-listed after three months does not stop being a re-listing
    because an aggregator also carries today's copy of it.
    """
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))
    memory_tracker.record_job(cross_source_twin(hours_old=2), now=NOW)

    new = make_job(ats_job_id="new", posted_at=NOW)
    gap = memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source,
        posted_at=new.posted_at, now=NOW)
    assert gap == pytest.approx(90, abs=0.01)


def test_a_newer_other_sighting_is_not_evidence_of_anything(memory_tracker):
    """Looking at the *older* of two listings. The other one is in its future,
    so it says nothing about this one having been re-listed — and a negative
    gap can never clear a threshold."""
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))
    new = make_job(ats_job_id="new", posted_at=NOW)
    memory_tracker.record_job(new, now=NOW)

    gap = memory_tracker.repost_gap_days(
        old.dedupe_key, key=old.key, posted_at=old.posted_at, now=NOW)
    assert gap is not None and gap < 0


def test_the_gap_is_not_shortened_by_the_old_listing_still_being_fetched(memory_tracker):
    """`last_seen_at` is deliberately not what this measures.

    A requisition that is still on the board is re-recorded every morning, and
    `record_job` moves `last_seen_at` each time. Measuring from that would
    quietly erase a ninety-day gap. Worse, it would read our own silence — a
    weekend, a watchlist edit, a board outage — as the employer closing the
    role. `posted_at` is frozen by COALESCE and `first_seen_at` is never
    rewritten, which is what makes them comparable across rows at all.
    """
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW)          # still on the board today

    new = make_job(ats_job_id="new", posted_at=NOW)
    gap = memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source,
        posted_at=new.posted_at, now=NOW)
    assert gap == pytest.approx(90, abs=0.01)


def test_an_empty_dedupe_key_is_never_a_repost(memory_tracker):
    """Same guard `has_applied_similar` has: an empty key would otherwise
    match every job that never got one."""
    assert memory_tracker.repost_gap_days("", key="whatever", now=NOW) is None
    assert memory_tracker.repost_gap_days("   ", key="whatever", now=NOW) is None


def test_repost_gap_days_reads_nothing_but_the_jobs_table(memory_tracker):
    """It is a measurement, not a decision: no application row is needed and
    none is written. The threshold that turns a gap into a flag lives in the
    config, not in here."""
    old = make_job(ats_job_id="old", posted_at=NOW - timedelta(days=90))
    memory_tracker.record_job(old, now=NOW - timedelta(days=90))
    new = make_job(ats_job_id="new", posted_at=NOW)

    assert memory_tracker.counts_by_status() == {}
    assert memory_tracker.repost_gap_days(
        new.dedupe_key, key=new.key, source=new.source,
        posted_at=new.posted_at, now=NOW) is not None
    assert memory_tracker.counts_by_status() == {}


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


def test_migration_v3_upgrades_a_v2_tracker_in_place(tmp_path):
    """An existing tracker gains the score_reasons column without losing a
    row — the same in-place contract every migration before it honoured."""
    import sqlite3

    from src.db import MIGRATIONS

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0])
    conn.executescript(MIGRATIONS[1])
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    tracker = Tracker(path)
    assert tracker.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    columns = {row[1] for row in
               tracker.conn.execute("PRAGMA table_info(applications)")}
    assert "score_reasons" in columns
    tracker.close()


def test_score_reasons_persist_and_survive_a_reasonless_rerecording():
    """The calibration data: kept as JSON, and COALESCE-protected so a later
    recording without reasons (statuses get re-recorded all the time) never
    erases what the scorer once said."""
    import json as json_lib

    from tests.conftest import make_job

    tracker = Tracker()
    job = make_job()
    tracker.record_job(job)
    tracker.record_status(job.key, "digest", score=81,
                          score_reasons=["title match", "forecasting overlap"])
    stored = tracker.get_application(job.key)["score_reasons"]
    assert json_lib.loads(stored) == ["title match", "forecasting overlap"]

    tracker.record_status(job.key, "digest", score=81)
    stored = tracker.get_application(job.key)["score_reasons"]
    assert json_lib.loads(stored) == ["title match", "forecasting overlap"]
    tracker.close()


def test_backup_writes_one_dated_consistent_copy_and_prunes(tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    tracker = Tracker(tmp_path / "tracker.sqlite3")
    day1 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    made = tracker.backup(keep=2, now=day1)
    assert made is not None and made.name == "tracker-2026-09-01.sqlite3"
    # The copy is a real snapshot, not a torn file copy of a WAL database.
    copy = sqlite3.connect(made)
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 3
    copy.close()

    assert tracker.backup(keep=2, now=day1) is None  # same day: no second copy

    for stamp in ("2026-08-20", "2026-08-21", "2026-08-22"):
        (tmp_path / "backups" / f"tracker-{stamp}.sqlite3").write_bytes(b"x")
    tracker.backup(keep=2, now=datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc))
    left = sorted(p.name for p in (tmp_path / "backups").glob("tracker-*.sqlite3"))
    assert left == ["tracker-2026-09-01.sqlite3", "tracker-2026-09-02.sqlite3"]
    tracker.close()


def test_an_in_memory_tracker_never_tries_to_back_itself_up():
    tracker = Tracker()
    assert tracker.backup(keep=5) is None
    tracker.close()


def test_a_failed_backup_never_claims_the_day(tmp_path):
    """A copy that dies mid-write must leave nothing behind: the torn file
    would satisfy the target-exists early return, and a same-day retry would
    then keep unreadable garbage as the backup of the one unregenerable file."""
    import sqlite3
    from datetime import datetime, timezone

    tracker = Tracker(tmp_path / "tracker.sqlite3")
    day = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

    class BrokenSource:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def backup(self, *_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    real = tracker.conn
    tracker.conn = BrokenSource(real)
    with pytest.raises(sqlite3.OperationalError):
        tracker.backup(keep=5, now=day)
    tracker.conn = real

    target = tmp_path / "backups" / "tracker-2026-09-01.sqlite3"
    assert not target.exists()

    made = tracker.backup(keep=5, now=day)  # the same-day retry succeeds
    assert made == target
    copy = sqlite3.connect(made)
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 3
    copy.close()
    tracker.close()


def test_backup_completes_despite_a_dangling_implicit_transaction(tmp_path):
    """A failed statement leaves its implicit transaction open on the
    connection, and `Connection.backup` retries BUSY forever against that
    lock — the nightly run would hang at the backup step, not fail. The
    backup commits first (every Tracker write commits on success, so nothing
    a caller meant to keep is ever rolled back by that)."""
    import threading
    from datetime import datetime, timezone

    made: list = []

    def work() -> None:
        # The whole tracker lives on this thread: sqlite connections are
        # bound to their creating thread, and the main thread only holds
        # the deadline.
        tracker = Tracker(tmp_path / "tracker.sqlite3")
        tracker.record_job(make_job())
        tracker.conn.execute(
            "INSERT INTO jobs (key, first_seen_at, last_seen_at) "
            "VALUES ('p','x','x')"
        )
        made.append(tracker.backup(
            keep=5, now=datetime(2026, 9, 1, tzinfo=timezone.utc)))
        tracker.close()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout=30.0)
    assert not worker.is_alive(), "backup hung on the open transaction"
    assert made and made[0] is not None
