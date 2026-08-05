"""Tests for the Workable pull in src/sources/ats_boards.py.

Workable is where a large share of European mid-size companies post, and its
public widget endpoint is the one used by every Workable-hosted careers page.
Driven by `tests/fixtures/workable_jobs.json`, which is structurally faithful
to that payload and deliberately includes the awkward cases: a null location,
a null date, a titleless posting, a closed requisition, the split
description/requirements/benefits blocks, a posting open in three offices on
two continents, and a req drafted a month before it was published.

The recurring theme, as everywhere in this module: one broken posting costs one
posting, one broken board costs one company, and neither ever costs the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources.ats_boards import check_slug, fetch, fetch_workable
from tests.conftest import (
    FakeResponse,
    FakeSession,
    json_response,
    load_json_fixture,
    write_config,
)

UTC = timezone.utc
WORKABLE = load_json_fixture("workable_jobs.json")


def wk_session(payload=None, **kwargs):
    return FakeSession([("apply.workable.com",
                         json_response(WORKABLE if payload is None else payload))],
                       **kwargs)


def by_id(jobs):
    return {j.ats_job_id: j for j in jobs}


# ==========================================================================
# request shape
# ==========================================================================


def test_workable_hits_the_public_widget_endpoint():
    """The widget account endpoint is the only Workable route that needs no
    API token — every other one would silently 401 on a user with no key."""
    session = wk_session()
    fetch_workable("contoso", session=session)
    assert session.calls[0]["url"] == (
        "https://apply.workable.com/api/v1/widget/accounts/contoso"
    )


def test_workable_requests_the_details_flag():
    """Without `details=true` the payload has no description, requirements or
    benefits at all — the scorer would be judging a job title."""
    session = wk_session()
    fetch_workable("contoso", session=session)
    assert session.calls[0]["params"] == {"details": "true"}


def test_workable_can_skip_details_for_a_cheap_check():
    session = wk_session()
    fetch_workable("contoso", session=session, details=False)
    assert not session.calls[0]["params"]


@pytest.mark.parametrize("pasted", [
    "https://apply.workable.com/contoso/",
    "apply.workable.com/contoso",
    "  contoso  ",
    "contoso/",
])
def test_a_pasted_careers_url_is_accepted_as_a_slug(pasted):
    """People copy the careers URL out of the browser; a wrong slug looks
    exactly like a company that is not hiring."""
    session = wk_session()
    fetch_workable(pasted, session=session)
    assert session.calls[0]["url"].endswith("/accounts/contoso")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_slug_raises_rather_than_hitting_the_network(bad):
    with pytest.raises(ValueError):
        fetch_workable(bad, session=FakeSession())


# ==========================================================================
# parsing
# ==========================================================================


def test_workable_parses_the_happy_path():
    job = by_id(fetch_workable("contoso", session=wk_session()))["A1B2C3D4E5"]
    assert job.source == "workable"
    assert job.title == "Senior Backend Engineer (m/f/d)"
    assert job.url == "https://apply.workable.com/contoso/j/A1B2C3D4E5/"
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_workable_dates_a_posting_by_when_it_was_published_not_drafted():
    """`created_at` is when the requisition *record* was made — the day a
    recruiter opened the draft — and drafting weeks ahead is ordinary practice.
    The fixture's flagship req was drafted on 6 July and went live on 4 August;
    dating it 6 July makes a brand-new posting a month old, so `max_age_hours`
    rejects it and it never reaches the digest. `published_on` is the only one
    of the two fields that answers "is this new?"."""
    job = by_id(fetch_workable("contoso", session=wk_session()))["A1B2C3D4E5"]
    assert job.raw["created_at"] == "2026-07-06T09:15:00Z"      # a month earlier
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_workable_ignores_updated_at_as_a_publication_date():
    """The mirror of `test_ashby_ignores_updated_at_as_a_publication_date`.

    `updated_at` moves on any edit, so a typo fix on a three-month-old req
    would look brand new. It must never outrank `published_on`, and on its own
    it is not a publication date at all — no date is more honest than an
    inflated one, because `freshness.skip_undated` can then decide openly."""
    payload = {"jobs": [
        {"title": "Engineer", "shortcode": "AA", "url": "https://x/1",
         "published_on": "2026-08-04T07:00:00Z",
         "created_at": "2026-07-06T09:15:00Z",
         "updated_at": "2026-08-04T08:30:00Z"},
        {"title": "Engineer", "shortcode": "BB", "url": "https://x/2",
         "updated_at": "2026-08-04T08:30:00Z"},
    ]}
    jobs = by_id(fetch_workable("contoso", session=wk_session(payload)))
    assert jobs["AA"].posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)
    assert jobs["BB"].posted_at is None


def test_workable_still_dates_a_posting_that_only_carries_created_at():
    """`created_at` stays as the last resort rather than being dropped.

    A record cannot be created after it was published, so it can only ever
    *under*state freshness — it is a floor, and a floor beats no date at all
    when `freshness.skip_undated` is on and an undated posting is discarded."""
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "url": "https://x/1", "created_at": "2026-08-04T07:00:00Z"}]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_workable_uses_shortcode_as_the_ats_id_not_the_customer_code():
    """`Job.key` is the ATS id alone. `code` is the customer's own requisition
    reference and they re-use and re-number it; `shortcode` is Workable's and
    is what every public URL is built from, so it is the stable one."""
    job = by_id(fetch_workable("contoso", session=wk_session()))["A1B2C3D4E5"]
    assert job.ats_job_id == "A1B2C3D4E5"
    assert job.raw["code"] == "REQ-201"


def test_workable_never_claims_an_auto_appliable_ats():
    """`autoapply` only knows how to bail safely out of Greenhouse and Lever
    forms. Claiming either here would drive the bot into a form nothing has
    tested it against."""
    from src.apply.autoapply import SUPPORTED_ATS, detect_ats

    for job in fetch_workable("contoso", session=wk_session()):
        assert job.ats == "workable"
        assert job.ats not in SUPPORTED_ATS
        assert detect_ats(job.url) is None


def test_workable_concatenates_description_requirements_and_benefits():
    """All three blocks matter and Workable splits them: `requirements` is
    where the years-of-experience and the stack live, and `benefits` is where
    relocation and visa support is stated. Keeping only `description` would
    score a Valencia relocation offer as if it had never been made."""
    job = by_id(fetch_workable("contoso", session=wk_session()))["A1B2C3D4E5"]
    assert "payment orchestration" in job.description          # description
    assert "6+ years of Python" in job.description             # requirements
    assert "Relocation support to Valencia" in job.description  # benefits
    assert "Requirements" in job.description
    assert "Benefits" in job.description
    # Flattened, not raw HTML — the scorer reads this verbatim.
    assert "<ul>" not in job.description


def test_workable_assembles_a_location_the_geo_filter_can_read():
    """Workable never sends a location sentence, only parts. `Job.location` is
    the entire input to the geo filter, so a bare "Valencia" with the country
    dropped is how a Spanish role becomes unresolvable."""
    from src import geo

    job = by_id(fetch_workable("contoso", session=wk_session()))["A1B2C3D4E5"]
    assert job.location == "Valencia, Comunidad Valenciana, Spain"
    assert geo.country_of(job.location) == "ES"


def test_workable_keeps_every_office_a_posting_is_open_in():
    """The `allLocations`/`secondaryLocations` failure, on the board where it
    bites hardest.

    `Job.location` is the entire input to the geo filter, and US companies list
    their offices home-first. Reading only `location` leaves this posting
    saying "San Francisco, California, United States" — unambiguously American,
    vetoed, deleted — when it is equally open in Valencia and Berlin. That is
    the exact case `_MAX_LOCATION_CHARS` documents at length."""
    from src import geo

    job = by_id(fetch_workable("contoso", session=wk_session()))["Q1R2S3T4U5"]
    assert job.location == (
        "San Francisco, California, United States; "
        "Valencia, Comunidad Valenciana, Spain; "
        "Berlin, Germany"
    )
    assert set(geo.countries_of(job.location)) >= {"ES", "DE"}


@pytest.mark.parametrize("key", [
    "locations", "secondary_locations", "secondaryLocations",
    "additional_locations", "additionalLocations", "other_locations",
])
def test_workable_reads_every_plausible_spelling_of_the_extra_offices(key):
    """Read defensively, exactly as the country key is read.

    Which spelling the live widget uses has never been checked against a real
    response (`docs/TESTING.md` says so, and `test_live_contract.py` is the
    only thing that can settle it). Asking for a key that is not there costs
    nothing; missing the one that is there deletes the Valencia job."""
    payload = {"jobs": [{
        "title": "Engineer", "shortcode": "AA", "url": "https://x/1",
        "location": {"city": "San Francisco", "region": "California",
                     "countryCode": "US"},
        key: [{"city": "Valencia", "countryCode": "ES"}],
    }]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert "Valencia, ES" in job.location


def test_workable_survives_a_location_field_that_is_itself_a_list():
    """Some payloads put every office in `location` rather than beside it. A
    list where an object was expected must cost nothing at all."""
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "url": "https://x/1",
                         "location": [{"city": "Valencia", "countryCode": "ES"},
                                      {"city": "Berlin", "countryCode": "DE"}]}]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert job.location == "Valencia, ES; Berlin, DE"


def test_workable_reads_both_spellings_of_the_region_key():
    """The same defensive reading the country key gets, for the same reason:
    a US posting whose state is dropped reads as a bare city, and "Berlin" with
    no region and no country is a German role to the geo table."""
    payload = {"name": "X", "jobs": [
        {"title": "Engineer", "shortcode": "AA", "url": "https://x/1",
         "location": {"city": "Austin", "region_code": "TX", "countryCode": "US"}},
        {"title": "Engineer", "shortcode": "BB", "url": "https://x/2",
         "location": {"city": "Austin", "state_code": "TX", "countryCode": "US"}},
        {"title": "Engineer", "shortcode": "CC", "url": "https://x/3",
         "location": {"city": "Austin", "regionCode": "TX", "countryCode": "US"}},
    ]}
    jobs = by_id(fetch_workable("contoso", session=wk_session(payload)))
    assert all(j.location == "Austin, TX, US" for j in jobs.values())


def test_workable_reads_the_country_name_as_well_as_the_code():
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "url": "https://x/1",
                         "location": {"city": "Porto", "country_name": "Portugal"}}]}
    assert fetch_workable("contoso", session=wk_session(payload))[0].location == \
        "Porto, Portugal"


def test_workable_reads_both_spellings_of_the_country_code():
    """The widget API and the v3 API disagree on the casing of this one field.
    Reading only `countryCode` loses the country on every payload that sends
    `country_code`, and a location with no country is a job the filter drops."""
    payload = {"name": "X", "jobs": [
        {"title": "Engineer", "shortcode": "AA", "url": "https://x/1",
         "location": {"city": "Lisbon", "country_code": "PT"}},
        {"title": "Engineer", "shortcode": "BB", "url": "https://x/2",
         "location": {"city": "Milan", "countryCode": "IT"}},
    ]}
    jobs = by_id(fetch_workable("contoso", session=wk_session(payload)))
    assert jobs["AA"].location == "Lisbon, PT"
    assert jobs["BB"].location == "Milan, IT"


def test_workable_telecommuting_yields_a_location_that_still_says_remote():
    """A fully-remote posting has no city at all. Left empty, `Job.location`
    is an empty string and the location filter cannot tell a remote EU role
    from an unparseable one — so it drops it."""
    job = by_id(fetch_workable("contoso", session=wk_session()))["F6G7H8I9J0"]
    assert job.location == "Remote"
    assert job.remote is True


def test_workable_marks_remote_positively_only():
    """`remote=False` would wrongly narrow the location filter: `telecommuting:
    false` is a statement about the office, not about the whole arrangement,
    and the geo resolver reads the title and description too."""
    jobs = by_id(fetch_workable("contoso", session=wk_session()))
    assert jobs["A1B2C3D4E5"].remote is None
    assert jobs["F6G7H8I9J0"].remote is True
    assert all(j.remote is not False for j in jobs.values())


@pytest.mark.parametrize("posting", [
    {"location": {"city": None, "telecommuting": True}},
    {"location": {"city": None, "workplace_type": "remote"}},
    {"location": {"city": None}, "workplace_type": "remote"},
    {"location": {"city": None}, "workplaceType": "Remote"},
    {"location": {"city": None, "is_remote": True}},
    {"location": {"city": None, "remote": True}},
])
def test_workable_reads_every_spelling_of_the_remote_flag(posting):
    """`telecommuting` at one key only was the gap. If Workable states remote
    as `workplace_type` instead — which is what its own hybrid/on-site/remote
    picker writes — the structured flag is lost and all that survives is the
    English-text heuristic, which a German or Spanish ad will not trip.

    A remote posting with no city then has an *empty* location, and the geo
    filter cannot tell a remote EU role from an unparseable one, so it drops
    it."""
    payload = {"jobs": [dict({"title": "Engineer", "shortcode": "AA",
                              "url": "https://x/1"}, **posting)]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert job.remote is True
    assert job.location == "Remote"


@pytest.mark.parametrize("text", [
    "Homeoffice möglich",      # de — one word, so `home[- ]office` never matched
    "Telearbeit",              # de
    "Teletrabajo",             # es
    "Télétravail",             # fr
    "Smart working",           # it
    "Thuiswerken",             # nl
    "Praca zdalna",            # pl
    "Teletrabalho",            # pt
])
def test_a_remote_marker_is_recognised_in_the_languages_this_tool_searches(text):
    """`_REMOTE_RE` was English-only while the watchlist covered eleven European
    countries. A Munich posting whose office field says "Homeoffice" kept
    `remote=None`, which loses the only structured evidence that a place-less
    ad is remote rather than unresolvable — and unresolvable is dropped."""
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "url": "https://x/1",
                         "location": {"city": text, "country": "Germany"}}]}
    assert fetch_workable("contoso", session=wk_session(payload))[0].remote is True


@pytest.mark.parametrize("text", ["Formación a distancia", "Travail à distance"])
def test_a_bare_prepositional_phrase_is_not_treated_as_a_remote_marker(text):
    """The other half of the pair. `remote=True` only ever *widens* the
    location filter, so a false positive is a US-only role reaching a European
    digest — the one direction this file may not be careless in."""
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "url": "https://x/1",
                         "location": {"city": text, "country": "Spain"}}]}
    assert fetch_workable("contoso", session=wk_session(payload))[0].remote is None


def test_workable_null_location_is_empty_not_a_crash():
    job = by_id(fetch_workable("contoso", session=wk_session()))["K1L2M3N4O5"]
    assert job.location == ""


def test_workable_undated_posting_yields_none_not_a_guess():
    """`freshness.skip_undated` decides what happens to a dateless posting.
    Inventing "now" here would defeat that setting silently and let a
    three-month-old req through as today's news."""
    job = by_id(fetch_workable("contoso", session=wk_session()))["K1L2M3N4O5"]
    assert job.posted_at is None


def test_workable_skips_a_titleless_posting_without_dropping_the_board():
    jobs = fetch_workable("contoso", session=wk_session())
    assert "U1V2W3X4Y5" not in by_id(jobs)
    assert len(jobs) == 5


def test_workable_skips_a_closed_requisition():
    """A closed posting is still in the payload and still has a working URL.
    Surfacing it costs a scoring call and sends the user to a dead form."""
    assert "Z6A7B8C9D0" not in by_id(fetch_workable("contoso", session=wk_session()))


def test_workable_keeps_a_posting_whose_state_it_does_not_recognise():
    """The state check is an allow-list of *closures*, not a requirement that
    the state be "published". A vendor that renames the open state must not
    silently empty every board."""
    payload = {"name": "X", "jobs": [
        {"title": "Engineer", "shortcode": "AA", "state": "live", "url": "https://x/1"},
        {"title": "Engineer", "shortcode": "BB", "url": "https://x/2"},
    ]}
    assert len(fetch_workable("contoso", session=wk_session(payload))) == 2


def test_workable_records_the_employment_type_where_the_filter_reads_it():
    """`filters.employment_type_exclude` only ever reads the structured field,
    never the title — which is the whole point: a plainly-titled internship
    passes every title rule ever written and is caught only here.

    This test used to call `passes_title`, which never looks at
    `employment_type` at all: it was rejecting "Working Student, Support" on
    its *title*, and would have gone on passing with the structured field
    deleted. The title is neutralised below so that only the structured field
    can produce the rejection, the way its three siblings do."""
    from src.config import DEFAULTS
    from src.filters import EMPLOYMENT_TYPE_KEYS, apply_filters

    job = by_id(fetch_workable("contoso", session=wk_session()))["P6Q7R8S9T0"]
    assert job.raw["employment_type"] == "Internship"
    assert any(k in job.raw for k in EMPLOYMENT_TYPE_KEYS)

    job.title = "Support Specialist"      # the title now says nothing at all
    result = apply_filters([job], {"filters": DEFAULTS["filters"],
                                   "freshness": {"max_age_hours": 100000}})
    assert result.counts.get("employment_type_excluded") == 1


def test_workable_prefers_the_accounts_display_name_over_the_slug():
    """Workable is the one board here that publishes a real company name.
    "Contoso Iberia" reads better in the digest than "Contoso" — and much
    better than the slug of a company whose board is named after its founder."""
    jobs = fetch_workable("contoso", session=wk_session())
    assert {j.company for j in jobs} == {"Contoso Iberia"}


def test_workable_falls_back_to_the_slug_when_no_name_is_published():
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA", "url": "https://x/1"}]}
    job = fetch_workable("acme-corp", session=wk_session(payload))[0]
    assert job.company == "Acme Corp"


def test_workable_reconstructs_a_missing_url_from_the_shortcode():
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA"}]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert job.url == "https://apply.workable.com/contoso/j/AA/"


def test_workable_falls_back_through_shortlink_and_application_url():
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "application_url": "https://apply.workable.com/contoso/j/AA/apply/"}]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert job.url.endswith("/apply/")


def test_workable_drops_a_posting_with_neither_url_nor_shortcode():
    payload = {"jobs": [{"title": "Engineer"}]}
    assert fetch_workable("contoso", session=wk_session(payload)) == []


def test_workable_tolerates_junk_entries_in_the_jobs_list():
    """Real payloads have carried nulls after a partial publish. One junk
    entry must cost one posting, not the company."""
    payload = {"jobs": ["not-an-object", None, 42,
                        {"title": "Engineer", "shortcode": "AA", "url": "https://x/1"}]}
    assert len(fetch_workable("contoso", session=wk_session(payload))) == 1


def test_workable_refuses_a_200_whose_body_is_not_a_board():
    """A 200 with no `jobs` list is not a quiet account — it is an error
    envelope, a login wall or the wrong endpoint, and parsing it as zero
    postings makes a broken slug read as a company that never hires. Loud is
    the only honest answer; if a real empty tenant ever omits the empty list,
    the live contract tests are where that shows up, as a loud failure rather
    than a silent zero."""
    with pytest.raises(ValueError, match="not a workable board"):
        fetch_workable("contoso", session=wk_session({"error": "not found"}))
    with pytest.raises(ValueError, match="not a workable board"):
        fetch_workable("contoso", session=wk_session({"name": "X"}))
    with pytest.raises(ValueError, match="not a workable board"):
        fetch_workable("contoso", session=wk_session([]))
    assert fetch_workable("contoso", session=wk_session({"name": "X", "jobs": []})) == []


def test_workable_survives_a_location_that_is_not_an_object():
    """Defensive by design: every field on this payload is untyped, and a
    string where an object was expected must cost the location, not the job."""
    payload = {"jobs": [{"title": "Engineer", "shortcode": "AA",
                         "url": "https://x/1", "location": "Valencia, Spain"}]}
    job = fetch_workable("contoso", session=wk_session(payload))[0]
    assert job.location == ""
    assert job.title == "Engineer"


def test_workable_records_provenance_in_raw():
    job = by_id(fetch_workable("contoso", session=wk_session()))["A1B2C3D4E5"]
    assert job.raw["board"] == "workable"
    assert job.raw["slug"] == "contoso"
    assert job.raw["department"] == "Engineering"
    assert job.raw["apply_url"].endswith("/apply/")


def test_workable_does_not_filter_by_date_or_location():
    """Sources normalise; filters.py decides. Mixing the two would make the
    digest's funnel counts meaningless."""
    jobs = fetch_workable("contoso", session=wk_session())
    assert any(j.posted_at is None for j in jobs)
    assert all(j.country is None for j in jobs)


# ==========================================================================
# failure isolation
# ==========================================================================


def test_a_404_raises_out_of_the_fetcher_so_the_caller_can_report_it():
    session = FakeSession([("apply.workable.com", FakeResponse(status_code=404))])
    with pytest.raises(Exception):
        fetch_workable("nope", session=session)


def test_check_slug_explains_a_404():
    session = FakeSession([("apply.workable.com", FakeResponse(status_code=404))])
    ok, message = check_slug("workable", "nope", session=session)
    assert ok is False
    assert "404" in message and "slug not found" in message


def test_check_slug_ok():
    ok, message = check_slug("workable", "contoso", session=wk_session())
    assert ok is True
    assert "5 postings" in message


def test_one_dead_workable_board_does_not_kill_the_others(tmp_path: Path):
    """The whole point of the per-slug try/except: a renamed board costs that
    company's postings and nothing else."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "workable": True}},
                       watchlist={"workable": ["dead", "contoso"]})
    session = FakeSession([
        ("accounts/dead", FakeResponse(status_code=404)),
        ("accounts/contoso", json_response(WORKABLE)),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert len(jobs) == 5
    assert len(errors) == 1
    assert "workable/dead" in errors[0] and "404" in errors[0]


def test_a_dead_workable_board_does_not_cost_the_greenhouse_boards(tmp_path: Path):
    """Cross-vendor isolation: the six boards share one `fetch()` call, and a
    Workable outage must not take the Greenhouse companies with it."""
    greenhouse = load_json_fixture("greenhouse_jobs.json")
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True, "workable": True}},
                       watchlist={"greenhouse": ["acme"], "workable": ["contoso"]})
    session = FakeSession([
        ("boards-api.greenhouse.io", json_response(greenhouse)),
        ("apply.workable.com", FakeResponse(status_code=500)),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert {j.source for j in jobs} == {"greenhouse"}
    assert len(errors) == 1


def test_fetch_skips_workable_when_the_source_is_off(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "workable": False}},
                       watchlist={"workable": ["contoso"]})
    session = wk_session()
    assert fetch(cfg, session=session) == []
    assert session.calls == []
