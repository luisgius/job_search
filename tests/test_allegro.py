"""Offline contract checks against reduced public evidence and negative controls."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.filters import apply_filters, dedupe, is_fresh, passes_location
from src.sources import allegro
from conftest import FakeResponse, FakeSession, html_response, json_response

FIXTURES = Path(__file__).parent / "fixtures" / "allegro"
AI_URL = "https://careers.allegro.eu/job/Warszawa-Data-Scientist-Allegro-AI-Hub-00-841/1365885255/"
NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def fixture(name):
    return (FIXTURES / name).read_text()


def payload():
    return json.loads(fixture("listing_page1.json"))


def config(**settings):
    return SimpleNamespace(source_enabled=lambda source: source == "allegro", watchlist={"allegro": settings})


def envelope(entries):
    return {"offers": entries, "max_pages": 1 if entries else 0, "results_count": len(entries)}


def routes(entries=None, detail=None):
    entries = [payload()["offers"][0]] if entries is None else entries
    return FakeSession([(allegro.LISTING_URL, json_response(envelope(entries)))],
                       default=detail or html_response(fixture("ai_hub_detail.html")))


def test_public_listing_original_uid_and_unknowns():
    job = allegro.parse_offer(payload()["offers"][0])
    assert job.ats_job_id == "allegro:uid:3519"
    assert job.ats == "allegro_public" and job.raw["platform"] is None
    assert job.raw["wordpress_id"] == 666854
    assert job.remote is None and job.posted_at is None and job.country is None
    assert job.raw["snippet_only"] and job.raw["description_status"] == "missing"
    before = job.key
    job.company = "Employer display override"
    job.title = "Edited title"
    assert job.key == before


def test_public_sap_detail_publication_not_fetch_time():
    job = allegro.parse_detail(fixture("ai_hub_detail.html"), AI_URL)
    assert job.title == "Data Scientist - Allegro AI Hub"
    assert job.company == "Allegro sp. z o.o."
    assert job.location == "Warszawa, PL, 00-841"
    assert job.posted_at == datetime(2026, 8, 28, tzinfo=timezone.utc)
    assert len(job.description) == 4928
    assert "At least 2 years" in job.description
    assert "Cookie" not in job.description
    assert job.remote is None  # hybrid and occasional remote days are not TELECOMMUTE
    assert job.raw["description_status"] == "full" and not job.raw["snippet_only"]
    assert job.ats_job_id == "allegro:career:1365885255"
    assert not is_fresh(job, 72, now=NOW)[0]


@pytest.mark.parametrize("entry", [None, [], {}, {"uid": True}, {"name": "fake"}])
def test_malformed_record(entry):
    assert allegro.parse_offer(entry) is None


@pytest.mark.parametrize("field,value", [("uid", ""), ("brand", ""), ("url", "https://evil.example/offer/a/"),
                                         ("url", "https://jobs.allegro.eu/profile/"), ("name", "We have moved to a new careers site"),
                                         ("name", "Test"), ("name", "This job is no longer available")])
def test_unusable_record(field, value):
    entry = payload()["offers"][0]
    entry[field] = value
    assert allegro.parse_offer(entry) is None


@pytest.mark.parametrize("value", [None, "yesterday", "Sep 9", "September 2026", "2026", "broken", 123, {}, "2026-99-99"])
def test_invalid_or_incomplete_dates_stay_unknown(value):
    entry = payload()["offers"][0]
    entry["releaseDate"] = value
    assert allegro.parse_offer(entry).posted_at is None


def test_date_offset_is_utc():
    entry = payload()["offers"][0]
    entry["releaseDate"] = "2026-09-09T10:00:00+02:00"
    assert allegro.parse_offer(entry).posted_at == datetime(2026, 9, 9, 8, tzinfo=timezone.utc)


def test_page_cap_and_original_id_dedupe():
    first = payload()
    second = json.loads(fixture("listing_page2.json"))
    second["offers"].append(first["offers"][0])
    session = FakeSession([(allegro.LISTING_URL, lambda url, params: json_response(first if params["page"] == 1 else second))])
    errors = []
    jobs = allegro.fetch(config(max_pages=2, max_details=0), session=session, errors=errors)
    assert len(jobs) == 19  # public test posting excluded; repeated uid does not add a job
    assert [c["params"] for c in session.calls] == [{"lang": "en", "page": 1}, {"lang": "en", "page": 2}]
    assert all(e.startswith("allegro:") for e in errors)
    assert any("page cap" in e for e in errors)


def test_valid_empty_is_distinct_from_failed_or_malformed_feed():
    errors = []
    assert allegro.fetch(config(), session=routes([]), errors=errors) == []
    assert errors == []
    for value in [{}, [], {"offers": [], "max_pages": 1, "results_count": 3},
                  {"offers": [], "max_pages": "0", "results_count": 0}]:
        errors = []
        assert allegro.fetch(config(), session=FakeSession(default=json_response(value)), errors=errors) == []
        assert errors and "listing page 1" in errors[0]


def test_second_page_failure_preserves_first():
    session = FakeSession([(allegro.LISTING_URL, lambda u, p: json_response(payload()) if p["page"] == 1 else html_response("down", 503))])
    errors = []
    jobs = allegro.fetch(config(max_details=0), session=session, errors=errors)
    assert len(jobs) == 9
    assert any("listing page 2" in e and "503" in e for e in errors)


@pytest.mark.parametrize("response", [html_response("down", 500), RuntimeError("timeout"),
                                      html_response(fixture("migration.html")), html_response(fixture("nonjob.html"))])
def test_detail_failure_keeps_listing_with_missing_provenance(response):
    errors = []
    jobs = allegro.fetch(config(), session=routes(detail=response), errors=errors)
    assert len(jobs) == 1
    assert jobs[0].description == "" and jobs[0].posted_at is None
    assert jobs[0].raw["detail_status"] == "failed" and jobs[0].raw["snippet_only"]
    assert any("detail https://jobs.allegro.eu/offer/dispatcher-4/" in e for e in errors)


@pytest.mark.parametrize("response", [html_response("gone", 404), html_response("gone", 410), html_response(fixture("closed.html"))])
def test_closed_job_removed_without_listing_fallback(response):
    errors = []
    assert allegro.fetch(config(), session=routes(detail=response), errors=errors) == []
    assert errors == []


def test_seed_detail_and_caps():
    session = routes([])
    jobs = allegro.fetch(config(detail_urls=[AI_URL, AI_URL + "?tracking=1"]), session=session)
    assert len(jobs) == 1 and jobs[0].ats_job_id == "allegro:career:1365885255"
    assert len(session.calls) == 2
    errors = []
    jobs = allegro.fetch(config(detail_urls=[AI_URL], max_requests=1), session=routes([]), errors=errors)
    assert jobs == [] and any("request cap" in e for e in errors)


def test_invalid_seeds_never_requested():
    session = routes([])
    errors = []
    assert allegro.fetch(config(detail_urls=["https://evil.example/job/a/1/", "https://careers.allegro.eu/talentcommunity/apply/1/", "//careers.allegro.eu.evil/job/a/1/"]), session=session, errors=errors) == []
    assert len(session.calls) == 1 and len(errors) == 3


def test_safe_redirect_and_request_budget():
    class Session(FakeSession):
        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"] == 30
            return super().get(url, **kwargs)
    session = Session(default=FakeResponse(status_code=302, headers={"Location": allegro.LISTING_URL + "?lang=en&page=1"}))
    errors = []
    allegro.fetch(config(timeout_seconds=100, max_requests=2), session=session, errors=errors)
    assert len(session.calls) == 2
    assert any("redirect" in e or "request cap" in e for e in errors)


@pytest.mark.parametrize("target", ["https://evil.example/", "http://jobs.allegro.eu//wp-json/api/v1/offer", "https://jobs.allegro.eu/login/"])
def test_unsafe_listing_redirect_not_followed(target):
    session = FakeSession(default=FakeResponse(status_code=302, headers={"Location": target}))
    errors = []
    assert allegro.fetch(config(), session=session, errors=errors) == []
    assert len(session.calls) == 1 and "unsafe" in errors[0]


def test_seed_redirect_cannot_change_job_identity():
    response = FakeResponse(status_code=302, headers={"Location": "/job/Different/999/"})
    session = routes([], response)
    errors = []
    assert allegro.fetch(config(detail_urls=[AI_URL]), session=session, errors=errors) == []
    assert len(session.calls) == 2 and "changed original" in errors[0]


def test_expiry_undated_empty_and_remote_jsonld():
    data = {"@type": "JobPosting", "title": "Data Scientist", "description": "Python machine learning", "jobLocationType": "TELECOMMUTE"}
    def parse():
        return allegro.parse_detail('<script type="application/ld+json">' + json.dumps(data) + '</script>', AI_URL, now=NOW)
    job = parse()
    assert job.remote is True and job.posted_at is None
    data["validThrough"] = "2020-01-01"
    assert parse() is None
    del data["validThrough"]
    data["description"] = ""
    assert parse().raw["description_status"] == "missing"


def test_downstream_identity_location_and_freshness_funnel():
    public = payload()["offers"] + json.loads(fixture("listing_page2.json"))["offers"]
    jobs = [job for item in public if (job := allegro.parse_offer(item))]
    jobs.append(allegro.parse_detail(fixture("ai_hub_detail.html"), AI_URL))
    assert len(jobs) == 20  # 19 real listing records + separate verified SAP page
    assert all(passes_location(j, {"filters": {"countries": ["PL"]}})[0] for j in jobs)
    assert all(j.country == "PL" for j in jobs)
    results = apply_filters(jobs, {"filters": {"countries": ["PL"]}, "freshness": {"max_age_hours": 72, "skip_undated": True}}, now=NOW)
    assert results.kept == []
    assert len(results.rejected) == 20
    assert sum("no posting date" in reason for _, reason in results.rejected) == 19
    assert sum("older than" in reason for _, reason in results.rejected) == 1
    full = jobs[-1]
    duplicate = copy.deepcopy(full)
    duplicate.source = "aggregator"
    duplicate.description = "snippet"
    assert dedupe([duplicate, full]) == [full]
    assert is_fresh(jobs[0], 72, skip_undated=False, now=NOW)[0]
    outside = copy.deepcopy(full)
    outside.location, outside.country = "Prague, CZ", "CZ"
    assert not passes_location(outside, {"filters": {"countries": ["PL"]}})[0]


def test_disabled_and_configuration_bounds():
    session = routes()
    cfg = config()
    cfg.source_enabled = lambda _: False
    assert allegro.fetch(cfg, session=session) == [] and not session.calls
    for value in [None, "broken", {}, -3, 10**100]:
        settings = {key: value for key in ["max_pages", "max_details", "max_requests", "timeout_seconds"]}
        allegro.fetch(config(**settings), session=routes([]))


def test_detail_priority_reserves_seed_then_ml_and_analytics():
    entries = payload()["offers"][:2]
    ml = dict(entries[0], uid="777", name="Machine Learning Engineer", url="https://jobs.allegro.eu/offer/ml/")
    analyst = dict(entries[0], uid="778", name="Data Analyst", url="https://jobs.allegro.eu/offer/analyst/")
    session = routes(entries + [analyst, ml], html_response("down", 500))
    allegro.fetch(config(detail_urls=[AI_URL], max_details=3), session=session)
    assert session.urls()[1:] == [AI_URL, ml["url"], analyst["url"]]


def test_offer_redirect_does_not_borrow_another_jobs_description():
    session = routes(detail=FakeResponse(status_code=302, headers={"Location": "/offer/another-job/"}))
    errors = []
    job = allegro.fetch(config(), session=session, errors=errors)[0]
    assert job.description == "" and len(session.calls) == 2
    assert "changed original offer" in errors[0]


def test_listing_record_failures_are_isolated():
    entries = [None, {}, payload()["offers"][0]]
    errors = []
    jobs = allegro.fetch(config(max_details=0), session=routes(entries), errors=errors)
    assert len(jobs) == 1 and any("skipped 2 malformed" in e for e in errors)


def test_nonjob_listing_and_failed_seed_do_not_become_jobs():
    session = FakeSession(default=html_response(fixture("migration.html")))
    errors = []
    assert allegro.fetch(config(detail_urls=[AI_URL]), session=session, errors=errors) == []
    assert len(errors) == 2


def test_body_size_is_bounded_and_attributed():
    session = FakeSession(default=html_response("x" * 2_000_001))
    errors = []
    assert allegro.fetch(config(), session=session, errors=errors) == []
    assert "body limit" in errors[0]


def test_request_cap_hard_bounds_pages_and_details():
    session = routes()
    errors = []
    jobs = allegro.fetch(config(max_requests=1, max_details=999999), session=session, errors=errors)
    assert len(session.calls) == 1 and len(jobs) == 1
    assert any("request cap" in e for e in errors)


def test_employer_structured_detail_does_not_claim_sap_vendor():
    listing = allegro.parse_offer(payload()["offers"][0])
    original_key = listing.key
    body = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": listing.title, "description": "Full public employer description."}) + '</script>'
    job = allegro.parse_detail(body, listing.url, listing=listing, now=NOW)
    assert job.key == original_key
    assert job.ats == "allegro_public" and job.raw["platform"] is None
    sap = allegro.parse_detail(fixture("ai_hub_detail.html"), AI_URL, now=NOW)
    assert sap.ats == "successfactors" and sap.raw["platform"] == "SAP SuccessFactors"


def test_frozen_expiry_propagates_through_fetch():
    data = {"@type": "JobPosting", "title": "Data Scientist", "description": "Full description", "validThrough": "2026-09-10T12:00:00Z"}
    body = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
    assert len(allegro.fetch(config(detail_urls=[AI_URL]), session=routes([], html_response(body)), now=NOW)) == 1
    later = datetime(2026, 9, 11, tzinfo=timezone.utc)
    assert allegro.fetch(config(detail_urls=[AI_URL]), session=routes([], html_response(body)), now=later) == []


@pytest.mark.parametrize("mode", ["repeated", "short", "duplicate_uid", "changed_metadata"])
def test_last_page_detects_incomplete_listing_without_losing_jobs(mode):
    entries = payload()["offers"][:2]
    first = {"offers": [entries[0]], "max_pages": 2, "results_count": 2}
    second = {"offers": [entries[1]], "max_pages": 2, "results_count": 2}
    if mode == "repeated":
        second = first
    elif mode == "short":
        first["results_count"] = second["results_count"] = 3
    elif mode == "duplicate_uid":
        second["offers"][0]["uid"] = first["offers"][0]["uid"]
    else:
        second["results_count"] = 3
    session = FakeSession([(allegro.LISTING_URL, lambda u, p: json_response(first if p["page"] == 1 else second))])
    errors = []
    jobs = allegro.fetch(config(max_pages=2, max_details=0), session=session, errors=errors)
    assert jobs
    assert any("count mismatch" in e and "coverage partial" in e for e in errors)
    if mode == "repeated":
        assert any("repeated listing" in e for e in errors)
    if mode == "changed_metadata":
        assert any("metadata changed" in e for e in errors)


def test_consistent_last_page_does_not_claim_partial_listing():
    entries = payload()["offers"][:2]
    session = FakeSession([(allegro.LISTING_URL, lambda u, p: json_response({"offers": [entries[p["page"] - 1]], "max_pages": 2, "results_count": 2}))])
    errors = []
    assert len(allegro.fetch(config(max_pages=2, max_details=0), session=session, errors=errors)) == 2
    assert not any("listing" in e or "page cap" in e for e in errors)
