"""Offline contract checks against reduced public evidence and negative controls."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import Tracker
from src.filters import apply_filters, dedupe, is_fresh, passes_location
from src.sources import allegro
import requests
from requests.adapters import BaseAdapter
from urllib.parse import urlsplit

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


# --- Feed shape captured 2026-10-05 (frontend build 2026-09-10) -----------------
# Listing fixtures are the public payloads as served; the two detail fixtures are
# reduced captures of the SAP pages the feed's invite links redirected to.

LIVE_NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
PM_URL = "https://careers.allegro.eu/job/Warszawa-Product-Manager-AdTech-00-841/1366343655/"
FILLED_URL = "https://careers.allegro.eu/job/Warszawa-Senior-Data-Analyst-%28Risk-Management%29-00-841/1363037255/"


def live(name):
    return json.loads(fixture(f"live_2026-10-05_{name}.json"))


def source_update(job):
    """What the digest prints as the source update (src/digest.py)."""
    return str(job.raw.get("updated_at") or job.raw.get("updatedAt") or "unknown")


def live_offer(uid):
    return next(o for o in live("listing_page1")["offers"] if o["id"] == uid)


def invite(uid):
    return f"https://careers.allegro.eu/job-invite/{uid}?locale=en_US"


def redirect(target):
    return FakeResponse(status_code=302, headers={"Location": target})


def invite_routes(entries, *detail_routes):
    return FakeSession(list(detail_routes) + [(allegro.LISTING_URL, json_response(envelope(entries)))])


def test_live_feed_shape_keeps_the_original_requisition_identity():
    jobs = [allegro.parse_offer(o) for o in live("listing_page1")["offers"]]
    assert all(jobs) and len({j.key for j in jobs}) == 10
    analyst = next(j for j in jobs if j.raw["uid"] == "3609")
    before = next(o for o in json.loads(fixture("listing_page2.json"))["offers"] if o["uid"] == "3609")
    assert before["name"] == analyst.title and allegro.parse_offer(before).key == analyst.key
    assert analyst.url == analyst.raw["listing_url"] == invite("3609")
    assert analyst.raw["uid_field"] == "id" and analyst.raw["wordpress_id"] is None
    assert analyst.posted_at == datetime(2026, 8, 5, 11, 52, 56, tzinfo=timezone.utc)
    assert analyst.raw["releaseDate"] == 1785930776
    assert analyst.remote is None and analyst.country is None
    assert analyst.description == "" and analyst.raw["snippet_only"]


@pytest.mark.parametrize("field,value", [
    ("id", 3506), ("id", "abc"), ("id", None), ("id", True),
    ("url", "https://careers.allegro.eu/job-invite/9999?locale=en_US"),
    ("url", "https://jobs.allegro.eu/offer/operations-process-leader/"),
    ("url", "https://careers.allegro.eu/talentcommunity/apply/3506/"),
    ("url", "https://careers.allegro.eu/job/Lublin-Operations-Process-Leader/1/"),
    ("url", "http://careers.allegro.eu/job-invite/3506"),
    ("url", "https://careers.allegro.eu.evil.example/job-invite/3506"),
    ("url", "https://user@careers.allegro.eu/job-invite/3506"),
])
def test_live_shape_rejects_unproven_identity_or_route(field, value):
    entry = dict(live_offer("3506"), **{field: value})
    assert allegro.parse_offer(entry) is None


def test_invite_link_keeps_only_its_published_locale():
    entry = dict(live_offer("3506"), url="https://careers.allegro.eu/job-invite/3506?locale=en_US&utm_source=x#apply")
    assert allegro.parse_offer(entry).url == "https://careers.allegro.eu/job-invite/3506"
    assert allegro.parse_offer(live_offer("3506")).url == invite("3506")


@pytest.mark.parametrize("value", [1784721682000, -1, 0, True, 1784721682.5, "1784721682"])
def test_implausible_epoch_release_dates_stay_unknown(value):
    job = allegro.parse_offer(dict(live_offer("3506"), releaseDate=value))
    assert job is not None and job.posted_at is None and job.raw["releaseDate"] == value


def test_historical_all_rows_rejected_page_now_parses_and_names_drift_when_it_does_not():
    page = live("listing_page1")
    session = FakeSession([(allegro.LISTING_URL, json_response(page))])
    errors = []
    jobs = allegro.fetch(config(max_pages=1, max_details=0), session=session, errors=errors)
    assert len(jobs) == 10 and all(j.posted_at for j in jobs)
    assert not any("malformed" in e for e in errors)
    assert any("page cap reached (1/16)" in e for e in errors)
    # The next reshaping must say what is missing instead of counting ten bad rows.
    for offer in page["offers"]:
        del offer["id"]
    errors = []
    assert allegro.fetch(config(max_pages=1, max_details=0), session=FakeSession([(allegro.LISTING_URL, json_response(page))]), errors=errors) == []
    assert any("skipped 10 malformed/non-job records (missing original id: 10)" in e and "drift suspected" in e for e in errors)


def test_one_bad_row_is_not_called_drift():
    errors = []
    jobs = allegro.fetch(config(max_details=0), session=routes([{}, live_offer("3506")]), errors=errors)
    assert len(jobs) == 1
    assert any("skipped 1 malformed" in e and "missing title or brand: 1" in e and "drift" not in e for e in errors)


def test_invite_redirect_enriches_listing_and_keeps_earliest_employer_date():
    session = invite_routes([live_offer("3509")],
                            ("job-invite/3509", redirect(PM_URL)),
                            (PM_URL, html_response(fixture("live_2026-10-05_detail_open.html"))))
    errors = []
    [job] = allegro.fetch(config(), session=session, errors=errors, now=LIVE_NOW)
    assert errors == []
    assert session.urls() == [allegro.LISTING_URL, invite("3509"), PM_URL]
    assert job.ats == "allegro_public" and job.ats_job_id == "allegro:uid:3509"
    assert job.url == PM_URL and job.raw["listing_url"] == invite("3509")
    assert job.raw["career_page_id"] == "1366343655" and job.raw["platform"] == "SAP SuccessFactors"
    assert job.title == "Product Manager - AdTech" and job.company == "Allegro sp. z o.o."
    assert job.location == "Warszawa, PL, 00-841" and job.remote is None
    assert len(job.description) == 5288 and "Cookie" not in job.description
    assert job.raw["description_status"] == "full" and not job.raw["snippet_only"]
    # Feed says 2026-09-02, the page says 2026-10-01. Both are kept as stated;
    # the earlier one dates the job and nothing is called an update.
    assert job.posted_at == datetime(2026, 9, 2, 11, 50, 14, tzinfo=timezone.utc)
    assert job.raw["releaseDate"] == 1788349814
    assert job.raw["datePosted"] == "Thu Oct 01 02:00:00 UTC 2026"
    assert job.raw["source_dates"] == {"feed releaseDate": "2026-09-02T11:50:14+00:00",
                                       "page datePosted": "2026-10-01T02:00:00+00:00"}
    assert job.raw["date_conflict"] is True
    assert job.raw["posted_at_source"] == "feed releaseDate"
    assert source_update(job) == "unknown"
    assert not is_fresh(job, 72, now=LIVE_NOW)[0]


def test_earliest_full_datetime_wins_whichever_field_states_it():
    listing = allegro.parse_offer(dict(live_offer("3509"), releaseDate=1790848800))  # 2026-10-01T10:00:00Z
    job = allegro.parse_detail(fixture("live_2026-10-05_detail_open.html"), PM_URL, listing=listing, now=LIVE_NOW)
    assert job.posted_at == datetime(2026, 10, 1, 2, tzinfo=timezone.utc)
    assert job.raw["posted_at_source"] == "page datePosted" and job.raw["date_conflict"] is True
    assert job.raw["source_dates"]["feed releaseDate"] == "2026-10-01T10:00:00+00:00"
    assert source_update(job) == "unknown"


@pytest.mark.parametrize("seeds", [[PM_URL], []])
def test_same_day_earlier_feed_time_is_kept_with_and_without_a_seed(seeds):
    """Feed 00:00, page 02:00 on one day: at 01:00 three days later the posting
    is 73 hours old, whether or not the page is also a configured seed."""
    midnight = datetime(2026, 10, 1, tzinfo=timezone.utc)
    entry = dict(live_offer("3509"), releaseDate=int(midnight.timestamp()))
    session = invite_routes([entry], ("job-invite/3509", redirect(PM_URL)),
                            (PM_URL, html_response(fixture("live_2026-10-05_detail_open.html"))))
    [job] = allegro.fetch(config(detail_urls=seeds), session=session, errors=[], now=LIVE_NOW)
    assert job.posted_at == midnight and job.raw["posted_at_source"] == "feed releaseDate"
    assert job.raw["date_conflict"] is True and source_update(job) == "unknown"
    assert not is_fresh(job, 72, now=datetime(2026, 10, 4, 1, tzinfo=timezone.utc))[0]


def test_agreeing_or_single_dates_are_not_a_conflict():
    listing = allegro.parse_offer(dict(live_offer("3509"), releaseDate=1790820000))  # 2026-10-01T02:00:00Z
    job = allegro.parse_detail(fixture("live_2026-10-05_detail_open.html"), PM_URL, listing=listing, now=LIVE_NOW)
    assert job.raw["date_conflict"] is False and len(job.raw["source_dates"]) == 2
    alone = allegro.parse_offer(live_offer("3509"))
    assert alone.raw["date_conflict"] is False and alone.raw["source_dates"] == {"feed releaseDate": "2026-09-02T11:50:14+00:00"}
    undated = allegro.parse_offer(dict(live_offer("3509"), releaseDate=None))
    assert undated.posted_at is None and undated.raw["posted_at_source"] is None and undated.raw["source_dates"] == {}


def test_live_filled_page_removes_a_job_the_feed_still_lists():
    session = invite_routes([live_offer("3609")],
                            ("job-invite/3609", redirect(FILLED_URL)),
                            (FILLED_URL, html_response(fixture("live_2026-10-05_detail_filled.html"))))
    errors = []
    assert allegro.fetch(config(), session=session, errors=errors, now=LIVE_NOW) == []
    assert errors == [] and len(session.calls) == 3


@pytest.mark.parametrize("target,message", [
    ("https://careers.allegro.eu/search/?q=3509", "unsafe"),
    ("https://careers.allegro.eu/talentcommunity/apply/1366343655/", "unsafe"),
    ("https://careers.allegro.eu/job-invite/3510", "unsafe"),
    ("https://evil.example/job/Product-Manager/1366343655/", "unsafe"),
])
def test_invite_redirect_only_reaches_a_career_job_page(target, message):
    session = invite_routes([live_offer("3509")], ("job-invite/3509", redirect(target)))
    errors = []
    [job] = allegro.fetch(config(), session=session, errors=errors)
    assert len(session.calls) == 2 and message in errors[0]
    assert job.url == invite("3509") and job.description == ""
    assert job.raw["detail_status"] == "failed" and job.raw["snippet_only"]


def test_career_page_reached_from_an_invite_cannot_move_to_another_job():
    session = invite_routes([live_offer("3509")],
                            ("job-invite/3509", redirect(PM_URL)),
                            (PM_URL, redirect("/job/Another-Role/999/")))
    errors = []
    [job] = allegro.fetch(config(), session=session, errors=errors)
    assert len(session.calls) == 3 and "changed original career job ID" in errors[0]
    assert job.description == "" and job.raw["detail_status"] == "failed"


def test_seed_and_listing_on_one_career_page_stay_one_job_with_the_seed_identity():
    session = invite_routes([live_offer("3509")],
                            ("job-invite/3509", redirect(PM_URL)),
                            (PM_URL, html_response(fixture("live_2026-10-05_detail_open.html"))))
    errors = []
    [job] = allegro.fetch(config(detail_urls=[PM_URL]), session=session, errors=errors, now=LIVE_NOW)
    assert errors == []
    assert session.urls() == [allegro.LISTING_URL, PM_URL, invite("3509")]  # page body is not requested twice
    assert job.ats == "successfactors" and job.ats_job_id == "allegro:career:1366343655"
    assert job.raw["uid"] == "3509" and job.raw["team"] == "IT - Product/Project Management"
    assert job.posted_at == datetime(2026, 9, 2, 11, 50, 14, tzinfo=timezone.utc)
    assert job.raw["date_conflict"] is True and source_update(job) == "unknown"
    assert job.raw["description_status"] == "full"


def seeded_run(first_seed_status):
    """Listing record 3509 plus its career page as a configured seed; the
    seed's own request answers `first_seed_status`, later requests answer 200."""
    calls = []
    def page(url, params):
        calls.append(url)
        if len(calls) == 1 and first_seed_status != 200:
            return FakeResponse(status_code=first_seed_status)
        return html_response(fixture("live_2026-10-05_detail_open.html"))
    session = invite_routes([live_offer("3509")], ("job-invite/3509", redirect(PM_URL)), (PM_URL, page))
    errors = []
    jobs = allegro.fetch(config(detail_urls=[PM_URL]), session=session, errors=errors, now=LIVE_NOW)
    return jobs, errors, session


@pytest.mark.parametrize("status", [500, 503])
def test_seed_identity_survives_a_failed_seed_request_recovered_through_the_listing(status):
    [healthy], healthy_errors, _ = seeded_run(200)
    [recovered], errors, session = seeded_run(status)
    assert healthy_errors == [] and any(f"detail {PM_URL}" in e and str(status) in e for e in errors)
    assert session.urls() == [allegro.LISTING_URL, PM_URL, invite("3509"), PM_URL]
    assert recovered.ats == "successfactors" and recovered.ats_job_id == "allegro:career:1366343655"
    assert recovered.key == healthy.key and recovered.url == healthy.url == PM_URL
    assert recovered.raw["uid"] == "3509" and recovered.raw["description_status"] == "full"
    assert recovered.posted_at == healthy.posted_at == datetime(2026, 9, 2, 11, 50, 14, tzinfo=timezone.utc)


def test_consecutive_runs_with_and_without_seed_failure_share_one_backlog_row():
    """A pending or exhausted evaluation recorded under the seed's key must
    still be found after a run in which the seed's own request failed."""
    tracker = Tracker()
    runs = [seeded_run(200), seeded_run(500), seeded_run(200)]
    admitted = [tracker.enqueue_scoring(jobs[0], now=LIVE_NOW) for jobs, _, _ in runs]
    assert admitted == [True, False, False]
    assert len(tracker.pending_scoring()) == 1
    assert len({jobs[0].key for jobs, _, _ in runs}) == 1
    # An exhausted evaluation is not handed a fresh retry budget by the next
    # run in which the seed's request fails.
    key = runs[0][0][0].key
    tracker.stop_scoring(key, "exhausted", "attempt limit")
    [again], _, _ = seeded_run(500)
    assert again.key == key and tracker.enqueue_scoring(again, now=LIVE_NOW) is False
    assert tracker.get_scoring(key)["state"] == "exhausted" and tracker.pending_scoring() == []


def test_a_seed_that_fails_and_is_never_reached_through_the_listing_reports_it():
    """Without the redirect nothing proves which record is the seed, so the
    record keeps its own identity for that run and the failure is visible."""
    session = invite_routes([live_offer("3509")], (PM_URL, FakeResponse(status_code=500)))
    errors = []
    [job] = allegro.fetch(config(detail_urls=[PM_URL], max_details=1), session=session, errors=errors, now=LIVE_NOW)
    assert job.ats_job_id == "allegro:uid:3509" and job.raw["detail_status"] == "not_fetched"
    assert any(f"detail {PM_URL}" in e for e in errors) and any("detail cap" in e for e in errors)


def test_each_invite_detail_costs_two_requests_inside_the_budget():
    session = invite_routes([live_offer("3509")],
                            ("job-invite/3509", redirect(PM_URL)),
                            (PM_URL, html_response(fixture("live_2026-10-05_detail_open.html"))))
    errors = []
    [job] = allegro.fetch(config(max_requests=2), session=session, errors=errors)
    assert len(session.calls) == 2 and any("request cap" in e for e in errors)
    assert job.description == "" and job.raw["detail_status"] == "failed"


def test_team_filter_is_sent_as_the_listing_page_sends_it():
    teams = ["Data & AI", "IT - Machine Learning", "IT - Analytics & Consulting"]
    session = FakeSession([(allegro.LISTING_URL, json_response(live("listing_team_filter")))])
    errors = []
    jobs = allegro.fetch(config(teams=teams, max_details=0), session=session, errors=errors)
    assert session.calls[0]["params"] == {"lang": "en", "page": 1, "team[]": teams}
    assert [j.title for j in jobs] == ["Machine Learning Engineer - Allegro Pay",
                                       "Senior Product Analyst - Allegro Pay (Machine Learning and Analytics)",
                                       "Data Scientist - Logistics AI & ML", "Data Scientist", "Junior Data Scientist"]
    assert errors == ["allegro: detail cap reached; description coverage partial"]


def test_ignored_team_filter_and_bad_team_config_are_reported_not_hidden():
    session = FakeSession([(allegro.LISTING_URL, json_response(live("listing_page1")))])
    errors = []
    jobs = allegro.fetch(config(teams=["Data & AI"], max_pages=1, max_details=0), session=session, errors=errors)
    assert len(jobs) == 10 and any("team filter not honoured" in e for e in errors)
    for bad in ["Data & AI", [""], [7], ["x" * 81]]:
        session = routes([live_offer("3506")])
        errors = []
        assert len(allegro.fetch(config(teams=bad, max_details=0), session=session, errors=errors)) == 1
        assert "team[]" not in session.calls[0]["params"] and any("teams must be a list" in e for e in errors)


def test_live_shape_funnel_dates_known_descriptions_missing_nothing_fresh():
    records = live("listing_page1")["offers"] + live("listing_team_filter")["offers"]
    jobs = [job for item in records if (job := allegro.parse_offer(item))]
    assert len(jobs) == 15 and all(j.posted_at for j in jobs)
    assert sum(j.raw["description_status"] == "full" for j in jobs) == 0
    settings = {"filters": {"countries": ["PL"]}, "freshness": {"max_age_hours": 72, "skip_undated": True}}
    located = [j for j in jobs if passes_location(j, settings)[0]]
    assert len(located) == 12  # three depot/mobile place names do not resolve to a country
    assert {j.raw["team"] for j in jobs if j not in located} == {"Delivery Experience", "Legal and Public Affairs"}
    results = apply_filters(jobs, settings, now=LIVE_NOW)
    assert results.kept == []
    assert sum("older than" in reason for _, reason in results.rejected) == 12
    assert sum("could not be resolved" in reason for _, reason in results.rejected) == 3
    assert not any("no posting date" in reason for _, reason in results.rejected)
    assert len(dedupe(jobs)) == 15


def test_same_career_page_moved_its_date_between_captures_and_the_feed_did_not():
    """One page, two recordings: datePosted read Aug 28 on 2026-09-09 and
    Sep 26 on 2026-10-05, while the feed states Aug 28 for that requisition.
    Why the page value changed is not known; the earlier stated date is the
    one used for freshness."""
    then = allegro.parse_detail(fixture("ai_hub_detail.html"), AI_URL, now=NOW)
    today = allegro.parse_detail(fixture("live_2026-10-05_detail_ai_hub.html"), AI_URL, now=LIVE_NOW)
    assert then.key == today.key and then.description == today.description
    assert then.posted_at == datetime(2026, 8, 28, tzinfo=timezone.utc)
    assert today.posted_at == datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
    assert today.raw["posted_at_source"] == "page datePosted"  # the only date a seed has
    assert today.raw["date_conflict"] is False and source_update(today) == "unknown"
    listed = next(o for o in live("listing_team_filter")["offers"] if o["id"] == "3784")
    session = invite_routes([listed], ("job-invite/3784", redirect(AI_URL)),
                            (AI_URL, html_response(fixture("live_2026-10-05_detail_ai_hub.html"))))
    for seeds in ([AI_URL], []):
        [job] = allegro.fetch(config(detail_urls=seeds), session=session, errors=[], now=LIVE_NOW)
        assert job.posted_at == datetime(2026, 8, 28, 6, 23, 20, tzinfo=timezone.utc)
        assert job.raw["source_dates"] == {"feed releaseDate": "2026-08-28T06:23:20+00:00",
                                           "page datePosted": "2026-09-26T02:00:00+00:00"}
        assert job.raw["date_conflict"] is True and source_update(job) == "unknown"
        assert job.raw["releaseDate"] == 1787898200 and job.raw["datePosted"] == "Sat Sep 26 02:00:00 UTC 2026"
        assert job.raw["posted_at_source"] == "feed releaseDate"
        assert job.ats_job_id == ("allegro:career:1365885255" if seeds else "allegro:uid:3784")
        assert job.raw["uid"] == "3784" and job.raw["career_page_id"] == "1365885255"


# --- Bounded walk of the whole listing (16 pages observed on 2026-10-05) ----------


def paged_feed(total_pages, last_page_rows=6):
    """A synthetic feed in the recorded shape: ten distinct records a page."""
    template = live_offer("3506")
    count = (total_pages - 1) * 10 + last_page_rows
    def page(url, params):
        number = params["page"]
        rows = 10 if number < total_pages else last_page_rows if number == total_pages else 0
        offers = []
        for row in range(rows):
            uid = str(10_000 + (number - 1) * 10 + row)
            offers.append(dict(template, id=uid, url=f"https://careers.allegro.eu/job-invite/{uid}?locale=en_US"))
        return json_response({"offers": offers, "max_pages": total_pages, "results_count": count})
    return page, count


def test_sixteen_page_feed_is_walked_completely_inside_the_configured_bounds():
    page, count = paged_feed(16)
    session = FakeSession([(allegro.LISTING_URL, page)])
    errors = []
    jobs = allegro.fetch(config(max_pages=16, max_details=0, max_requests=40), session=session, errors=errors)
    assert count == 156 and len(jobs) == 156 and len({j.key for j in jobs}) == 156
    assert [c["params"]["page"] for c in session.calls] == list(range(1, 17))
    assert errors == ["allegro: detail cap reached; description coverage partial"]


@pytest.mark.parametrize("configured,feed_pages,walked,capped", [
    (16, 17, 16, True), (17, 17, 17, False), (20, 20, 20, False), (20, 21, 20, True), (99, 25, 20, True),
])
def test_page_ceiling_is_twenty_and_a_longer_feed_reports_partial(configured, feed_pages, walked, capped):
    page, _ = paged_feed(feed_pages)
    session = FakeSession([(allegro.LISTING_URL, page)])
    errors = []
    jobs = allegro.fetch(config(max_pages=configured, max_details=0, max_requests=40), session=session, errors=errors)
    assert len(session.calls) == walked and len(jobs) == min(walked * 10, (feed_pages - 1) * 10 + 6)
    assert any(f"page cap reached ({walked}/{feed_pages}); coverage partial" in e for e in errors) is capped


def test_request_ceiling_is_forty_and_details_stop_when_it_is_spent():
    """16 listing pages leave 24 requests; each invite detail costs two, so
    twelve details fit and the thirteenth reports the cap."""
    page, _ = paged_feed(16)
    detail = html_response(fixture("live_2026-10-05_detail_open.html"))
    def careers(url, params):
        if "/job-invite/" in url:
            return redirect(f"https://careers.allegro.eu/job/Role/{url.split('/job-invite/')[1].split('?')[0]}/")
        return detail
    session = FakeSession([("careers.allegro.eu", careers), (allegro.LISTING_URL, page)])
    errors = []
    jobs = allegro.fetch(config(max_pages=16, max_details=20, max_requests=10**6), session=session, errors=errors, now=LIVE_NOW)
    assert len(session.calls) == 40
    assert sum(j.raw["description_status"] == "full" for j in jobs) == 12 and len(jobs) == 156
    assert any("request cap reached; coverage partial" in e for e in errors)
    assert any("detail cap reached" in e for e in errors)


def test_the_configured_watchlist_slice_fits_its_request_budget():
    """watchlist.yaml: 16 pages, 8 details, 40 requests, one seed, no team filter."""
    page, _ = paged_feed(16)
    def careers(url, params):
        if "/job-invite/" in url:
            return redirect(f"https://careers.allegro.eu/job/Role/{url.split('/job-invite/')[1].split('?')[0]}/")
        return html_response(fixture("live_2026-10-05_detail_open.html"))
    session = FakeSession([("careers.allegro.eu", careers), (allegro.LISTING_URL, page)])
    errors = []
    jobs = allegro.fetch(config(max_pages=16, max_details=8, max_requests=40, detail_urls=[AI_URL]),
                         session=session, errors=errors, now=LIVE_NOW)
    assert len(session.calls) == 16 + 1 + 7 * 2
    assert "team[]" not in session.calls[0]["params"]
    assert not any("request cap" in e or "page cap" in e for e in errors)
    assert sum(j.raw["description_status"] == "full" for j in jobs) == 8


# --- The request budget, measured on a real requests.Session ------------------------


class OfflineAdapter(BaseAdapter):
    """Real redirect machinery, no socket: `route(url) -> (status, headers, text)`."""

    def __init__(self, route):
        super().__init__()
        self.route, self.calls = route, []

    def send(self, request, **kwargs):
        self.calls.append(request.url)
        status, headers, text = self.route(request.url)
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, status
        response.headers.update(headers)
        response._content, response.encoding = text.encode("utf-8"), "utf-8"
        return response

    def close(self):
        pass


def offline(route):
    session = requests.Session()
    session.trust_env = False
    adapter = OfflineAdapter(route)
    session.mount("https://", adapter)
    return session, adapter


def test_real_session_never_follows_a_redirect_off_the_career_routes():
    listing = json.dumps(envelope([live_offer("3509")]))
    def route(url):
        if "wp-json" in url:
            return 200, {"Content-Type": "application/json"}, listing
        if "careers.allegro.eu" in url:
            return 302, {"Location": "https://outside.invalid/hop/0"}, ""
        return 302, {"Location": "https://outside.invalid/hop/1"}, ""
    session, adapter = offline(route)
    errors = []
    [job] = allegro.fetch(config(), session=session, errors=errors)
    assert [urlsplit(u).netloc for u in adapter.calls] == ["jobs.allegro.eu", "careers.allegro.eu"]
    assert "unsafe or looping redirect" in errors[0] and job.raw["detail_status"] == "failed"


def test_real_session_charges_every_redirect_hop_to_the_request_budget():
    listing = json.dumps(envelope([live_offer("3509")]))
    def route(url):
        if "wp-json" in url:
            return 200, {"Content-Type": "application/json"}, listing
        if "job-invite" in url:
            return 302, {"Location": "/job/Role-A/777/"}, ""
        slug = urlsplit(url).path.split("/")[2]
        return 302, {"Location": f"/job/{slug}x/777/"}, ""        # same job number, endless
    for budget, expected in ((3, 3), (40, 5)):                      # request cap, then the four-hop redirect cap
        session, adapter = offline(route)
        errors = []
        allegro.fetch(config(max_requests=budget), session=session, errors=errors)
        assert len(adapter.calls) == expected
        assert any("request cap reached" in e or "redirect cap reached" in e for e in errors)
