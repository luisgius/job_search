"""Tests for src/sources/landing_jobs.py.

Landing.jobs is the second global feed: keyless, offset/limit paginated,
Lisbon/Porto-anchored with a remote-EU tail. It is also the one source with
a *client-side* DS/ML title gate — the feed is all of tech, and hundreds of
sales and frontend postings must not ride through dedupe every morning. The
gate's contract matters more than its breadth: word-bounded (so "ML" cannot
hide inside "HTML", nor "AI" inside "Retail"), broader than any sane
`filters.title_include`, and stage 2 stays the only real decider.

The listing carries NO employer field in any spelling (schema recorded live
2026-09-01), so resolution goes through the per-job detail endpoint — one
extra request per posting the gate keeps — and from there, when the detail
names the employer only by id, through the companies endpoint. That chain
is the expensive and the fragile half of this adapter, and most of this
file is about its contract: gated-out postings are never detailed, lookups
are cached and bounded, and a posting whose chain ends nameless is skipped
— this pipeline never invents an employer.

Driven by `tests/fixtures/landing_jobs_page.json`, whose *key set* is the
one recorded from the live API on 2026-09-01 (values synthetic). The live
`network`-marked tests in `test_live_contract.py` pin the schema and the
detail endpoint against reality — run `pytest -m network` for those.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sources.landing_jobs import (
    API_URL,
    DETAIL_MAX_REQUESTS,
    DS_TITLE_RE,
    MAX_PAGES,
    PAGE_LIMIT,
    fetch,
    parse_job,
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


#: What the per-job detail endpoint knows. 88231 names its employer by id
#: (the companies endpoint finishes the chain), 88233 inline; 88234 is
#: deliberately absent — its lookup 404s and the job must be skipped. The
#: Account Executive's detail exists but must never be requested.
DETAILS = {
    88231: {"id": 88231, "company_id": 4401},
    88232: {"id": 88232, "company_id": 4402},
    88233: {"id": 88233, "company_name": "Monk's Cafe Tech"},
}

#: What the companies endpoint knows.
COMPANIES = {4401: "Del Boca Vista Analytics", 4402: "Vandelay Industries"}


def lj_session(body=None, companies=None, details=None, **kwargs):
    ctable = COMPANIES if companies is None else companies
    dtable = DETAILS if details is None else details

    def _by_id(url, table):
        tail = url.rsplit("/", 1)[-1].removesuffix(".json")
        return table.get(int(tail)) if tail.isdigit() else None

    def company_route(url, params):
        name = _by_id(url, ctable)
        if name is None:
            return FakeResponse(status_code=404)
        return json_response({"id": int(url.rsplit("/", 1)[-1].removesuffix(".json")),
                              "name": name})

    def detail_route(url, params):
        detail = _by_id(url, dtable)
        if detail is None:
            return FakeResponse(status_code=404)
        return json_response(detail)

    return FakeSession(
        [
            # Ordered: every URL here also contains "landing.jobs", and the
            # listing URL ends in "jobs.json" (no slash), so neither prefix
            # below can swallow it.
            ("/api/v1/companies/", company_route),
            ("/api/v1/jobs/", detail_route),
            ("landing.jobs", json_response(PAYLOAD if body is None else body)),
        ],
        **kwargs,
    )


def by_title(jobs):
    return {j.title: j for j in jobs}


def detail_calls(session):
    return [u for u in session.urls() if "/api/v1/jobs/" in u]


def company_calls(session):
    return [u for u in session.urls() if "/api/v1/companies/" in u]


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
    """The live listing names no employer at all — 88231's arrives through
    the detail endpoint, which names it by id, which the companies endpoint
    resolves. The rest maps straight off the recorded schema."""
    job = by_title(fetch(None, session=lj_session()))["Data Scientist"]
    assert job.company == "Del Boca Vista Analytics"
    assert job.url == "https://landing.jobs/jobs/88231"
    assert job.location == "Lisbon"
    assert job.country == "PT"
    assert job.remote is True
    assert job.posted_at == datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    assert job.salary == "35000–48000 EUR"
    assert job.ats_job_id == "88231"
    assert job.raw["relocation_paid"] is True
    assert job.raw["tags"] == ["python", "sql"]
    # Requirements are the scoring-relevant half; they must survive joining.
    assert "experimentation" in job.description
    assert "Nice to have" in job.description
    assert "<p>" not in job.description


def test_a_detail_that_names_the_employer_inline_resolves_without_the_companies_endpoint():
    job = by_title(fetch(None, session=lj_session()))["Machine Learning Engineer"]
    assert job.company == "Monk's Cafe Tech"
    assert job.url == "https://landing.jobs/jobs/88233"  # derived from id
    assert job.posted_at == datetime(2026, 8, 30, 8, 30, tzinfo=UTC)  # created_at
    assert job.remote is True
    assert job.location == "Remote"  # empty locations list + remote
    assert job.salary is None


def test_a_posting_whose_detail_names_nobody_is_skipped():
    jobs = fetch(None, session=lj_session())
    assert "AI Engineer" not in {j.title for j in jobs}  # 88234: detail 404s


def test_a_company_slug_in_the_detail_is_the_last_resort():
    """Real data, worse typography — better than losing the posting, and the
    honest floor before the skip."""
    details = {88231: {"id": 88231, "company_slug": "kruger-industrial-smoothing"}}
    jobs = fetch(None, session=lj_session(details=details))
    assert by_title(jobs)["Data Scientist"].company == "Kruger Industrial Smoothing"


def test_detail_lookups_run_after_the_gate():
    """Resolution is the expensive half now — one request per posting — so
    the Account Executive's detail must never be asked for, and an id the
    detail names is resolved through the (cached) companies endpoint."""
    session = lj_session()
    fetch(None, session=session)
    assert detail_calls(session) == [
        "https://landing.jobs/api/v1/jobs/88231.json",
        "https://landing.jobs/api/v1/jobs/88233.json",
        "https://landing.jobs/api/v1/jobs/88234.json",
    ]
    assert company_calls(session) == [
        "https://landing.jobs/api/v1/companies/4401.json",
    ]


def test_a_failed_company_lookup_skips_the_job_never_invents():
    jobs = fetch(None, session=lj_session(companies={}))
    assert "Data Scientist" not in {j.title for j in jobs}
    # The inline-named posting is untouched by the companies path.
    assert "Machine Learning Engineer" in {j.title for j in jobs}


def test_a_jobs_envelope_is_accepted_alongside_the_bare_list():
    jobs = fetch(None, session=lj_session({"jobs": list(PAYLOAD)}))
    assert len(jobs) == 2


def test_the_pre_2026_listing_spellings_still_parse():
    """The API has already reshaped once; the old flat spellings stay
    readable so a rollback on their side costs nothing on this side."""
    job = parse_job({
        "id": 7,
        "title": "Data Scientist",
        "company_name": "Kruger Industrial Smoothing",
        "share_url": "https://landing.jobs/jobs/7",
        "city": "Porto",
        "country_code": "PT",
        "salary_low": 30000,
        "salary_high": 40000,
        "currency": "EUR",
        "published_at": "2026-08-29T10:00:00Z",
    })
    assert job.company == "Kruger Industrial Smoothing"
    assert job.url == "https://landing.jobs/jobs/7"
    assert job.location == "Porto"
    assert job.country == "PT"
    assert job.salary == "30000–40000 EUR"


def test_the_company_object_shape_is_read_too():
    """The API has served the employer as `company_name` and as a
    `company: {name}` object; both must resolve, listing- or detail-side."""
    job = parse_job({
        "id": 8,
        "title": "Data Scientist",
        "company": {"name": "Monk's Cafe Tech"},
        "url": "https://landing.jobs/jobs/8",
    })
    assert job.company == "Monk's Cafe Tech"


def test_a_digit_string_id_still_derives_url_and_key():
    """JSON APIs flip integer ids to strings without warning, and the digit
    string names the same job page. A slug id derives nothing — a URL is
    never invented from a spelling that was not seen working."""
    job = parse_job({"id": "31337", "title": "Data Scientist",
                     "company_name": "Kramerica Industries"})
    assert job.url == "https://landing.jobs/jobs/31337"
    assert job.ats_job_id == "31337"
    assert parse_job({"id": "head-of-data", "title": "Data Scientist",
                      "company_name": "Kramerica Industries"}) is None


def test_zeroed_salary_bounds_mean_not_published():
    base = {"id": 9, "title": "Data Scientist", "company_name": "X",
            "url": "https://landing.jobs/jobs/9"}
    assert parse_job({**base, "gross_salary_low": 0,
                      "gross_salary_high": 0, "currency_code": "EUR"}).salary is None
    assert parse_job({**base, "gross_salary_low": 0, "gross_salary_high": 48000,
                      "currency_code": "EUR"}).salary == "48000 EUR"


def test_a_seven_figure_salary_stays_decimal():
    """Bare %g turns 1200000 into '1.2e+06' — a CZK or HUF range crosses
    seven digits without anything being wrong with the data."""
    job = parse_job({"id": 9, "title": "Data Scientist", "company_name": "X",
                     "url": "https://landing.jobs/jobs/9",
                     "gross_salary_low": 1200000, "gross_salary_high": 1800000,
                     "currency_code": "CZK"})
    assert job.salary == "1200000–1800000 CZK"


# ==========================================================================
# pagination and the request budget
# ==========================================================================


def _listing_calls(session):
    return [c for c in session.calls if "jobs.json" in c["url"]]


def test_offset_and_limit_are_sent_and_a_short_page_stops():
    session = lj_session()
    fetch(None, session=session)
    listing = _listing_calls(session)
    assert len(listing) == 1  # 4 < PAGE_LIMIT: no second listing request
    assert listing[0]["params"] == {"offset": 0, "limit": PAGE_LIMIT}


def test_full_pages_advance_the_offset_and_the_detail_budget_holds():
    """Three full pages of DS titles would want 300 detail lookups; the
    budget stops at `DETAIL_MAX_REQUESTS`, postings past it are skipped
    (never shipped employer-less), and one employer id shared across every
    detail costs the companies endpoint exactly one request."""
    full_page = [dict(PAYLOAD[0], id=n) for n in range(PAGE_LIMIT)]
    details = {n: {"id": n, "company_id": 4401} for n in range(PAGE_LIMIT)}
    session = lj_session(full_page, details=details)
    jobs = fetch(None, session=session)
    assert [c["params"]["offset"] for c in _listing_calls(session)] == [
        n * PAGE_LIMIT for n in range(MAX_PAGES)
    ]
    assert len(detail_calls(session)) == DETAIL_MAX_REQUESTS
    assert len(jobs) == DETAIL_MAX_REQUESTS
    assert len(company_calls(session)) == 1  # the cache holds across pages


def test_a_titleless_entry_cannot_spend_the_detail_budget():
    """parse_job rejects it whatever the detail would say, so a listing that
    drops titles must not be able to drain `DETAIL_MAX_REQUESTS` for it."""
    body = [{"id": 99, "remote": True}] + list(PAYLOAD)
    session = lj_session(body)
    jobs = fetch(None, session=session)
    assert "https://landing.jobs/api/v1/jobs/99.json" not in detail_calls(session)
    assert len(jobs) == 2  # and the rest of the page is untouched


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


def test_a_failed_detail_lookup_never_raises():
    """The detail endpoint erroring (not just 404ing) costs that posting
    only — the page, and the run, go on. One resolution-needing entry, not
    the whole fixture: each raising lookup pays http_get's real-time retry
    backoff, so this is the one place fixture size is wall-clock time."""
    body = [PAYLOAD[0],
            {"id": 1, "title": "ML Engineer", "company_name": "Vandelay",
             "url": "https://landing.jobs/jobs/1"}]
    session = FakeSession([
        ("/api/v1/jobs/", HttpError("detail down")),
        ("landing.jobs", json_response(body)),
    ])
    jobs = fetch(None, session=session)
    # The dead chain skipped its posting; the rest of the page still parsed.
    assert [j.title for j in jobs] == ["ML Engineer"]


def test_one_malformed_entry_does_not_kill_the_page():
    body = ["not-a-mapping"] + list(PAYLOAD)
    jobs = fetch(None, session=lj_session(body))
    assert len(jobs) == 2
