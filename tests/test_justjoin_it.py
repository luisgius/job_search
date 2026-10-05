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

import io
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from src.filters import apply_filters, dedupe
from src.sources.justjoin_it import (
    API_URL,
    DS_RE,
    EXPERIENCE_LEVELS,
    LISTING_CATEGORIES,
    LISTING_MAX_PAGES,
    LISTING_MAX_REQUESTS,
    LISTING_PAGE_SIZE,
    MAX_BODY_BYTES,
    MAX_PAGES,
    PER_PAGE,
    fetch,
    parse_listing_page,
    parse_offer,
)
from src.util import HttpError
from tests.conftest import (
    FakeResponse,
    FakeSession,
    html_response,
    json_response,
    load_fixture,
    load_json_fixture,
)

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
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert [c["params"]["page"] for c in session.calls] == list(range(1, MAX_PAGES + 1))
    # A full last page proves nothing about what lies beyond it.
    assert errors == [f"justjoin_it: page cap reached ({MAX_PAGES} full pages of {PER_PAGE}); "
                      "more offers may exist, coverage partial"]
    assert {j.raw["coverage"] for j in jobs} == {"bounded_api"}


def test_a_short_last_page_is_not_reported_as_capped_and_still_claims_no_completeness():
    errors: list[str] = []
    jobs = fetch(None, session=jj_session(), errors=errors)
    assert errors == [] and {j.raw["coverage"] for j in jobs} == {"bounded_api"}


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


# ==========================================================================
# what 2026-10-05 showed: the refused API and the public listing pages
# ==========================================================================
#
# Unlike `justjoin_offers.json`, these fixtures are recorded. The API one is
# the 503 body as served. The two listing ones are reduced captures of
# https://justjoin.it/job-offers/all-locations/{data,ai}: the Next.js flight
# row carrying the offers and the pagination row are verbatim, a handful of
# the 50 embedded offers are kept with every field, the rest of the page is
# gone. `listing_html()` below builds further pages to that observed envelope
# from those recorded offers; such pages are synthetic and say so.

LIVE_NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
API_503 = load_fixture("justjoin/live_2026-10-05_api_503.html")
DATA_PAGE = load_fixture("justjoin/live_2026-10-05_listing_data.html")
AI_PAGE = load_fixture("justjoin/live_2026-10-05_listing_ai.html")
DATA_OFFERS = parse_listing_page(DATA_PAGE)[0]


def listing_html(offers, last_page=None):
    """A synthetic listing page in the recorded envelope."""
    rows = '1:"$Sreact.fragment"\n88:["$","$L8b",null,' + json.dumps({"offers": offers}, separators=(",", ":")) + "]\n"
    if last_page:
        rows += '8a:["$","$L8d","%d",{"href":"/job-offers/all-locations/data?experience-levels=junior,mid&page=%d"}]\n' % (last_page, last_page)
    return ("<html><body><script>(self.__next_f=self.__next_f||[]).push([0])</script>"
            "<script>self.__next_f.push([1," + json.dumps(rows) + "])</script></body></html>")


def full_page(tag, last_page=None):
    template = DATA_OFFERS[8]  # the recorded junior/mid Data Engineer offer
    return listing_html(
        [dict(template, slug=f"{tag}-{n}") for n in range(LISTING_PAGE_SIZE)], last_page)


def refused_session(listing):
    """The API host as it answered on 2026-10-05; `listing(category, page)`
    supplies the public pages."""
    def page(url, params):
        found = listing(url.rsplit("/", 1)[1], (params or {}).get("page", 1))
        return html_response(found) if isinstance(found, str) else found
    return FakeSession([
        ("api.justjoin.it", FakeResponse(status_code=503, text=API_503)),
        ("justjoin.it/job-offers/all-locations/", page),
    ])


def recorded(category, page):
    return {"data": DATA_PAGE, "ai": AI_PAGE}[category]


def by_slug_end(jobs, ending):
    return next(j for j in jobs if j.ats_job_id.endswith(ending))


def test_the_recorded_503_is_an_origin_error_page_not_an_empty_board():
    """The body is nginx's stock page relayed by the CDN — no challenge, no
    JSON. With the listing pages failing too, the source reports each failure
    and returns nothing; it never reports an empty board."""
    assert "503 Service Temporarily Unavailable" in API_503 and "nginx" in API_503
    session = refused_session(lambda category, page: FakeResponse(status_code=404))
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert errors[0] == "justjoin_it: page 1: https://api.justjoin.it/v2/user-panel/offers -> HTTP 503"
    assert ["listing page data/1" in errors[1], "listing page ai/1" in errors[2]] == [True, True]
    assert len(errors) == 3 and len(session.calls) == 3


def test_a_refused_api_reads_the_bounded_public_listing_pages():
    session = refused_session(recorded)
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert [(c["url"], c["params"]) for c in session.calls[1:]] == [
        ("https://justjoin.it/job-offers/all-locations/data", {"experience-levels": "junior,mid"}),
        ("https://justjoin.it/job-offers/all-locations/ai", {"experience-levels": "junior,mid"}),
    ]
    # Plain pipeline identity on the public pages; browser shape only on the API.
    assert "Origin" in session.calls[0]["headers"]
    assert all("Origin" not in c["headers"] for c in session.calls[1:])
    # 13 recorded offers: "Sales Operations & CRM Specialist" and the Polish
    # "Analityk Danych (SQL, BI)" fail the unchanged title/skills gate.
    assert len(jobs) == 11
    assert all(j.raw["surface"] == "listing_page" and j.raw["snippet_only"] for j in jobs)
    assert all(j.raw["coverage"] == "partial" for j in jobs)
    assert all(j.url == f"https://justjoin.it/job-offer/{j.ats_job_id}" for j in jobs)
    assert "HTTP 503" in errors[0]
    assert errors[1] == (
        "justjoin_it: API unavailable; read 13 offers from the public listing "
        "pages of data, ai only — partial coverage, not the board"
    )
    assert len(errors) == 2


def test_a_healthy_api_never_touches_the_listing_pages():
    session = jj_session()
    jobs = fetch(None, session=session)
    assert {c["url"] for c in session.calls} == {API_URL}
    assert all(j.raw["surface"] == "api" for j in jobs)


def test_a_later_api_page_failing_keeps_what_was_read_and_does_not_fall_back():
    full = {"data": [dict(PAYLOAD["data"][0], slug=f"offer-{n}") for n in range(PER_PAGE)]}
    session = FakeSession([("api.justjoin.it", lambda url, params: (
        json_response(full) if params["page"] == 1 else FakeResponse(status_code=404)))])
    errors: list[str] = []
    assert len(fetch(None, session=session, errors=errors)) == PER_PAGE
    assert len(errors) == 1 and "page 2" in errors[0]
    assert errors[0].endswith("earlier pages kept, coverage partial")
    assert {c["url"] for c in session.calls} == {API_URL}


# --------------------------------------------------------------------------
# two publication fields, neither of them an update time
# --------------------------------------------------------------------------


def source_update(job):
    """What the digest prints as the source update (src/digest.py)."""
    return str(job.raw.get("updated_at") or job.raw.get("updatedAt") or "unknown")


def test_conflicting_publication_fields_keep_both_and_date_by_the_earlier():
    """Recorded: `publishedAt` 2026-10-05T08:00, `lastPublishedAt` 2026-08-31
    on the same offer. What either means is not established, so the earlier
    one dates the job, both stay as sent, and nothing is called an update."""
    jobs = fetch(None, session=refused_session(recorded))
    job = by_slug_end(jobs, "allegro-ai-hub-warszawa-data")
    assert job.raw["publishedAt"] == "2026-10-05T08:00:05.3000752Z"
    assert job.raw["lastPublishedAt"] == "2026-08-31T07:26:08.34276Z"
    assert job.posted_at == datetime(2026, 8, 31, 7, 26, 8, 342760, tzinfo=UTC)
    assert job.raw["posted_at_source"] == "lastPublishedAt"
    assert job.raw["source_dates"] == {"lastPublishedAt": "2026-08-31T07:26:08.342760+00:00",
                                       "publishedAt": "2026-10-05T08:00:05.300075+00:00"}
    assert job.raw["date_conflict"] is True
    assert source_update(job) == "unknown"
    assert job.age_hours_at(LIVE_NOW) > 72


def test_agreeing_publication_fields_are_not_a_conflict():
    job = by_slug_end(fetch(None, session=refused_session(recorded)), "sollers-consulting-data-analyst-warszawa-data")
    assert job.posted_at == datetime(2026, 10, 5, 7, 33, 6, 668650, tzinfo=UTC)
    assert job.raw["date_conflict"] is False and source_update(job) == "unknown"


def test_no_recorded_offer_claims_a_source_update():
    jobs = fetch(None, session=refused_session(recorded))
    assert {source_update(j) for j in jobs} == {"unknown"}
    assert sum(j.raw["date_conflict"] for j in jobs) == 8


@pytest.mark.parametrize("published,last,expected,source", [
    ("2026-10-05T08:00:00Z", None, datetime(2026, 10, 5, 8, tzinfo=UTC), "publishedAt"),
    (None, "2026-08-31T07:00:00Z", datetime(2026, 8, 31, 7, tzinfo=UTC), "lastPublishedAt"),
    ("garbage", "2026-08-31T07:00:00Z", datetime(2026, 8, 31, 7, tzinfo=UTC), "lastPublishedAt"),
    ("2026-10-05T10:00:00+02:00", "2026-10-05T09:00:00Z", datetime(2026, 10, 5, 8, tzinfo=UTC), "publishedAt"),
    ("2026-10-05", None, datetime(2026, 10, 5, tzinfo=UTC), "publishedAt"),
    (None, None, None, None),
    ("", "", None, None),
    (1790000000, 1790000000000, None, None),
])
def test_a_missing_date_is_never_filled_in(published, last, expected, source):
    offer = dict(DATA_OFFERS[0], publishedAt=published, lastPublishedAt=last)
    job = parse_offer(offer)
    assert job.posted_at == expected and job.raw["posted_at_source"] == source
    assert job.raw["publishedAt"] == published and job.raw["lastPublishedAt"] == last
    assert source_update(job) == "unknown"


@pytest.mark.parametrize("partial", ["Oct 5", "2026-10", "10:30", "5 October", "yesterday", "2 days ago",
                                     "Monday", "2026", "10/05", "T08:00:00Z", "2026-10-05T", "now"])
@pytest.mark.parametrize("field", ["publishedAt", "lastPublishedAt"])
def test_an_incomplete_date_stays_unknown_instead_of_borrowing_today(field, partial):
    """The generic parser completes these from the current date, which would
    put a malformed offer inside the freshness window."""
    other = "lastPublishedAt" if field == "publishedAt" else "publishedAt"
    job = parse_offer(dict(DATA_OFFERS[0], **{field: partial, other: None}))
    assert job.posted_at is None and job.raw[field] == partial
    assert job.raw["source_dates"] == {} and job.raw["date_conflict"] is False
    complete = parse_offer(dict(DATA_OFFERS[0], **{field: partial, other: "2026-08-31T07:00:00Z"}))
    assert complete.posted_at == datetime(2026, 8, 31, 7, tzinfo=UTC)


# --------------------------------------------------------------------------
# fields the recorded payload changed or settled
# --------------------------------------------------------------------------


def test_only_the_advertised_salary_is_reported_never_a_conversion():
    """Each recorded range is repeated in USD, EUR, CHF and GBP with
    `currencySource: "conversion"`."""
    jobs = fetch(None, session=refused_session(recorded))
    assert by_slug_end(jobs, "allegro-ai-hub-warszawa-data").salary == "14600–20825 PLN/month"
    assert by_slug_end(jobs, "sollers-consulting-data-analyst-warszawa-data").salary is None
    converted = [dict(node, **{"from": 5000, "to": 9000})
                 for node in DATA_OFFERS[0]["employmentTypes"] if node["currencySource"] == "conversion"]
    assert parse_offer(dict(DATA_OFFERS[0], employmentTypes=converted)).salary is None


def test_recorded_workplace_and_place_stay_what_the_board_states():
    jobs = fetch(None, session=refused_session(recorded))
    london = by_slug_end(jobs, "london-data-b15a8209")
    assert (london.location, london.remote, london.country) == ("London", True, None)
    bonn = by_slug_end(jobs, "bonn-ai-bf89dab3")
    assert (bonn.location, bonn.remote, bonn.country) == ("Bonn", None, None)   # hybrid
    assert all(j.country is None for j in jobs)
    assert {j.raw["workplace_type"] for j in jobs} == {"hybrid", "remote"}


# --------------------------------------------------------------------------
# the embedded payload: drift, empty, caps
# --------------------------------------------------------------------------


def test_the_recorded_pages_yield_their_offers_and_last_page():
    offers, last = parse_listing_page(DATA_PAGE)
    assert len(offers) == 9 and last == 7
    assert {"slug", "title", "companyName", "publishedAt", "lastPublishedAt", "expiredAt",
            "guid", "employmentTypes", "workplaceType", "experienceLevel"} <= set(offers[0])
    assert [len(parse_listing_page(AI_PAGE)[0]), parse_listing_page(AI_PAGE)[1]] == [4, 4]


def test_a_payload_split_across_script_chunks_still_parses():
    marker = 'self.__next_f.push([1,'
    start = DATA_PAGE.index(marker) + len(marker)
    end = DATA_PAGE.index("])</script>", start)
    flight = json.loads(DATA_PAGE[start:end])
    cut = flight.index('"lastPublishedAt"')
    split = (DATA_PAGE[:start] + json.dumps(flight[:cut]) + "])</script><script>"
             + marker + json.dumps(flight[cut:]) + DATA_PAGE[end:])
    assert parse_listing_page(split) == parse_listing_page(DATA_PAGE)


@pytest.mark.parametrize("body,message", [
    ("<html><body>Please enable JavaScript</body></html>", "no embedded page data"),
    (listing_html([]).replace("offers", "items"), "no offers list"),
    (listing_html([{"title": "no slug"}]), "malformed offers"),
    (listing_html([]) + listing_html([{"title": "renamed slug field", "offerId": "123"}]), "malformed offers"),
    (listing_html([{"title": "renamed", "offerId": "1"}]) + listing_html([]), "malformed offers"),
    (listing_html([DATA_OFFERS[0]]) + listing_html([DATA_OFFERS[1]]), "ambiguous"),
    (listing_html([]) + listing_html([]).replace('\\"offers\\":[]', '\\"offers\\":[{\\"slug\\":'), "cannot be decoded"),
    ('<script>self.__next_f.push([1,"broken \\x"])</script>', "unreadable"),
    ("x" * 4_000_001, "body limit"),
])
def test_a_reshaped_listing_page_is_drift_not_an_empty_board(body, message):
    with pytest.raises(ValueError, match=message):
        parse_listing_page(body)
    errors: list[str] = []
    assert fetch(None, session=refused_session(lambda category, page: body), errors=errors) == []
    assert message in errors[1] and "listing page data/1" in errors[1]
    assert not any("listed no offers" in e or "read " in e for e in errors)


def test_a_malformed_row_does_not_cost_its_valid_neighbours():
    valid = dict(DATA_OFFERS[0])
    offers, _ = parse_listing_page(listing_html([valid, {"title": "missing slug"}, 42]))
    assert valid in offers and len(offers) == 3
    mixed = listing_html([valid, {"title": "missing slug"}, 42, {"slug": "only-a-slug"}])
    errors: list[str] = []
    jobs = fetch(None, session=refused_session(lambda category, page: mixed if category == "data" else listing_html([])),
                 errors=errors)
    assert [j.ats_job_id for j in jobs] == [valid["slug"]]
    assert "justjoin_it: listing page data/1: skipped 3 malformed row(s) of 4; valid rows kept" in errors
    assert "read 4 offers" in errors[-1]


def test_an_auxiliary_empty_list_beside_the_real_one_changes_nothing():
    assert parse_listing_page(listing_html([]) + DATA_PAGE)[0] == DATA_OFFERS
    assert parse_listing_page(listing_html([]) + listing_html([])) == ([], None)


def test_an_empty_listing_is_reported_as_empty_not_as_failed():
    assert parse_listing_page(listing_html([])) == ([], None)
    session = refused_session(lambda category, page: listing_html([]))
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert len(session.calls) == 1 + len(LISTING_CATEGORIES)
    assert errors[1:] == ["justjoin_it: API unavailable; the public listing pages "
                          "of data, ai answered and listed no offers"]


def test_full_pages_walk_to_the_cap_and_say_where_they_stopped():
    session = refused_session(lambda category, page: full_page(f"{category}-{page}", last_page=7))
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert len(session.calls) == 1 + len(LISTING_CATEGORIES) * LISTING_MAX_PAGES
    assert [c["params"].get("page", 1) for c in session.calls[1:]] == [1, 2, 3, 1, 2, 3]
    assert len(jobs) == len(LISTING_CATEGORIES) * LISTING_MAX_PAGES * LISTING_PAGE_SIZE
    assert errors[-1].endswith("page cap reached (data 3/7, ai 3/7)")


def test_the_walk_ends_at_the_last_page_the_site_links():
    session = refused_session(lambda category, page: full_page(f"{category}-{page}", last_page=2))
    errors: list[str] = []
    fetch(None, session=session, errors=errors)
    assert [c["params"].get("page", 1) for c in session.calls[1:]] == [1, 2, 1, 2]
    assert "page cap" not in errors[-1]


def test_a_repeated_slug_across_pages_is_kept_once():
    session = refused_session(lambda category, page: full_page("same"))
    assert len(fetch(None, session=session)) == LISTING_PAGE_SIZE


def test_a_failing_later_listing_page_keeps_the_earlier_ones():
    session = refused_session(lambda category, page: (
        full_page(f"{category}-{page}") if page == 1 else FakeResponse(status_code=404)))
    errors: list[str] = []
    assert len(fetch(None, session=session, errors=errors)) == 2 * LISTING_PAGE_SIZE
    assert sum("listing page" in e and "HTTP 404" in e for e in errors) == 2
    assert "read 100 offers" in errors[-1]


def test_an_oversized_offer_list_is_cut_and_reported():
    many = listing_html([dict(DATA_OFFERS[8], slug=f"row-{n}") for n in range(130)])
    session = refused_session(lambda category, page: many if category == "data" else listing_html([]))
    errors: list[str] = []
    assert len(fetch(None, session=session, errors=errors)) == 100
    assert any("record cap reached" in e for e in errors)


# --------------------------------------------------------------------------
# what survives downstream
# --------------------------------------------------------------------------


def test_recorded_offers_through_dedupe_location_and_freshness():
    """13 embedded -> 11 past the adapter's gate -> 10 after dedupe (one job is
    listed under both categories with two slugs) -> 3 inside a 72h window for
    Poland. The five rejected as old all carry a later `publishedAt` that
    would have placed them inside the window."""
    jobs = fetch(None, session=refused_session(recorded))
    assert sum(j.posted_at is not None for j in jobs) == 11
    assert sum(not j.raw["snippet_only"] for j in jobs) == 0      # no ad body on this surface
    unique = dedupe(jobs)
    assert len(unique) == 10
    assert sum(j.title == "Data Scientist - Allegro AI Hub" for j in unique) == 1
    settings = {"filters": {"countries": ["PL"]},
                "freshness": {"max_age_hours": 72, "skip_undated": True}}
    results = apply_filters(unique, settings, now=LIVE_NOW)
    assert sorted((j.title, j.company) for j in results.kept) == [
        ("Data Analyst", "Sollers Consulting"),
        ("Data Engineer", "Jit Team"),
        ("Mid ML Engineer", "Jit Team"),
    ]
    reasons = [reason for _, reason in results.rejected]
    assert sum("older than" in r for r in reasons) == 5
    assert sum("United Kingdom" in r or "Germany" in r for r in reasons) == 2
    conflicting = [j for j, r in results.rejected
                   if j.raw["date_conflict"] and "older than" in r]
    assert len(conflicting) == 5
    assert all(j.raw["coverage"] == "partial" and j.raw["snippet_only"] for j in results.kept)


# --------------------------------------------------------------------------
# the bounds, measured on the transport rather than on the helper
# --------------------------------------------------------------------------
#
# FakeSession counts calls to `get`; a real `requests.Session` can turn one
# call into many requests by following redirects. These tests drive the real
# session machinery over an adapter that never touches a socket.


class OfflineAdapter(BaseAdapter):
    """Answers every request from `route(url) -> (status, headers, body)`."""

    def __init__(self, route):
        super().__init__()
        self.route, self.calls = route, []

    def send(self, request, **kwargs):
        self.calls.append(request.url)
        status, headers, body = self.route(request.url)
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, status
        response.headers.update(headers)
        if hasattr(body, "read"):
            response.raw = body
        else:
            response._content = body
        return response

    def close(self):
        pass


def offline_session(route):
    session = requests.Session()
    session.trust_env = False
    adapter = OfflineAdapter(route)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter


def hosts(adapter):
    return sorted({urlsplit(url).netloc for url in adapter.calls})


def test_a_redirect_off_the_listing_route_is_refused_and_never_followed():
    def route(url):
        if "api.justjoin.it" in url:
            return 503, {}, API_503.encode()
        return 302, {"Location": "https://outside.invalid/redirect/0"}, b""
    session, adapter = offline_session(route)
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert len(adapter.calls) == 1 + len(LISTING_CATEGORIES)
    assert hosts(adapter) == ["api.justjoin.it", "justjoin.it"]
    assert sum("redirect leaves the listing route (outside.invalid/redirect/0)" in e for e in errors) == 2
    assert not any("listed no offers" in e or "read " in e for e in errors)


@pytest.mark.parametrize("location", ["/job-offers/all-locations/analytics", "/login",
                                      "http://justjoin.it/job-offers/all-locations/data",
                                      "https://justjoin.it.evil.example/job-offers/all-locations/data"])
def test_a_redirect_to_another_path_scheme_or_lookalike_host_is_refused(location):
    def route(url):
        return (503, {}, b"") if "api.justjoin.it" in url else (302, {"Location": location}, b"")
    session, adapter = offline_session(route)
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert len(adapter.calls) == 1 + len(LISTING_CATEGORIES)
    assert any("listing page data/1: redirect leaves the listing route" in e for e in errors)


def test_a_same_route_redirect_is_followed_and_charged_to_the_budget():
    page = listing_html([DATA_OFFERS[0]]).encode()
    def route(url):
        if "api.justjoin.it" in url:
            return 503, {}, b""
        if "canonical=1" in url:
            return 200, {"Content-Type": "text/html; charset=utf-8"}, page
        return 302, {"Location": urlsplit(url).path + "?experience-levels=junior,mid&canonical=1"}, b""
    session, adapter = offline_session(route)
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert [j.company for j in jobs] == ["Allegro"]
    assert len(adapter.calls) == 1 + 2 * len(LISTING_CATEGORIES)


def test_a_redirect_loop_spends_the_listing_budget_and_stops():
    def route(url):
        if "api.justjoin.it" in url:
            return 503, {}, b""
        return 302, {"Location": urlsplit(url).path + "?again=1"}, b""
    session, adapter = offline_session(route)
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert len(adapter.calls) == 1 + LISTING_MAX_REQUESTS
    assert sum("request cap reached; coverage partial" in e for e in errors) == 2


def test_the_whole_fallback_walk_never_exceeds_one_api_request_plus_the_listing_budget():
    full = lambda tag: listing_html(  # noqa: E731
        [dict(DATA_OFFERS[8], slug=f"{tag}-{n}") for n in range(LISTING_PAGE_SIZE)], 7).encode()
    def route(url):
        if "api.justjoin.it" in url:
            return 503, {}, b""
        return 200, {}, full(url)
    session, adapter = offline_session(route)
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert len(adapter.calls) == 1 + len(LISTING_CATEGORIES) * LISTING_MAX_PAGES <= 1 + LISTING_MAX_REQUESTS
    assert len(jobs) == len(LISTING_CATEGORIES) * LISTING_MAX_PAGES * LISTING_PAGE_SIZE


def test_an_api_redirect_is_not_followed_and_counts_as_page_one_failing():
    page = listing_html([DATA_OFFERS[0]]).encode()
    def route(url):
        if "api.justjoin.it" in url:
            return 302, {"Location": "https://outside.invalid/v2/offers"}, b""
        return 200, {}, page
    session, adapter = offline_session(route)
    errors: list[str] = []
    jobs = fetch(None, session=session, errors=errors)
    assert hosts(adapter) == ["api.justjoin.it", "justjoin.it"]
    assert "HTTP 302 redirect not followed" in errors[0] and len(jobs) == 1


def test_the_body_cap_stops_a_streamed_download_before_it_is_read_whole():
    class Endless(io.RawIOBase):
        served = 0
        def read(self, size=-1):
            Endless.served += size
            return b"x" * size
    def route(url):
        return (503, {}, b"") if "api.justjoin.it" in url else (200, {}, Endless())
    session, adapter = offline_session(route)
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert sum("body limit" in e for e in errors) == 2
    assert Endless.served <= 2 * (MAX_BODY_BYTES + 2 * 65536)


def test_the_body_cap_counts_bytes_not_characters():
    body = "ą" * 2_100_000 + listing_html([])
    assert len(body) < MAX_BODY_BYTES < len(body.encode("utf-8"))
    with pytest.raises(ValueError, match="body limit"):
        parse_listing_page(body)
    errors: list[str] = []
    assert fetch(None, session=refused_session(lambda category, page: body), errors=errors) == []
    assert "body limit" in errors[1]


def test_a_declared_oversized_body_is_refused_before_reading():
    class Unread(io.RawIOBase):
        def read(self, size=-1):
            raise AssertionError("the body must not be read")
    def route(url):
        if "api.justjoin.it" in url:
            return 503, {}, b""
        return 200, {"Content-Length": str(MAX_BODY_BYTES + 1)}, Unread()
    session, _ = offline_session(route)
    errors: list[str] = []
    assert fetch(None, session=session, errors=errors) == []
    assert sum("body limit" in e for e in errors) == 2


def test_a_streamed_recorded_page_parses_like_the_fixture_with_polish_characters():
    def route(url):
        if "api.justjoin.it" in url:
            return 503, {}, API_503.encode()
        page = DATA_PAGE if url.split("?")[0].endswith("/data") else AI_PAGE
        return 200, {"Content-Type": "text/html"}, io.BytesIO(page.encode("utf-8"))
    session, adapter = offline_session(route)
    jobs = fetch(None, session=session)
    assert len(jobs) == 11 and len(adapter.calls) == 3
    assert any("Wrocław" in j.location for j in jobs)
