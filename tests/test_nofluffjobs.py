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
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.filters import apply_filters, dedupe, passes_location
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


def test_a_three_letter_country_code_is_normalized():
    """Explicit POL evidence survives even when a city cannot resolve it."""
    job = by_company(fetch(None, session=nf_session()))["Kramerica Labs"]
    assert job.country == "PL"


def test_a_two_letter_code_is_passed_through():
    job = by_company(fetch(None, session=nf_session()))["Pendant Publishing"]
    assert job.country == "PL"


def test_fully_remote_reads_remote_and_drops_the_placeholder_city():
    job = by_company(fetch(None, session=nf_session()))["Pendant Publishing"]
    assert job.remote is True
    assert job.location == "Remote"


def test_parse_posting_requires_title_company_and_slug():
    assert parse_posting({"title": "X", "name": "Y", "url": ""}) is None
    assert parse_posting({"title": "X", "url": "z"}) is None
    assert parse_posting({"name": "Y", "url": "z"}) is None


# Small synthetic examples of the September 2026 listing: a Remote placeholder
# followed by country-bearing province-only places, with no recognized city.
NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
POLAND_FILTERS = {
    "filters": {"countries": ["PL"], "allow_remote": True,
                "remote_requires_eu_hint": True},
    "freshness": {"max_age_hours": 72, "skip_undated": True},
}


def province_posting(*codes, fully_remote=True, age_hours=24, company="DCG"):
    posted = None if age_hours is None else int(
        (NOW - timedelta(hours=age_hours)).timestamp() * 1000
    )
    return {
        "id": f"data-scientist-{company}-remote",
        "url": f"data-scientist-{company}-remote",
        "name": company, "title": "Data Scientist", "category": "data",
        "seniority": ["Mid"], "technology": "Python", "posted": posted,
        "location": {
            "fullyRemote": fully_remote,
            "places": ([{"city": "Remote"}] if fully_remote else []) + [
                {"country": {"code": code}, "provinceOnly": True,
                 "province": "masovian"} for code in codes
            ],
        },
    }


@pytest.mark.parametrize("code, expected", [
    ("POL", "PL"), ("ARE", "AE"), ("DEU", "DE"), ("USA", "US"),
    ("SAU", "SA"), ("DNK", "DK"), ("HRV", "HR"), ("ROU", "RO"),
    ("ESP", "ES"), ("CHE", "CH"), ("FRA", "FR"), ("AUT", "AT"),
    ("NLD", "NL"), ("BEL", "BE"), ("FIN", "FI"), ("HUN", "HU"),
    ("GRC", "GR"), ("SRB", "RS"), ("NOR", "NO"), ("UKR", "UA"),
    ("SVK", "SK"), ("pl", "PL"), ("ua", "UA"), (" de ", "DE"),
    (" pol ", "PL"), ("US", "US"),
])
def test_observed_country_codes_normalize_without_a_city(code, expected):
    job = parse_posting(province_posting(code))
    assert job.country == expected
    assert job.remote is True


@pytest.mark.parametrize("code", [None, "", "XXX", "PLO", "PL1", "Poland", {}, 123])
def test_unknown_country_codes_are_not_guessed(code):
    job = parse_posting(province_posting(code))
    assert job.country is None
    assert job.location == "Remote"
    assert passes_location(job, POLAND_FILTERS)[0] is False


@pytest.mark.parametrize("location", [None, {}, "Remote", {"places": None},
                                      {"places": [None, {}, {"country": "POL"}]}])
def test_missing_or_malformed_location_stays_unknown(location):
    posting = province_posting()
    posting["location"] = location
    job = parse_posting(posting)
    assert job.country is None
    assert job.remote is None
    assert passes_location(job, POLAND_FILTERS)[0] is False


@pytest.mark.parametrize("code, expected, city", [
    ("DEU", "DE", "Berlin"), ("USA", "US", "New York"),
    ("ARE", "AE", "Dubai"), ("SAU", "SA", "Riyadh"),
    ("HUN", "HU", "Budapest"), ("UKR", "UA", "Kyiv"),
    ("SRB", "RS", "Belgrade"),
])
@pytest.mark.parametrize("include_city", [False, True])
def test_explicit_foreign_remote_does_not_become_polish(code, expected, city, include_city):
    posting = province_posting(code)
    if include_city:
        posting["location"]["places"][-1]["city"] = city
    job = parse_posting(posting)
    job.description += " Our colleagues work in Poland and Berlin."
    assert job.country == expected
    assert passes_location(job, POLAND_FILTERS)[0] is False
    assert job.country == expected


@pytest.mark.parametrize("codes", [("DEU", "POL"), ("POL", "DEU"), ("USA", "POL")])
@pytest.mark.parametrize("fully_remote", [True, False])
def test_multiple_places_preserve_poland_without_inventing_remote(codes, fully_remote):
    job = parse_posting(province_posting(*codes, fully_remote=fully_remote))
    assert job.remote is (True if fully_remote else None)
    assert job.raw["location_countries"] == [
        {"DEU": "DE", "POL": "PL", "USA": "US"}[code] for code in codes
    ]
    assert job.location == ("Remote" if fully_remote else "")
    # The existing filter reads a single declared country. Preserve secondary
    # evidence for future consumers without changing historical tracker keys.
    assert passes_location(job, POLAND_FILTERS)[0] is (codes[0] == "POL")
    assert job.remote is fully_remote
    assert passes_location(job, {"filters": {"countries": ["ES"]}})[0] is False


@pytest.mark.parametrize("code", ["PL", "POL"])
@pytest.mark.parametrize("cities, fully_remote, expected", [
    (["Remote"], True, "Remote"),
    (["Warszawa"], False, "Warszawa"),
    (["Warszawa", "Kraków", "Warszawa"], False, "Warszawa, Kraków"),
    (["Warszawa", "Remote"], True, "Warszawa"),
])
def test_country_normalization_preserves_legacy_location_key_and_date(
    code, cities, fully_remote, expected,
):
    posting = province_posting(code, fully_remote=fully_remote)
    posting["location"]["places"] = [
        {"city": city, "country": {"code": code}} for city in cities
    ]
    job = parse_posting(posting)
    legacy = replace(job, location=expected, country="PL" if code == "PL" else None)
    assert job.location == expected
    assert job.key == legacy.key
    assert job.ats is None
    assert job.ats_job_id == posting["id"]
    assert job.posted_at == NOW - timedelta(hours=24)


def test_requirement_tiles_keep_only_nonempty_deduplicated_requirements():
    posting = province_posting("POL")
    posting["tiles"] = {"values": [
        {"type": "category", "value": "data"},
        {"type": "requirement", "value": " Python "},
        {"type": "requirement", "value": "python"},
        {"type": "requirement", "value": "ML"},
        {"type": "requirement", "value": "Power BI"},
        {"type": "requirement", "value": "Tableau"},
        {"type": "promotional", "value": "Apply now"},
        {"type": "requirement", "value": " "},
        {"type": "requirement", "value": ["SQL"]},
        {"type": "requirement", "value": 42}, None, "SQL", {},
    ]}
    job = parse_posting(posting)
    assert job.raw["requirements"] == ["Python", "ML", "Power BI", "Tableau"]
    assert "Requirements: Python, ML, Power BI, Tableau." in job.description
    assert "Apply now" not in job.description
    assert job.raw["snippet_only"] is True


@pytest.mark.parametrize("tiles", [None, [], "Python", {}, {"values": "Python"},
                                   {"values": {"type": "requirement", "value": "SQL"}}])
def test_malformed_tiles_do_not_invent_requirements(tiles):
    posting = province_posting("POL")
    posting["tiles"] = tiles
    job = parse_posting(posting)
    assert job.raw["requirements"] == []
    assert "Requirements:" not in job.description
    assert job.raw["snippet_only"] is True


@pytest.mark.parametrize("period, suffix", [
    ("Hour", "/hour"), ("Month", "/month"), (" hour ", "/hour"),
    ("Fortnight", ""), (None, ""), ("", ""), ({"unit": "Hour"}, ""),
])
def test_salary_preserves_explicit_period_and_contract_without_conversion(period, suffix):
    posting = province_posting("POL")
    posting["salary"] = {"from": 120, "to": 150, "currency": "PLN",
                         "period": period, "type": "b2b"}
    job = parse_posting(posting)
    assert job.salary == f"120–150 PLN{suffix}"
    assert job.raw["salary_period"] == (period if isinstance(period, str) else None)
    assert job.raw["salary_contract_type"] == "b2b"


@pytest.mark.parametrize("salary", [None, [], "120 PLN", {},
                                    {"period": "Hour", "type": {"name": "b2b"}}])
def test_malformed_or_amountless_salary_does_not_invent_pay(salary):
    posting = province_posting("POL")
    posting["salary"] = salary
    job = parse_posting(posting)
    assert job.salary is None
    assert job.raw["salary_contract_type"] is None


def test_fresh_polish_remote_survives_dedupe_and_strict_72_hour_filters():
    fresh = province_posting("POL", "POL")
    duplicate = {**fresh, "id": "data-scientist-DCG-masovian",
                 "url": "data-scientist-DCG-masovian"}
    stale = province_posting("POL", age_hours=72.001, company="Stale")
    stale["renewed"] = int(NOW.timestamp() * 1000)  # renewal is not publication
    postings = [fresh, duplicate, stale,
                province_posting("POL", age_hours=72, company="Boundary"),
                province_posting("POL", age_hours=None, company="Undated"),
                province_posting("USA", company="Foreign"),
                province_posting(company="Unknown")]
    errors = []
    jobs = fetch(None, session=nf_session({"postings": postings}), errors=errors)
    assert not errors
    assert len(jobs) == 7
    unique = dedupe(jobs)
    assert len(unique) == 6
    result = apply_filters(unique, POLAND_FILTERS, now=NOW)
    assert {j.company for j in result.kept} == {"DCG", "Boundary"}
    assert all(j.country == "PL" and j.remote is True for j in result.kept)
    assert all(j.raw["snippet_only"] for j in result.kept)
    reasons = {j.company: reason for j, reason in result.rejected}
    assert set(reasons) == {"Stale", "Undated", "Foreign", "Unknown"}
    assert "European hint" in reasons["Unknown"]
    assert result.counts["location_outside_eu"] == 2


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
