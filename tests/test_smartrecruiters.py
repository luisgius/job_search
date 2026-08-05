"""Tests for the SmartRecruiters pull in src/sources/ats_boards.py.

SmartRecruiters is where large European employers post — the German, French and
Spanish enterprises that never appear on Greenhouse. It is also the awkward one:
**the listing carries no description at all**, so the ad lives behind one extra
request per posting. That shape drives most of this file — the cap, the
per-posting isolation, and the fact that a description-less job still reaches
the digest rather than being dropped.

Driven by `tests/fixtures/smartrecruiters_postings.json` and
`..._posting_detail.json`, which deliberately include a null location, a null
date and a titleless posting.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources.ats_boards import (
    SMARTRECRUITERS_MAX_DESCRIPTIONS,
    SMARTRECRUITERS_MAX_PAGES,
    check_slug,
    fetch,
    fetch_smartrecruiters,
)
from tests.conftest import (
    FakeResponse,
    FakeSession,
    json_response,
    load_json_fixture,
    write_config,
)

UTC = timezone.utc
LISTING = load_json_fixture("smartrecruiters_postings.json")
DETAIL = load_json_fixture("smartrecruiters_posting_detail.json")

FIRST = "743999900000001"
REMOTE = "743999900000002"
UNDATED = "743999900000003"
TITLELESS = "743999900000004"

#: The detail route has to be matched *first*: both URLs contain "/postings".
_DETAIL_RE = re.compile(r"/postings/\S+")


def sr_session(listing=None, detail=None, **kwargs):
    return FakeSession(
        [(_DETAIL_RE, json_response(DETAIL if detail is None else detail)),
         ("/postings", json_response(LISTING if listing is None else listing))],
        **kwargs,
    )


def by_id(jobs):
    return {j.ats_job_id: j for j in jobs}


def paged_listing(total, *, reported=None):
    """A listing route that serves `total` postings in `offset`-sized pages.

    This is the whole point of the seam: the vendor pages its listing, so the
    fake has to page too, or the offline suite can only ever prove that page
    one is parsed.
    """
    postings = [
        {"id": f"7439999{n:08d}", "name": f"Engineer {n}",
         "company": {"identifier": "Umbrella", "name": "Umbrella Iberia S.L."},
         "location": {"city": "Valencia", "country": "es", "remote": False},
         "releasedDate": "2026-08-04T07:00:00.000Z"}
        for n in range(total)
    ]

    def route(url, params):
        params = params or {}
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 100))
        return json_response({
            "offset": offset,
            "limit": limit,
            "totalFound": total if reported is None else reported,
            "content": postings[offset:offset + limit],
        })

    return route


# ==========================================================================
# request shape
# ==========================================================================


def test_smartrecruiters_hits_the_public_posting_api():
    session = sr_session()
    fetch_smartrecruiters("Umbrella", session=session, details=False)
    assert session.calls[0]["url"] == (
        "https://api.smartrecruiters.com/v1/companies/Umbrella/postings"
    )
    assert session.calls[0]["params"] == {"limit": 100, "offset": 0}


def test_smartrecruiters_stops_after_one_page_when_the_company_is_small():
    """The pagination must not cost a request on the boards that do not need
    it. A short page is the end of the board, and the loop believes it."""
    session = sr_session()
    fetch_smartrecruiters("Umbrella", session=session, details=False)
    assert len(session.calls) == 1


# ==========================================================================
# pagination — one page is not the board
# ==========================================================================


def test_smartrecruiters_follows_the_offsets_to_the_end_of_the_board():
    """The listing is paged, at 100 per page, and asking for page one is not
    asking for the board.

    A company with 250 open roles contributed exactly 100 of them: no error, no
    warning, nothing above DEBUG — 150 European jobs deleted every morning and
    the pipeline reporting success. `totalFound` was read only to write an INFO
    line, and nothing in `health.py` reads INFO."""
    session = FakeSession([("/postings", paged_listing(250))])
    jobs = fetch_smartrecruiters("Umbrella", session=session, details=False)

    assert len(jobs) == 250
    assert [c["params"]["offset"] for c in session.calls] == [0, 100, 200]
    assert len({j.ats_job_id for j in jobs}) == 250


def test_smartrecruiters_does_not_pay_for_a_page_it_knows_is_empty():
    """`totalFound` is exactly two pages here. A third request would be a
    request the company already told us has nothing in it."""
    session = FakeSession([("/postings", paged_listing(200))])
    jobs = fetch_smartrecruiters("Umbrella", session=session, details=False)
    assert len(jobs) == 200
    assert [c["params"]["offset"] for c in session.calls] == [0, 100]


def test_smartrecruiters_trusts_a_short_page_over_a_wrong_total():
    """A short page is the end of the board whatever the envelope claims.
    `totalFound` understating the truth must not truncate the walk, and
    overstating it must not spin."""
    session = FakeSession([("/postings", paged_listing(150, reported=9999))])
    jobs = fetch_smartrecruiters("Umbrella", session=session, details=False)
    assert len(jobs) == 150
    assert len(session.calls) == 2


def test_a_board_that_never_runs_out_is_bounded_and_says_what_it_dropped(caplog):
    """The other half of the bargain. A board whose `totalFound` is wrong — or
    that simply keeps answering full pages — must not turn one watchlist slug
    into an unbounded request loop.

    But a silent cap is the bug this module exists to avoid, so the stop is
    announced at WARNING and names the number left behind. `--check` and the
    run log are the only places a user can find out that their 2,000-role
    employer is being read in part; a DEBUG line is not one of them."""
    import logging

    def never_ends(url, params):
        offset = int((params or {}).get("offset", 0))
        return json_response({
            "totalFound": 100000,
            "content": [{"id": str(offset + n), "name": f"Engineer {offset + n}"}
                        for n in range(100)],
        })

    session = FakeSession([("/postings", never_ends)])
    with caplog.at_level(logging.WARNING, logger="src.sources.ats_boards"):
        jobs = fetch_smartrecruiters("Umbrella", session=session, details=False,
                                     max_pages=3)

    assert len(session.calls) == 3
    assert len(jobs) == 300
    assert "100000" in caplog.text
    assert "not fetched" in caplog.text.lower()


def test_the_shipped_page_cap_is_a_real_bound():
    assert 0 < SMARTRECRUITERS_MAX_PAGES <= 100
    # Generous enough that no real employer ever meets it.
    assert SMARTRECRUITERS_MAX_PAGES * 100 >= 2000


def test_a_second_page_that_404s_costs_the_company_and_not_the_run():
    """The listing is allowed to raise — `fetch` catches it per slug. What must
    not happen is a half-page of jobs being returned as if it were the board."""
    def route(url, params):
        if int((params or {}).get("offset", 0)):
            return FakeResponse(status_code=404)
        return paged_listing(250)(url, params)

    session = FakeSession([("/postings", route)])
    with pytest.raises(Exception):
        fetch_smartrecruiters("Umbrella", session=session, details=False)


def test_smartrecruiters_fetches_one_description_per_posting():
    """The single fact that shapes this source: the listing has no `jobAd` at
    all, so the ad costs one request each. Anyone reading the call log needs to
    see that plainly rather than discovering it from a rate-limit."""
    session = sr_session()
    fetch_smartrecruiters("Umbrella", session=session)
    detail_calls = [c["url"] for c in session.calls if _DETAIL_RE.search(c["url"])]
    assert len(detail_calls) == 3            # one per parsed posting
    assert detail_calls[0].endswith(f"/postings/{FIRST}")


def test_smartrecruiters_can_skip_the_detail_calls_entirely():
    """`--check` only needs to know the company answers. Paying N requests to
    prove one slug exists is exactly the trap the cheap-check flag avoids."""
    session = sr_session()
    jobs = fetch_smartrecruiters("Umbrella", session=session, details=False)
    assert len(session.calls) == 1
    assert all(j.description == "" for j in jobs)


def test_smartrecruiters_caps_the_number_of_descriptions_it_fetches():
    """A company with 400 open roles would otherwise mean 400 HTTP calls in the
    stage that is supposed to be the cheap one. Jobs past the cap still reach
    the digest — they are just scored on title, company and location."""
    session = sr_session()
    jobs = fetch_smartrecruiters("Umbrella", session=session, max_descriptions=1)
    detail_calls = [c for c in session.calls if _DETAIL_RE.search(c["url"])]
    assert len(detail_calls) == 1
    assert len(jobs) == 3                    # nothing was dropped
    assert jobs[0].description
    assert jobs[1].description == ""


def test_the_shipped_description_cap_is_a_real_bound():
    assert 0 < SMARTRECRUITERS_MAX_DESCRIPTIONS <= 200


def test_the_description_cap_is_announced_in_the_log(caplog):
    """"These 40 jobs were scored on their titles alone" is something the user
    has to be able to find out; a silent cap is indistinguishable from a
    board full of ads nobody wrote."""
    import logging

    with caplog.at_level(logging.INFO, logger="src.sources.ats_boards"):
        fetch_smartrecruiters("Umbrella", session=sr_session(), max_descriptions=1)
    text = caplog.text.lower()
    assert "cap" in text
    assert "1 of 3" in text


@pytest.mark.parametrize("pasted", [
    "https://jobs.smartrecruiters.com/Umbrella",
    "https://jobs.smartrecruiters.com/Umbrella/743999900000001",
    "jobs.smartrecruiters.com/Umbrella",
    "  Umbrella  ",
])
def test_a_pasted_board_url_is_accepted_as_a_slug(pasted):
    session = sr_session()
    fetch_smartrecruiters(pasted, session=session, details=False)
    assert session.calls[0]["url"].endswith("/companies/Umbrella/postings")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_slug_raises_rather_than_hitting_the_network(bad):
    with pytest.raises(ValueError):
        fetch_smartrecruiters(bad, session=FakeSession())


# ==========================================================================
# parsing
# ==========================================================================


def test_smartrecruiters_parses_the_happy_path():
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[FIRST]
    assert job.source == "smartrecruiters"
    assert job.title == "Senior Backend Engineer (m/f/d)"
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_smartrecruiters_builds_the_applicant_facing_url_not_the_api_one():
    """`ref` in the payload is the API URL — JSON, no form, useless to a human.
    The digest's apply link has to be the page a person can actually apply on."""
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[FIRST]
    assert job.url == f"https://jobs.smartrecruiters.com/Umbrella/{FIRST}"
    assert "api.smartrecruiters.com" not in job.url


def test_smartrecruiters_prefers_the_canonical_company_identifier_in_the_url():
    """The watchlist slug is whatever the user typed; `company.identifier` is
    the spelling the public host actually serves, and the two differ in case
    often enough to 404 the apply link."""
    listing = {"content": [{"id": "1", "name": "Engineer",
                            "company": {"identifier": "UmbrellaCorp"}}]}
    job = fetch_smartrecruiters("umbrella", session=sr_session(listing),
                                details=False)[0]
    assert job.url == "https://jobs.smartrecruiters.com/UmbrellaCorp/1"


def test_smartrecruiters_never_claims_an_auto_appliable_ats():
    """`autoapply` only knows how to bail safely out of Greenhouse and Lever
    forms. Claiming either here would drive the bot into a form nothing has
    tested it against."""
    from src.apply.autoapply import SUPPORTED_ATS, detect_ats

    for job in fetch_smartrecruiters("Umbrella", session=sr_session()):
        assert job.ats == "smartrecruiters"
        assert job.ats not in SUPPORTED_ATS
        assert detect_ats(job.url) is None


def test_smartrecruiters_assembles_the_whole_ad_from_jobad_sections():
    """`qualifications` decides most scores and `additionalInformation` is
    where relocation support is stated. Reading only `jobDescription` throws
    both away — the same mistake as dropping Lever's `lists`."""
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[FIRST]
    assert "logistics software from Valencia" in job.description   # companyDescription
    assert "routing services in Python" in job.description         # jobDescription
    assert "6+ years of Python" in job.description                 # qualifications
    assert "Relocation support" in job.description                 # additionalInformation
    assert "<ul>" not in job.description


def test_smartrecruiters_uses_the_display_company_name_when_it_has_one():
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[FIRST]
    assert job.company == "Umbrella Iberia S.L."


def test_smartrecruiters_assembles_a_location_the_geo_filter_can_read():
    from src import geo

    jobs = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))
    assert jobs[FIRST].location == "Valencia, Comunidad Valenciana, ES"
    assert geo.country_of(jobs[FIRST].location) == "ES"
    assert jobs[UNDATED].location == "Berlin, DE"


def test_smartrecruiters_remote_posting_still_says_remote():
    """A remote posting has no city. Left empty, `Job.location` is an empty
    string and the location filter cannot tell a remote EU role from an
    unparseable one — so it drops it."""
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[REMOTE]
    assert job.location == "Remote"
    assert job.remote is True


def test_smartrecruiters_marks_remote_positively_only():
    jobs = fetch_smartrecruiters("Umbrella", session=sr_session())
    assert all(j.remote is not False for j in jobs)


def test_smartrecruiters_null_location_is_not_a_crash():
    listing = {"content": [{"id": "1", "name": "Engineer", "location": None}]}
    assert fetch_smartrecruiters("Umbrella", session=sr_session(listing),
                                 details=False)[0].location == ""


def test_smartrecruiters_undated_posting_yields_none_not_a_guess():
    """`freshness.skip_undated` decides what happens to a dateless posting.
    Inventing "now" here would defeat that setting silently."""
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[UNDATED]
    assert job.posted_at is None


def test_smartrecruiters_ignores_updated_on_as_a_publication_date():
    """The mirror of `test_ashby_ignores_updated_at_as_a_publication_date`, and
    the test this file did not have.

    Three dates arrive on the fixture's flagship posting: `createdOn` (6 July,
    when the req was drafted), `releasedDate` (4 August, when it went live) and
    `updatedOn` (later that morning, when somebody fixed a typo). Only the
    middle one is a publication date. `updatedOn` overstates freshness — a
    three-month-old req looks like today's news after one edit — and `createdOn`
    understates it, which is worse, because a posting that looks a month old is
    rejected as stale and vanishes."""
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[FIRST]
    assert job.raw["released_date"] == "2026-08-04T07:00:00.000Z"
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_smartrecruiters_will_not_date_a_posting_from_updated_on_alone():
    """No date is more honest than an inflated one: undated leaves the decision
    to `freshness.skip_undated`, where the user can see it."""
    listing = {"content": [{"id": "1", "name": "Engineer",
                            "updatedOn": "2026-08-04T08:45:00.000Z"}]}
    job = fetch_smartrecruiters("Umbrella", session=sr_session(listing),
                                details=False)[0]
    assert job.posted_at is None


def test_smartrecruiters_falls_back_to_created_on_when_nothing_was_released():
    """`createdOn` can only ever *under*state freshness, so it is a floor —
    and a floor beats no date at all when `skip_undated` is on."""
    listing = {"content": [{"id": "1", "name": "Engineer",
                            "createdOn": "2026-08-04T07:00:00.000Z"}]}
    job = fetch_smartrecruiters("Umbrella", session=sr_session(listing),
                                details=False)[0]
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_smartrecruiters_skips_a_titleless_posting_without_dropping_the_company():
    jobs = fetch_smartrecruiters("Umbrella", session=sr_session())
    assert TITLELESS not in by_id(jobs)
    assert len(jobs) == 3


def test_smartrecruiters_drops_a_posting_with_no_id():
    """The id is both the apply URL and `Job.key`. Without it the job cannot be
    linked to or tracked, and an untrackable job is one the never-apply-twice
    guarantee cannot cover."""
    listing = {"content": [{"name": "Engineer"}]}
    assert fetch_smartrecruiters("Umbrella", session=sr_session(listing),
                                 details=False) == []


def test_smartrecruiters_records_the_employment_type_where_the_filter_reads_it():
    """`typeOfEmployment.label` is the only place an internship with a neutral
    title declares itself, and `filters.employment_type_exclude` reads exactly
    the keys in `EMPLOYMENT_TYPE_KEYS` and nothing else."""
    from src.config import DEFAULTS
    from src.filters import EMPLOYMENT_TYPE_KEYS, apply_filters

    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[UNDATED]
    assert job.raw["employment_type"] == "Internship"
    assert any(k in job.raw for k in EMPLOYMENT_TYPE_KEYS)

    job.title = "Operations Associate"    # the title now says nothing at all
    result = apply_filters([job], {"filters": DEFAULTS["filters"],
                                   "freshness": {"skip_undated": False}})
    assert result.counts.get("employment_type_excluded") == 1


def test_smartrecruiters_tolerates_junk_entries_in_the_content_list():
    listing = {"content": ["nope", None, 7, {"id": "1", "name": "Engineer"}]}
    assert len(fetch_smartrecruiters("Umbrella", session=sr_session(listing),
                                     details=False)) == 1


def test_smartrecruiters_refuses_a_200_whose_body_is_not_a_board():
    """A 200 with neither a `content` list nor a numeric `totalFound` is not
    this vendor's envelope — it is an error object or a login wall, and
    parsing it as zero postings fabricates "exists, nothing open" out of it.
    `{"totalFound": 0}` alone still counts as the envelope: the count *is*
    SmartRecruiters speaking, even on a page with no list attached."""
    assert fetch_smartrecruiters("Umbrella", session=sr_session({"totalFound": 0}),
                                 details=False) == []
    with pytest.raises(ValueError, match="not a smartrecruiters board"):
        fetch_smartrecruiters("Umbrella", session=sr_session({"error": "not found"}),
                              details=False)
    with pytest.raises(ValueError, match="not a smartrecruiters board"):
        fetch_smartrecruiters("Umbrella", session=sr_session([]), details=False)


def test_smartrecruiters_records_provenance_in_raw():
    job = by_id(fetch_smartrecruiters("Umbrella", session=sr_session()))[FIRST]
    assert job.raw["board"] == "smartrecruiters"
    assert job.raw["ref_number"] == "REF-301"
    assert job.raw["department"] == "Engineering"
    assert job.raw["description_fetched"] is True


# ==========================================================================
# the detail call is allowed to fail
# ==========================================================================


def test_a_failed_detail_call_costs_the_description_and_not_the_job():
    """A job with no description still scores, just worse — `scoring` handles a
    thin description and the digest shows what it saw. Dropping the posting
    instead would delete a real Valencia role because one HTTP call flaked.

    404 rather than 500 on purpose: a 500 is retried three times with real
    backoff, which would buy no extra coverage and cost this offline suite ten
    seconds. It is also the likelier failure — a req pulled between the listing
    call and the detail call answers 404, not 500.
    """
    session = FakeSession([(_DETAIL_RE, FakeResponse(status_code=404)),
                           ("/postings", json_response(LISTING))])
    jobs = fetch_smartrecruiters("Umbrella", session=session)
    assert len(jobs) == 3
    assert all(j.description == "" for j in jobs)
    assert all(j.title for j in jobs)
    assert all(j.raw["description_fetched"] is False for j in jobs)


def test_one_failed_detail_call_does_not_stop_the_others():
    def route(url, params):
        return (FakeResponse(status_code=404) if url.endswith(FIRST)
                else json_response(DETAIL))

    session = FakeSession([(_DETAIL_RE, route), ("/postings", json_response(LISTING))])
    jobs = by_id(fetch_smartrecruiters("Umbrella", session=session))
    assert jobs[FIRST].description == ""
    assert jobs[REMOTE].description


def test_a_detail_payload_with_no_job_ad_is_survivable():
    session = sr_session(detail={"id": FIRST, "name": "Engineer"})
    jobs = fetch_smartrecruiters("Umbrella", session=session)
    assert len(jobs) == 3
    assert all(j.description == "" for j in jobs)


def test_a_detail_payload_with_junk_sections_is_survivable():
    session = sr_session(detail={"jobAd": {"sections": "not-an-object"}})
    assert len(fetch_smartrecruiters("Umbrella", session=session)) == 3


# ==========================================================================
# failure isolation
# ==========================================================================


def test_check_slug_ok_and_cheap():
    session = sr_session()
    ok, message = check_slug("smartrecruiters", "Umbrella", session=session)
    assert ok is True
    assert "3 postings" in message
    assert len(session.calls) == 1     # no per-posting detail calls


def test_check_slug_explains_a_404():
    session = FakeSession([("api.smartrecruiters.com", FakeResponse(status_code=404))])
    ok, message = check_slug("smartrecruiters", "nope", session=session)
    assert ok is False
    assert "404" in message and "slug not found" in message


def test_one_dead_smartrecruiters_company_does_not_kill_the_others(tmp_path: Path):
    """The whole point of the per-slug try/except: a renamed company costs that
    company's postings and nothing else."""
    cfg = write_config(tmp_path,
                       {"sources": {"greenhouse": False, "smartrecruiters": True}},
                       watchlist={"smartrecruiters": ["dead", "Umbrella"]})
    session = FakeSession([
        ("companies/dead/", FakeResponse(status_code=404)),
        (_DETAIL_RE, json_response(DETAIL)),
        ("/postings", json_response(LISTING)),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert len(jobs) == 3
    assert len(errors) == 1
    assert "smartrecruiters/dead" in errors[0] and "404" in errors[0]


def test_fetch_never_raises_even_when_every_company_is_gone(tmp_path: Path):
    cfg = write_config(tmp_path,
                       {"sources": {"greenhouse": False, "smartrecruiters": True}},
                       watchlist={"smartrecruiters": ["a", "b"]})
    session = FakeSession(default=FakeResponse(status_code=404))
    errors: list[str] = []
    assert fetch(cfg, session=session, errors=errors) == []
    assert len(errors) == 2
