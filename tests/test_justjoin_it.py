"""Tests for src/sources/justjoin_it.py — Tier 2.

Just Join IT has no official API; the adapter reads the internal
`api.justjoin.it/v2/user-panel/offers` endpoint the site's own frontend
uses. That bargain fixes what these tests are for: the hard rule that
nothing ever crashes the run (HTTP error, reshaped payload, malformed
entries — all degrade), the client-side data/AI + junior/mid cut, and the
snippet-only description contract, since the listing carries no ad body.

Driven by `tests/fixtures/justjoin_offers.json`. The fixture is
**spec-derived, not recorded** — written from the known v2 payload shape
while this environment had no network route to justjoin.it — and the
category ids in it are treated as opaque on purpose: the adapter never
filters by them, precisely because they are an internal enumeration that has
been renumbered before. The `network`-marked tests in
`test_live_contract.py` are what pin the real field names; re-record the
fixture from a live page when they disagree.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.sources.justjoin_it import (
    API_URL,
    DS_RE,
    EXPERIENCE_LEVELS,
    MAX_PAGES,
    PER_PAGE,
    fetch,
    parse_offer,
)
from src.util import HttpError
from tests.conftest import FakeSession, json_response, load_json_fixture

UTC = timezone.utc
PAYLOAD = load_json_fixture("justjoin_offers.json")


def jj_session(body=None, **kwargs):
    return FakeSession(
        [("api.justjoin.it", json_response(PAYLOAD if body is None else body))],
        **kwargs,
    )


def by_company(jobs):
    return {j.company: j for j in jobs}


# ==========================================================================
# the client-side cut: data/AI titles, junior+mid only
# ==========================================================================


def test_only_junior_mid_ds_offers_leave_the_adapter():
    jobs = fetch(None, session=jj_session())
    companies = {j.company for j in jobs}
    assert companies == {"Kramerica Labs", "Pendant Publishing"}
    # Frontend Developer: DS gate. Senior Data Scientist: experience gate.
    # The nameless fifth entry: parse_offer's floor.


def test_the_experience_param_is_sent_and_rechecked():
    """The request narrows to junior/mid, but an internal API is allowed to
    start ignoring its own query string — the per-offer re-check is what the
    spec's "junior+mid" actually rests on."""
    session = jj_session()
    fetch(None, session=session)
    assert session.calls[0]["params"]["experienceLevels[]"] == list(EXPERIENCE_LEVELS)
    # The fixture's senior offer came back anyway and was dropped client-side.
    assert "Monk's Cafe Tech" not in by_company(fetch(None, session=jj_session()))


def test_skills_count_toward_the_ds_gate():
    """A title like "Analyst" says nothing; `requiredSkills: ["Machine
    Learning"]` does. The gate reads both, so a DS job with a vague title
    survives to stage 2, where the real title rules decide."""
    offer = dict(PAYLOAD["data"][0], title="Analyst",
                 requiredSkills=["Machine Learning", "SQL"])
    assert parse_offer(offer) is not None  # parseable either way
    jobs = fetch(None, session=jj_session({"data": [offer], "meta": {}}))
    assert len(jobs) == 1


@pytest.mark.parametrize("title", ["HTML Developer", "Email Marketing", "Retail Ops"])
def test_the_gate_is_word_bounded(title):
    assert not DS_RE.search(title)


# ==========================================================================
# parsing
# ==========================================================================


def test_the_full_offer_maps_every_field():
    job = by_company(fetch(None, session=jj_session()))["Kramerica Labs"]
    assert job.title == "Data Scientist"
    assert job.url == (
        "https://justjoin.it/job-offer/kramerica-labs-data-scientist-warszawa-1a2b3c"
    )
    assert job.location == "Warszawa, Kraków"     # multilocation joined
    assert job.posted_at == datetime(2026, 8, 29, 6, 30, tzinfo=UTC)
    assert job.remote is None                     # hybrid is not fully remote
    assert job.raw["experience"] == "mid"
    assert job.raw["snippet_only"] is True
    assert "Python" in job.description and "Airflow" in job.description


def test_the_salary_is_the_widest_advertised_range():
    """b2b 16-22k beats permanent 13-18k — the board headlines the bigger
    number and so does the digest's one salary line."""
    job = by_company(fetch(None, session=jj_session()))["Kramerica Labs"]
    assert job.salary == "16000–22000 PLN/month"


def test_v1_style_skill_dicts_still_parse():
    job = by_company(fetch(None, session=jj_session()))["Pendant Publishing"]
    assert "PyTorch" in job.description
    assert job.salary == "12000 PLN/month"        # from == to collapses
    assert job.remote is True
    assert job.location == "Remote"


def test_country_is_left_to_geo():
    """The listing names cities, never countries — inventing `PL` here would
    be wrong the day the board lists a Berlin office. `geo.country_of`
    already resolves Polish cities."""
    jobs = fetch(None, session=jj_session())
    assert all(j.country is None for j in jobs)


# ==========================================================================
# pagination
# ==========================================================================


def test_a_short_page_stops_the_walk():
    session = jj_session()
    fetch(None, session=session)
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["page"] == 1
    assert session.calls[0]["params"]["perPage"] == PER_PAGE


def test_full_pages_advance_up_to_the_budget():
    full = {"data": [dict(PAYLOAD["data"][0], slug=f"offer-{n}") for n in range(PER_PAGE)],
            "meta": {}}
    session = FakeSession([("api.justjoin.it", json_response(full))])
    fetch(None, session=session)
    assert [c["params"]["page"] for c in session.calls] == list(range(1, MAX_PAGES + 1))


# ==========================================================================
# the hard rule: never crash the run
# ==========================================================================


def test_http_failure_degrades_instead_of_raising():
    errors: list[str] = []
    jobs = fetch(None, session=FakeSession([("api.justjoin.it", HttpError("boom"))]),
                 errors=errors)
    assert jobs == []
    assert "justjoin_it" in errors[0]


def test_a_reshaped_payload_degrades_with_a_message_naming_the_drift():
    errors: list[str] = []
    jobs = fetch(None, session=jj_session({"offers": []}), errors=errors)
    assert jobs == []
    assert "changed shape" in errors[0]


def test_one_malformed_entry_does_not_kill_the_page():
    body = json.loads(json.dumps(PAYLOAD))
    body["data"].insert(0, 42)
    assert len(fetch(None, session=jj_session(body))) == 2
