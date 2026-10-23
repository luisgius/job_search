"""`python -m src.track dashboard`: the tracker's history as one offline page.

The page is read weeks into a search, so it is tested for the two ways it
could mislead: a section that silently vanishes (a brand-new tracker must
still produce a page that says plainly what is missing), and numbers that
drift from the code that already computes them. And it renders text strangers
wrote into a file:// page, so it is tested the way a hostile posting would
try to break it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from src import dashboard, track
from src.db import Tracker
from src.models import ApplyStatus
from tests.conftest import NOW, make_job, write_config


@pytest.fixture
def config(tmp_path: Path):
    return write_config(tmp_path, {"scoring": {"threshold": 70}})


@pytest.fixture
def tracker(config):
    with Tracker(config.db_path) as tracker:
        yield tracker


def scored(tracker, company: str, score: int, *, gaps=(), job_id: str = "") -> str:
    job = make_job(company=company, ats_job_id=job_id or company)
    tracker.record_job(job, now=NOW)
    status = ApplyStatus.DIGEST if score >= 70 else ApplyStatus.SCORED_BELOW
    tracker.record_status(job.key, status, score=score, score_gaps=list(gaps), now=NOW)
    return job.key


def applied(tracker, company: str, *stages: tuple[str, str], url: str | None = None) -> str:
    job = make_job(company=company, ats_job_id=company, url=url)
    tracker.record_job(job, now=NOW)
    for stage, on in stages:
        tracker.record_event(job.key, stage, on=on, now=NOW)
    return job.key


def section(page: str, anchor: str) -> str:
    """The HTML of one `<section id=…>`, so an assertion cannot pass on text
    that belongs to another section."""
    match = re.search(rf'<section id="{anchor}">(.*?)</section>', page, re.S)
    assert match, f"no section {anchor}"
    return match.group(1)


# ==========================================================================
# an empty tracker
# ==========================================================================


def test_a_brand_new_tracker_produces_a_complete_page_that_says_what_is_missing(
        tracker, config):
    page = dashboard.build_dashboard(tracker, config, now=NOW)
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</html>")
    assert "No application recorded yet" in section(page, "funnel")
    assert "Nothing due" in section(page, "followups")
    assert "Nothing tracked yet" in section(page, "applications")
    assert "No job scored in the last 90 days" in section(page, "scores")
    assert "No finished run" in section(page, "sources")
    assert "no gaps to count" in section(page, "gaps")
    assert "None" not in page and "<svg" not in page


def test_the_page_loads_nothing_and_runs_nothing(tracker, config):
    applied(tracker, "Acme", ("applied", "2026-07-01"))
    scored(tracker, "Beta", 80)
    page = dashboard.build_dashboard(tracker, config, now=NOW)
    lowered = page.lower()
    assert "<script" not in lowered and "<link" not in lowered
    assert "@import" not in lowered and "url(" not in lowered and " src=" not in lowered
    assert "prefers-color-scheme: dark" in page


# ==========================================================================
# untrusted text
# ==========================================================================


def test_a_hostile_company_name_and_a_javascript_url_come_out_inert(tracker, config):
    applied(tracker, "<script>alert(1)</script>‮evil", ("applied", "2026-07-01"),
            url="javascript:alert(document.cookie)")
    applied(tracker, "Tabbed", ("applied", "2026-07-02"), url="java\tscript:alert(1)")
    page = dashboard.build_dashboard(tracker, config, now=NOW)
    assert "<script>" not in page.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;evil" in section(page, "applications")
    assert "‮" not in page
    hrefs = re.findall(r'href="([^"]*)"', page)
    assert all(href.startswith(("#", "https://", "http://")) for href in hrefs), hrefs
    # Refused, but still shown as text so the row says what the tracker holds.
    assert "javascript:alert(document.cookie)" in section(page, "applications")


def test_a_link_shows_its_real_host_not_a_lookalike():
    assert dashboard._link("https://greenhouse.io@evil.example/jobs/1") == {
        "href": "https://greenhouse.io@evil.example/jobs/1", "text": "evil.example"}
    assert dashboard._link("data:text/html,hi")["href"] == ""
    assert dashboard._link("") == {"href": "", "text": ""}


# ==========================================================================
# sections with data
# ==========================================================================


def test_the_funnel_counts_each_application_at_its_furthest_stage(tracker, config):
    applied(tracker, "Acme", ("applied", "2026-07-01"), ("interview", "2026-07-10"),
            ("rejected", "2026-07-20"))
    applied(tracker, "Beta", ("applied", "2026-07-02"), ("no_response", "2026-07-30"))
    applied(tracker, "Gamma", ("applied", "2026-07-03"))
    funnel = section(dashboard.build_dashboard(tracker, config, now=NOW), "funnel")
    assert "<title>applied: 3</title>" in funnel
    assert "<title>reached interview: 1</title>" in funnel
    assert "<title>hired: 0</title>" in funnel
    assert "Rejected or no response: <strong>2/2</strong> final outcomes" in funnel
    assert "<td>greenhouse</td><td class=\"num\">3</td>" in funnel


def test_applications_are_listed_newest_activity_first_with_their_stage(tracker, config):
    applied(tracker, "Older", ("applied", "2026-06-01"))
    applied(tracker, "Newer", ("applied", "2026-07-01"), ("interview", "2026-07-15"))
    table = section(dashboard.build_dashboard(tracker, config, now=NOW), "applications")
    assert table.index("Newer") < table.index("Older")
    assert "<td>interview</td>" in table and "2026-07-15" in table
    assert 'href="https://boards.greenhouse.io/newer/jobs/Newer"' in table


def test_scores_fall_into_ten_point_buckets_with_the_threshold_marked(tracker, config):
    for index, score in enumerate((12, 55, 64, 69, 70, 71, 100)):
        scored(tracker, f"Co{index}", score)
    scores = section(dashboard.build_dashboard(tracker, config, now=NOW), "scores")
    assert "<strong>3</strong> of 7 scored jobs at or above the threshold of 70" in scores
    assert "<title>60–69: 2 jobs</title>" in scores
    assert "<title>90–100: 1 job</title>" in scores  # 100 joins the 90s
    assert "<title>0–9: 0 jobs</title>" in scores
    assert "threshold 70</text>" in scores
    # 70 of 0-100 across the plot: left edge 12, plot width 616.
    assert 'class="marker" x1="443.2"' in scores


def test_scores_older_than_the_window_are_left_out(tracker, config):
    key = scored(tracker, "Stale", 90)
    tracker.conn.execute("UPDATE applications SET updated_at = ? WHERE key = ?",
                         ((NOW - timedelta(days=120)).isoformat(), key))
    tracker.conn.commit()
    scored(tracker, "Fresh", 40)
    scores = section(dashboard.build_dashboard(tracker, config, now=NOW, days=30), "scores")
    assert "of 1 scored jobs" in scores and "last 30 days" in scores


def test_source_yield_sums_recent_runs_and_shows_the_last_one(tracker, config):
    first = tracker.start_run(now=NOW - timedelta(days=1))
    tracker.finish_run(first, {"source_counts": {"greenhouse": 30, "lever": 5, "ashby": 400},
                               "source_after_filters": {"greenhouse": 6, "lever": 1,
                                                        "ashby": 1}},
                       now=NOW - timedelta(days=1))
    broken = tracker.start_run(now=NOW - timedelta(hours=12))
    tracker.conn.execute("UPDATE runs SET stats_json = 'not json' WHERE id = ?", (broken,))
    tracker.conn.commit()
    last = tracker.start_run(now=NOW)
    tracker.finish_run(last, {"source_counts": {"greenhouse": 10},
                              "source_after_filters": {"greenhouse": 3}}, now=NOW)
    tracker.start_run(now=NOW)  # still running: no stats yet

    sources = section(dashboard.build_dashboard(tracker, config, now=NOW), "sources")
    cells = lambda name: re.findall(  # noqa: E731
        r"<td[^>]*>([^<]*)</td>",
        re.search(rf"<tr>\s*<td>{name}</td>.*?</tr>", sources, re.S).group(0))
    assert cells("greenhouse") == ["greenhouse", "2", "40", "9", "22%", "10", "3"]
    assert cells("lever") == ["lever", "1", "5", "1", "20%", "—", "—"]
    assert cells("ashby")[4] == "&lt;1%"  # "<1%", escaped: a thin yield is not 0%
    assert "2 finished runs" in sources


def test_skill_gaps_rank_near_misses_first_and_mark_minor_ones(tmp_path):
    config = write_config(tmp_path, {"scoring": {"threshold": 70,
                                                 "minor_gaps": ["Google Cloud (GCP)"]}})
    with Tracker(config.db_path) as tracker:
        # The test CV (tests/conftest.py BASE_CV) lists Kafka, not Snowflake or GCP.
        scored(tracker, "Acme", 62, gaps=["No GCP."])
        scored(tracker, "Beta", 20, gaps=["Snowflake is missing."])
        scored(tracker, "Gamma", 30, gaps=["Snowflake required."])
        scored(tracker, "Delta", 66, gaps=["Kafka is fine."])
        gaps = section(dashboard.build_dashboard(tracker, config, now=NOW), "gaps")
    assert gaps.index("<td>GCP</td>") < gaps.index("<td>Snowflake</td>")
    assert "tool <span class=\"tag\">minor</span>" in gaps
    assert "Acme – Backend Engineer (62)" in gaps
    assert "<td>Kafka</td>" not in gaps
    assert "scored 55–69" in gaps


def test_a_quiet_application_appears_as_a_follow_up_with_its_command(tracker, config):
    key = applied(tracker, "Acme", ("applied", "2026-07-01"))
    applied(tracker, "Fresh", ("applied", "2026-08-01"))
    followups = section(dashboard.build_dashboard(tracker, config, now=NOW), "followups")
    assert "Acme — Backend Engineer" in followups and "Fresh" not in followups
    assert f"python -m src.track mark {key[:8]} followed_up" in followups


# ==========================================================================
# writing
# ==========================================================================


def test_write_dashboard_writes_the_page_and_replaces_an_old_one(tracker, config, tmp_path):
    target = tmp_path / "reports" / "dashboard.html"
    target.parent.mkdir()
    target.write_text("yesterday's page", encoding="utf-8")
    applied(tracker, "Acme", ("applied", "2026-07-01"))
    path = dashboard.write_dashboard(tracker, config, target, now=NOW)
    assert path == target
    text = target.read_text(encoding="utf-8")
    assert text == dashboard.build_dashboard(tracker, config, now=NOW)
    assert "Acme" in text and "yesterday's page" not in text


@pytest.mark.skipif("dashboard" not in track.COMMANDS,
                    reason="`dashboard` is not wired into src/track.py yet")
def test_the_dashboard_command_writes_to_the_output_dir_unless_told_otherwise(
        tmp_path, capsys):
    config = write_config(tmp_path)
    with Tracker(config.db_path) as tracker:
        applied(tracker, "Acme", ("applied", "2026-07-01"))
    argv = ["dashboard", "--config", str(tmp_path / "config.yaml"),
            "--watchlist", str(tmp_path / "watchlist.yaml")]
    assert track.main(argv, today=date(2026, 8, 4), now=NOW) == 0
    assert "Acme" in (config.output_dir / "dashboard.html").read_text(encoding="utf-8")
    assert "wrote" in capsys.readouterr().out
    elsewhere = tmp_path / "elsewhere.html"
    assert track.main([*argv, "--out", str(elsewhere)], today=date(2026, 8, 4), now=NOW) == 0
    assert elsewhere.is_file()
    assert track.main([*argv, "--days", "0"], today=date(2026, 8, 4), now=NOW) == 2
