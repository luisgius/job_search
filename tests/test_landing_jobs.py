"""Tests for src/sources/landing_jobs.py.

Landing.jobs is the second global feed: keyless, offset/limit paginated,
Lisbon/Porto-anchored with a remote-EU tail. It is also the one source with
a *client-side* DS/ML title gate — the feed is all of tech, and hundreds of
sales and frontend postings must not ride through dedupe every morning. The
gate's contract matters more than its breadth: word-bounded (so "ML" cannot
hide inside "HTML", nor "AI" inside "Retail"), broader than any sane
`filters.title_include`, and stage 2 stays the only real decider.

Driven by `tests/fixtures/landing_jobs_page.json`. The fixture is
**spec-derived, not recorded** (documented v1 endpoint,
`landing.jobs/api/v1/jobs.json`): it was written while this environment had
no network route to landing.jobs. Its assumptions are pinned against the
live API by the `network`-marked tests in `test_live_contract.py` — run
`pytest -m network` once the network allows.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sources.landing_jobs import (
    API_URL,
    DS_TITLE_RE,
    MAX_PAGES,
    PAGE_LIMIT,
    fetch,
)
from src.util import HttpError
from tests.conftest import (
    FakeResponse,
    FakeSession,
    json_response,
    load_json_fixture,
)

UTC = timezone.utc
PAYLOAD = load_json_fixture("landing_jobs_page.json")


#: What the companies endpoint knows — the listing itself names employers
#: only by id (live shape, 2026-09-01).
COMPANIES = {4401: "Del Boca Vista Analytics", 4402: "Vandelay Industries"}


def lj_session(body=None, companies=None, **kwargs):
    table = COMPANIES if companies is None else companies
    def company_route(url, params):
        cid = url.rsplit("/", 1)[-1].removesuffix(".json")
        name = table.get(int(cid)) if cid.isdigit() else None
        if name is None:
            return FakeResponse(status_code=404)
        return json_response({"id": int(cid), "name": name})
    return FakeSession(
        [
            # Ordered: the companies URL also contains "landing.jobs".
            ("/api/v1/companies/", company_route),
            ("landing.jobs", json_response(PAYLOAD if body is None else body)),
        ],
        **kwargs,
    )


def by_title(jobs):
    return {j.title: j for j in jobs}


# ==========================================================================
# the client-side DS/ML gate
# ==========================================================================


def test_non_ds_titles_never_leave_the_adapter():
    jobs = fetch(None, session=lj_session())
    titles = {j.title for j in jobs}
    assert "Account Executive" not in titles
    assert titles == {"Data Scientist", "Machine Learning Engineer"}


@pytest.mark.parametrize("title", [
    "Data Scientist", "Senior Machine Learning Engineer", "AI Engineer",
    "ML Engineer", "Applied Scientist", "Decision Scientist",
    "Analytics Engineer", "NLP Engineer", "Data Analyst",
])
def test_the_gate_keeps_plausible_ds_titles(title):
    assert DS_TITLE_RE.search(title)


@pytest.mark.parametrize("title", [
    "Account Executive", "Retail Manager", "Email Marketing Specialist",
    "HTML Developer", "Mail Carrier", "Sales Development Representative",
])
def test_the_gate_is_word_bounded(title):
    """"AI" inside "Retail" and "ML" inside "HTML"/"Email" are the classic
    substring traps; a gate that matched them would stop being coarse and
    start being wrong."""
    assert not DS_TITLE_RE.search(title)


# ==========================================================================
# parsing
# ==========================================================================


def test_the_full_listing_maps_every_field():
    """The employer's name is no longer in the listing — it arrives through
    the companies endpoint, resolved by company_id (live shape 2026-09-01)."""
    job = by_title(fetch(None, session=lj_session()))["Data Scientist"]
    assert job.company == "Del Boca Vista Analytics"
    assert job.url == "https://landing.jobs/jobs/88231"
    assert job.location == "Lisbon"
    assert job.country == "PT"
    assert job.remote is True
    assert job.posted_at == datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    assert job.salary == "35000–48000 EUR"
    assert job.ats_job_id == "88231"
    # Requirements are the scoring-relevant half; they must survive joining.
    assert "experimentation" in job.description
    assert "Nice to have" in job.description
    assert "<p>" not in job.description


def test_the_company_object_shape_is_read_too():
    """The API has served the employer both as `company_name` and as a
    `company: {name}` object; both must resolve."""
    job = by_title(fetch(None, session=lj_session()))["Machine Learning Engineer"]
    assert job.company == "Monk's Cafe Tech"
    assert job.url == "https://landing.jobs/jobs/88233"  # share_url fallback
    assert job.posted_at == datetime(2026, 8, 30, 8, 30, tzinfo=UTC)  # created_at
    assert job.remote is True
    assert job.location == "Remote"  # remote with no city


def test_a_listing_without_company_or_url_is_skipped():
    jobs = fetch(None, session=lj_session())
    assert "AI Engineer" not in {j.title for j in jobs}  # 88234: no company_id


def test_company_lookups_run_after_the_gate_and_are_cached():
    """Resolution is the expensive half now, so it must be (a) skipped for
    postings the DS gate already dropped — the Account Executive's employer
    is never asked about — and (b) cached per id."""
    session = lj_session()
    fetch(None, session=session)
    lookups = [u for u in session.urls() if "/api/v1/companies/" in u]
    assert lookups == ["https://landing.jobs/api/v1/companies/4401.json"]


def test_a_failed_company_lookup_skips_the_job_never_invents():
    jobs = fetch(None, session=lj_session(companies={}))
    assert "Data Scientist" not in {j.title for j in jobs}
    # The inline-company posting is untouched by the lookup path.
    assert "Machine Learning Engineer" in {j.title for j in jobs}


def test_a_jobs_envelope_is_accepted_alongside_the_bare_list():
    jobs = fetch(None, session=lj_session({"jobs": list(PAYLOAD)}))
    assert len(jobs) == 2


# ==========================================================================
# pagination
# ==========================================================================


def _listing_calls(session):
    return [c for c in session.calls if "jobs.json" in c["url"]]


def test_offset_and_limit_are_sent_and_a_short_page_stops():
    session = lj_session()
    fetch(None, session=session)
    listing = _listing_calls(session)
    assert len(listing) == 1  # 4 < PAGE_LIMIT: no second listing request
    assert listing[0]["params"] == {"offset": 0, "limit": PAGE_LIMIT}


def test_full_pages_advance_the_offset_up_to_the_budget():
    full_page = [dict(PAYLOAD[0], id=n) for n in range(PAGE_LIMIT)]
    session = lj_session(full_page)
    fetch(None, session=session)
    assert [c["params"]["offset"] for c in _listing_calls(session)] == [
        n * PAGE_LIMIT for n in range(MAX_PAGES)
    ]
    # One employer, one lookup — the cache holds across every page.
    assert len([u for u in session.urls() if "/api/v1/companies/" in u]) == 1


# ==========================================================================
# the hard rule: never crash the run
# ==========================================================================


def test_http_failure_degrades_instead_of_raising():
    errors: list[str] = []
    jobs = fetch(None, session=FakeSession([("landing.jobs", HttpError("boom"))]),
                 errors=errors)
    assert jobs == []
    assert "landing_jobs" in errors[0]


def test_a_reshaped_payload_degrades_with_a_message_naming_the_drift():
    errors: list[str] = []
    jobs = fetch(None, session=lj_session({"error": "maintenance"}), errors=errors)
    assert jobs == []
    assert "changed shape" in errors[0]


def test_one_malformed_entry_does_not_kill_the_page():
    body = ["not-a-mapping"] + list(PAYLOAD)
    jobs = fetch(None, session=lj_session(body))
    assert len(jobs) == 2
