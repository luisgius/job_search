"""Tests for src/sources/ats_boards.py — the Greenhouse + Lever pulls.

Driven by `tests/fixtures/{greenhouse_jobs,lever_postings}.json`, which are
structurally faithful to the real payloads, deliberately including the awkward
cases: a null location, a null date, a titleless posting, and Greenhouse's
double-escaped `content`.

The recurring theme is *degradation*: one broken posting costs one posting,
one broken board costs one company, and neither ever costs the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources import ats_boards
from src.sources.ats_boards import (
    check_slug,
    company_from_slug,
    fetch,
    fetch_greenhouse,
    fetch_lever,
    main,
)
from src.util import HttpError
from tests.conftest import (
    FakeResponse,
    FakeSession,
    json_response,
    load_json_fixture,
    write_config,
)

UTC = timezone.utc
GREENHOUSE = load_json_fixture("greenhouse_jobs.json")
LEVER = load_json_fixture("lever_postings.json")


def gh_session(payload=None, **kwargs):
    return FakeSession([("boards-api.greenhouse.io",
                         json_response(GREENHOUSE if payload is None else payload))],
                       **kwargs)


def lever_session(payload=None, **kwargs):
    return FakeSession([("api.lever.co",
                         json_response(LEVER if payload is None else payload))],
                       **kwargs)


# ==========================================================================
# slug handling
# ==========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("spotify", "Spotify"),
        ("acme-corp", "Acme Corp"),
        ("acme_corp", "Acme Corp"),
        ("gitlab", "Gitlab"),
    ],
)
def test_company_from_slug(raw, expected):
    assert company_from_slug(raw) == expected


@pytest.mark.parametrize(
    "pasted",
    [
        "https://boards.greenhouse.io/spotify",
        "https://boards.greenhouse.io/spotify/jobs/123",
        "boards.greenhouse.io/spotify",
        "https://jobs.lever.co/spotify",
        "  spotify  ",
        "spotify/",
    ],
)
def test_a_pasted_board_url_is_accepted_as_a_slug(pasted):
    """People copy the URL out of the browser far more often than they type
    the slug, and a wrong slug looks exactly like an empty board."""
    session = gh_session()
    fetch_greenhouse(pasted, session=session)
    assert session.calls[0]["url"].endswith("/boards/spotify/jobs")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_slug_raises_rather_than_hitting_the_network(bad):
    with pytest.raises(ValueError):
        fetch_greenhouse(bad, session=FakeSession())
    with pytest.raises(ValueError):
        fetch_lever(bad, session=FakeSession())


@pytest.mark.parametrize(
    "pasted,expected",
    [
        ("https://boards.greenhouse.io/spotify", "spotify"),
        ("boards.greenhouse.io/spotify/jobs/1", "spotify"),
        ("jobs.lever.co/plaid", "plaid"),
        ("apply.workable.com/contoso/j/ABC/", "contoso"),
        ("jobs.ashbyhq.com/initech/uuid", "initech"),
        ("jobs.smartrecruiters.com/Umbrella/74399", "Umbrella"),
        ("https://apply.workable.com/contoso?lang=en", "contoso"),
        ("www.example.com/acme", "acme"),
        # A bare hostname is a slug, not a URL: nothing follows it to keep.
        ("booking.com", "booking.com"),
        ("acme-corp", "acme-corp"),
    ],
)
def test_the_host_is_recognised_by_shape_not_by_a_list_of_domains(pasted, expected):
    """The host-stripping rule used to be a literal check for `.io/` and
    `.co/` — Greenhouse and Lever, and nothing else. A pasted
    `apply.workable.com/contoso` fell straight through it and was requested
    verbatim as a slug, producing a 404 that reads exactly like a dead company.
    """
    from src.sources.ats_boards import _clean_slug

    assert _clean_slug(pasted) == expected


@pytest.mark.parametrize(
    "pasted,expected",
    [
        # The whole invariant in one line: a host has a dot in it. Without
        # that, "contoso" is a host, gets dropped, and the slug becomes "j".
        ("contoso/j/ABC123/", "contoso"),
        ("spotify/jobs", "spotify"),
        ("spotify/jobs/1", "spotify"),
        ("Umbrella/743999900000001", "Umbrella"),
        ("initech/uuid", "initech"),
        ("acme-corp/", "acme-corp"),
        ("acme_corp/jobs", "acme_corp"),
    ],
)
def test_a_first_segment_with_no_dot_in_it_is_the_slug_not_a_host(pasted, expected):
    """`_HOST_LIKE_RE` requires **two or more** dot-separated labels, and this
    is the test that says so.

    Relax `(?:\\.[\\w-]+)+$` to `*$` — one character — and a single bare label
    counts as a host, so the first path segment is thrown away and the *second*
    becomes the slug: `contoso/j/ABC123/` yields `"j"` and `spotify/jobs`
    yields `"jobs"`. Both are perfectly plausible-looking slugs, both 404, and
    a 404 on this path is indistinguishable from a company that closed its
    board. Nothing asserted the dot, and this helper is shared by Greenhouse
    and Lever — the two vendors the apply leg is allowed to act on.
    """
    from src.sources.ats_boards import _clean_slug

    assert _clean_slug(pasted) == expected


@pytest.mark.parametrize("host_shaped", [
    "boards.greenhouse.io",
    "www.example.com",
    "apply.workable.com",
    "acme.jobs.personio.de",
])
def test_a_first_segment_with_a_dot_in_it_is_dropped_as_a_host(host_shaped):
    """The other half of the pair: something *does* follow it, so it is a URL."""
    from src.sources.ats_boards import _clean_slug

    assert _clean_slug(f"{host_shaped}/acme-corp") == "acme-corp"


@pytest.mark.parametrize("pasted", [
    "https://boards.greenhouse.io/embed/job_board?for=spotify",
    "boards.greenhouse.io/embed/job_board?for=spotify",
    "https://boards.greenhouse.io/embed/job_board/js?for=spotify&b=1",
    "https://boards.greenhouse.io/embed/job_app?for=spotify&token=1",
])
def test_the_greenhouse_embed_url_keeps_its_slug(pasted):
    """The embed URL is what a great many careers pages link to, so it is what
    gets pasted — and it is the one shape where the slug is in the query string
    rather than the path. Dropping the query left the literal segment `embed`
    as the slug: a 404, and a 404 here is indistinguishable from a company that
    closed its board."""
    from src.sources.ats_boards import _clean_slug

    assert _clean_slug(pasted) == "spotify"


@pytest.mark.parametrize("pasted,expected", [
    ("https://apply.workable.com/contoso?lang=en", "contoso"),
    ("https://boards.greenhouse.io/spotify?token=1", "spotify"),
    ("https://jobs.lever.co/plaid/uuid?lever-origin=applied", "plaid"),
])
def test_an_ordinary_query_string_is_still_just_discarded(pasted, expected):
    """The rescue above must stay narrow: only `?for=` on the embed
    scaffolding. Any other query string is noise and the path is the truth."""
    from src.sources.ats_boards import _clean_slug

    assert _clean_slug(pasted) == expected


# ==========================================================================
# Greenhouse
# ==========================================================================


def test_greenhouse_requests_the_content_flag():
    session = gh_session()
    fetch_greenhouse("acme", session=session)
    assert session.calls[0]["params"] == {"content": "true"}


def test_greenhouse_can_skip_content_for_a_cheap_check():
    session = gh_session()
    fetch_greenhouse("acme", session=session, content=False)
    assert not session.calls[0]["params"]


def test_greenhouse_parses_the_happy_path():
    jobs = fetch_greenhouse("acme", session=gh_session())
    job = jobs[0]
    assert job.source == "greenhouse"
    assert job.ats == "greenhouse"
    assert job.ats_job_id == "4012345"
    assert job.company == "Acme"
    assert job.title == "Senior Backend Engineer (m/f/d)"
    assert job.location == "Berlin, Germany"
    assert job.url == "https://boards.greenhouse.io/acme/jobs/4012345"


def test_greenhouse_unescapes_content_exactly_once():
    """`content` is entity-escaped HTML. Unescaping zero times leaves `&lt;p&gt;`
    in the description; twice would corrupt a literal `&amp;lt;` in an ad."""
    job = fetch_greenhouse("acme", session=gh_session())[0]
    assert "&lt;" not in job.description
    assert "<p>" not in job.description
    assert "Senior Backend Engineer" in job.description
    assert "PostgreSQL schema design" in job.description
    # The requirements list must survive as separate lines, not one run-on blob.
    assert "5+ years backend experience" in job.description.splitlines()


def test_greenhouse_prefers_first_published_over_updated_at():
    """`updated_at` moves on any edit, so it overstates freshness badly — a
    typo fix on a three-month-old req would look brand new."""
    jobs = {j.ats_job_id: j for j in fetch_greenhouse("acme", session=gh_session())}
    stale = jobs["4012346"]
    assert stale.posted_at == datetime(2026, 7, 20, 13, 0, tzinfo=UTC)  # first_published
    assert stale.posted_at != datetime(2026, 8, 4, 12, 0, tzinfo=UTC)   # updated_at


def test_greenhouse_falls_back_to_updated_at():
    payload = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1",
                         "updated_at": "2026-08-04T07:30:00-04:00",
                         "location": {"name": "Berlin"}, "content": ""}]}
    job = fetch_greenhouse("acme", session=gh_session(payload))[0]
    assert job.posted_at == datetime(2026, 8, 4, 11, 30, tzinfo=UTC)


def test_greenhouse_undated_posting_yields_none_not_a_guess():
    jobs = {j.ats_job_id: j for j in fetch_greenhouse("acme", session=gh_session())}
    assert jobs["4012348"].posted_at is None


def test_greenhouse_null_location_falls_back_to_offices():
    payload = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1",
                         "location": None, "content": "",
                         "offices": [{"name": "Berlin", "location": "Berlin, Germany"}]}]}
    assert fetch_greenhouse("acme", session=gh_session(payload))[0].location == "Berlin, Germany"


def test_greenhouse_null_location_and_no_offices_is_empty_not_a_crash():
    jobs = {j.ats_job_id: j for j in fetch_greenhouse("acme", session=gh_session())}
    assert jobs["4012348"].location == ""


def test_greenhouse_skips_a_titleless_posting_without_dropping_the_board():
    jobs = fetch_greenhouse("acme", session=gh_session())
    assert "4012349" not in {j.ats_job_id for j in jobs}
    assert len(jobs) == 4  # the other four survived


def test_greenhouse_reconstructs_a_missing_url_from_the_id():
    payload = {"jobs": [{"id": 77, "title": "Engineer", "content": "",
                         "location": {"name": "Berlin"}}]}
    job = fetch_greenhouse("acme", session=gh_session(payload))[0]
    assert job.url == "https://boards.greenhouse.io/acme/jobs/77"


def test_greenhouse_drops_a_posting_with_neither_url_nor_id():
    payload = {"jobs": [{"title": "Engineer", "content": ""}]}
    assert fetch_greenhouse("acme", session=gh_session(payload)) == []


def test_greenhouse_tolerates_junk_entries_in_the_jobs_list():
    payload = {"jobs": ["not-an-object", None, 42,
                        {"id": 1, "title": "Engineer", "absolute_url": "https://x/1",
                         "content": "", "location": {"name": "Berlin"}}]}
    assert len(fetch_greenhouse("acme", session=gh_session(payload))) == 1


def test_greenhouse_refuses_a_200_whose_body_is_not_a_board():
    """A 200 with valid JSON but no `jobs` list is an error envelope or a login
    wall, not a quiet company. This used to parse as zero postings, which
    manufactured "exists, nothing open" out of `{"error": "not found"}` — for
    the daily run a broken slug that reads as a quiet market forever, and for
    `--discover` fabricated evidence. The JSON twin of Personio's root-tag
    check."""
    with pytest.raises(ValueError, match="not a greenhouse board"):
        fetch_greenhouse("acme", session=gh_session({"error": "not found"}))
    with pytest.raises(ValueError, match="not a greenhouse board"):
        fetch_greenhouse("acme", session=gh_session({"meta": {"total": 0}}))
    with pytest.raises(ValueError, match="not a greenhouse board"):
        fetch_greenhouse("acme", session=gh_session([]))
    # The real empty state still parses as the real empty state.
    assert fetch_greenhouse("acme", session=gh_session({"jobs": []})) == []


def test_greenhouse_marks_remote_positively_only():
    """`remote=False` would wrongly narrow the location filter: the absence of
    a remote marker is not evidence of an onsite role."""
    payload = {"jobs": [
        {"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "content": "",
         "location": {"name": "Remote - Europe"}},
        {"id": 2, "title": "Engineer", "absolute_url": "https://x/2", "content": "",
         "location": {"name": "Berlin, Germany"}},
    ]}
    jobs = fetch_greenhouse("acme", session=gh_session(payload))
    assert jobs[0].remote is True
    assert jobs[1].remote is None


def test_greenhouse_reads_salary_out_of_metadata_when_present():
    payload = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1",
                         "content": "", "location": {"name": "Berlin"},
                         "metadata": [{"name": "Other", "value": "x"},
                                      {"name": "Salary Range", "value": "€70k–€90k"}]}]}
    assert fetch_greenhouse("acme", session=gh_session(payload))[0].salary == "€70k–€90k"


def test_greenhouse_records_provenance_in_raw():
    job = fetch_greenhouse("acme", session=gh_session())[0]
    assert job.raw["board"] == "greenhouse"
    assert job.raw["slug"] == "acme"
    assert job.raw["departments"] == ["Engineering"]


# ==========================================================================
# Lever
# ==========================================================================


def test_lever_parses_the_happy_path():
    job = fetch_lever("globex", session=lever_session())[0]
    assert job.source == "lever"
    assert job.ats == "lever"
    assert job.ats_job_id == "9f2b1c4e-1111-4a2b-9d3e-000000000001"
    assert job.title == "Senior Python Engineer"
    assert job.company == "Globex"
    assert job.url.startswith("https://jobs.lever.co/globex/")


def test_lever_parses_the_millisecond_epoch():
    job = fetch_lever("globex", session=lever_session())[0]
    assert job.posted_at == datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def test_lever_keeps_the_lists_blocks_where_requirements_live():
    """Dropping `lists` would throw away the most scoring-relevant text in the
    ad — the intro alone says almost nothing about fit."""
    job = fetch_lever("globex", session=lever_session())[0]
    assert "ingestion pipeline" in job.description       # descriptionPlain
    assert "Kafka consumers past 100k msg/s" in job.description   # lists[0]
    assert "6+ years of Python" in job.description               # lists[1]
    assert "Requirements" in job.description


def test_lever_merges_all_locations():
    job = fetch_lever("globex", session=lever_session())[0]
    assert "Remote - Europe" in job.location
    assert "Berlin" in job.location


def test_lever_trusts_an_explicit_workplace_type():
    jobs = {j.title: j for j in fetch_lever("globex", session=lever_session())}
    assert jobs["Senior Python Engineer"].remote is True
    assert jobs["Engineering Manager, Payments"].remote is False


def test_lever_remote_is_none_when_unstated():
    job = fetch_lever("globex", session=lever_session())[-1]
    assert job.title == "Site Reliability Engineer"
    assert job.remote is None


def test_lever_undated_posting_yields_none():
    job = fetch_lever("globex", session=lever_session())[-1]
    assert job.posted_at is None


def test_lever_handles_missing_categories_and_empty_description():
    job = fetch_lever("globex", session=lever_session())[-1]
    assert job.location == ""
    assert job.description == ""


def test_lever_falls_back_from_hostedurl_to_applyurl():
    payload = [{"id": "abc", "text": "Engineer",
                "applyUrl": "https://jobs.lever.co/globex/abc/apply",
                "categories": {"location": "Berlin"}}]
    assert fetch_lever("globex", session=lever_session(payload))[0].url.endswith("/apply")


def test_lever_reconstructs_a_missing_url_from_the_id():
    payload = [{"id": "abc", "text": "Engineer", "categories": {}}]
    assert fetch_lever("globex", session=lever_session(payload))[0].url == \
        "https://jobs.lever.co/globex/abc"


def test_lever_drops_a_titleless_posting():
    payload = [{"id": "abc", "categories": {}, "hostedUrl": "https://x/1"}]
    assert fetch_lever("globex", session=lever_session(payload)) == []


def test_lever_tolerates_junk_entries():
    payload = ["nope", None, {"id": "a", "text": "Engineer", "hostedUrl": "https://x/1"}]
    assert len(fetch_lever("globex", session=lever_session(payload))) == 1


def test_lever_records_the_apply_url_in_raw():
    job = fetch_lever("globex", session=lever_session())[0]
    assert job.raw["apply_url"].endswith("/apply")
    assert job.raw["commitment"] == "Full-time"


# ==========================================================================
# fetch() over the watchlist
# ==========================================================================


def test_fetch_pulls_only_enabled_boards(tmp_path: Path):
    cfg = write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": False}},
        watchlist={"greenhouse": ["acme"], "lever": ["globex"]},
    )
    session = FakeSession([("greenhouse", json_response(GREENHOUSE)),
                           ("lever", json_response(LEVER))])
    jobs = fetch(cfg, session=session)
    assert {j.source for j in jobs} == {"greenhouse"}
    assert not any("lever" in url for url in session.urls())


def test_fetch_covers_both_boards_when_enabled(tmp_path: Path):
    cfg = write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": True}},
        watchlist={"greenhouse": ["acme"], "lever": ["globex"]},
    )
    session = FakeSession([("greenhouse", json_response(GREENHOUSE)),
                           ("lever", json_response(LEVER))])
    assert {j.source for j in fetch(cfg, session=session)} == {"greenhouse", "lever"}


def test_one_dead_board_does_not_kill_the_others(tmp_path: Path):
    """The whole point of the per-slug try/except: a renamed board costs that
    company's postings and nothing else."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": ["dead", "acme"]})
    session = FakeSession([
        ("boards/dead/", FakeResponse(status_code=404)),
        ("boards/acme/", json_response(GREENHOUSE)),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert len(jobs) == 4
    assert len(errors) == 1
    assert "greenhouse/dead" in errors[0]
    assert "404" in errors[0]


def test_fetch_never_raises_even_when_everything_fails(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": ["a", "b"]})
    session = FakeSession(default=ConnectionError("network is down"))
    errors: list[str] = []
    assert fetch(cfg, session=session, errors=errors) == []
    assert len(errors) == 2


def test_fetch_with_an_empty_watchlist_is_a_no_op(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": []})
    session = FakeSession()
    assert fetch(cfg, session=session) == []
    assert session.calls == []


@pytest.mark.parametrize(
    "watchlist_value",
    [
        ["acme"],
        "acme",
        [{"slug": "acme"}],
        {"acme": "Acme Corporation"},
    ],
)
def test_watchlist_accepts_the_shapes_people_actually_write(tmp_path: Path, watchlist_value):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": watchlist_value})
    jobs = fetch(cfg, session=gh_session())
    assert len(jobs) == 4


def test_company_override_from_the_watchlist_wins(tmp_path: Path):
    cfg = write_config(
        tmp_path, {"sources": {"greenhouse": True}},
        watchlist={"greenhouse": [{"slug": "acme", "company": "ACME Corporation"}]},
    )
    assert {j.company for j in fetch(cfg, session=gh_session())} == {"ACME Corporation"}


def test_duplicate_slugs_are_fetched_once(tmp_path: Path):
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": ["acme", "ACME", "acme"]})
    session = gh_session()
    fetch(cfg, session=session)
    assert len(session.calls) == 1


def test_fetch_does_not_filter_by_date_or_location(tmp_path: Path):
    """Sources normalise; filters.py decides. Mixing the two would make the
    digest's funnel counts meaningless."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True}},
                       watchlist={"greenhouse": ["acme"]})
    jobs = fetch(cfg, session=gh_session())
    assert any(j.location == "San Francisco, CA" for j in jobs)
    assert any(j.posted_at is None for j in jobs)
    assert all(j.country is None for j in jobs)   # geo stamps this later


def test_all_six_vendors_come_back_from_one_fetch_call(tmp_path: Path):
    """`main._fetch_all` calls `ats_boards.fetch` exactly once and keeps the
    jobs whose `source` is in `BOARD_SOURCES`. If a vendor stamped a `source`
    string nobody else uses, its postings would be fetched over the network and
    then silently discarded — and the per-source counts in the digest, which
    are how a broken board is spotted, would never show it."""
    import re as _re

    from tests.conftest import load_fixture, xml_response

    cfg = write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": True, "workable": True,
                     "ashby": True, "smartrecruiters": True, "personio": True}},
        watchlist={"greenhouse": ["acme"], "lever": ["globex"],
                   "workable": ["contoso"], "ashby": ["initech"],
                   "smartrecruiters": ["Umbrella"], "personio": ["vandelay"]},
    )
    session = FakeSession([
        ("boards-api.greenhouse.io", json_response(GREENHOUSE)),
        ("api.lever.co", json_response(LEVER)),
        ("apply.workable.com", json_response(load_json_fixture("workable_jobs.json"))),
        ("api.ashbyhq.com", json_response(load_json_fixture("ashby_jobs.json"))),
        (_re.compile(r"/postings/\S+"),
         json_response(load_json_fixture("smartrecruiters_posting_detail.json"))),
        ("api.smartrecruiters.com",
         json_response(load_json_fixture("smartrecruiters_postings.json"))),
        ("jobs.personio.de", xml_response(load_fixture("personio_positions.xml"))),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)

    assert errors == []
    assert {j.source for j in jobs} == set(ats_boards.BOARDS)
    from src.main import BOARD_SOURCES

    assert {j.source for j in jobs} <= BOARD_SOURCES


def test_keys_never_collide_across_vendors(tmp_path: Path):
    """`Job.key` is the tracker's primary key and is now the ATS id *alone*.
    Two vendors that happened to issue the same id would merge into one row —
    and a job merged into an `applied` row is a job the user never sees again.
    The vendor name is mixed into the hash, which is what prevents that."""
    import re as _re

    from tests.conftest import load_fixture, xml_response

    cfg = write_config(
        tmp_path,
        {"sources": {"greenhouse": True, "lever": True, "workable": True,
                     "ashby": True, "smartrecruiters": True, "personio": True}},
        watchlist={"greenhouse": ["acme"], "lever": ["globex"],
                   "workable": ["contoso"], "ashby": ["initech"],
                   "smartrecruiters": ["Umbrella"], "personio": ["vandelay"]},
    )
    session = FakeSession([
        ("boards-api.greenhouse.io", json_response(GREENHOUSE)),
        ("api.lever.co", json_response(LEVER)),
        ("apply.workable.com", json_response(load_json_fixture("workable_jobs.json"))),
        ("api.ashbyhq.com", json_response(load_json_fixture("ashby_jobs.json"))),
        (_re.compile(r"/postings/\S+"),
         json_response(load_json_fixture("smartrecruiters_posting_detail.json"))),
        ("api.smartrecruiters.com",
         json_response(load_json_fixture("smartrecruiters_postings.json"))),
        ("jobs.personio.de", xml_response(load_fixture("personio_positions.xml"))),
    ])
    jobs = fetch(cfg, session=session)
    keys = [j.key for j in jobs]
    assert len(set(keys)) == len(keys)

    # Same id, different vendor, must not be the same job.
    from tests.conftest import make_job

    a = make_job(source="ashby", ats="ashby", ats_job_id="12345")
    b = make_job(source="personio", ats="personio", ats_job_id="12345")
    assert a.key != b.key


def test_no_board_claims_an_ats_the_apply_stage_would_act_on(tmp_path: Path):
    """The safety property, stated once for every vendor at once.

    `autoapply` has only ever been hardened against Greenhouse and Lever forms.
    A new board that claimed `ats="greenhouse"` — or served a greenhouse.io
    URL — would put the form-filler in front of markup no test has ever seen,
    and the failure mode there is a wrong application sent under the user's
    name."""
    from src.apply.autoapply import SUPPORTED_ATS, detect_ats

    import re as _re

    from tests.conftest import load_fixture, xml_response

    cfg = write_config(
        tmp_path,
        {"sources": {"greenhouse": False, "lever": False, "workable": True,
                     "ashby": True, "smartrecruiters": True, "personio": True}},
        watchlist={"workable": ["contoso"], "ashby": ["initech"],
                   "smartrecruiters": ["Umbrella"], "personio": ["vandelay"]},
    )
    session = FakeSession([
        ("apply.workable.com", json_response(load_json_fixture("workable_jobs.json"))),
        ("api.ashbyhq.com", json_response(load_json_fixture("ashby_jobs.json"))),
        (_re.compile(r"/postings/\S+"),
         json_response(load_json_fixture("smartrecruiters_posting_detail.json"))),
        ("api.smartrecruiters.com",
         json_response(load_json_fixture("smartrecruiters_postings.json"))),
        ("jobs.personio.de", xml_response(load_fixture("personio_positions.xml"))),
    ])
    jobs = fetch(cfg, session=session)
    assert jobs
    for job in jobs:
        assert job.ats not in SUPPORTED_ATS, f"{job.ats} claims to be auto-appliable"
        assert detect_ats(job.url) is None, f"{job.url} would be auto-applied to"


# ==========================================================================
# check_slug
# ==========================================================================


def test_check_slug_ok():
    ok, message = check_slug("greenhouse", "acme", session=gh_session())
    assert ok is True
    assert "4 postings" in message


def test_check_slug_reports_a_reachable_but_empty_board_as_ok():
    ok, message = check_slug("greenhouse", "acme", session=gh_session({"jobs": []}))
    assert ok is True
    assert "0 postings" in message


@pytest.mark.parametrize(
    "status,expected",
    [(404, "slug not found"), (403, "refused"), (401, "authentication")],
)
def test_check_slug_explains_the_failure(status, expected):
    session = FakeSession([("greenhouse", FakeResponse(status_code=status))])
    ok, message = check_slug("greenhouse", "nope", session=session)
    assert ok is False
    assert expected in message
    assert str(status) in message


def test_check_slug_rejects_an_unknown_board():
    ok, message = check_slug("workday", "acme", session=FakeSession())
    assert ok is False
    assert "unknown board" in message


def test_check_slug_rejects_an_empty_slug():
    ok, message = check_slug("greenhouse", "", session=FakeSession())
    assert ok is False
    assert "empty slug" in message


def test_check_slug_never_raises_on_a_transport_error():
    session = FakeSession(default=ConnectionError("dns failure"))
    ok, message = check_slug("greenhouse", "acme", session=session)
    assert ok is False
    assert message


# ==========================================================================
# CLI
# ==========================================================================


@pytest.fixture
def stub_boards(monkeypatch):
    """Intercept the network for CLI tests, which cannot pass a session."""
    calls: list[tuple[str, str]] = []
    results: dict[tuple[str, str], object] = {}

    def _fake(board, slug, *, session=None, **kwargs):
        calls.append((board, slug))
        outcome = results.get((board, slug), "default")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "default":
            return [object()] * 3
        return outcome

    monkeypatch.setattr(ats_boards, "_fetch_board", _fake)
    _fake.calls = calls          # type: ignore[attr-defined]
    _fake.results = results      # type: ignore[attr-defined]
    return _fake


def test_cli_check_single_slug_prints_ok(stub_boards, capsys):
    assert main(["--check", "greenhouse", "spotify"]) == 0
    out = capsys.readouterr().out
    assert "OK greenhouse/spotify" in out
    assert "3 postings" in out
    assert stub_boards.calls == [("greenhouse", "spotify")]


def test_cli_check_reports_failure_and_exits_nonzero(stub_boards, capsys):
    stub_boards.results[("greenhouse", "nope")] = HttpError("... -> HTTP 404")
    assert main(["--check", "greenhouse", "nope"]) == 1
    out = capsys.readouterr().out
    assert "FAIL greenhouse/nope" in out
    assert "slug not found" in out


def test_cli_check_all_walks_the_watchlist(stub_boards, tmp_path: Path, capsys):
    write_config(tmp_path, {"sources": {"greenhouse": True, "lever": True}},
                 watchlist={"greenhouse": ["acme", "globex"], "lever": ["plaid"]})
    code = main(["--check-all", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 0
    assert set(stub_boards.calls) == {("greenhouse", "acme"), ("greenhouse", "globex"),
                                      ("lever", "plaid")}
    assert "3/3 slugs OK" in capsys.readouterr().out


def test_cli_check_all_walks_every_vendor(stub_boards, tmp_path: Path, capsys):
    """`--check-all` is the one command that answers "are my slugs real?", and
    a vendor missing from `BOARDS` is a vendor whose slugs it silently never
    checks — which is how a board rots for months looking like a quiet
    company."""
    write_config(
        tmp_path,
        watchlist={"greenhouse": ["acme"], "lever": ["plaid"],
                   "workable": ["contoso"], "ashby": ["initech"],
                   "smartrecruiters": ["Umbrella"], "personio": ["vandelay"]},
    )
    code = main(["--check-all", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert code == 0
    assert set(stub_boards.calls) == {
        ("greenhouse", "acme"), ("lever", "plaid"), ("workable", "contoso"),
        ("ashby", "initech"), ("smartrecruiters", "Umbrella"),
        ("personio", "vandelay"),
    }
    assert "6/6 slugs OK" in capsys.readouterr().out


@pytest.mark.parametrize(
    "board,slug",
    [("workable", "contoso"), ("ashby", "initech"),
     ("smartrecruiters", "Umbrella"), ("personio", "vandelay")],
)
def test_cli_check_accepts_every_new_board(stub_boards, capsys, board, slug):
    assert main(["--check", board, slug]) == 0
    assert f"OK {board}/{slug}" in capsys.readouterr().out


def test_cli_check_all_keeps_a_personio_host_intact(stub_boards, tmp_path: Path):
    """The generic slug rule would reduce `acme.jobs.personio.com` to nothing
    usable. `--check` has to test the same string the daily run will fetch, or
    it certifies a slug the pipeline never uses."""
    write_config(tmp_path, watchlist={"personio": ["acme.jobs.personio.com"]})
    main(["--check-all", "--config", str(tmp_path / "config.yaml"),
          "--watchlist", str(tmp_path / "watchlist.yaml")])
    assert ("personio", "acme.jobs.personio.com") in stub_boards.calls


def test_cli_check_all_is_independent_of_sources_being_enabled(stub_boards, tmp_path: Path):
    """You verify a slug *before* switching the source on, so --check-all must
    not silently check nothing."""
    write_config(tmp_path, {"sources": {"greenhouse": False, "lever": False}},
                 watchlist={"greenhouse": ["acme"]})
    assert main(["--check-all", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 0
    assert ("greenhouse", "acme") in stub_boards.calls


def test_cli_check_all_with_nothing_to_check_fails_loudly(stub_boards, tmp_path: Path, capsys):
    write_config(tmp_path, watchlist={"greenhouse": [], "lever": []})
    assert main(["--check-all", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 1
    assert "no board slugs found" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(stub_boards, capsys):
    import json

    main(["--check", "greenhouse", "spotify", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["results"][0] == {
        "board": "greenhouse", "slug": "spotify", "ok": True,
        "count": 3, "message": "3 postings",
    }


def test_cli_with_no_arguments_prints_help_and_fails(capsys):
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_partial_failure_exits_nonzero(stub_boards, tmp_path: Path, capsys):
    stub_boards.results[("greenhouse", "dead")] = HttpError("-> HTTP 404")
    write_config(tmp_path, watchlist={"greenhouse": ["acme", "dead"]})
    assert main(["--check-all", "--config", str(tmp_path / "config.yaml"),
                 "--watchlist", str(tmp_path / "watchlist.yaml")]) == 1
    assert "1/2 slugs OK" in capsys.readouterr().out
