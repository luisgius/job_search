"""Tests for src/main.py — the whole pipeline, end to end, offline.

`run_pipeline` takes four seams (`tracker=`, `llm_client=`, `now=`,
`sources=`), and with those supplied the entire run — fetch, dedupe, filter,
tracker gate, score, tailor, PDF, apply, digest — executes with no network,
no API key and no browser. That is what makes an integration test of this
shape possible at all, so the first thing tested is that it really is
hermetic.

The properties that matter here are the ones no unit test can see:

  * the stages compose in the right order and each degrades independently —
    a dead source, a broken filter or a failed digest must not abort the run;
  * the funnel adds up, because those numbers are the only way a user can
    tell a quiet day from a broken pipeline;
  * running twice does not re-surface yesterday's jobs, which is the whole
    reason the tracker exists.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from src import main as main_module
from src.config import ConfigError
from src.db import Tracker
from src.main import (
    apply_cli_overrides,
    build_parser,
    format_summary,
    main,
    run_pipeline,
)
from src.models import ApplyStatus
from tests.conftest import (
    BASE_CV,
    NOW,
    FakeSession,
    json_response,
    llm_client,
    load_json_fixture,
    make_job,
    write_config,
)

GREENHOUSE = load_json_fixture("greenhouse_jobs.json")

SCORE_JSON = json.dumps({
    "score": 88, "verdict": "Strong fit",
    "reasons": ["CV shows 8y Python, posting asks 5+"],
    "strengths": ["Python"], "gaps": ["No Kafka"],
})
TAILORED = "# Ada Lovelace\n\n## Summary\nSenior backend engineer.\n"
COVER = "Dear team,\n\nI cut p99 latency 840ms to 210ms.\n\nAda Lovelace\n"

# score, cv, cover — repeated for each job; the last entry repeats forever.
LLM_SCRIPT = [SCORE_JSON, TAILORED, COVER, SCORE_JSON]


def pipeline_config(tmp_path: Path, **overrides):
    base = {
        "sources": {"greenhouse": True, "lever": False,
                    "adzuna": False, "linkedin_email": False},
        "output": {"dir": str(tmp_path / "output"), "open_browser": False},
        "db": {"path": str(tmp_path / "output" / "tracker.sqlite3"),
               "skip_seen_days": 30},
        "scoring": {"threshold": 65, "concurrency": 1},
        "tailoring": {"enabled": True, "max_per_run": 10},
        "apply": {"enabled": False},     # no browser in the default test run
        "freshness": {"max_age_hours": 24},
    }
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return write_config(tmp_path, base, watchlist={"greenhouse": ["acme"]})


@pytest.fixture
def stub_sources(monkeypatch):
    """Replace the network-facing source fetchers.

    `run_pipeline` has no `session=` seam (sources read it from config), so
    this is the one place the suite patches module attributes — and it patches
    the *public* fetchers, exactly as a caller would see them.
    """
    calls: list[str] = []
    results: dict[str, object] = {}

    def make(name):
        def _fetch(config, **kwargs):
            calls.append(name)
            outcome = results.get(name, [])
            if isinstance(outcome, Exception):
                raise outcome
            return list(outcome)
        return _fetch

    monkeypatch.setattr(main_module.ats_boards, "fetch", make("ats_boards"))
    monkeypatch.setattr(main_module.adzuna, "fetch", make("adzuna"))
    monkeypatch.setattr(main_module.linkedin_email, "fetch", make("linkedin_email"))
    make.calls = calls          # type: ignore[attr-defined]
    make.results = results      # type: ignore[attr-defined]
    return make


def fresh_jobs(n=3):
    return [make_job(company=f"Company{i}", title="Backend Engineer",
                     location="Berlin, Germany", hours_old=2, ats_job_id=str(i))
            for i in range(n)]


# ==========================================================================
# the happy path
# ==========================================================================


def test_a_full_run_produces_a_digest(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    cfg = pipeline_config(tmp_path)

    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))

    assert len(scored) == 2
    assert stats.fetched == 2
    assert stats.matches == 2
    digest = Path(stats.digest_path)
    assert digest.exists()
    assert "Company0" in digest.read_text(encoding="utf-8")


def test_the_run_is_hermetic(tmp_path: Path, stub_sources, memory_tracker):
    """No network, no key, no browser — asserted by the absence of any real
    session, and by the run completing with a fake LLM client."""
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = pipeline_config(tmp_path, keys={"anthropic": ""})
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))
    assert len(scored) == 1


def test_tailored_documents_land_next_to_each_other(tmp_path: Path, stub_sources,
                                                    memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = pipeline_config(tmp_path)
    scored, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                             llm_client=llm_client(LLM_SCRIPT))

    directory = Path(scored[0].artifacts.dir)
    assert (directory / "cv.md").exists()
    assert (directory / "cover_letter.md").exists()
    assert (directory / "job.json").exists()


def test_the_funnel_adds_up(tmp_path: Path, stub_sources, memory_tracker):
    """These numbers are the only way to tell a quiet day from a broken run,
    so they have to be internally consistent."""
    jobs = fresh_jobs(2) + [
        make_job(company="Stale", location="Berlin, Germany", hours_old=99,
                 ats_job_id="s"),
        make_job(company="US", location="San Francisco, CA", hours_old=1,
                 ats_job_id="u"),
    ]
    stub_sources.results["ats_boards"] = jobs
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))

    assert stats.fetched == 4
    assert stats.after_dedupe == 4
    assert stats.after_filters == 2
    assert stats.scored == 2
    assert stats.matches == 2
    assert stats.filter_counts == {"stale": 1, "location_outside_eu": 1}
    assert stats.matches == stats.auto_applied + stats.dry_run + stats.digest_items


def test_source_counts_are_recorded(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(3)
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert sum(stats.source_counts.values()) == 3


# ==========================================================================
# the tracker gate
# ==========================================================================


def test_a_second_run_does_not_resurface_the_same_jobs(tmp_path: Path, stub_sources,
                                                       memory_tracker):
    """The reason the tracker exists. Without it every morning's digest is
    yesterday's digest plus one."""
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    cfg = pipeline_config(tmp_path)

    first, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                            llm_client=llm_client(LLM_SCRIPT))
    # Two hours later, not a day: the postings must still be *fresh*, so that
    # the tracker gate is demonstrably what drops them rather than the
    # freshness filter quietly doing it for the wrong reason.
    second, stats = run_pipeline(cfg, tracker=memory_tracker,
                                 now=NOW + timedelta(hours=2),
                                 llm_client=llm_client(LLM_SCRIPT))

    assert len(first) == 2
    assert second == []
    assert stats.after_filters == 2      # they survived every hard filter ...
    assert stats.already_seen == 2       # ... and the tracker is what stopped them


def test_jobs_resurface_once_the_window_expires(tmp_path: Path, stub_sources,
                                                memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = pipeline_config(tmp_path, db={"skip_seen_days": 7})

    run_pipeline(cfg, tracker=memory_tracker, now=NOW, llm_client=llm_client(LLM_SCRIPT))
    # The posting has to still be fresh, so move its date forward too.
    stub_sources.results["ats_boards"] = [
        make_job(company="Company0", title="Backend Engineer",
                 location="Berlin, Germany", ats_job_id="0",
                 posted_at=NOW + timedelta(days=8))
    ]
    later, _ = run_pipeline(cfg, tracker=memory_tracker, now=NOW + timedelta(days=8, hours=2),
                            llm_client=llm_client(LLM_SCRIPT))
    assert len(later) == 1


def test_filtered_jobs_are_recorded_so_they_are_not_re_evaluated(
        tmp_path: Path, stub_sources, memory_tracker):
    """A stale posting only ever gets staler. Without a row, every morning
    re-fetches and re-judges the identical job, forever."""
    stale = make_job(company="Stale", location="Berlin, Germany", hours_old=99,
                     ats_job_id="s")
    stub_sources.results["ats_boards"] = [stale]
    run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker, now=NOW,
                 llm_client=llm_client(LLM_SCRIPT))
    assert memory_tracker.get_status(stale.key) == ApplyStatus.FILTERED.value


def test_final_statuses_are_persisted(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    scored, _ = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                             now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert memory_tracker.get_status(scored[0].key) == ApplyStatus.DIGEST.value


def test_the_pipeline_runs_without_a_tracker_at_all(tmp_path: Path, stub_sources):
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    scored, _ = run_pipeline(pipeline_config(tmp_path), tracker=None, now=NOW,
                             llm_client=llm_client(LLM_SCRIPT))
    assert len(scored) == 1


# ==========================================================================
# degradation — one broken stage must not abort the run
# ==========================================================================


def test_a_dead_source_costs_that_source_and_nothing_else(tmp_path: Path,
                                                          stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = RuntimeError("greenhouse is down")
    cfg = pipeline_config(tmp_path)
    scored, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                                 llm_client=llm_client(LLM_SCRIPT))
    assert scored == []
    assert any("greenhouse is down" in e for e in stats.errors)
    assert Path(stats.digest_path).exists()      # the digest still gets written


def test_a_scoring_outage_still_produces_a_digest(tmp_path: Path, stub_sources,
                                                  memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(2)
    scored, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                                 now=NOW, llm_client=llm_client(["not json at all"]))
    assert len(scored) == 2
    assert all(s.status is ApplyStatus.DIGEST for s in scored)
    assert Path(stats.digest_path).exists()


def test_no_sources_active_is_reported_not_silent(tmp_path: Path, stub_sources,
                                                  memory_tracker):
    cfg = pipeline_config(tmp_path, sources={"greenhouse": False})
    _, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                            llm_client=llm_client(LLM_SCRIPT))
    assert any("no sources" in e for e in stats.errors)


def test_a_missing_cv_is_fatal_before_any_api_spend(tmp_path: Path, stub_sources,
                                                    memory_tracker):
    """Scoring compares against the CV and tailoring rewrites it. Without one
    the run would still "work" and hand back a page of confident nonsense."""
    stub_sources.results["ats_boards"] = fresh_jobs(1)
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True},
                                  "output": {"dir": str(tmp_path / "output")}},
                       watchlist={"greenhouse": ["acme"]}, cv=None)
    with pytest.raises(ConfigError, match="cannot read the CV"):
        run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                     llm_client=llm_client(LLM_SCRIPT))


def test_a_stub_cv_is_rejected_too(tmp_path: Path, stub_sources, memory_tracker):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True},
                                  "output": {"dir": str(tmp_path / "output")}},
                       watchlist={"greenhouse": ["acme"]}, cv="# TODO write my CV")
    with pytest.raises(ConfigError, match="that is not"):
        run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                     llm_client=llm_client(LLM_SCRIPT))


# ==========================================================================
# source selection and limits
# ==========================================================================


def test_only_enabled_sources_are_fetched(tmp_path: Path, stub_sources, memory_tracker):
    run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker, now=NOW,
                 llm_client=llm_client(LLM_SCRIPT))
    assert stub_sources.calls == ["ats_boards"]


def test_sources_argument_narrows_further(tmp_path: Path, stub_sources, memory_tracker):
    cfg = pipeline_config(tmp_path, sources={"greenhouse": True, "adzuna": True},
                          keys={"adzuna_app_id": "x", "adzuna_app_key": "y"})
    run_pipeline(cfg, tracker=memory_tracker, now=NOW, sources=["adzuna"],
                 llm_client=llm_client(LLM_SCRIPT))
    assert stub_sources.calls == ["adzuna"]


def test_limit_caps_what_reaches_scoring(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(5)
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, limit=2, llm_client=llm_client(LLM_SCRIPT))
    assert stats.scored == 2


def test_dedupe_collapses_the_same_role_seen_through_two_sources(
        tmp_path: Path, stub_sources, memory_tracker):
    """The cross-source case dedupe exists for: one posting reached us from
    both an ATS board and an aggregator."""
    stub_sources.results["ats_boards"] = [
        make_job(company="Acme", title="Backend Engineer", source="greenhouse",
                 location="Berlin, Germany", hours_old=2, ats=None, ats_job_id=None)
    ]
    stub_sources.results["adzuna"] = [
        make_job(company="Acme", title="Backend Engineer", source="adzuna",
                 location="Berlin, Germany", hours_old=2, ats=None,
                 ats_job_id=None, url="https://adzuna/1")
    ]
    cfg = pipeline_config(tmp_path, sources={"greenhouse": True, "adzuna": True},
                          keys={"adzuna_app_id": "x", "adzuna_app_key": "y"})
    _, stats = run_pipeline(cfg, tracker=memory_tracker, now=NOW,
                            llm_client=llm_client(LLM_SCRIPT))
    assert stats.fetched == 2
    assert stats.after_dedupe == 1
    assert stats.after_filters == 1


def test_a_board_fetch_returning_a_foreign_source_is_discarded(
        tmp_path: Path, stub_sources, memory_tracker):
    """`ats_boards.fetch` serves greenhouse and lever in one call, so its
    output is filtered by source afterwards. A job tagged anything else came
    from somewhere unexpected and is dropped rather than trusted."""
    stub_sources.results["ats_boards"] = [
        make_job(company="Acme", source="greenhouse", location="Berlin, Germany",
                 hours_old=2, ats_job_id="1"),
        make_job(company="Sneaky", source="linkedin_email", location="Berlin, Germany",
                 hours_old=2, ats=None, ats_job_id=None),
    ]
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert stats.fetched == 1
    assert stats.source_counts == {"greenhouse": 1}


# ==========================================================================
# CLI
# ==========================================================================


def test_parser_accepts_the_documented_flags():
    args = build_parser().parse_args([
        "--no-browser", "--dry-run", "--skip-apply", "--limit", "5",
        "--source", "greenhouse", "--source", "lever", "--verbose",
    ])
    assert args.no_browser is True
    assert args.dry_run is True
    assert args.skip_apply is True
    assert args.limit == 5
    assert args.sources == ["greenhouse", "lever"]


def test_dry_run_and_no_dry_run_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--dry-run", "--no-dry-run"])


def test_dry_run_defaults_to_whatever_the_config_says():
    assert build_parser().parse_args([]).dry_run is None


def test_cli_overrides_are_written_into_the_config(tmp_path: Path):
    """Downstream stages read the config and nothing else, so a flag that is
    not reflected here is a flag half the pipeline cannot see."""
    cfg = pipeline_config(tmp_path, apply={"enabled": True, "dry_run": True})
    args = build_parser().parse_args(["--no-dry-run", "--skip-apply", "--no-browser"])
    apply_cli_overrides(cfg, args)

    assert cfg.get("apply.dry_run") is False
    assert cfg.get("apply.enabled") is False
    assert cfg.get("output.open_browser") is False


def test_source_flag_narrows_but_never_enables(tmp_path: Path):
    """`--source adzuna` must not quietly switch on a paid API the user
    turned off on purpose."""
    cfg = pipeline_config(tmp_path, sources={"greenhouse": True, "adzuna": False})
    apply_cli_overrides(cfg, build_parser().parse_args(["--source", "adzuna"]))
    assert cfg.get("sources.adzuna") is False
    assert cfg.get("sources.greenhouse") is False


def test_validate_only_reports_and_exits(tmp_path: Path, capsys):
    write_config(tmp_path)
    code = main(["--validate-only", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 0
    assert "config OK" in capsys.readouterr().out


def test_an_invalid_config_exits_1_and_lists_every_problem(tmp_path: Path, capsys):
    write_config(tmp_path, {"applicant": {"name": "", "email": ""},
                            "keys": {"anthropic": ""}}, cv=None)
    code = main(["--validate-only", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 1
    err = capsys.readouterr().err
    assert "problem(s)" in err
    assert "applicant.name" in err
    assert "anthropic" in err


def test_a_missing_config_file_still_validates_the_defaults(tmp_path: Path, capsys):
    code = main(["--validate-only", "--config", str(tmp_path / "nope.yaml"),
                 "--watchlist", str(tmp_path / "nope2.yaml")])
    assert code == 1                       # no applicant, no CV, no key


def test_main_returns_2_on_an_unexpected_failure(tmp_path: Path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setattr(main_module, "_run_cli",
                        lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["--config", str(tmp_path / "config.yaml")]) == 2


def test_main_returns_130_on_interrupt(tmp_path: Path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setattr(main_module, "_run_cli",
                        lambda args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["--config", str(tmp_path / "config.yaml")]) == 130


def test_the_epilog_documents_the_canonical_invocations():
    help_text = build_parser().format_help()
    assert "python -m src.main" in help_text
    assert "--no-browser" in help_text
    assert "cron" in help_text.lower()


# ==========================================================================
# summary
# ==========================================================================


def test_format_summary_reads_like_the_documented_output(tmp_path: Path,
                                                         stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = fresh_jobs(3)
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    summary = format_summary(stats)
    for token in ("fetched", "deduped", "filtered", "scored", "matched",
                  "auto-applied", "dry-run", "needs your click", "digest:"):
        assert token in summary


def test_format_summary_says_when_no_digest_was_written():
    from src.models import RunStats

    assert "not written" in format_summary(RunStats())


def test_format_summary_counts_errors(tmp_path: Path, stub_sources, memory_tracker):
    stub_sources.results["ats_boards"] = RuntimeError("down")
    _, stats = run_pipeline(pipeline_config(tmp_path), tracker=memory_tracker,
                            now=NOW, llm_client=llm_client(LLM_SCRIPT))
    assert "error(s) this run" in format_summary(stats)


# ==========================================================================
# failure notification
# ==========================================================================


def alerting_config(tmp_path: Path, **notify_overrides):
    settings = {"enabled": True,
                "channels": {"console": False, "file": True,
                             "command": "", "email": {}}}
    settings.update(notify_overrides)
    return write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": False},
         "output": {"dir": str(tmp_path / "output"), "open_browser": False},
         "db": {"path": str(tmp_path / "output" / "tracker.sqlite3")},
         "apply": {"enabled": False}, "tailoring": {"enabled": False},
         "notify": settings},
        watchlist={"greenhouse": ["acme"]},
    )


def test_a_run_that_fetches_nothing_writes_an_alert(tmp_path: Path, monkeypatch,
                                                    capsys):
    """The whole point: an empty digest because every board 404'd must not
    look like a genuinely quiet Tuesday."""
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    alerting_config(tmp_path)

    code = main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 0                       # exit_nonzero is off by default
    alert = (tmp_path / "output" / ALERT_FILENAME)
    assert alert.exists()
    assert "no postings fetched" in alert.read_text(encoding="utf-8")


def test_a_healthy_run_clears_yesterdays_alert(tmp_path: Path, monkeypatch):
    """A stale ALERT.txt after recovery is worse than none: it trains you to
    ignore the file."""
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch",
                        lambda config, **kw: fresh_jobs(2))
    monkeypatch.setattr(main_module.scoring, "score_jobs",
                        lambda jobs, cv, cfg, **kw: _scored(jobs))
    alerting_config(tmp_path)
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / ALERT_FILENAME).write_text("old", encoding="utf-8")

    main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
          "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert not (tmp_path / "output" / ALERT_FILENAME).exists()


def test_exit_nonzero_is_opt_in(tmp_path: Path, monkeypatch):
    """It changes the documented exit codes, so it must never turn itself on."""
    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])

    alerting_config(tmp_path)
    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0

    alerting_config(tmp_path, exit_nonzero=True)
    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 4


def test_notify_on_filters_which_alerts_are_delivered(tmp_path: Path, monkeypatch):
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    alerting_config(tmp_path, on=["missed_run"])     # deliberately not no_jobs

    main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
          "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert not (tmp_path / "output" / ALERT_FILENAME).exists()


def test_a_broken_notifier_does_not_break_the_run(tmp_path: Path, monkeypatch,
                                                  capsys):
    """A notifier that takes down the run it was meant to warn about is
    strictly worse than no notifier."""
    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    monkeypatch.setattr(main_module.notify, "send",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    alerting_config(tmp_path)

    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0
    assert "digest:" in capsys.readouterr().out


def test_a_broken_health_check_does_not_break_the_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    monkeypatch.setattr(main_module.health, "assess",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    alerting_config(tmp_path)
    assert main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0


def test_the_baseline_comes_from_runs_before_this_one(tmp_path: Path, monkeypatch):
    """`assess` must not compare the run against itself — reading the history
    after inserting this run's row would make every source look normal."""
    from src.notify import ALERT_FILENAME

    monkeypatch.setattr(main_module.ats_boards, "fetch",
                        lambda config, **kw: fresh_jobs(40))
    monkeypatch.setattr(main_module.scoring, "score_jobs",
                        lambda jobs, cv, cfg, **kw: _scored(jobs))
    alerting_config(tmp_path)
    argv = ["--no-browser", "--config", str(tmp_path / "config.yaml"),
            "--watchlist", str(tmp_path / "watchlist.yaml")]

    main(argv)                                  # establishes a baseline
    assert not (tmp_path / "output" / ALERT_FILENAME).exists()

    monkeypatch.setattr(main_module.ats_boards, "fetch", lambda config, **kw: [])
    main(argv)                                  # the board went silent
    assert (tmp_path / "output" / ALERT_FILENAME).exists()


# ==========================================================================
# a real end-to-end run through the CLI
# ==========================================================================


def test_the_cli_runs_the_whole_pipeline_against_a_fake_board(
        tmp_path: Path, monkeypatch, capsys):
    """The closest thing to `python -m src.main` this suite can do offline:
    a real Tracker on disk, real filters, a fake HTTP session behind the
    Greenhouse fetcher and a fake LLM behind scoring."""
    # Only two things are faked: the socket behind the Greenhouse fetcher, and
    # the model behind scoring. Filters, dedupe, the tracker, the digest and
    # the CLI itself are all the real implementations.
    session = FakeSession([("boards-api.greenhouse.io", json_response(GREENHOUSE))])
    real_fetch = main_module.ats_boards.fetch
    monkeypatch.setattr(
        main_module.ats_boards, "fetch",
        lambda config, **kw: real_fetch(config, session=session, errors=kw.get("errors")),
    )
    monkeypatch.setattr(main_module.scoring, "score_jobs",
                        lambda jobs, cv, cfg, **kw: _scored(jobs))

    write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": False},
         "output": {"dir": str(tmp_path / "output"), "open_browser": False},
         "db": {"path": str(tmp_path / "output" / "tracker.sqlite3")},
         "apply": {"enabled": False},
         "tailoring": {"enabled": False},
         "freshness": {"max_age_hours": 24, "skip_undated": True}},
        watchlist={"greenhouse": ["acme"]},
    )

    code = main(["--no-browser", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    out = capsys.readouterr().out
    assert code == 0
    assert "digest:" in out
    assert list((tmp_path / "output").glob("digest_*.html"))
    # The tracker is a real file on disk and has recorded the run.
    with Tracker(tmp_path / "output" / "tracker.sqlite3") as tracker:
        assert tracker.recent_runs()
        assert tracker.counts_by_status()


def _scored(jobs):
    """Deterministic stand-in for the scoring stage in the CLI test."""
    from src.models import Score, ScoredJob

    return [
        ScoredJob(job=job, status=ApplyStatus.DIGEST,
                  score=Score(value=90, verdict="fits", model="test"))
        for job in jobs
    ]
