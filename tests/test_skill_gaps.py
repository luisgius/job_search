"""`python -m src.track gaps`: the scorer's gaps, counted against the CV.

Covers the v7 column round trip, the reasons fallback for rows scored before
it, the CV check (a skill the CV shows is never a gap), near-miss ranking and
the CLI.
"""

from __future__ import annotations

from datetime import date, timedelta

from src import track
from src.db import Tracker
from src.models import ApplyStatus
from src.skill_gaps import build_report, missing_clauses, report_lines, terms_in
from tests.conftest import NOW, make_job, write_config

CV = "Python, SQL, AWS, Azure, Spark, Presto, Docker, PyTorch, LightGBM. 2 years."


def row(score, gaps=(), reasons=(), company="Acme", title="Data Scientist"):
    return {"score": score, "gaps": list(gaps), "reasons": list(reasons),
            "company": company, "title": title}


def test_a_skill_the_cv_shows_is_never_a_gap():
    gap = "Missing Google Cloud Platform (GCP) experience; CV only lists AWS and Azure."
    assert terms_in([gap], CV) == {"GCP"}


def test_years_of_experience_is_a_gap_even_though_the_cv_states_years():
    assert terms_in(["Requires 5+ years in production ML; the CV shows 2."], CV) \
        == {"Years of experience"}


def test_case_sensitive_terms_do_not_fire_on_ordinary_words():
    assert terms_in(["Posting says go to market and R&D; nothing missing."], "") == set()
    assert terms_in(["No Go or R experience"], "") == {"Go", "R"}
    assert terms_in(["Java is required"], "") == {"Java"}
    assert terms_in(["JavaScript is required"], "") == {"TypeScript/JavaScript"}


def test_reasons_are_read_only_in_clauses_that_say_something_is_missing():
    reasons = [
        "Posting requires 'Kubernetes', 'GCP'; CV lists 'AWS', 'Spark', but does "
        "not mention Kubernetes or GCP.",
        "CV shows Databricks-like work at Uber.",
    ]
    clauses = missing_clauses(reasons)
    assert clauses == ["CV lists 'AWS', 'Spark', but does not mention Kubernetes or GCP."]
    assert terms_in(clauses, CV) == {"Kubernetes", "GCP"}


def test_near_misses_rank_first_and_minor_gaps_are_marked():
    rows = [
        row(15, gaps=["Rust is the main language; the CV has none."]),
        row(20, gaps=["Rust required."]),
        row(58, gaps=["No GCP."]),
        row(40, reasons=["No experience with BigQuery."]),
        row(70, gaps=["Lacks Tableau."]),
        row(None, gaps=["Lacks Rust."]),  # never scored: ignored
    ]
    report = build_report(rows, cv_text=CV, threshold=65, since=NOW,
                          minor_gaps=["Google Cloud (GCP), Vertex AI (CV has AWS)"])
    names = [gap.name for gap in report.gaps]
    assert names[0] == "GCP"          # the one near miss leads despite 1 job
    assert names[1] == "Rust"         # then by job count
    gcp = report.gaps[0]
    assert gcp.minor and gcp.near_misses == 1 and gcp.average == 58
    assert report.scored == 5
    text = "\n".join(report_lines(report))
    assert "GCP" in text and "tool (minor)" in text and "50-64" in text


def test_a_row_whose_gaps_match_no_term_is_counted_as_unrecognised():
    report = build_report([row(30, gaps=["Wants a driving licence."])],
                          cv_text=CV, threshold=65, since=NOW)
    assert report.gaps == [] and report.unrecognised == 1
    assert "No gap from the vocabulary" in "\n".join(report_lines(report))


def test_score_gaps_survive_a_later_recording_without_them(tmp_path):
    config = write_config(tmp_path)
    job = make_job(company="Acme", ats_job_id="1")
    with Tracker(config.db_path) as tracker:
        tracker.record_job(job, now=NOW)
        tracker.record_status(job.key, ApplyStatus.DIGEST, score=58,
                              score_reasons=["fits"], score_gaps=["No GCP."], now=NOW)
        tracker.record_status(job.key, ApplyStatus.DIGEST, score=58, now=NOW)
        [scored] = tracker.scored_applications((NOW - timedelta(days=1)).isoformat())
    assert scored["gaps"] == ["No GCP."] and scored["reasons"] == ["fits"]
    assert scored["company"] == "Acme" and scored["score"] == 58


def test_the_gaps_command_prints_the_table(tmp_path, capsys):
    config = write_config(tmp_path)
    job = make_job(company="Acme", title="Data Scientist", ats_job_id="1")
    with Tracker(config.db_path) as tracker:
        tracker.record_job(job, now=NOW)
        # The test CV (tests/conftest.py BASE_CV) lists Kafka, not Snowflake.
        tracker.record_status(job.key, ApplyStatus.DIGEST, score=55,
                              score_gaps=["Kafka is fine; Snowflake is missing."], now=NOW)
    argv = ["gaps", "--days", "30", "--config", str(tmp_path / "config.yaml"),
            "--watchlist", str(tmp_path / "watchlist.yaml")]
    assert track.main(argv, today=date(2026, 8, 4), now=NOW) == 0
    out = capsys.readouterr().out
    assert "Snowflake" in out and "Kafka" not in out.split("Behind")[0]
    assert "Acme - Data Scientist (55)" in out
    assert track.main(["gaps", "--days", "0", *argv[3:]], today=date(2026, 8, 4), now=NOW) == 2
