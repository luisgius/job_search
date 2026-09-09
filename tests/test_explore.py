"""Offline exploration boundaries, evidence and forbidden-operation sentinels."""
import builtins
import copy
import importlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import explore
from src.config import Config, SOURCE_NAMES
from src.models import Job

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    return Config(data={
        "sources": {name: name in {"greenhouse", "lever", "linkedin_email"} for name in SOURCE_NAMES},
        "filters": {"title_include": ["Data Scientist"], "title_exclude": ["Senior"],
                    "countries": ["PL"], "min_description_chars": 0},
        "freshness": {"max_age_hours": 24, "skip_undated": True},
        "output": {"dir": "output"}, "db": {"path": "output/tracker.sqlite3"},
        "cv": {"path": "cv/base_cv.md"},
        "apply": {"enabled": True, "dry_run": False},
        "tailoring": {"enabled": True},
    }, watchlist={"greenhouse": ["fictional"], "lever": ["fictional"]}, root=tmp_path)


def job(identifier="a", days=1, **changes):
    fields = dict(source="greenhouse", ats="greenhouse", ats_job_id=identifier,
                  company=f"Example {identifier}", title="Data Scientist",
                  url=f"https://example.invalid/jobs/{identifier}", location="Warsaw, Poland",
                  country="PL", description="Python machine learning data scientist role.",
                  posted_at=None if days is None else NOW - timedelta(days=days))
    fields.update(changes)
    return Job(**fields)


def run(config, jobs, **kwargs):
    return explore.explore(config, now=NOW, fetchers={"ats_boards": lambda *a, **k: jobs}, **kwargs)


def test_default_window_and_explicit_undated_preserve_config_and_identity(config):
    jobs = [job("fresh"), job("edge", 30), job("old", 30.00001), job("unknown", None)]
    before, original_jobs = copy.deepcopy(config), copy.deepcopy(jobs)
    report = run(config, jobs)
    assert report["days"] == 30
    assert {row["key"] for row in report["opportunities"]} == {jobs[0].key, jobs[1].key}
    assert report["undated_review"] == []
    assert report["first_rejection_counts"] == {"stale": 1, "undated": 1}
    report = run(config, jobs, undated_limit=1)
    assert report["undated_review"][0]["posted_at"] is None
    assert report["undated_review"][0]["key"] == jobs[3].key
    assert report["opportunities"][1]["posted_at"] == jobs[1].posted_at.isoformat()
    assert config == before and jobs == original_jobs


@pytest.mark.parametrize("kwargs", [
    {"days": 0}, {"days": 91}, {"days": 1.5}, {"days": True},
    {"limit": 0}, {"limit": 501}, {"undated_limit": -1}, {"undated_limit": 51},
    {"sources": ["linkedin_email"]}, {"sources": ["invented"]},
])
def test_invalid_arguments_fail_before_fetch(config, kwargs):
    def fail(*a, **k):
        pytest.fail("invalid options must not fetch")
    with pytest.raises(ValueError):
        explore.explore(config, fetchers={"ats_boards": fail}, **kwargs)


def test_maximum_window_future_dates_and_first_rejection(config):
    report = run(config, [job("edge", 90), job("stale", 91), job("future", -1),
                          job("senior", 100, title="Senior Data Scientist")], days=90)
    assert [row["company"] for row in report["opportunities"]] == ["Example edge"]
    assert report["first_rejection_counts"] == {"future_date": 1, "stale": 1, "title_excluded": 1}
    assert sum(report["first_rejection_counts"].values()) == report["counts"]["rejected"]


def test_deterministic_dedupe_and_caps(config):
    jobs = [job(str(i), i + 1) for i in range(5)]
    jobs += [job(f"u{i}", None) for i in range(5)]
    jobs += [job(f"r{i}", 60) for i in range(5)]
    duplicate = copy.deepcopy(jobs[0])
    duplicate.description = "snippet"
    jobs.append(duplicate)
    left = run(config, jobs, limit=2, undated_limit=1)
    right = run(config, list(reversed(jobs)), limit=2, undated_limit=1)
    assert left == right
    assert left["counts"] == {"fetched": 16, "after_dedupe": 15, "dated_eligible": 5,
                              "undated_eligible": 5, "dated_displayed": 2,
                              "undated_displayed": 1, "rejected": 5, "rejected_displayed": 2}
    assert len(left["opportunities"]) == len(left["rejected_examples"]) == 2
    assert len(left["undated_review"]) == 1
    assert left["opportunities"][0]["description"] == jobs[0].description


def test_sources_narrow_before_ats_fetch_and_never_enable_disabled(config):
    calls = []
    def fetch(local, errors):
        calls.append(copy.deepcopy(local))
        assert local.source_enabled("greenhouse")
        assert not local.source_enabled("lever")
        assert not local.source_enabled("linkedin_email")
        return [job(), job("lever", source="lever")]
    report = explore.explore(config, now=NOW, sources=["greenhouse", "adzuna"],
                             fetchers={"ats_boards": fetch})
    assert len(calls) == 1
    assert report["selected_sources"] == ["greenhouse"]
    assert report["source_counts"] == {"greenhouse": 1}
    assert report["counts"]["fetched"] == 1
    assert config.source_enabled("lever") and config.source_enabled("linkedin_email")


def test_source_errors_are_visible_and_other_sources_survive(config):
    config.data["sources"]["arbeitnow"] = True
    def broken(*a, **k):
        raise RuntimeError("fixture failure")
    report = explore.explore(config, now=NOW, fetchers={
        "ats_boards": broken, "arbeitnow": lambda *a, **k: [job(source="arbeitnow")],
    })
    assert len(report["opportunities"]) == 1
    assert report["source_counts"]["greenhouse"] == 0
    assert report["errors"] == ["ats_boards: source fetch failed: fixture failure"]


def test_injected_fetchers_never_fall_back_to_live_network(config, monkeypatch):
    monkeypatch.setattr(importlib, "import_module", lambda *a, **k: pytest.fail("network fallback"))
    report = explore.explore(config, now=NOW, fetchers={})
    assert report["errors"] and report["counts"]["fetched"] == 0


def test_evidence_labels_and_escaped_full_description(config):
    text = '<script>alert("x")</script> Full evidence ' + 'detail ' * 100
    report = run(config, [job("snippet", raw={"snippet_only": True}, description=text),
                          job("missing", description=""), job("other", description="available text")])
    by_company = {row["company"]: row for row in report["opportunities"]}
    assert by_company["Example snippet"]["evidence_kind"] == "snippet"
    assert by_company["Example missing"]["evidence_kind"] == "missing"
    assert by_company["Example other"]["evidence_kind"] == "description_completeness_unknown"
    assert by_company["Example snippet"]["description"] == text
    for row in report["opportunities"]:
        assert row["assessment"] == "unassessed" and row["availability"] == "unknown"
        assert len(row["snippet"]) <= 320
        assert "score" not in row
    html = explore.render_html(report)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "All available description" in html and "First rejection counts" in html
    assert "detail " * 100 in html


def test_unsafe_url_is_not_linked(config):
    report = run(config, [job(url="javascript:alert(1)")])
    assert 'href="javascript:' not in explore.render_html(report)


def test_new_output_directory_and_no_overwrite(config):
    report = run(config, [job()])
    first = explore.write_report(report, config)
    second = explore.write_report(report, config)
    assert first[0].parent != second[0].parent
    assert not config.output_dir.exists()
    assert json.loads(first[1].read_text()) == report
    assert first[0].name == "exploration.html"
    for target in [config.output_dir, config.output_dir / "nested", config.root]:
        with pytest.raises(ValueError, match="separate"):
            explore.write_report(report, config, output_dir=target)
    alias = config.root / "alias"
    alias.symlink_to(config.output_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="separate"):
        explore.write_report(report, config, output_dir=alias)


def test_cli_path_with_failing_forbidden_operation_sentinels(config, monkeypatch, capsys):
    """Exercise default adapter loading and actual HTML/JSON writes under tripwires."""
    forbidden = {"src.main", "src.db", "src.scoring", "src.tailor", "src.llm",
                 "src.apply", "src.digest", "src.sources.linkedin_email", "smtplib"}
    original_import = builtins.__import__
    original_import_module = importlib.import_module
    original_open = Path.open
    calls = []
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        full = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                if level else name)
        candidates = [full, *(f"{full}.{part}" for part in fromlist or ())]
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in forbidden):
                pytest.fail(f"forbidden dependency import: {candidate}")
        return original_import(name, globals, locals, fromlist, level)
    def guarded_module(name, package=None):
        full = importlib.util.resolve_name(name, package) if name.startswith(".") else name
        assert not any(full == prefix or full.startswith(prefix + ".") for prefix in forbidden), full
        if full == "src.sources.ats_boards":
            calls.append(full)
            return SimpleNamespace(fetch=lambda *a, **k: [job()])
        return original_import_module(name, package)
    def guarded_open(path, *args, **kwargs):
        assert not path.is_relative_to(config.output_dir), "production file read/write"
        assert not path.is_relative_to(config.root / "cv"), "CV read/write"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(importlib, "import_module", guarded_module)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("tracker connection"))
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Config, "load", lambda *a, **k: config)
    importlib.reload(explore)  # Catch forbidden top-level imports as well as runtime calls.
    assert explore.main(["--source", "greenhouse", "--undated-limit", "2"]) == 0
    assert calls == ["src.sources.ats_boards"]
    assert "Unassessed exploration HTML" in capsys.readouterr().out
    assert not config.output_dir.exists() and not (config.root / "cv").exists()
    assert len(list((config.root / "exploration_output").glob("*/exploration.json"))) == 1


def test_module_entry_help():
    completed = subprocess.run([sys.executable, "-m", "src.explore", "--help"],
                               capture_output=True, text=True, check=True)
    assert "--undated-limit" in completed.stdout
    assert "availability unknown" in completed.stdout


def test_cli_invalid_output_fails_before_fetch(config, monkeypatch):
    monkeypatch.setattr(Config, "load", lambda *a, **k: config)
    monkeypatch.setattr(explore, "_fetch_sources", lambda *a, **k: pytest.fail("fetch"))
    with pytest.raises(SystemExit) as exc:
        explore.main(["--output-dir", str(config.output_dir)])
    assert exc.value.code == 2


def test_country_override_filters_poland_without_mutating_normal_config(config):
    config.data["filters"]["countries"] = ["PL", "DE"]
    before = copy.deepcopy(config)
    jobs = [job("pl"), job("de", country="DE", location="Berlin, Germany")]
    assert len(run(config, jobs)["opportunities"]) == 2
    report = run(config, jobs, countries=["pl"])
    assert report["countries"] == ["PL"]
    assert [row["company"] for row in report["opportunities"]] == ["Example pl"]
    assert report["first_rejection_counts"] == {"location_outside_eu": 1}
    assert config == before
    assert explore.build_parser().parse_args(["--country", "pl", "--country", "de"]).country == ["PL", "DE"]


@pytest.mark.parametrize("countries", [[], ["Poland"], ["ZZ"], ["PL", "invalid"]])
def test_invalid_country_override_fails_before_fetch(config, monkeypatch, countries):
    monkeypatch.setattr(explore, "_fetch_sources", lambda *a, **k: pytest.fail("fetch"))
    with pytest.raises(ValueError, match="ISO2"):
        run(config, [], countries=countries)


def test_zero_selection_valid_empty_and_source_errors_have_distinct_labels(config):
    none = run(config, [], sources=[])
    assert none["diagnostics"] and not none["errors"]
    assert "No enabled public sources selected" in explore.render_html(none)
    empty = run(config, [])
    assert not empty["diagnostics"] and not empty["errors"]
    assert "No source fetch errors reported" in explore.render_html(empty)
    failed = explore.explore(config, now=NOW, fetchers={})
    assert failed["errors"]
    html = explore.render_html(failed)
    assert "missing results cannot be interpreted as no openings" in html
    assert "<table>" in html and "Dated displayed" in html
    assert '&quot;fetched&quot;' not in html


def test_registered_allegro_uses_public_source_dispatch(config, monkeypatch):
    assert "allegro" in explore.PUBLIC_SOURCES
    config.data["sources"]["allegro"] = True
    calls = []
    def module(name, package):
        calls.append(name)
        assert name == ".sources.allegro"
        return SimpleNamespace(fetch=lambda *a, **k: [job(source="allegro")])
    monkeypatch.setattr(importlib, "import_module", module)
    report = explore.explore(config, now=NOW, sources=["allegro"])
    assert calls == [".sources.allegro"]
    assert report["source_counts"] == {"allegro": 1}
    assert len(report["opportunities"]) == 1


@pytest.mark.parametrize("countries,expected", [
    (["PL"], {"Example pl"}),
    (["GB"], {"Example gb"}),
    (["PL", "GB"], {"Example pl", "Example gb"}),
])
def test_explicit_countries_cannot_leak_sponsorship_exceptions(config, countries, expected):
    config.data["filters"]["countries"] = ["PL", "DE"]
    config.data["filters"]["countries_if_sponsorship"] = ["GB", "FR"]
    before = copy.deepcopy(config)
    jobs = [job("pl", remote=False),
            job("gb", country="GB", location="London, UK", remote=False,
                description="Data Scientist. Visa sponsorship is available."),
            job("fr", country="FR", location="Paris, France", remote=False,
                description="Data Scientist. Visa sponsorship is available.")]
    report = run(config, jobs, countries=countries)
    assert {row["company"] for row in report["opportunities"]} == expected
    assert report["first_rejection_counts"] == {"location_outside_eu": 3 - len(expected)}
    assert report["countries"] == sorted(countries)
    assert config == before
