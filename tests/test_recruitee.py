"""Tests for the Recruitee pull in src/sources/ats_boards.py.

Recruitee is the Dutch-founded ATS — the default for NL/BE mid-size
companies — and, like Personio, it is addressed by *subdomain*, so the
generic slug rule ("drop the host, keep the first path segment") would turn
a pasted careers URL into the slug `o`. It is also the one board here whose
public payload carries a structured salary, which stage 2 and the digest
want kept.

Driven by `tests/fixtures/recruitee_offers.json`. The fixture is
**spec-derived, not recorded**: it was written from the documented
`/api/offers/` shape while this environment had no network route to
recruitee.com. Field names it assumes are pinned against the live API by the
`network`-marked tests in `test_live_contract.py` — run `pytest -m network`
once the network allows, and re-record the fixture if those fail.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sources.ats_boards import (
    _recruitee_slug,
    check_slug,
    fetch_recruitee,
)
from tests.conftest import (
    FakeSession,
    json_response,
    load_json_fixture,
)

UTC = timezone.utc
PAYLOAD = load_json_fixture("recruitee_offers.json")


def rc_session(body=None, **kwargs):
    return FakeSession(
        [("recruitee.com", json_response(PAYLOAD if body is None else body))],
        **kwargs,
    )


def by_id(jobs):
    return {j.ats_job_id: j for j in jobs}


# ==========================================================================
# addressing: the slug is the subdomain
# ==========================================================================


def test_recruitee_requests_the_public_offers_endpoint():
    session = rc_session()
    fetch_recruitee("vandelay", session=session)
    assert session.calls[0]["url"] == "https://vandelay.recruitee.com/api/offers/"


@pytest.mark.parametrize("written", [
    "vandelay",
    "  Vandelay  ",
    "vandelay.recruitee.com",
    "https://vandelay.recruitee.com/",
    "https://vandelay.recruitee.com/o/data-scientist-marketplace",
    "https://vandelay.recruitee.com/api/offers/",
])
def test_recruitee_accepts_the_bare_tenant_or_a_pasted_url(written):
    """Recruitee is per-*subdomain*: `vandelay.recruitee.com/o/…` names the
    tenant in the host, and the generic first-path-segment rule would extract
    the literal `o`."""
    assert _recruitee_slug(written) == "vandelay"


def test_a_non_recruitee_value_falls_back_to_the_generic_rule():
    assert _recruitee_slug("https://boards.example.com/vandelay") == "vandelay"


# ==========================================================================
# parsing
# ==========================================================================


def test_parses_offers_and_skips_the_untitled_one():
    jobs = fetch_recruitee("vandelay", session=rc_session())
    assert len(jobs) == 3  # 1200004 has no title
    assert all(j.source == "recruitee" and j.ats == "recruitee" for j in jobs)


def test_the_full_offer_maps_every_field():
    job = by_id(fetch_recruitee("vandelay", session=rc_session()))["1200001"]
    assert job.title == "Data Scientist, Marketplace"
    assert job.company == "Vandelay"
    assert job.url == "https://vandelay.recruitee.com/o/data-scientist-marketplace"
    assert job.location == "Amsterdam, Netherlands"
    assert job.country == "NL"
    assert job.remote is False
    assert job.posted_at == datetime(2026, 8, 28, 9, 15, tzinfo=UTC)
    assert "Forecasting demand" in job.description
    # The requirements block is the scoring-relevant half of any ad.
    assert "2+ years with Python and SQL" in job.description
    assert job.raw["employment_type"] == "fulltime"
    assert job.raw["department"] == "Data"


def test_salary_object_is_formatted_into_the_schema():
    job = by_id(fetch_recruitee("vandelay", session=rc_session()))["1200001"]
    assert job.salary == "4200–5300 EUR/month"


def test_legacy_min_salary_fields_still_produce_a_salary():
    job = by_id(fetch_recruitee("vandelay", session=rc_session()))["1200002"]
    assert job.salary == "60000"


def test_an_offer_without_careers_url_builds_one_from_its_slug():
    job = by_id(fetch_recruitee("vandelay", session=rc_session()))["1200002"]
    assert job.url == "https://vandelay.recruitee.com/o/ml-engineer-remote"


def test_remote_true_with_no_city_reads_remote():
    job = by_id(fetch_recruitee("vandelay", session=rc_session()))["1200002"]
    assert job.remote is True
    assert job.location == "Remote"
    assert job.country is None


def test_internship_offers_carry_the_employment_type_the_filter_reads():
    """A neutrally-titled internship is only catchable through the structured
    field — `filters.employment_type_exclude` matches on `raw["employment_type"]`."""
    job = by_id(fetch_recruitee("vandelay", session=rc_session()))["1200003"]
    assert job.raw["employment_type"] == "internship"


def test_the_date_prefers_published_at_over_created_at():
    jobs = by_id(fetch_recruitee("vandelay", session=rc_session()))
    assert jobs["1200001"].posted_at == datetime(2026, 8, 28, 9, 15, tzinfo=UTC)
    # 1200002 has an empty published_at: created_at is the fallback.
    assert jobs["1200002"].posted_at == datetime(2026, 8, 29, 11, 30, tzinfo=UTC)


# ==========================================================================
# the envelope gate and --check
# ==========================================================================


def test_a_200_that_is_not_an_offers_payload_raises():
    """`{"error": …}` parsing as zero postings would read, every morning, as a
    company that is not hiring — the silent failure every board here refuses."""
    session = rc_session({"error": "tenant suspended"})
    with pytest.raises(Exception, match="not a recruitee board payload"):
        fetch_recruitee("vandelay", session=session)


def test_check_slug_passes_a_real_board():
    ok, message = check_slug("recruitee", "vandelay", session=rc_session())
    assert ok
    assert "3" in message


def test_check_slug_fails_a_missing_tenant():
    session = FakeSession()  # default: 404 on everything
    ok, message = check_slug("recruitee", "vandelay", session=session)
    assert not ok


def test_check_slug_treats_an_empty_board_as_ok():
    ok, message = check_slug("recruitee", "vandelay",
                             session=rc_session({"offers": []}))
    assert ok
    assert "0 postings" in message
