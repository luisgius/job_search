"""Durable scoring recovery through the real offline pipeline and SQLite."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from src import main
from src.db import MIGRATIONS, Tracker
from src.models import ApplyStatus
from tests.conftest import NOW, make_job, write_config


class Scorer:
    def __init__(self, error=False):
        self.error = error
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError("provider unavailable")
        return {"score": 85, "reasons": ["Python matches"], "strengths": [],
                "gaps": [], "verdict": "good fit"}


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, {
        "scoring": {"max_jobs": 1, "concurrency": 1},
        "freshness": {"max_age_hours": 24},
        "tailoring": {"enabled": False}, "apply": {"enabled": False},
        "db": {"backups_keep": 0},
    })
    source = {"jobs": [], "error": None}

    def fetch(config, errors):
        if source["error"]:
            raise RuntimeError(source["error"])
        return source["jobs"]

    monkeypatch.setattr(main.ats_boards, "fetch", fetch)
    return cfg, source


def posting(i, hours_old=2):
    return make_job(company=f"Company{i}", ats_job_id=str(i), hours_old=hours_old)


def run(cfg, tracker, client=None, hours=0, **kwargs):
    return main.run_pipeline(cfg, tracker=tracker, now=NOW + timedelta(hours=hours),
                             llm_client=client or Scorer(), sources=["greenhouse"], **kwargs)


def test_overflow_survives_freshness_and_restart_with_original_evidence(scenario, tmp_path):
    cfg, source = scenario
    newest, overflow = posting(1, 1), posting(2, 23)
    source["jobs"] = [overflow, newest]
    path = tmp_path / "queue.sqlite3"
    with Tracker(path) as tracker:
        first, _ = run(cfg, tracker)
        assert [s.key for s in first] == [newest.key]
        row = tracker.get_scoring(overflow.key)
        assert row["attempts"] == 0
        assert json.loads(row["job_json"])["posted_at"] == overflow.posted_at.isoformat()
        assert tracker.get_status(overflow.key) is None
        first_seen = tracker.first_seen(overflow.key)
    # The source changes its date and description; only current evidence is
    # scored, but this must not make the queued posting look newly published.
    refreshed = replace(overflow, posted_at=NOW + timedelta(hours=25),
                        description="Current Python and PostgreSQL role description")
    new_arrival = replace(posting(3), posted_at=NOW + timedelta(hours=25))
    source["jobs"] = [new_arrival, refreshed]
    with Tracker(path) as tracker:
        client = Scorer()
        second, _ = run(cfg, tracker, client, hours=26)
        assert [s.key for s in second] == [overflow.key]  # backlog fairness
        assert second[0].job.posted_at == overflow.posted_at
        assert second[0].job.description == refreshed.description
        assert len(client.calls) == 1  # shared new + retry cap
        assert tracker.first_seen(overflow.key) == first_seen
        assert tracker.get_scoring(overflow.key) is None
        assert tracker.get_scoring(new_arrival.key)["attempts"] == 0
        assert tracker.get_application(overflow.key)["score"] == 85


def test_outage_retries_after_backoff_without_handled_status(scenario, tmp_path):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    path = tmp_path / "queue.sqlite3"
    with Tracker(path) as tracker:
        failed, _ = run(cfg, tracker, Scorer(error=True))
        assert "pending scoring retry" in failed[0].status_detail
        assert tracker.get_application(job.key) is None
        original = tracker.get_scoring(job.key)
    with Tracker(path) as tracker:
        client = Scorer()
        early, _ = run(cfg, tracker, client, hours=0.5)
        assert early == [] and client.calls == []
        assert tracker.get_scoring(job.key)["attempts"] == 1
        recovered, _ = run(cfg, tracker, client, hours=2)
        assert recovered[0].score.ok and len(client.calls) == 1
        assert tracker.get_scoring(job.key) is None
        assert original["queued_at"] == NOW.isoformat()
        third, _ = run(cfg, tracker, client, hours=3)
        assert third == [] and len(client.calls) == 1


@pytest.mark.parametrize("source_failure", [False, True])
def test_absence_needs_current_evidence_never_asserts_closed(scenario, source_failure):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        run(cfg, tracker, Scorer(error=True))
        source["jobs"] = []
        source["error"] = "board outage" if source_failure else None
        client = Scorer()
        scored, stats = run(cfg, tracker, client, hours=26)
        row = tracker.get_scoring(job.key)
        assert scored == [] and client.calls == []
        assert row["state"] == "pending" and row["attempts"] == 1
        assert row["last_observed_at"] == NOW.isoformat()
        assert "outdated" in row["detail"] and "availability unknown" in row["detail"]
        assert any("1 need current source" in error for error in stats.errors)
        source.update(jobs=[job], error=None)
        recovered, _ = run(cfg, tracker, client, hours=27)
        assert recovered[0].score.ok


@pytest.mark.parametrize("mode", ["cap0", "no_llm", "limit0"])
def test_paused_scoring_retains_work_without_spending_attempts(scenario, mode):
    cfg, source = scenario
    source["jobs"] = [posting(1), posting(2)]
    options = {}
    if mode == "cap0":
        cfg.data["scoring"]["max_jobs"] = 0
    elif mode == "no_llm":
        options["skip_llm"] = True
    else:
        options["limit"] = 0
    with Tracker() as tracker:
        client = Scorer()
        scored, stats = run(cfg, tracker, client, **options)
        assert client.calls == [] and stats.scored == 0
        assert len(tracker.pending_scoring()) == 2
        assert all(row["attempts"] == 0 for row in tracker.pending_scoring())
        assert all(tracker.get_status(job.key) is None for job in source["jobs"])
        if mode == "no_llm":
            assert len(scored) == 2 and all(item.score is None for item in scored)
        cfg.data["scoring"]["max_jobs"] = 2
        recovered, _ = run(cfg, tracker, client, hours=26)
        assert len(recovered) == 2 and len(client.calls) == 2


def test_exhaustion_is_durable_and_does_not_reset_on_observation(scenario, tmp_path):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    path = tmp_path / "queue.sqlite3"
    for hour, attempt in [(0, 1), (1, 2), (3, 3)]:
        with Tracker(path) as tracker:
            run(cfg, tracker, Scorer(error=True), hours=hour)
            row = tracker.get_scoring(job.key)
            assert row["attempts"] == attempt
            assert row["queued_at"] == NOW.isoformat()
            assert row["expires_at"] == (NOW + timedelta(days=7)).isoformat()
    with Tracker(path) as tracker:
        row = tracker.get_scoring(job.key)
        assert row["state"] == "exhausted" and row["job_json"] is None
        assert "retry limit" in row["detail"]
        client = Scorer()
        run(cfg, tracker, client, hours=8)
        assert client.calls == [] and tracker.get_status(job.key) is None


def test_queue_expiry_is_distinct_from_closure_and_visible(scenario):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        run(cfg, tracker, skip_llm=True)
        source.update(jobs=[], error="outage")
        client = Scorer()
        _, stats = run(cfg, tracker, client, hours=7 * 24)
        row = tracker.get_scoring(job.key)
        assert row["state"] == "expired" and row["job_json"] is None
        assert "not confirmed closed" in row["detail"]
        assert any("queue lifetime expired" in error for error in stats.errors)
        assert client.calls == [] and tracker.get_status(job.key) is None


def test_backlog_capacity_and_snapshot_size_are_explicit(scenario):
    cfg, source = scenario
    cfg.data["scoring"].update(backlog_max_jobs=1, max_jobs=0)
    source["jobs"] = [posting(1, 1), posting(2, 2)]
    with Tracker() as tracker:
        _, stats = run(cfg, tracker)
        assert len(tracker.pending_scoring()) == 1
        assert tracker.pending_scoring()[0]["key"] == source["jobs"][0].key
        assert any("backlog full (1)" in error for error in stats.errors)
        assert tracker.get_status(source["jobs"][1].key) is None
        giant = replace(posting(3), description="x" * (256 * 1024))
        with pytest.raises(ValueError, match="256 KiB"):
            tracker.enqueue_scoring(giant, max_pending=2, now=NOW)
        assert tracker.get_scoring(giant.key) is None


@pytest.mark.parametrize("status", [ApplyStatus.APPLIED, ApplyStatus.SUBMITTED_UNCONFIRMED,
                                    ApplyStatus.DRY_RUN, ApplyStatus.APPLY_FAILED,
                                    ApplyStatus.DIGEST, ApplyStatus.SCORED_BELOW])
def test_application_outcomes_written_after_admission_are_preserved(scenario, status):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        run(cfg, tracker, skip_llm=True)
        tracker.record_status(job.key, status, detail="original", score=90, now=NOW)
        original = tracker.get_application(job.key)
        client = Scorer()
        scored, _ = run(cfg, tracker, client, hours=26)
        assert scored == [] and client.calls == []
        assert tracker.get_application(job.key) == original
        assert tracker.get_scoring(job.key)["state"] == "protected"


@pytest.mark.parametrize("protection", ["submit_attempt", "similar_application"])
def test_retry_never_bypasses_duplicate_or_submit_attempt(scenario, protection):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        run(cfg, tracker, skip_llm=True)
        if protection == "submit_attempt":
            tracker.record_submit_attempt(job.key, now=NOW)
        else:
            previous = replace(job, ats_job_id="previous")
            tracker.record_job(previous, now=NOW)
            tracker.record_status(previous.key, ApplyStatus.APPLIED, now=NOW)
        client = Scorer()
        scored, _ = run(cfg, tracker, client, hours=2)
        assert scored == [] and client.calls == []
        assert tracker.get_scoring(job.key)["state"] == "protected"
        if protection == "submit_attempt":
            assert tracker.submit_attempted(job.key)


def test_current_nonfreshness_filters_still_reject_queued_work(scenario):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        run(cfg, tracker, skip_llm=True)
        source["jobs"] = [replace(job, raw={"employment_type": "internship"})]
        client = Scorer()
        scored, _ = run(cfg, tracker, client, hours=26)
        assert scored == [] and client.calls == []
        assert tracker.get_scoring(job.key)["state"] == "ineligible"
        assert tracker.get_status(job.key) is None


def test_unexpected_scorer_exception_is_durable(scenario, monkeypatch):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    real = main.scoring.score_jobs

    def broken(*args, **kwargs):
        raise RuntimeError("batch failed")

    with Tracker() as tracker:
        monkeypatch.setattr(main.scoring, "score_jobs", broken)
        run(cfg, tracker)
        assert tracker.get_scoring(job.key)["attempts"] == 1
        monkeypatch.setattr(main.scoring, "score_jobs", real)
        scored, _ = run(cfg, tracker, hours=2)
        assert scored[0].score.ok


def test_write_ahead_attempt_survives_interrupted_process(scenario, tmp_path):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    path = tmp_path / "queue.sqlite3"
    with Tracker(path) as tracker:
        tracker.enqueue_scoring(job, now=NOW)
        tracker.begin_scoring(job.key, retry_hours=1, now=NOW)
    with Tracker(path) as tracker:
        client = Scorer()
        run(cfg, tracker, client, hours=0.5)
        assert client.calls == [] and tracker.get_scoring(job.key)["attempts"] == 1
        run(cfg, tracker, client, hours=2)
        assert len(client.calls) == 1 and tracker.get_scoring(job.key) is None


def test_failed_application_persistence_does_not_clear_queue(scenario, monkeypatch):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(tracker, "record_status", broken)
        scored, _ = run(cfg, tracker)
        assert scored[0].score.ok
        assert tracker.get_scoring(job.key)["state"] == "pending"
        assert tracker.get_status(job.key) is None


def test_v3_migration_preserves_application_and_submission_history(tmp_path):
    path = tmp_path / "v3.sqlite3"
    with sqlite3.connect(path) as conn:
        for migration in MIGRATIONS[:3]:
            conn.executescript(migration)
        conn.execute("PRAGMA user_version = 3")
        conn.execute("INSERT INTO submit_attempts VALUES ('known', 'url', 'greenhouse', ?)",
                     (NOW.isoformat(),))
    with Tracker(path) as tracker:
        assert tracker.migrate() == 4
        assert tracker.pending_scoring() == []
        assert tracker.submit_attempted("known")
        assert tracker.submit_attempt("known")["attempted_at"] == NOW.isoformat()


@pytest.mark.parametrize("name,value", [("backlog_max_jobs", -1), ("backlog_max_jobs", True),
    ("backlog_max_age_days", 0), ("retry_max_attempts", 11), ("retry_base_hours", "soon")])
def test_backlog_policy_validation(scenario, name, value):
    cfg, _ = scenario
    cfg.data["scoring"][name] = value
    assert any(f"scoring.{name}" in problem for problem in cfg.validate())


def test_replay_only_evidence_cannot_enter_apply_but_current_evidence_uses_normal_stage(
        scenario, monkeypatch):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    cfg.data["apply"]["enabled"] = True
    apply_calls = []

    def apply_stage(items, config, tracker):
        apply_calls.extend(items)
        # This seam replaces only external actions, not scoring/revalidation.
        return items

    monkeypatch.setattr(main.autoapply, "run", apply_stage)
    with Tracker() as tracker:
        run(cfg, tracker, skip_llm=True)
        source["jobs"] = []
        run(cfg, tracker, hours=26)
        assert apply_calls == []
        source["jobs"] = [replace(job, description="Current backend Python opening")]
        recovered, stats = run(cfg, tracker, hours=27)
        assert [item.key for item in apply_calls] == [job.key]
        assert apply_calls[0].job.description == source["jobs"][0].description
        assert apply_calls[0].job.posted_at == job.posted_at
        assert stats.after_filters == 1
        assert "queued since" in recovered[0].status_detail


def test_admission_filters_failing_cannot_license_durable_scoring(scenario, monkeypatch):
    cfg, source = scenario
    source["jobs"] = [posting(1)]

    def broken(*args, **kwargs):
        raise RuntimeError("filter bug")

    monkeypatch.setattr(main.filters, "apply_filters", broken)
    with Tracker() as tracker:
        client = Scorer()
        scored, stats = run(cfg, tracker, client)
        assert scored == [] and client.calls == []
        assert tracker.pending_scoring() == []
        assert any("filtering failed" in error for error in stats.errors)


def test_reminder_window_rescore_failure_retries_without_erasing_prior_outcome(scenario):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        tracker.record_job(job, now=NOW - timedelta(days=40))
        tracker.record_status(job.key, ApplyStatus.DIGEST, score=70,
                              now=NOW - timedelta(days=40))
        original = tracker.get_application(job.key)
        run(cfg, tracker, Scorer(error=True))
        assert tracker.get_application(job.key) == original
        recovered, _ = run(cfg, tracker, hours=2)
        assert recovered[0].score.ok
        assert tracker.get_scoring(job.key) is None
        assert tracker.get_application(job.key)["score"] == 85


def test_missing_scorer_results_exhaust_visibly(scenario, monkeypatch):
    cfg, source = scenario
    cfg.data["scoring"]["retry_max_attempts"] = 1
    job = posting(1)
    source["jobs"] = [job]
    monkeypatch.setattr(main.scoring, "score_jobs", lambda *args, **kwargs: [])
    with Tracker() as tracker:
        scored, stats = run(cfg, tracker)
        assert scored == [] and tracker.get_scoring(job.key)["state"] == "exhausted"
        assert any("scoring retry limit reached: scorer returned no result" in error
                   for error in stats.errors)


def assert_storage_error_digest(stats):
    from pathlib import Path
    assert stats.digest_path is not None
    rendered = Path(stats.digest_path).read_text(encoding="utf-8")
    assert any("database is locked" in error for error in stats.errors)
    assert "database is locked" in rendered


@pytest.mark.parametrize("committed", [False, True])
def test_attempt_write_failure_skips_only_unconfirmed_job_and_recovers_after_restart(
        scenario, tmp_path, monkeypatch, committed):
    cfg, source = scenario
    cfg.data["scoring"]["max_jobs"] = 2
    cfg.data["apply"]["enabled"] = True
    blocked, healthy = posting(1), posting(2)
    source["jobs"] = [blocked, healthy]
    apply_calls = []

    def apply_stage(items, config, tracker):
        apply_calls.extend(item.key for item in items)
        return items

    monkeypatch.setattr(main.autoapply, "run", apply_stage)
    path = tmp_path / "locked.sqlite3"
    with Tracker(path) as tracker:
        original = tracker.begin_scoring

        def locked(key, **kwargs):
            if key == blocked.key:
                if committed:
                    original(key, **kwargs)
                raise sqlite3.OperationalError("database is locked")
            return original(key, **kwargs)

        monkeypatch.setattr(tracker, "begin_scoring", locked)
        client = Scorer()
        scored, stats = run(cfg, tracker, client)
        assert [item.key for item in scored] == [healthy.key]
        assert len(client.calls) == 1 and apply_calls == [healthy.key]
        assert_storage_error_digest(stats)
        assert tracker.get_status(blocked.key) is None
        row = tracker.get_scoring(blocked.key)
        assert row["attempts"] == int(committed)
        assert row["queued_at"] == NOW.isoformat()
    with Tracker(path) as tracker:
        recovered, _ = run(cfg, tracker, hours=2)
        assert [item.key for item in recovered] == [blocked.key]
        assert tracker.get_scoring(blocked.key) is None
        assert apply_calls == [healthy.key, blocked.key]


@pytest.mark.parametrize("method", [
    "get_scoring", "pending_scoring", "has_applied", "submit_attempted",
    "get_application", "observe_scoring", "scoring_detail", "stop_scoring",
    "has_applied_similar",
])
def test_queue_admission_and_preparation_storage_errors_preserve_work_and_digest(
        scenario, monkeypatch, method):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    cfg.data["apply"]["enabled"] = True

    def unexpected_apply(*args, **kwargs):
        pytest.fail("uncertain queue work reached application")

    monkeypatch.setattr(main.autoapply, "run", unexpected_apply)
    with Tracker() as tracker:
        tracker.enqueue_scoring(job, now=NOW)
        original = getattr(tracker, method)

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(tracker, method, locked)
        client = Scorer()
        hours = 168 if method == "stop_scoring" else 2
        scored, stats = run(cfg, tracker, client, hours=hours)
        assert scored == [] and client.calls == []
        assert_storage_error_digest(stats)
        monkeypatch.setattr(tracker, method, original)
        row = tracker.get_scoring(job.key)
        assert row["state"] == "pending" and row["attempts"] == 0
        assert row["queued_at"] == NOW.isoformat()
        assert tracker.get_application(job.key) is None


def test_enqueue_storage_failure_is_visible_and_never_scores_unretained_job(scenario, monkeypatch):
    cfg, source = scenario
    job = posting(1)
    source["jobs"] = [job]
    with Tracker() as tracker:
        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(tracker, "enqueue_scoring", locked)
        client = Scorer()
        scored, stats = run(cfg, tracker, client)
        assert scored == [] and client.calls == []
        assert_storage_error_digest(stats)
        assert tracker.get_scoring(job.key) is None
        assert tracker.get_application(job.key) is None


@pytest.mark.parametrize("method", ["get_scoring", "scoring_detail", "stop_scoring"])
def test_result_bookkeeping_storage_error_holds_batch_without_application_or_outcome(
        scenario, monkeypatch, method):
    cfg, source = scenario
    cfg.data["scoring"].update(max_jobs=2, retry_max_attempts=1 if method == "stop_scoring" else 3)
    cfg.data["apply"]["enabled"] = True
    source["jobs"] = [posting(1), posting(2)]
    real_score_jobs = main.scoring.score_jobs

    def unexpected_apply(*args, **kwargs):
        pytest.fail("uncertain result bookkeeping reached application")

    monkeypatch.setattr(main.autoapply, "run", unexpected_apply)
    with Tracker() as tracker:
        original = getattr(tracker, method)

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def score_then_lock(*args, **kwargs):
            results = real_score_jobs(*args, **kwargs)
            results[0].score.error = "provider unavailable"
            monkeypatch.setattr(tracker, method, locked)
            return results

        monkeypatch.setattr(main.scoring, "score_jobs", score_then_lock)
        scored, stats = run(cfg, tracker)
        assert len(scored) == 2 and scored[1].score.ok
        assert all("automatic application withheld" in item.status_detail for item in scored)
        assert_storage_error_digest(stats)
        monkeypatch.setattr(tracker, method, original)
        assert len(tracker.pending_scoring()) == 2
        assert all(row["attempts"] == 1 for row in tracker.pending_scoring())
        assert all(tracker.get_application(job.key) is None for job in source["jobs"])


def test_evidence_read_error_after_successful_score_prevents_application(scenario, monkeypatch):
    cfg, source = scenario
    source["jobs"] = [posting(1)]
    cfg.data["apply"]["enabled"] = True
    real_score_jobs = main.scoring.score_jobs

    def unexpected_apply(*args, **kwargs):
        pytest.fail("unreadable queue evidence reached application")

    monkeypatch.setattr(main.autoapply, "run", unexpected_apply)
    with Tracker() as tracker:
        original = tracker.get_scoring

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def score_then_lock(*args, **kwargs):
            results = real_score_jobs(*args, **kwargs)
            monkeypatch.setattr(tracker, "get_scoring", locked)
            return results

        monkeypatch.setattr(main.scoring, "score_jobs", score_then_lock)
        scored, stats = run(cfg, tracker)
        assert scored[0].score.ok
        assert "automatic application withheld" in scored[0].status_detail
        assert_storage_error_digest(stats)
        monkeypatch.setattr(tracker, "get_scoring", original)
        assert tracker.get_scoring(source["jobs"][0].key)["state"] == "pending"
        assert tracker.get_application(source["jobs"][0].key) is None


@pytest.mark.parametrize("method", ["pending_scoring", "get_job", "complete_scoring"])
def test_final_queue_diagnostics_and_cleanup_storage_errors_do_not_cost_digest(
        scenario, monkeypatch, method):
    cfg, source = scenario
    source["jobs"] = [posting(1)]
    with Tracker() as tracker:
        original = getattr(tracker, method)

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        if method == "pending_scoring":
            calls = []

            def fail_final(*args, **kwargs):
                calls.append(None)
                if len(calls) > 1:
                    return locked()
                return original(*args, **kwargs)

            monkeypatch.setattr(tracker, method, fail_final)
        else:
            monkeypatch.setattr(tracker, method, locked)
        # Keep a pending row for label diagnostics; completion tests score it.
        scored, stats = run(cfg, tracker, skip_llm=method != "complete_scoring")
        assert len(scored) == 1
        assert_storage_error_digest(stats)
        monkeypatch.setattr(tracker, method, original)
        assert tracker.get_scoring(source["jobs"][0].key) is not None
        if method == "complete_scoring":
            assert tracker.get_application(source["jobs"][0].key)["score"] == 85
            # Committed outcome protects against a duplicate after cleanup failed.
            later, _ = run(cfg, tracker, hours=2)
            assert later == []


def test_transient_language_detector_failure_retains_evidence_and_recovers_after_restart(
        scenario, tmp_path, monkeypatch):
    from pathlib import Path
    cfg, source = scenario
    cfg.data["filters"].update(languages=["en"], language_min_chars=0)
    job = posting(1)
    source["jobs"] = [job]
    path = tmp_path / "revalidate.sqlite3"
    original_detector = main.filters._language_detector

    class BrokenDetector:
        def compute_language_confidence_values(self, text):
            raise RuntimeError("language detector unavailable")

    with Tracker(path) as tracker:
        run(cfg, tracker, skip_llm=True)
        admitted = tracker.get_scoring(job.key)
        monkeypatch.setattr(main.filters, "_language_detector", lambda allowed: BrokenDetector())
        client = Scorer()
        scored, stats = run(cfg, tracker, client, hours=26)
        assert scored == [] and client.calls == []
        row = tracker.get_scoring(job.key)
        assert row["state"] == "pending" and row["attempts"] == 0
        assert row["job_json"] is not None
        assert row["queued_at"] == admitted["queued_at"]
        assert row["expires_at"] == admitted["expires_at"]
        assert "pending revalidation" in row["detail"]
        assert any("language detector unavailable" in error for error in stats.errors)
        assert "language detector unavailable" in Path(stats.digest_path).read_text()
        assert tracker.get_application(job.key) is None
    monkeypatch.setattr(main.filters, "_language_detector", original_detector)
    with Tracker(path) as tracker:
        client = Scorer()
        recovered, _ = run(cfg, tracker, client, hours=27)
        assert len(recovered) == 1 and recovered[0].score.ok
        assert len(client.calls) == 1
        assert tracker.get_scoring(job.key) is None
