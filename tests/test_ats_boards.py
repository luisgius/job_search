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


def test_greenhouse_tolerates_a_payload_with_no_jobs_key():
    assert fetch_greenhouse("acme", session=gh_session({"meta": {"total": 0}})) == []
    assert fetch_greenhouse("acme", session=gh_session([])) == []


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
