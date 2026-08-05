"""Tests for the Ashby pull in src/sources/ats_boards.py.

Ashby is where modern European start-ups post, and its public job-board
endpoint is the one their careers page renders from. Driven by
`tests/fixtures/ashby_jobs.json`, which is structurally faithful to that
payload and deliberately includes the awkward cases: an unlisted posting, a
null location, a null date, a titleless posting, and the `secondaryLocations`
array that makes a multi-city role visible to the geo filter.

The recurring theme, as everywhere in this module: one broken posting costs one
posting, one broken board costs one company, and neither ever costs the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources.ats_boards import check_slug, fetch, fetch_ashby
from tests.conftest import (
    FakeResponse,
    FakeSession,
    json_response,
    load_json_fixture,
    write_config,
)

UTC = timezone.utc
ASHBY = load_json_fixture("ashby_jobs.json")
FIRST = "3a1f9c20-1111-4d55-8b77-000000000001"
REMOTE = "3a1f9c20-2222-4d55-8b77-000000000002"
UNDATED = "3a1f9c20-3333-4d55-8b77-000000000003"
INTERN = "3a1f9c20-4444-4d55-8b77-000000000004"
UNLISTED = "3a1f9c20-5555-4d55-8b77-000000000005"
TITLELESS = "3a1f9c20-6666-4d55-8b77-000000000006"


def ab_session(payload=None, **kwargs):
    return FakeSession([("api.ashbyhq.com",
                         json_response(ASHBY if payload is None else payload))],
                       **kwargs)


def by_id(jobs):
    return {j.ats_job_id: j for j in jobs}


# ==========================================================================
# request shape
# ==========================================================================


def test_ashby_hits_the_public_posting_api():
    session = ab_session()
    fetch_ashby("initech", session=session)
    assert session.calls[0]["url"] == (
        "https://api.ashbyhq.com/posting-api/job-board/initech"
    )


def test_ashby_asks_for_compensation():
    """Ashby withholds the pay range unless asked, and a published salary is
    the single most useful thing on a digest card after the title."""
    session = ab_session()
    fetch_ashby("initech", session=session)
    assert session.calls[0]["params"] == {"includeCompensation": "true"}


@pytest.mark.parametrize("pasted", [
    "https://jobs.ashbyhq.com/initech",
    "https://jobs.ashbyhq.com/initech/3a1f9c20-1111-4d55-8b77-000000000001",
    "jobs.ashbyhq.com/initech",
    "  initech  ",
])
def test_a_pasted_board_url_is_accepted_as_a_slug(pasted):
    session = ab_session()
    fetch_ashby(pasted, session=session)
    assert session.calls[0]["url"].endswith("/job-board/initech")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_slug_raises_rather_than_hitting_the_network(bad):
    with pytest.raises(ValueError):
        fetch_ashby(bad, session=FakeSession())


# ==========================================================================
# parsing
# ==========================================================================


def test_ashby_parses_the_happy_path():
    job = by_id(fetch_ashby("initech", session=ab_session()))[FIRST]
    assert job.source == "ashby"
    assert job.title == "Senior Python Engineer"
    assert job.company == "Initech"
    assert job.url == f"https://jobs.ashbyhq.com/initech/{FIRST}"
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_ashby_never_claims_an_auto_appliable_ats():
    """`autoapply` only knows how to bail safely out of Greenhouse and Lever
    forms. Claiming either here would drive the bot into a form nothing has
    tested it against."""
    from src.apply.autoapply import SUPPORTED_ATS, detect_ats

    for job in fetch_ashby("initech", session=ab_session()):
        assert job.ats == "ashby"
        assert job.ats not in SUPPORTED_ATS
        assert detect_ats(job.url) is None


def test_ashby_merges_secondary_locations():
    """A role open in Valencia *and* Berlin is one job you may take from
    either. `filters.passes_location` gates on `geo.countries_of(location)` and
    can only see this one string, so pinning it to the primary city loses the
    German half of the posting with no trace anywhere."""
    from src import geo

    job = by_id(fetch_ashby("initech", session=ab_session()))[FIRST]
    assert "Valencia" in job.location
    assert "Madrid" in job.location
    assert "Berlin" in job.location
    assert set(geo.countries_of(job.location)) >= {"ES", "DE"}


def test_ashby_reads_secondary_locations_given_as_plain_strings():
    """The current API sends objects; an earlier one sent bare strings. Reading
    only one shape silently drops every extra city on the other."""
    payload = {"jobs": [{"id": "1", "title": "Engineer", "location": "Valencia",
                         "secondaryLocations": ["Milan", "Lisbon"],
                         "jobUrl": "https://x/1"}]}
    job = fetch_ashby("initech", session=ab_session(payload))[0]
    assert "Milan" in job.location and "Lisbon" in job.location


def test_ashby_appends_the_structured_country_to_a_bare_city():
    """Ashby's `location` is very often just a city, and the geo city table
    only covers the larger hubs. `address.postalAddress.addressCountry` is the
    cheapest possible rescue for a role in a town the table has never heard
    of — and it is only safe for a single-location posting, where the country
    unambiguously belongs to the one city named."""
    payload = {"jobs": [{"id": "1", "title": "Engineer", "location": "Vigo",
                         "secondaryLocations": [],
                         "address": {"postalAddress": {"addressCountry": "Spain"}},
                         "jobUrl": "https://x/1"}]}
    job = fetch_ashby("initech", session=ab_session(payload))[0]
    assert job.location == "Vigo, Spain"


def test_ashby_does_not_append_a_country_to_a_multi_city_posting():
    """The address belongs to the primary office only. Appending "Spain" to
    "Valencia; Berlin" would claim a Spanish role the posting never offered."""
    job = by_id(fetch_ashby("initech", session=ab_session()))[FIRST]
    assert not job.location.endswith("Spain")


def test_ashby_does_not_repeat_a_country_the_location_already_names():
    payload = {"jobs": [{"id": "1", "title": "Engineer",
                         "location": "Valencia, Spain", "secondaryLocations": [],
                         "address": {"postalAddress": {"addressCountry": "Spain"}},
                         "jobUrl": "https://x/1"}]}
    assert fetch_ashby("initech", session=ab_session(payload))[0].location == \
        "Valencia, Spain"


def test_ashby_skips_an_unlisted_posting():
    """`isListed: false` is a draft or an internal-only req. It has a working
    URL, so surfacing it costs a scoring call and sends the user to a page
    that does not exist for them."""
    assert UNLISTED not in by_id(fetch_ashby("initech", session=ab_session()))


def test_ashby_keeps_a_posting_that_simply_omits_is_listed():
    """A missing field is not the same statement as `isListed: false`. Reading
    it as one would empty every board the moment Ashby stops sending it."""
    payload = {"jobs": [{"id": "1", "title": "Engineer", "jobUrl": "https://x/1"}]}
    assert len(fetch_ashby("initech", session=ab_session(payload))) == 1


def test_ashby_prefers_description_plain_and_falls_back_to_html():
    """`descriptionPlain` is empty on plenty of real postings; without the HTML
    fallback the scorer would judge those on their title alone."""
    jobs = by_id(fetch_ashby("initech", session=ab_session()))
    assert "Kafka consumers" in jobs[FIRST].description
    assert "Terraform" in jobs[REMOTE].description
    assert "<strong>" not in jobs[REMOTE].description


def test_ashby_keeps_the_whole_ad_and_not_just_its_opening_lines():
    """The assertions above all land inside the first eighty characters, so a
    parser that kept only an opening fragment would satisfy every one of them.

    That is not a hypothetical shape of bug: it is the failure the whole
    description-assembly effort exists to prevent, and its three siblings
    (Workable, SmartRecruiters, Personio) already assert across every block.
    The requirements — "6+ years", the work authorisation — and the benefits —
    relocation — are the parts that actually decide a score, and they are at
    the *end* of an ad, which is exactly where a truncation lands."""
    jobs = by_id(fetch_ashby("initech", session=ab_session()))

    plain = jobs[FIRST].description
    assert "Own our ingestion services" in plain          # the opening
    assert "edge collectors to the warehouse" in plain    # the middle
    assert "6+ years of production Python" in plain       # the requirements
    assert "EU work authorisation" in plain
    assert "Relocation support to Valencia" in plain      # the closing benefits
    assert len(plain) > 400

    html = jobs[REMOTE].description
    assert "Keep the fleet alive" in html                 # the opening
    assert "error budget" in html                         # the middle
    assert "5+ years running production Linux" in html    # the requirements
    assert "relocation anywhere in the EU" in html        # the closing benefits
    assert len(html) > 300


def test_ashby_scores_the_real_ad_and_never_the_social_teaser():
    """`descriptionSocial` is the one-line blurb Ashby renders into an OG card.

    It is plausible, it is present, and it is a *teaser* — substituting it
    would hand the model a sentence of marketing to score a job on, and the
    result would look entirely reasonable: a number, no error, no empty field,
    nothing in the digest to suggest anything went wrong."""
    jobs = by_id(fetch_ashby("initech", session=ab_session()))
    for job in (jobs[FIRST], jobs[REMOTE]):
        assert "Apply now" not in job.description
        assert not job.description.startswith("Initech is hiring")


def test_ashby_marks_remote_positively_only():
    """`remote=False` would wrongly narrow the location filter, and a "Hybrid"
    workplace type is not the same claim as "not remote at all"."""
    jobs = by_id(fetch_ashby("initech", session=ab_session()))
    assert jobs[REMOTE].remote is True
    assert jobs[FIRST].remote is None
    assert all(j.remote is not False for j in jobs.values())


def test_ashby_null_location_is_empty_not_a_crash():
    assert by_id(fetch_ashby("initech", session=ab_session()))[UNDATED].location == ""


def test_ashby_undated_posting_yields_none_not_a_guess():
    """`freshness.skip_undated` decides what happens to a dateless posting.
    Inventing "now" here would defeat that setting silently."""
    assert by_id(fetch_ashby("initech", session=ab_session()))[UNDATED].posted_at is None


def test_ashby_ignores_updated_at_as_a_publication_date():
    """`updatedAt` moves on any edit, so it overstates freshness badly — a typo
    fix on a three-month-old req would look brand new. Only `publishedAt` is a
    publication date, and no date is more honest than an inflated one."""
    payload = {"jobs": [{"id": "1", "title": "Engineer", "jobUrl": "https://x/1",
                         "updatedAt": "2026-08-04T08:00:00.000+00:00"}]}
    assert fetch_ashby("initech", session=ab_session(payload))[0].posted_at is None


def test_ashby_skips_a_titleless_posting_without_dropping_the_board():
    jobs = fetch_ashby("initech", session=ab_session())
    assert TITLELESS not in by_id(jobs)
    assert len(jobs) == 4


def test_ashby_reads_a_published_pay_range():
    assert by_id(fetch_ashby("initech", session=ab_session()))[FIRST].salary == "€70K – €90K"


def test_ashby_salary_is_none_when_compensation_is_empty():
    assert by_id(fetch_ashby("initech", session=ab_session()))[REMOTE].salary is None


def test_ashby_records_the_employment_type_where_the_filter_reads_it():
    """`filters.employment_type_exclude` only ever reads the structured field.
    An Ashby posting titled plainly "Software Engineer" with
    `employmentType: Intern` passes every title rule ever written."""
    from src.filters import EMPLOYMENT_TYPE_KEYS, apply_filters
    from src.config import DEFAULTS

    job = by_id(fetch_ashby("initech", session=ab_session()))[INTERN]
    assert job.raw["employment_type"] == "Intern"
    assert any(k in job.raw for k in EMPLOYMENT_TYPE_KEYS)

    job.title = "Software Engineer"   # the title now says nothing at all
    result = apply_filters([job], {"filters": DEFAULTS["filters"],
                                   "freshness": {"max_age_hours": 100000}})
    assert result.counts.get("employment_type_excluded") == 1


def test_ashby_reconstructs_a_missing_url_from_the_id():
    payload = {"jobs": [{"id": "abc", "title": "Engineer"}]}
    job = fetch_ashby("initech", session=ab_session(payload))[0]
    assert job.url == "https://jobs.ashbyhq.com/initech/abc"


def test_ashby_falls_back_from_job_url_to_apply_url():
    payload = {"jobs": [{"id": "abc", "title": "Engineer",
                         "applyUrl": "https://jobs.ashbyhq.com/initech/abc/application"}]}
    assert fetch_ashby("initech", session=ab_session(payload))[0].url.endswith(
        "/application"
    )


def test_ashby_drops_a_posting_with_neither_url_nor_id():
    assert fetch_ashby("initech", session=ab_session({"jobs": [{"title": "Engineer"}]})) == []


def test_ashby_tolerates_junk_entries_in_the_jobs_list():
    payload = {"jobs": ["nope", None, 7,
                        {"id": "1", "title": "Engineer", "jobUrl": "https://x/1"}]}
    assert len(fetch_ashby("initech", session=ab_session(payload))) == 1


def test_ashby_refuses_a_200_whose_body_is_not_a_board():
    """A 200 without a `jobs` list is an error envelope, not an empty board —
    parsing it as zero postings turns a broken slug into a company that reads
    as never hiring. `{"apiVersion": "1"}` alone is refused too: the version
    marker without the list the parser feeds on proves the endpoint spoke,
    not that it published a board."""
    with pytest.raises(ValueError, match="not an? ashby board"):
        fetch_ashby("initech", session=ab_session({"error": "not found"}))
    with pytest.raises(ValueError, match="not an? ashby board"):
        fetch_ashby("initech", session=ab_session({"apiVersion": "1"}))
    with pytest.raises(ValueError, match="not an? ashby board"):
        fetch_ashby("initech", session=ab_session([]))
    assert fetch_ashby("initech", session=ab_session({"jobs": []})) == []


def test_ashby_survives_junk_inside_secondary_locations():
    """One malformed entry in a list must cost that entry, not the posting."""
    payload = {"jobs": [{"id": "1", "title": "Engineer", "location": "Valencia",
                         "secondaryLocations": [None, 42, {"nope": "x"},
                                                {"location": "Milan"}],
                         "jobUrl": "https://x/1"}]}
    job = fetch_ashby("initech", session=ab_session(payload))[0]
    assert "Valencia" in job.location and "Milan" in job.location


def test_ashby_records_provenance_in_raw():
    job = by_id(fetch_ashby("initech", session=ab_session()))[FIRST]
    assert job.raw["board"] == "ashby"
    assert job.raw["slug"] == "initech"
    assert job.raw["team"] == "Platform"
    assert job.raw["secondary_locations"] == ["Madrid", "Berlin"]


# ==========================================================================
# failure isolation
# ==========================================================================


def test_check_slug_ok():
    ok, message = check_slug("ashby", "initech", session=ab_session())
    assert ok is True
    assert "4 postings" in message


def test_check_slug_explains_a_404():
    session = FakeSession([("api.ashbyhq.com", FakeResponse(status_code=404))])
    ok, message = check_slug("ashby", "nope", session=session)
    assert ok is False
    assert "404" in message and "slug not found" in message


def test_one_dead_ashby_board_does_not_kill_the_others(tmp_path: Path):
    """The whole point of the per-slug try/except: a renamed board costs that
    company's postings and nothing else."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "ashby": True}},
                       watchlist={"ashby": ["dead", "initech"]})
    session = FakeSession([
        ("job-board/dead", FakeResponse(status_code=404)),
        ("job-board/initech", json_response(ASHBY)),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert len(jobs) == 4
    assert len(errors) == 1
    assert "ashby/dead" in errors[0] and "404" in errors[0]


def test_fetch_never_raises_even_when_every_ashby_board_is_gone(tmp_path: Path):
    """404 rather than a transport error: `util.http_get` retries transport
    failures three times with real backoff, and the transport path is already
    covered once in `test_ats_boards.py` — repeating it per vendor would buy
    nothing and add ten seconds to an offline suite."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "ashby": True}},
                       watchlist={"ashby": ["a", "b"]})
    session = FakeSession(default=FakeResponse(status_code=404))
    errors: list[str] = []
    assert fetch(cfg, session=session, errors=errors) == []
    assert len(errors) == 2
