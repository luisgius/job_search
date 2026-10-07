"""Offline tests for SingaporeStore — merge, dedupe, scores, MCF candidates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models import Job, Score
from src.singapore.store import SingaporeStore, job_identity, strip_pte_ltd

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)


def make_job(**kwargs) -> Job:
    base = dict(
        source="greenhouse",
        company="Acme Pte. Ltd.",
        title="Data Scientist",
        url="https://boards.example.com/jobs/1",
        location="Singapore",
        description="Short",
        source_type="board",
        sources=["greenhouse"],
    )
    base.update(kwargs)
    return Job(**base)


def test_pte_ltd_aliases_share_identity():
    a = make_job(company="Acme Pte Ltd")
    b = make_job(company="Acme Private Limited")
    assert "Pte" not in strip_pte_ltd("Acme Pte. Ltd.")
    assert job_identity(a) == job_identity(b)


def test_merge_new_then_seen_and_union_sources(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    first = store.merge(make_job(), now=NOW)
    assert first.raw["status"] == "new"
    second = store.merge(
        make_job(
            source="lever",
            sources=["lever"],
            source_type="board",
            description="Much longer description with details about the team and stack.",
            posted_at=NOW - timedelta(days=1),
            url="https://jobs.lever.co/acme/abc",
        ),
        now=NOW + timedelta(hours=1),
    )
    assert second.raw["status"] == "seen"
    assert set(second.sources) >= {"greenhouse", "lever"}
    assert "Much longer" in second.description
    assert second.posted_at == NOW - timedelta(days=1)
    store.close()


def test_merge_prefers_company_site_apply_url(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.merge(
        make_job(
            source="glints",
            source_type="board",
            apply_url="https://glints.com/job/1",
            url="https://glints.com/job/1",
        ),
        now=NOW,
    )
    merged = store.merge(
        make_job(
            source="company",
            source_type="company_site",
            apply_url="https://careers.acme.com/ds",
            url="https://careers.acme.com/ds",
            description="Richer company careers text for the same role.",
        ),
        now=NOW + timedelta(minutes=5),
    )
    assert merged.apply_url == "https://careers.acme.com/ds"
    assert merged.source_type == "company_site"
    # Later board sighting must not overwrite company_site apply URL.
    again = store.merge(
        make_job(
            source="mcf",
            source_type="mcf",
            apply_url="https://www.mycareersfuture.gov.sg/job/xyz",
            url="https://www.mycareersfuture.gov.sg/job/xyz",
        ),
        now=NOW + timedelta(minutes=10),
    )
    assert again.apply_url == "https://careers.acme.com/ds"
    store.close()


def test_merge_preserves_fs_when_either_sighting_is_fs(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.merge(make_job(raw={"fs": False}), now=NOW)
    merged = store.merge(make_job(raw={"fs": True}, description="fs bank role"), now=NOW)
    assert merged.raw.get("fs") is True
    store.close()


def test_pending_includes_failed_scores_not_successful(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    a = store.merge(make_job(title="Data Scientist", url="https://ex/a"), now=NOW)
    b = store.merge(
        make_job(title="ML Engineer", company="Beta", url="https://ex/b"),
        now=NOW,
    )
    c = store.merge(
        make_job(title="AI Engineer", company="Gamma", url="https://ex/c"),
        now=NOW,
    )
    store.save_score(a, Score(value=80, verdict="fit", model="test"))
    store.save_score(b, Score(value=0, error="timeout", model="test"))
    pending = store.pending_jobs()
    titles = {job.title for job in pending}
    assert "Data Scientist" not in titles
    assert "ML Engineer" in titles
    assert "AI Engineer" in titles
    # Failed score must remain retryable — get_score still returns the error row.
    failed = store.get_score(b)
    assert failed is not None and failed.error == "timeout" and not failed.ok
    # Successful score still readable; discovery status stays independent.
    assert store.get_score(a) is not None and store.get_score(a).ok
    assert a.raw["status"] == "new"
    store.close()


def test_save_score_does_not_mark_discovery_seen(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    job = store.merge(make_job(), now=NOW)
    assert job.raw["status"] == "new"
    store.save_score(job, Score(value=0, error="parse failure"))
    pending = store.pending_jobs()
    assert len(pending) == 1
    assert pending[0].raw.get("status") == "new"
    store.close()


def test_recent_jobs_weekly_window(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.merge(make_job(title="Data Scientist"), now=NOW - timedelta(days=2))
    store.merge(
        make_job(company="Other Co", title="ML Engineer"),
        now=NOW - timedelta(days=10),
    )
    recent = store.recent_jobs(NOW - timedelta(days=7))
    titles = {job.title for job in recent}
    assert "Data Scientist" in titles
    assert "ML Engineer" not in titles
    store.close()


def test_record_mcf_candidates_only_established(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.record_mcf("NewCo Pte Ltd", NOW, established=False)
    store.record_mcf("Solid Bank Pte Ltd", NOW, established=True)
    store.record_mcf("Solid Bank Private Limited", NOW + timedelta(days=1), established=True)
    candidates = store.candidate_companies(since=NOW - timedelta(days=1))
    names = [row["company"] for row in candidates]
    assert any("Solid Bank" in name for name in names)
    assert not any("NewCo" in name for name in names)
    row = next(r for r in candidates if "Solid Bank" in r["company"])
    assert row["first_seen"]
    assert row["last_seen"]
    assert row["status"] == "candidate"
    # Never claim sponsorship.
    assert "sponsor" not in row
    assert "verified" not in row.get("status", "")
    store.close()


def test_candidate_companies_without_since_returns_all_established(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.record_mcf("Old Established", NOW - timedelta(days=30), established=True)
    all_rows = store.candidate_companies()
    assert len(all_rows) == 1
    store.close()


def test_old_roles_and_candidates_do_not_become_new_when_seen_again(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.merge(make_job(), now=NOW - timedelta(days=30))
    store.merge(make_job(), now=NOW)
    store.record_mcf("Established Firm", NOW - timedelta(days=30), established=True)
    store.record_mcf("Established Firm", NOW, established=True)
    assert store.recent_jobs(NOW - timedelta(days=7)) == []
    assert store.candidate_companies(NOW - timedelta(days=7)) == []
    assert len(store.candidate_companies()) == 1
    store.close()


def test_normalized_title_dedupes_parentheticals(tmp_path):
    store = SingaporeStore(tmp_path / "sg.sqlite3")
    store.merge(make_job(title="Data Scientist (Singapore)"), now=NOW)
    merged = store.merge(make_job(title="Data Scientist"), now=NOW)
    assert merged.raw["status"] == "seen"
    assert len(store.recent_jobs(NOW - timedelta(days=1))) == 1
    store.close()
