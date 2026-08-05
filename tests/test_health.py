"""Tests for src/health.py — telling a quiet day from a broken pipeline.

The whole point is a *specific* discrimination, so the tests come in pairs:
for each alert there is a case that must fire it and a neighbouring case that
must stay silent. An alerting system that cries wolf gets ignored, and an
ignored alert is worth exactly as much as no alert.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from src.health import (
    ALERT_KINDS,
    weekend_days_between,
    BASELINE_MIN_AVERAGE,
    MISSED_RUN_HOURS,
    Alert,
    HealthReport,
    assess,
    filter_alerts,
    last_finished_run,
    source_baselines,
)
from src.models import RunStats
from tests.conftest import NOW


def stats(**kwargs):
    sources = kwargs.pop("source_counts", None)
    errors = kwargs.pop("errors", None)
    s = RunStats(**kwargs)
    if sources:
        s.source_counts.update(sources)
    if errors:
        s.errors.extend(errors)
    return s


def run_row(*, finished=True, when=None, counts=None, fetched=40):
    """One row shaped like `Tracker.recent_runs()` returns."""
    moment = when or (NOW - timedelta(days=1))
    payload = {"fetched": fetched, "source_counts": counts or {"greenhouse": 40}}
    return {
        "id": 1,
        "started_at": moment.isoformat(),
        "finished_at": moment.isoformat() if finished else None,
        "stats_json": json.dumps(payload),
    }


def healthy_history(n=5, counts=None):
    return [run_row(when=NOW - timedelta(days=i + 1), counts=counts)
            for i in range(n)]


# ==========================================================================
# a healthy run says nothing
# ==========================================================================


def test_a_normal_run_raises_no_alerts():
    report = assess(
        stats(fetched=40, source_counts={"greenhouse": 40}),
        previous_runs=healthy_history(),
        digest_path="/tmp/digest.html",
        now=NOW,
        active_sources=["greenhouse"],
    )
    assert report.ok
    assert report.alerts == []


def test_a_genuinely_quiet_day_is_not_an_alert():
    """Two jobs instead of forty is a quiet Tuesday, not a broken pipeline.
    Alerting here is exactly how an alert gets ignored."""
    report = assess(
        stats(fetched=2, source_counts={"greenhouse": 2}),
        previous_runs=healthy_history(),
        digest_path="/tmp/digest.html",
        now=NOW,
        active_sources=["greenhouse"],
    )
    assert report.ok


def test_zero_matches_is_not_an_alert():
    """Nothing scoring above the threshold is a normal outcome, and the
    digest already says so."""
    report = assess(
        stats(fetched=40, matches=0, source_counts={"greenhouse": 40}),
        previous_runs=healthy_history(), digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse"],
    )
    assert report.ok


def test_the_first_ever_run_has_nothing_to_be_suspicious_about():
    report = assess(
        stats(fetched=12, source_counts={"greenhouse": 12}),
        previous_runs=[], digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse"],
    )
    assert report.ok


# ==========================================================================
# no_digest
# ==========================================================================


def test_a_run_without_a_digest_is_critical():
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=healthy_history(), digest_path=None, now=NOW,
                    active_sources=["greenhouse"])
    assert "no_digest" in report.kinds()
    assert report.worst == "critical"


def test_the_no_digest_alert_points_at_the_log():
    report = assess(stats(fetched=1), previous_runs=[], digest_path=None, now=NOW,
                    active_sources=["greenhouse"])
    alert = next(a for a in report.alerts if a.kind == "no_digest")
    assert "log" in alert.detail.lower()


# ==========================================================================
# missed_run
# ==========================================================================


def test_a_skipped_weekday_is_reported():
    """The laptop-was-shut case. Nothing else in the system notices it.

    Tue -> Thu: 48h with no weekend in between, so nothing excuses it."""
    from datetime import datetime, timezone

    tuesday = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
    thursday = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=[run_row(when=tuesday)],
                    digest_path="/tmp/d.html", now=thursday,
                    active_sources=["greenhouse"])
    assert "missed_run" in report.kinds()


def test_a_saturday_to_tuesday_gap_did_miss_monday():
    """Same 72 hours as Friday -> Monday, but only one weekend day sits
    between them, so Monday really was skipped."""
    from datetime import datetime, timezone

    saturday = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    tuesday = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=[run_row(when=saturday)],
                    digest_path="/tmp/d.html", now=tuesday,
                    active_sources=["greenhouse"])
    assert "missed_run" in report.kinds()


def test_a_normal_daily_gap_is_silent():
    previous = [run_row(when=NOW - timedelta(hours=24))]
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=previous, digest_path="/tmp/d.html", now=NOW,
                    active_sources=["greenhouse"])
    assert "missed_run" not in report.kinds()


def test_a_late_run_within_the_grace_window_is_silent():
    previous = [run_row(when=NOW - timedelta(hours=MISSED_RUN_HOURS - 1))]
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=previous, digest_path="/tmp/d.html", now=NOW,
                    active_sources=["greenhouse"])
    assert "missed_run" not in report.kinds()


def test_a_weekend_gap_is_not_a_missed_run():
    """A weekday cron is *supposed* to be silent Saturday and Sunday. NOW is a
    Tuesday, so this is the Friday-to-Monday case shifted one day."""
    from datetime import datetime, timezone

    monday = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=[run_row(when=friday)],
                    digest_path="/tmp/d.html", now=monday,
                    active_sources=["greenhouse"])
    assert "missed_run" not in report.kinds()


def test_an_unfinished_previous_run_does_not_count_as_a_run():
    """A run that crashed mid-fetch left a row but produced nothing."""
    previous = [run_row(when=NOW - timedelta(hours=2), finished=False),
                run_row(when=NOW - timedelta(days=4))]
    report = assess(stats(fetched=40, source_counts={"greenhouse": 40}),
                    previous_runs=previous, digest_path="/tmp/d.html", now=NOW,
                    active_sources=["greenhouse"])
    assert "missed_run" in report.kinds()


# ==========================================================================
# no_jobs / all_sources_failed
# ==========================================================================


def test_fetching_nothing_at_all_is_critical():
    report = assess(stats(fetched=0, source_counts={"greenhouse": 0}),
                    previous_runs=healthy_history(), digest_path="/tmp/d.html",
                    now=NOW, active_sources=["greenhouse"])
    assert "no_jobs" in report.kinds()
    assert report.worst == "critical"


def test_the_no_jobs_alert_tells_you_how_to_check_your_slugs():
    report = assess(stats(fetched=0), previous_runs=[], digest_path="/tmp/d.html",
                    now=NOW, active_sources=["greenhouse"])
    alert = next(a for a in report.alerts if a.kind == "no_jobs")
    assert "--check-all" in alert.detail


def test_every_source_failing_is_reported_as_such():
    report = assess(
        stats(fetched=0, errors=["greenhouse/lever source failed: HTTP 500",
                                 "adzuna source failed: timeout"]),
        previous_runs=healthy_history(), digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse", "adzuna"],
    )
    assert "all_sources_failed" in report.kinds()
    # ... and not also reported as the vaguer "no jobs".
    assert "no_jobs" not in report.kinds()


def test_no_jobs_is_silent_when_no_source_was_active():
    report = assess(stats(fetched=0), previous_runs=[], digest_path="/tmp/d.html",
                    now=NOW, active_sources=[])
    assert "no_jobs" not in report.kinds()


# ==========================================================================
# source_zero — the renamed-board case
# ==========================================================================


def test_a_source_that_used_to_work_going_silent_is_reported():
    """A renamed Greenhouse board returns an empty list forever, and looks
    exactly like a company that stopped hiring."""
    history = healthy_history(counts={"greenhouse": 40, "lever": 12})
    report = assess(
        stats(fetched=40, source_counts={"greenhouse": 40, "lever": 0}),
        previous_runs=history, digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse", "lever"],
    )
    assert "source_zero" in report.kinds()
    alert = next(a for a in report.alerts if a.kind == "source_zero")
    assert "lever" in alert.detail
    assert "renamed" in alert.detail


@pytest.mark.parametrize(
    "board", ["workable", "ashby", "smartrecruiters", "personio"]
)
def test_a_european_board_going_silent_is_reported_too(board):
    """`assess` is generic over source names, but "generic" is a claim, not a
    fact: the alert only reaches the user if `main` puts the source in
    `active_sources` and the fetcher stamps `Job.source` with the same string.
    A Personio tenant that renames itself returns an empty feed forever and
    looks exactly like a company that stopped hiring."""
    history = healthy_history(counts={"greenhouse": 40, board: 12})
    report = assess(
        stats(fetched=40, source_counts={"greenhouse": 40, board: 0}),
        previous_runs=history, digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse", board],
    )
    assert "source_zero" in report.kinds()
    alert = next(a for a in report.alerts if a.kind == "source_zero")
    assert board in alert.detail


def test_every_board_source_can_be_seen_by_the_health_check():
    """The names `main` reports as active and the names the fetchers stamp on
    `Job.source` have to be the same strings, or `source_zero` silently never
    fires for the mismatched one."""
    from src.config import BOARD_SOURCE_NAMES
    from src.sources.ats_boards import BOARDS

    assert set(BOARDS) == set(BOARD_SOURCE_NAMES)


def test_a_source_that_never_produced_much_is_not_missed():
    """One posting a week from a tiny board going quiet is not news."""
    history = healthy_history(counts={"greenhouse": 40,
                                      "lever": BASELINE_MIN_AVERAGE - 1})
    report = assess(
        stats(fetched=40, source_counts={"greenhouse": 40, "lever": 0}),
        previous_runs=history, digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse", "lever"],
    )
    assert "source_zero" not in report.kinds()


def test_a_source_switched_off_on_purpose_is_not_missed():
    history = healthy_history(counts={"greenhouse": 40, "adzuna": 30})
    report = assess(
        stats(fetched=40, source_counts={"greenhouse": 40}),
        previous_runs=history, digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse"],      # adzuna disabled since
    )
    assert "source_zero" not in report.kinds()


def test_source_zero_is_not_stacked_on_top_of_no_jobs():
    """One problem should read as one problem."""
    report = assess(
        stats(fetched=0, source_counts={"greenhouse": 0}),
        previous_runs=healthy_history(), digest_path="/tmp/d.html", now=NOW,
        active_sources=["greenhouse"],
    )
    assert "no_jobs" in report.kinds()
    assert "source_zero" not in report.kinds()


# ==========================================================================
# baselines
# ==========================================================================


def test_source_baselines_averages_completed_runs():
    rows = [run_row(counts={"greenhouse": 10}), run_row(counts={"greenhouse": 30})]
    assert source_baselines(rows) == {"greenhouse": 20.0}


def test_unfinished_runs_do_not_drag_the_baseline_to_zero():
    """Otherwise one crashed run permanently silences the alert."""
    rows = [run_row(counts={"greenhouse": 0}, finished=False),
            run_row(counts={"greenhouse": 40})]
    assert source_baselines(rows) == {"greenhouse": 40.0}


def test_source_baselines_survives_corrupt_history():
    rows = [{"finished_at": "x", "stats_json": "not json"},
            {"finished_at": "x", "stats_json": json.dumps({"source_counts": "nope"})},
            run_row(counts={"greenhouse": 10})]
    assert source_baselines(rows) == {"greenhouse": 10.0}


def test_source_baselines_with_no_history():
    assert source_baselines([]) == {}


def test_last_finished_run_skips_the_run_in_progress():
    done = NOW - timedelta(days=1)
    rows = [run_row(finished=False), run_row(when=done)]
    assert last_finished_run(rows) == done


def test_last_finished_run_with_no_history():
    assert last_finished_run([]) is None


def test_last_finished_run_ignores_an_unparseable_timestamp():
    rows = [{"finished_at": "not-a-date", "stats_json": "{}"},
            run_row(when=NOW - timedelta(days=1))]
    assert last_finished_run(rows) is not None


# ==========================================================================
# report shaping
# ==========================================================================


def test_the_title_reads_on_a_phone():
    report = assess(stats(fetched=0), previous_runs=[], digest_path=None, now=NOW,
                    active_sources=["greenhouse"])
    title = report.title()
    assert title.startswith("Job Hunter:")
    assert len(title) < 120
    assert "more)" in title          # more than one alert, summarised


def test_the_summary_puts_the_worst_first():
    report = HealthReport([Alert("errors", "info", "3 errors"),
                           Alert("no_digest", "critical", "no digest")])
    assert report.summary().splitlines()[0] == "no digest"
    assert report.worst == "critical"


def test_a_healthy_report_titles_itself_ok():
    assert "OK" in HealthReport().title()


def test_filter_alerts_honours_the_configured_list():
    report = HealthReport([Alert("errors", "info", "3 errors"),
                           Alert("no_digest", "critical", "no digest")])
    kept = filter_alerts(report, ["no_digest"])
    assert kept.kinds() == ["no_digest"]


def test_filter_alerts_with_none_keeps_everything():
    report = HealthReport([Alert("errors", "info", "x")])
    assert filter_alerts(report, None).kinds() == ["errors"]


def test_filter_alerts_with_an_empty_list_silences_everything():
    """The user's call, and it must be respected without argument."""
    report = HealthReport([Alert("no_digest", "critical", "x")])
    assert filter_alerts(report, []).ok


def test_every_alert_kind_is_declared():
    """A kind that is not in ALERT_KINDS cannot be named in `notify.on`, so
    the user could never switch it off."""
    produced = set()
    for case in (
        dict(stats=stats(fetched=0), digest_path=None, previous_runs=[]),
        dict(stats=stats(fetched=0, errors=["greenhouse/lever source failed: x",
                                           "adzuna source failed: y"]),
             digest_path="/d", previous_runs=[]),
        dict(stats=stats(fetched=40, source_counts={"greenhouse": 40, "lever": 0}),
             digest_path="/d", previous_runs=healthy_history(
                 counts={"greenhouse": 40, "lever": 20})),
        dict(stats=stats(fetched=40, source_counts={"greenhouse": 40},
                         errors=["scoring x failed"]),
             digest_path="/d",
             previous_runs=[run_row(when=NOW - timedelta(days=5))]),
    ):
        report = assess(case["stats"], previous_runs=case["previous_runs"],
                        digest_path=case["digest_path"], now=NOW,
                        active_sources=["greenhouse", "lever"])
        produced.update(report.kinds())
    assert produced <= set(ALERT_KINDS)
    assert produced == set(ALERT_KINDS), f"never produced: {set(ALERT_KINDS) - produced}"


def test_assess_never_raises_on_junk_input():
    class Junk:
        pass

    assert assess(Junk(), previous_runs=[{"nonsense": True}], digest_path=None,
                  now=NOW, active_sources=None) is not None


# ==========================================================================
# the weekend heuristic
# ==========================================================================


@pytest.mark.parametrize(
    "start,end,expected",
    [
        # Fri 2026-07-31 -> Mon 2026-08-03: Sat and Sun both excused.
        ((2026, 7, 31), (2026, 8, 3), 2),
        # Sat 2026-08-01 -> Tue 2026-08-04: only Sunday sits between them.
        ((2026, 8, 1), (2026, 8, 4), 1),
        # Tue -> Thu: nothing excused.
        ((2026, 8, 4), (2026, 8, 6), 0),
        # Same day.
        ((2026, 8, 4), (2026, 8, 4), 0),
    ],
)
def test_weekend_days_between(start, end, expected):
    from datetime import datetime, timezone

    a = datetime(*start, 8, 0, tzinfo=timezone.utc)
    b = datetime(*end, 8, 0, tzinfo=timezone.utc)
    assert weekend_days_between(a, b) == expected
