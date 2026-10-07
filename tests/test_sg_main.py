"""Offline orchestration contracts: policy gates, scoring retry and quiet runs."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.models import Job, Score, ScoredJob
from src.singapore.main import _coalesce, _source_rules, run_monitor

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def company(name="Acme", **updates):
    return {"name": name, "ats": "greenhouse", "fs": False, "status": "verified",
            "automation_allowed": True, "verified_url": "https://example.com/jobs",
            "robots_url": "https://example.com/robots.txt", "terms_url": "https://example.com/terms",
            "evidence": {"checked_at": NOW.isoformat(), "http_status": 200}, **updates}


class Store:
    def __init__(self):
        self.jobs = {}
        self.scores = {}
        self.radar = []

    def merge(self, job, now=None):
        key = (job.company, job.title)
        job.raw["status"] = "seen" if key in self.jobs else "new"
        self.jobs[key] = job
        return job

    def pending_jobs(self):
        return [j for key, j in self.jobs.items() if key not in self.scores or not self.scores[key].ok]

    def save_score(self, job, score):
        self.scores[(job.company, job.title)] = score

    def get_score(self, job):
        return self.scores.get((job.company, job.title))

    def recent_jobs(self, since):
        return list(self.jobs.values())

    def candidate_companies(self, since=None):
        return []

    def record_mcf(self, company, observed_at, established=False):
        self.radar.append((company, established))


def prepare(tmp_path, monkeypatch, jobs):
    from src.singapore import adapters, filters
    calls = []
    monkeypatch.setattr(adapters, "get_adapter", lambda ats: SimpleNamespace(
        fetch=lambda entry, client: calls.append(entry["name"]) or jobs))
    monkeypatch.setattr(filters, "apply_filters", lambda values, settings, now=None:
                        SimpleNamespace(kept=list(values), rejected=[], counts={}))
    cfg = Config(root=tmp_path)
    cfg.data["output"]["dir"] = str(tmp_path)
    cv = tmp_path / "cv.md"
    cv.write_text("# Candidate\nData Scientist, forecasting, causal inference, SQL and Python.")
    return cfg, cv, calls


def test_manual_and_unverified_never_reach_an_adapter(tmp_path, monkeypatch):
    cfg, cv, calls = prepare(tmp_path, monkeypatch, [])
    result = run_monitor(cfg, [company(status="unverified"), company("Blocked", automation_allowed=False)],
                         [], {}, now=NOW, mode="weekly", client=object(), store=Store(), cv_path=cv)
    assert calls == [] and result["companies_checked"] == []
    assert Path(result["digest_path"]).exists()


def test_scorer_is_reused_and_discovery_is_persistent(tmp_path, monkeypatch):
    jobs = [Job("greenhouse", "Acme", "Data Scientist", "https://example.com/job", location="Singapore")]
    cfg, cv, calls = prepare(tmp_path, monkeypatch, jobs)
    scored_batches = []
    def scorer(batch, profile, config, **kwargs):
        scored_batches.append(list(batch))
        assert "forecasting" in profile
        return [ScoredJob(job, Score(88, verdict="Strong forecasting fit")) for job in batch]
    monkeypatch.setattr("src.singapore.main.score_jobs", scorer)
    tracker = Store()
    first = run_monitor(cfg, [company()], [], {}, now=NOW, mode="weekly", client=object(), store=tracker, cv_path=cv)
    second = run_monitor(cfg, [company()], [], {}, now=NOW, mode="weekly", client=object(), store=tracker, cv_path=cv)
    assert first["new_jobs"] == 1 and second["new_jobs"] == 0
    assert len(scored_batches) == 1
    assert "88/100" in Path(second["digest_path"]).read_text()


def test_daily_quiet_run_has_log_and_no_digest(tmp_path, monkeypatch):
    cfg, cv, calls = prepare(tmp_path, monkeypatch, [])
    result = run_monitor(cfg, [company()], [], {}, now=NOW, mode="daily", client=object(), store=Store(), cv_path=cv)
    assert result["digest_path"] is None and not calls
    assert (tmp_path / "singapore_runs.jsonl").exists()


def test_failed_scoring_remains_pending_for_retry(tmp_path, monkeypatch):
    job = Job("greenhouse", "Acme", "Data Scientist", "https://example.com/job", location="Singapore")
    cfg, cv, calls = prepare(tmp_path, monkeypatch, [job])
    batches = []
    def scorer(batch, *args, **kwargs):
        batches.append(list(batch))
        return [ScoredJob(j, Score(0, error="temporary unavailable")) for j in batch]
    monkeypatch.setattr("src.singapore.main.score_jobs", scorer)
    tracker = Store()
    for _ in range(2):
        run_monitor(cfg, [company()], [], {}, now=NOW, mode="weekly", client=object(), store=tracker, cv_path=cv)
    assert len(batches) == 2 and tracker.pending_jobs()


def test_mcf_radar_includes_knocked_out_ds_but_rejects_undated(tmp_path, monkeypatch):
    from src.singapore import filters
    old = Job("mcf", "Old", "Data Scientist", "https://example.com/old", location="Singapore", posted_at=NOW - timedelta(days=8))
    senior = Job("mcf", "Established", "Senior Data Scientist", "https://example.com/senior", location="Singapore", posted_at=NOW,
                 raw={"established": True})
    cfg, cv, calls = prepare(tmp_path, monkeypatch, [old, senior])
    monkeypatch.setattr(filters, "apply_filters", lambda values, settings, now=None: SimpleNamespace(
        kept=[], rejected=[(j, "seniority") for j in values], counts={"seniority": 1}))
    tracker = Store()
    entry = {**company("MCF"), "id": "mcf", "source_type": "mcf", "frequency": "daily", "type": "json"}
    result = run_monitor(cfg, [], [entry], {}, now=NOW, mode="daily", client=object(), store=tracker, cv_path=cv)
    assert tracker.radar == [("Established", True)]
    assert result["jobs_filtered"] == 2
    assert result["filter_counts"]["mcf_outside_last_7_days_or_undated"] == 1


def test_recruiter_requires_client_evidence_and_small_company_is_flagged():
    recruiter = Job("hays", "Hays", "Data Scientist", "https://example.com/job")
    kept, rejected = _source_rules([recruiter], {"source_type": "recruiter", "fs": True})
    assert not kept and rejected[0][1] == "unverified_recruiter_client"
    startup = Job("glints", "Named Startup", "Data Scientist", "https://example.com/job")
    kept, rejected = _source_rules([startup], {"id": "glints", "source_type": "board"})
    assert not rejected and "small_company" in kept[0].flags


def test_cross_source_merge_preserves_requirements_and_company_application():
    board = Job("mcf", "Acme Pte Ltd", "Data Scientist", "https://example.com/board",
                location="Singapore", source_type="mcf", description="Python and SQL.", raw={"fs": True})
    direct = Job("greenhouse", "Acme", "Data Scientist", "https://example.com/direct",
                 location="Singapore", description="At least 5 years of experience.", raw={"fs": False})
    merged = _coalesce([board, direct])
    assert len(merged) == 1
    assert merged[0].apply_url == direct.url and merged[0].source_type == "company_site"
    assert merged[0].sources == ["mcf", "greenhouse"] and merged[0].raw["fs"] is True
    assert "At least 5 years" in merged[0].description and "Python" in merged[0].description
    assert board.description == "Python and SQL."  # caller-owned fixtures stay intact


def test_foreign_same_title_does_not_hide_singapore_role(tmp_path, monkeypatch):
    jobs = [Job("greenhouse", "Acme", "Data Scientist", "https://example.com/uk", location="London", description="At least 8 years required."),
            Job("greenhouse", "Acme", "Data Scientist", "https://example.com/sg", location="Singapore", description="Python and SQL.")]
    cfg, cv, calls = prepare(tmp_path, monkeypatch, jobs)
    tracker = Store()
    result = run_monitor(cfg, [company()], [], {}, now=NOW, mode="weekly", client=object(), store=tracker, cv_path=cv, fetch_only=True)
    assert result["new_jobs"] == 1
    assert result["filter_counts"] == {"location_not_singapore": 1}
    assert tracker.jobs[("Acme", "Data Scientist")].apply_url == "https://example.com/sg"
