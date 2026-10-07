from datetime import datetime, timezone

from src.models import Job, Score, ScoredJob
from src.singapore.digest import render_digest, write_digest

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def render(items):
    return render_digest(items, now=NOW, weekly=True, stats={}, candidates=[], companies=[], sources=[])


def test_digest_prefers_established_companies_and_shows_provenance():
    small = Job("glints", "Small Co", "Data Scientist", "https://example.com/job/1",
                flags=["small_company"], source_type="board")
    established = Job("workday", "Established Co", "Data Scientist", "https://example.com/job/2",
                      apply_url="https://example.com/apply/2", sources=["workday", "mcf"],
                      posted_at=NOW, flags=["2–3 years"])
    content = render([ScoredJob(small, Score(95, verdict="Strong fit")),
                      ScoredJob(established, Score(85, verdict="Forecasting and causal inference fit"))])
    assert content.index("Established Co") < content.index("Small Co")
    assert "https://example.com/apply/2" in content
    assert "Sources: workday, mcf" in content
    assert "85/100" in content and "2–3 years" in content
    assert "LinkedIn" in content and "does not establish" in content


def test_scoring_failure_has_no_invented_numeric_score():
    job = Job("ashby", "Acme", "Data Scientist", "https://example.com/jobs/1")
    content = render([ScoredJob(job, Score(0, error="Missing API key"))])
    assert "unscored" in content and "Missing API key" in content
    assert "0/100" not in content


def test_manual_policies_and_warsaw_filename(tmp_path):
    content = render_digest([], now=NOW, weekly=False, stats={}, candidates=[], companies=[
        {"name": "Blocked", "status": "unverified", "notes": "robots denies API"}
    ], sources=[{"name": "MCF", "status": "manual_check", "manual_alert": "Daily saved search"}])
    assert "Daily saved search" in content and "Blocked (unverified)" in content
    moment = datetime(2026, 10, 7, 23, tzinfo=timezone.utc)
    path = write_digest(content, tmp_path, moment)
    assert path.name == "digest_singapore_2026-10-08.md"
    assert path.read_text() == content


def test_unsafe_apply_link_never_becomes_a_markdown_link():
    job = Job("custom", "Acme", "Data Scientist", "javascript:alert(1)")
    content = render([ScoredJob(job, Score(50))])
    assert "javascript:" not in content
