"""Tests for src/sources/arbeitnow.py.

Arbeitnow is a *global feed*, not a watchlist board: no slug, no key, one
paginated JSON document for the whole (Germany-heavy) board. What
correctness means here is therefore different from the ATS adapters — the
tests pin the aggregator concerns: epoch dates, the employment-type passthrough that catches
neutrally-titled internships, pagination that stops when `links.next` says
so, and above all the hard rule that nothing raises out of `fetch()`.

Driven by `tests/fixtures/arbeitnow_page.json`. The fixture is
**spec-derived, not recorded** (documented at
documenter.getpostman.com/view/18545278/UVJbJdKh): it was written while this
environment had no network route to arbeitnow.com. Its assumptions are
pinned against the live API by the `network`-marked tests in
`test_live_contract.py` — run `pytest -m network` once the network allows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.sources.arbeitnow import API_URL, MAX_PAGES, fetch, parse_job
from src.util import HttpError
from tests.conftest import FakeResponse, FakeSession, json_response, load_json_fixture

UTC = timezone.utc
PAYLOAD = load_json_fixture("arbeitnow_page.json")


def an_session(body=None, **kwargs):
    return FakeSession(
        [("arbeitnow.com", json_response(PAYLOAD if body is None else body))],
        **kwargs,
    )


def by_company(jobs):
    return {j.company: j for j in jobs}


# ==========================================================================
# parsing
# ==========================================================================


def test_parses_the_page_and_skips_the_nameless_entry():
    jobs = fetch(None, session=an_session())
    assert len(jobs) == 3  # the fourth entry has no company and no URL
    assert all(j.source == "arbeitnow" for j in jobs)


def test_the_full_entry_maps_every_field():
    job = by_company(fetch(None, session=an_session()))["Kramerica Labs"]
    assert job.title == "Data Scientist - Forecasting"
    assert job.location == "Berlin"
    assert job.remote is False
    assert "LightGBM" in job.description
    assert "<p>" not in job.description
    assert job.ats is None  # a board, not the employer's ATS
    assert job.ats_job_id == "data-scientist-forecasting-berlin-1043"


def test_created_at_is_a_unix_epoch():
    job = by_company(fetch(None, session=an_session()))["Kramerica Labs"]
    assert job.posted_at == datetime.fromtimestamp(1787471100, tz=UTC)


def test_visa_sponsorship_is_read_when_the_feed_sends_it():
    """The live feed dropped this field in 2026 (so the fixture no longer
    carries it), but the parser still reads it defensively for the day it
    returns — no ATS in this pipeline publishes that bit."""
    entry = dict(PAYLOAD["data"][0], visa_sponsorship=True)
    assert parse_job(entry).raw["visa_sponsorship"] is True
    assert by_company(fetch(None, session=an_session()))[
        "Kramerica Labs"].raw["visa_sponsorship"] is None


def test_job_types_land_where_the_employment_filter_reads():
    """`Werkstudent Data Analytics` passes naive title rules; `job_types:
    ["internship"]` is the structured truth `filters.employment_type_exclude`
    matches on."""
    job = by_company(fetch(None, session=an_session()))["Vandelay Industries"]
    assert job.raw["employment_type"] == "internship"


def test_remote_with_a_city_keeps_the_city():
    job = by_company(fetch(None, session=an_session()))["Pendant Publishing"]
    assert job.remote is True
    assert job.location == "Munich"


def test_parse_job_requires_title_company_and_url():
    assert parse_job({"title": "X", "company_name": "Y"}) is None
    assert parse_job({"title": "X", "url": "https://e.example"}) is None
    assert parse_job({"company_name": "Y", "url": "https://e.example"}) is None


# ==========================================================================
# pagination
# ==========================================================================


def test_a_null_next_link_stops_after_one_page():
    session = an_session()
    fetch(None, session=session)
    assert len(session.calls) == 1
    assert session.calls[0]["params"] == {"page": 1}


def test_pagination_follows_next_until_it_ends():
    first = dict(PAYLOAD)
    first["links"] = {"next": API_URL + "?page=2"}

    def route(url, params):
        page = (params or {}).get("page")
        return json_response(first if page == 1 else PAYLOAD)

    session = FakeSession([("arbeitnow.com", route)])
    jobs = fetch(None, session=session)
    assert [c["params"]["page"] for c in session.calls] == [1, 2]
    assert len(jobs) == 6  # three parsed per page


def test_the_page_budget_is_bounded_even_if_next_never_ends():
    endless = dict(PAYLOAD)
    endless["links"] = {"next": API_URL + "?page=999"}
    session = FakeSession([("arbeitnow.com", json_response(endless))])
    fetch(None, session=session)
    assert len(session.calls) == MAX_PAGES


# ==========================================================================
# the hard rule: never crash the run
# ==========================================================================


def test_http_failure_degrades_instead_of_raising():
    session = FakeSession([("arbeitnow.com", HttpError("boom"))])
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert jobs == []
    assert len(errors) == 1
    assert "arbeitnow" in errors[0]


def test_a_reshaped_payload_degrades_with_a_message_naming_the_drift():
    """Valid JSON that is not the feed — an error envelope, a maintenance
    page — must be reported, not read as an empty board."""
    errors: list[str] = []
    jobs = fetch(None, session=an_session({"message": "gone fishing"}),
                 errors=errors)
    assert jobs == []
    assert "changed shape" in errors[0]


def test_one_malformed_entry_does_not_kill_the_page():
    body = json.loads(json.dumps(PAYLOAD))
    body["data"].insert(0, "not-a-mapping")
    jobs = fetch(None, session=an_session(body))
    assert len(jobs) == 3
