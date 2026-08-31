"""Tests for src/sources/nofluffjobs.py — Tier 2.

No Fluff Jobs has no official API; the adapter reads the internal
`nofluffjobs.com/api/posting` listing the site's own frontend uses — the
whole board in one document. These tests pin the Tier 2 contract (nothing
ever crashes the run; a reshaped 200 is reported as drift by name), the
structured data/AI + junior/mid cut with its title fallback, the
three-letter country code the API actually sends, and the snippet-only
description contract.

Driven by `tests/fixtures/nofluffjobs_postings.json`. The fixture is
**spec-derived, not recorded** — written from the known listing shape while
this environment had no network route to nofluffjobs.com. The
`network`-marked tests in `test_live_contract.py` are what pin the real
field names; re-record the fixture from the live endpoint when they
disagree.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.sources.nofluffjobs import (
    DATA_CATEGORIES,
    fetch,
    parse_posting,
)
from src.util import HttpError
from tests.conftest import FakeSession, json_response, load_json_fixture

UTC = timezone.utc
PAYLOAD = load_json_fixture("nofluffjobs_postings.json")


def nf_session(body=None, **kwargs):
    return FakeSession(
        [("nofluffjobs.com", json_response(PAYLOAD if body is None else body))],
        **kwargs,
    )


def by_company(jobs):
    return {j.company: j for j in jobs}


# ==========================================================================
# the cut: category data/AI, seniority junior+mid
# ==========================================================================


def test_only_data_ai_junior_mid_postings_leave_the_adapter():
    jobs = fetch(None, session=nf_session())
    assert {j.company for j in jobs} == {
        "Kramerica Labs",          # category data, Mid
        "Pendant Publishing",      # category artificial-intelligence, Junior+Mid
        "Del Boca Vista Analytics" # no category — title fallback caught it
    }
    # Vandelay (backend) fell to the category cut, Monk's Cafe (Senior) to the
    # seniority cut, and the company-less last entry to parse_posting's floor.


def test_a_missing_category_falls_back_to_the_title():
    job = by_company(fetch(None, session=nf_session()))["Del Boca Vista Analytics"]
    assert job.title == "Analytics Engineer"
    assert job.raw["category"] is None


def test_a_multi_level_posting_counts_as_junior_mid():
    """`seniority: ["Junior", "Mid"]` is one posting open at two levels — it
    must survive a filter that reads the list as all-or-nothing."""
    assert "Pendant Publishing" in by_company(fetch(None, session=nf_session()))


def test_the_category_set_is_lowercase_slugs():
    """The API capitalises display names elsewhere; the `category` field is a
    slug. Guard the set so nobody 'fixes' it to display case."""
    assert all(c == c.lower() for c in DATA_CATEGORIES)


# ==========================================================================
# parsing
# ==========================================================================


def test_the_full_posting_maps_every_field():
    job = by_company(fetch(None, session=nf_session()))["Kramerica Labs"]
    assert job.title == "Data Scientist"
    assert job.url == (
        "https://nofluffjobs.com/job/data-scientist-kramerica-labs-warszawa-data1ab2"
    )
    assert job.location == "Warszawa, Kraków"
    assert job.salary == "15000–21000 PLN"
    assert job.posted_at == datetime.fromtimestamp(1787466600, tz=UTC)  # epoch ms
    assert job.ats_job_id == "DATA1AB2"
    assert job.raw["snippet_only"] is True
    assert job.raw["renewed"] == 1787553000000  # kept for the repost logic
    assert "Python" in job.description


def test_a_three_letter_country_code_is_not_passed_through():
    """The fixture's first posting says `POL`; `Job.country` speaks ISO
    alpha-2 and a wrong pass-through would corrupt the geo filter. Left unset,
    geo resolves the city instead — Warszawa is already in its tables."""
    job = by_company(fetch(None, session=nf_session()))["Kramerica Labs"]
    assert job.country is None


def test_a_two_letter_code_is_passed_through():
    job = by_company(fetch(None, session=nf_session()))["Pendant Publishing"]
    assert job.country == "PL"


def test_fully_remote_reads_remote_and_drops_the_placeholder_city():
    job = by_company(fetch(None, session=nf_session()))["Pendant Publishing"]
    assert job.remote is True
    assert job.location == "Remote"  # the literal "Remote" place is not a city


def test_parse_posting_requires_title_company_and_slug():
    assert parse_posting({"title": "X", "name": "Y", "url": ""}) is None
    assert parse_posting({"title": "X", "url": "z"}) is None
    assert parse_posting({"name": "Y", "url": "z"}) is None


# ==========================================================================
# the hard rule: never crash the run
# ==========================================================================


def test_http_failure_degrades_instead_of_raising():
    errors: list[str] = []
    jobs = fetch(None, session=FakeSession([("nofluffjobs.com", HttpError("boom"))]),
                 errors=errors)
    assert jobs == []
    assert "nofluffjobs" in errors[0]


def test_a_reshaped_payload_degrades_with_a_message_naming_the_drift():
    errors: list[str] = []
    jobs = fetch(None, session=nf_session({"data": []}), errors=errors)
    assert jobs == []
    assert "changed shape" in errors[0]


def test_one_malformed_posting_does_not_kill_the_board():
    body = json.loads(json.dumps(PAYLOAD))
    body["postings"].insert(0, "not-a-mapping")
    assert len(fetch(None, session=nf_session(body))) == 3
