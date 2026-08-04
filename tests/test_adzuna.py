"""Tests for src/sources/adzuna.py.

Adzuna is the aggregator in the mix, which changes what correctness means:
its descriptions are truncated teasers, its results repeat across (and
within) queries, and its `created` is an ingest time rather than a publish
time. The tests below pin the handling of all three, plus the two failure
modes that matter operationally — a rejected API key and a broken query.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources.adzuna import BASE_URL, MAX_RESULTS_PER_PAGE, fetch, parse_result
from src.util import HttpError
from tests.conftest import (
    FakeResponse,
    FakeSession,
    json_response,
    load_json_fixture,
    write_config,
)

UTC = timezone.utc
PAYLOAD = load_json_fixture("adzuna_search_de.json")
RESULTS = PAYLOAD["results"]


def adzuna_config(tmp_path: Path, watch=None, **overrides):
    base = {
        "sources": {"greenhouse": False, "adzuna": True},
        "keys": {"adzuna_app_id": "app-id", "adzuna_app_key": "app-key"},
    }
    if overrides:
        base.update(overrides)
    return write_config(
        tmp_path, base,
        watchlist={"adzuna": watch if watch is not None else
                   {"countries": ["de"], "queries": ["python engineer"],
                    "max_days_old": 1, "results_per_page": 50}},
    )


def adzuna_session(payload=None):
    return FakeSession([("api.adzuna.com",
                         json_response(PAYLOAD if payload is None else payload))])


# ==========================================================================
# parse_result
# ==========================================================================


def test_parse_result_happy_path():
    job = parse_result(RESULTS[0], "de")
    assert job.source == "adzuna"
    assert job.company == "Northwind GmbH"
    assert job.title == "Python Backend Engineer (f/m/d)"
    assert job.url.startswith("https://www.adzuna.de/land/ad/5012345678")
    assert job.posted_at == datetime(2026, 8, 4, 6, 12, tzinfo=UTC)


def test_parse_result_has_no_ats_because_the_link_redirects_elsewhere():
    """Adzuna is not an ATS: the apply link bounces to some other system, so
    there is no stable ATS id to key on and auto-apply must never fire."""
    job = parse_result(RESULTS[0], "de")
    assert job.ats is None
    assert job.ats_job_id is None


def test_parse_result_flags_the_description_as_a_snippet():
    """The scorer needs to know it is judging a teaser, not a job description
    — Adzuna truncates every one of them."""
    job = parse_result(RESULTS[0], "de")
    assert job.raw["snippet_only"] is True
    assert "20k requests per second" in job.description


def test_parse_result_uses_the_area_to_seed_the_country():
    job = parse_result(RESULTS[0], "de")
    assert job.country == "DE"
    assert "Germany" in job.raw["area"]


def test_parse_result_resolves_an_accented_city():
    job = parse_result(RESULTS[1], "de")
    assert job.country == "DE"
    assert "München" in job.location


def test_parse_result_formats_a_salary_range():
    assert "65,000" in parse_result(RESULTS[0], "de").salary


def test_parse_result_marks_a_predicted_salary_as_estimated():
    """An Adzuna-guessed number presented as fact would be actively
    misleading in the digest."""
    salary = parse_result(RESULTS[1], "de").salary
    assert "estimated" in salary.lower()


def test_parse_result_without_a_title_or_url_is_none():
    assert parse_result(RESULTS[4], "de") is None          # no title
    assert parse_result({"title": "Engineer"}, "de") is None  # no url


def test_parse_result_without_a_company_still_produces_a_job():
    """An anonymous listing with a good title is still worth scoring."""
    job = parse_result({"title": "Engineer", "redirect_url": "https://x/1",
                        "created": "2026-08-04T06:00:00Z"}, "de")
    assert job is not None
    assert job.company == ""


def test_parse_result_rejects_non_mappings():
    assert parse_result("nope", "de") is None
    assert parse_result(None, "de") is None


def test_parse_result_marks_remote_positively_only():
    remote = parse_result({"title": "Engineer (Remote)", "redirect_url": "https://x/1"}, "de")
    onsite = parse_result({"title": "Engineer", "redirect_url": "https://x/2",
                           "location": {"display_name": "Berlin"}}, "de")
    assert remote.remote is True
    assert onsite.remote is None


def test_parse_result_keeps_the_raw_metadata():
    raw = parse_result(RESULTS[0], "de").raw
    assert raw["id"] == "5012345678"
    assert raw["country"] == "de"
    assert raw["contract_time"] == "full_time"


# ==========================================================================
# fetch — query construction
# ==========================================================================


def test_fetch_sends_the_documented_params(tmp_path: Path):
    session = adzuna_session()
    fetch(adzuna_config(tmp_path), session=session)
    params = session.calls[0]["params"]
    assert params["app_id"] == "app-id"
    assert params["app_key"] == "app-key"
    assert params["what"] == "python engineer"
    assert params["results_per_page"] == 50
    assert params["max_days_old"] == 1
    # The hyphen is not a typo: Adzuna returns XML without this query param.
    assert params["content-type"] == "application/json"


def test_fetch_hits_the_right_url(tmp_path: Path):
    session = adzuna_session()
    fetch(adzuna_config(tmp_path), session=session)
    assert session.calls[0]["url"] == BASE_URL.format(country="de", page=1)


def test_fetch_covers_every_country_query_pair(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": ["de", "nl"],
                                         "queries": ["python", "golang"]})
    session = adzuna_session()
    fetch(cfg, session=session)
    assert len(session.calls) == 4


def test_fetch_caps_results_per_page_at_the_api_maximum(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": ["de"], "queries": ["x"],
                                         "results_per_page": 5000})
    session = adzuna_session()
    fetch(cfg, session=session)
    assert session.calls[0]["params"]["results_per_page"] == MAX_RESULTS_PER_PAGE


def test_fetch_only_sends_distance_with_a_where(tmp_path: Path):
    """`distance` without `where` is meaningless to Adzuna and silently
    narrows nothing."""
    cfg = adzuna_config(tmp_path, watch={"countries": ["de"], "queries": ["x"],
                                         "distance_km": 30})
    session = adzuna_session()
    fetch(cfg, session=session)
    assert "distance" not in session.calls[0]["params"]

    cfg2 = adzuna_config(tmp_path, watch={"countries": ["de"], "queries": ["x"],
                                          "where": "Berlin", "distance_km": 30})
    session2 = adzuna_session()
    fetch(cfg2, session=session2)
    assert session2.calls[0]["params"]["distance"] == 30
    assert session2.calls[0]["params"]["where"] == "Berlin"


def test_uk_alias_is_translated_to_gb(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": ["uk"], "queries": ["x"]})
    session = adzuna_session()
    fetch(cfg, session=session)
    assert "/jobs/gb/" in session.calls[0]["url"]


# ==========================================================================
# fetch — results
# ==========================================================================


def test_fetch_deduplicates_within_a_single_payload(tmp_path: Path):
    """The fixture repeats id 5012345678, exactly as real payloads do."""
    jobs = fetch(adzuna_config(tmp_path), session=adzuna_session())
    ids = [j.raw["id"] for j in jobs]
    assert len(ids) == len(set(ids))


def test_fetch_deduplicates_across_overlapping_queries(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": ["de"],
                                         "queries": ["python", "backend"]})
    jobs = fetch(cfg, session=adzuna_session())
    ids = [j.raw["id"] for j in jobs]
    assert len(ids) == len(set(ids))


def test_fetch_skips_unusable_results_without_dropping_the_page(tmp_path: Path):
    jobs = fetch(adzuna_config(tmp_path), session=adzuna_session())
    assert len(jobs) == 3     # 5 results, one titleless, one duplicate
    assert all(j.title for j in jobs)


def test_fetch_does_not_filter_by_date_or_title(tmp_path: Path):
    """Sources normalise, filters.py decides — the Werkstudent posting must
    reach the filter stage so it is counted in the funnel."""
    jobs = fetch(adzuna_config(tmp_path), session=adzuna_session())
    assert any("Werkstudent" in j.title for j in jobs)


def test_fetch_returns_nothing_when_the_source_is_disabled(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"adzuna": False}},
                       watchlist={"adzuna": {"countries": ["de"], "queries": ["x"]}})
    session = adzuna_session()
    assert fetch(cfg, session=session) == []
    assert session.calls == []


# ==========================================================================
# fetch — failure handling
# ==========================================================================


def test_missing_keys_report_once_and_make_no_requests(tmp_path: Path):
    cfg = write_config(
        tmp_path, {"sources": {"adzuna": True}, "keys": {"adzuna_app_id": ""}},
        watchlist={"adzuna": {"countries": ["de"], "queries": ["x"]}},
    )
    errors: list[str] = []
    session = adzuna_session()
    assert fetch(cfg, session=session, errors=errors) == []
    assert session.calls == []
    assert any("developer.adzuna.com" in e for e in errors)


def test_empty_watchlist_reports_rather_than_silently_doing_nothing(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": [], "queries": []})
    errors: list[str] = []
    assert fetch(cfg, session=adzuna_session(), errors=errors) == []
    assert any("empty" in e for e in errors)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_abandons_that_country_immediately(tmp_path: Path, status):
    """Every query for that country would fail identically — hammering the
    API another nine times just to collect nine copies of the same 401 is
    wasteful and looks like abuse."""
    cfg = adzuna_config(tmp_path, watch={"countries": ["de"],
                                         "queries": ["a", "b", "c"]})
    session = FakeSession([("api.adzuna.com", FakeResponse(status_code=status))])
    errors: list[str] = []
    assert fetch(cfg, session=session, errors=errors) == []
    assert len(session.calls) == 1
    assert any("check keys" in e for e in errors)


def test_one_failing_query_does_not_stop_the_others(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": ["de"], "queries": ["bad", "good"]})

    def route(url, params):
        if params.get("what") == "bad":
            return FakeResponse(status_code=500)
        return json_response(PAYLOAD)

    session = FakeSession([("api.adzuna.com", route)])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert jobs
    assert len(errors) == 1


def test_one_failing_country_does_not_stop_the_others(tmp_path: Path):
    cfg = adzuna_config(tmp_path, watch={"countries": ["de", "nl"], "queries": ["x"]})

    def route(url, params):
        return FakeResponse(status_code=500) if "/de/" in url else json_response(PAYLOAD)

    session = FakeSession([("api.adzuna.com", route)])
    assert fetch(cfg, session=session, errors=[])


def test_fetch_never_raises(tmp_path: Path):
    cfg = adzuna_config(tmp_path)
    session = FakeSession(default=ConnectionError("network down"))
    errors: list[str] = []
    assert fetch(cfg, session=session, errors=errors) == []
    assert errors


def test_api_keys_are_never_leaked_into_an_error_message(tmp_path: Path):
    """Error strings land in the digest and in run.log; a key in there is a
    key in your shell history and your backups."""
    cfg = adzuna_config(tmp_path)
    session = FakeSession(
        default=HttpError("GET https://api.adzuna.com/...?app_key=app-key -> HTTP 500")
    )
    errors: list[str] = []
    fetch(cfg, session=session, errors=errors)
    assert errors
    joined = " ".join(errors)
    assert "app-key" not in joined
    assert "app-id" not in joined


def test_a_malformed_payload_is_survived(tmp_path: Path):
    for payload in ({"results": "not-a-list"}, {}, [], {"results": [None, 42]}):
        assert fetch(adzuna_config(tmp_path), session=adzuna_session(payload)) == []
