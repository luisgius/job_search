"""Tests for src/sources/freehire.py — an aggregator of ATS boards.

freehire.me's public `GET /api/v1/agent/jobs/search` returns pages of hits
whose `url` is the upstream ATS posting. These tests pin what the adapter
must not get wrong: freshness from `created_at` (freehire re-dates
`posted_at`), the envelope check that keeps a reshaped 200 from reading as
an empty market, the request cap, the upstream exclusion list, dedupe
against the direct ATS record, and that a freehire copy is never
auto-applied even when its URL is a real Greenhouse form.

Driven by `tests/fixtures/freehire_search.json`. Its first four hits are
reduced from a live response captured 2026-10-22 (descriptions, skills and
requirement lists trimmed; vote and view counters dropped). The five hits
whose slugs end in `synthNN` are synthetic contract cases in the same shape:
an entity-escaped title, a missing company, an excluded upstream, an onsite
posting and a posting without `created_at`. The `network`-marked test in
`test_live_contract.py` is what pins the live field names.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from src import main as main_module
from src.apply.autoapply import eligible
from src.config import DEFAULTS, WATCHLIST_DEFAULTS, Config
from src.filters import EMPLOYMENT_TYPE_KEYS, SOURCE_RANK, dedupe
from src.sources.freehire import API_URL, fetch, parse_job
from src.util import canonical_url
from tests.conftest import (
    ROOT,
    FakeSession,
    json_response,
    load_json_fixture,
    make_job,
    make_scored,
    write_config,
)

UTC = timezone.utc
PAYLOAD = load_json_fixture("freehire_search.json")


class Settings:
    """The two attributes `fetch` reads; `Config` without the YAML files."""

    def __init__(self, **overrides):
        self.watchlist = {"freehire": {**copy.deepcopy(WATCHLIST_DEFAULTS["freehire"]),
                                       **overrides}}


def fh_session(body=None, **kwargs):
    return FakeSession(
        [("freehire.me", json_response(PAYLOAD if body is None else body))], **kwargs
    )


def one_query(**overrides):
    return Settings(queries=[{"countries": ["pl"]}], **overrides)


def by_slug(jobs):
    return {j.raw["public_slug"]: j for j in jobs}


def hit(slug):
    return next(e for e in PAYLOAD["data"] if e["public_slug"] == slug)


# ==========================================================================
# mapping
# ==========================================================================


def test_a_re_dated_hit_is_dated_by_created_at_never_posted_at():
    """The trap: both Capco hits carry the same re-dated `posted_at` (the
    capture afternoon) while `created_at` is two months apart."""
    jobs = by_slug(fetch(one_query(), session=fh_session()))
    analyst = jobs["mid-data-analyst-capco-hlwgfdts"]
    snowflake = jobs["snowflake-data-engineer-qatar-location-capco-svvacacz"]
    assert analyst.raw["source_posted_at"] == snowflake.raw["source_posted_at"] \
        == "2026-10-22T14:42:15Z"
    assert analyst.posted_at == datetime(2026, 8, 13, 10, 8, 9, tzinfo=UTC)
    assert snowflake.posted_at == datetime(2026, 10, 12, 13, 52, 32, tzinfo=UTC)
    assert analyst.raw["posted_at_source"] == "created_at"


def test_without_created_at_a_hit_stays_undated():
    job = by_slug(fetch(one_query(), session=fh_session()))["data-engineer-undated-synth05"]
    assert job.posted_at is None
    assert job.raw["source_posted_at"] == "2026-10-22T11:00:00Z"


def test_a_hit_without_a_company_is_skipped():
    slugs = set(by_slug(fetch(one_query(), session=fh_session())))
    assert "data-scientist-no-company-synth02" not in slugs
    assert parse_job(hit("data-scientist-no-company-synth02")) is None


def test_html_entities_in_the_title_are_decoded_and_trimmed():
    job = parse_job(hit("intern-fullstack-ai-innovation-example-labs-synth01"))
    assert job.title == "Intern - Fullstack & AI Innovation"
    assert job.description == "Build AI features & ship them."


def test_a_text_description_keeps_its_angle_brackets():
    """`description_format=text` is text, and `html_to_text` read everything
    from "<2 years" to ">20k" as one tag and deleted it. Entities are still
    decoded and spacing tidied; paragraph breaks stay."""
    entry = {**hit("mid-data-analyst-capco-hlwgfdts"),
             "description": "Experience <2 years with Python;  salary >20k PLN.\n\n\n"
                            "  Hybrid &amp; remote\xa0 \n"}
    job = parse_job(entry)
    assert job.description == ("Experience <2 years with Python; salary >20k PLN.\n\n"
                               "Hybrid & remote")


def test_several_countries_leave_country_to_the_location_text():
    job = parse_job(hit("senior-machine-learning-engineer-speech-synthesis-telnyx-urxxwr4u"))
    assert job.country is None
    assert job.raw["location_countries"] == ["IE", "NL", "PL"]
    assert job.location == "Dublin; Amsterdam; Krakow"
    assert parse_job(hit("mid-data-analyst-capco-hlwgfdts")).country == "PL"


def test_the_url_is_the_upstream_posting_and_canonicalises_without_the_tag():
    job = parse_job(hit("mid-data-analyst-capco-hlwgfdts"))
    assert job.url == ("https://job-boards.greenhouse.io/capco/jobs/8111831"
                       "?utm_source=freehire.me")
    assert canonical_url(job.url) == "https://job-boards.greenhouse.io/capco/jobs/8111831"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "ftp://jobs.example.com/1",
    "https://user:pass@jobs.example.com/1",
    "/jobs/relative",
    "",
])
def test_a_hit_whose_url_is_not_a_plain_web_link_is_skipped(url):
    entry = {**hit("mid-data-analyst-capco-hlwgfdts"), "url": url}
    assert parse_job(entry) is None


@pytest.mark.parametrize("slug,mode,expected", [
    ("intern-fullstack-ai-innovation-example-labs-synth01", "remote", True),
    ("ml-engineer-example-bank-synth04", "onsite", False),
    ("mid-data-analyst-capco-hlwgfdts", "hybrid", None),
    ("snowflake-data-engineer-qatar-location-capco-svvacacz", None, None),
])
def test_remote_only_from_an_explicit_work_mode(slug, mode, expected):
    entry = hit(slug)
    assert entry.get("work_mode") == mode
    assert parse_job(entry).remote is expected


def test_the_record_is_not_an_ats_record_and_keeps_its_provenance():
    job = parse_job(hit("mid-data-analyst-capco-hlwgfdts"))
    assert job.source == "freehire"
    assert job.ats is None
    assert job.ats_job_id == "mid-data-analyst-capco-hlwgfdts"
    assert job.raw["upstream_source"] == "greenhouse"
    assert job.raw["external_id"] == "capco:8111831"
    assert job.raw["public_slug"] == "mid-data-analyst-capco-hlwgfdts"


def test_freehire_inferences_are_kept_where_no_hard_filter_reads():
    """freehire guessed "contract" (and "internship" for the intern hit). Were
    those guesses stored where the employer's own field goes, the employment
    type filter would reject postings on a model's inference."""
    job = parse_job(hit("ml-engineer-example-bank-synth04"))
    inferred = job.raw["freehire_enrichment"]
    assert inferred["enrichment"]["employment_type"] == "contract"
    assert inferred["enrichment"]["salary_max"] == 26000
    assert not set(EMPLOYMENT_TYPE_KEYS) & set(job.raw)
    assert "level" not in job.raw
    assert job.salary is None


# ==========================================================================
# fetch: envelope, exclusion, request cap
# ==========================================================================


def test_the_request_names_the_project_asks_for_text_and_never_the_redated_filter():
    session = fh_session()
    fetch(one_query(), session=session)
    call = session.calls[0]
    assert call["url"] == API_URL
    assert "job-hunter" in call["headers"]["User-Agent"]
    assert call["params"]["description_format"] == "text"
    assert call["params"]["countries"] == ["pl"]
    assert (call["params"]["limit"], call["params"]["offset"]) == (50, 0)
    assert "posted_within_days" not in call["params"]


def test_excluded_upstreams_are_dropped_and_the_list_is_configurable():
    kept = by_slug(fetch(one_query(), session=fh_session()))
    assert "data-analyst-whatjobs-synth03" not in kept
    assert len(kept) == 7       # 9 hits - no company - whatjobs-pl
    unfiltered = by_slug(fetch(one_query(exclude_upstream=[]), session=fh_session()))
    assert "data-analyst-whatjobs-synth03" in unfiltered


def test_a_closed_hit_is_not_emitted():
    body = copy.deepcopy(PAYLOAD)
    body["data"][0]["closed_at"] = "2026-10-22T16:00:00Z"
    slugs = set(by_slug(fetch(one_query(), session=fh_session(body))))
    assert "mid-data-analyst-capco-hlwgfdts" not in slugs


@pytest.mark.parametrize("body", [
    [],                                         # a bare list
    {"error": "maintenance"},                   # no data at all
    {"data": {"total": 3, "facets": {}}},       # the facets shape, not a list
    "<html>maintenance</html>",
])
def test_a_reshaped_200_is_reported_as_drift_never_an_empty_market(body):
    errors: list[str] = []
    assert fetch(one_query(), session=fh_session(body), errors=errors) == []
    assert len(errors) == 1
    assert errors[0].startswith("freehire: ")
    assert "changed shape" in errors[0]


def test_a_404_on_the_endpoint_is_an_error():
    errors: list[str] = []
    session = FakeSession([("freehire.me", json_response({"error": "not found"}, 404))])
    assert fetch(one_query(), session=session, errors=errors) == []
    assert len(errors) == 1 and "HTTP 404" in errors[0]
    assert errors[0].startswith("freehire: ")


def test_a_rejected_request_costs_only_that_query():
    errors: list[str] = []

    def answer(url, params):
        if "countries" in params:
            return json_response({"error": "forbidden"}, 403)
        return json_response(PAYLOAD)

    jobs = fetch(Settings(), session=FakeSession([("freehire.me", answer)]),
                 errors=errors)
    assert len(errors) == 1 and "query 1" in errors[0] and "HTTP 403" in errors[0]
    assert jobs, "the second query must still run"


def test_a_valid_empty_result_is_not_an_error():
    errors: list[str] = []
    body = {"data": [], "meta": {"limit": 50, "offset": 0, "total": 0}}
    assert fetch(one_query(), session=fh_session(body), errors=errors) == []
    assert errors == []


def test_a_page_where_every_hit_is_unusable_is_reported_as_drift():
    errors: list[str] = []
    body = {"data": [{"headline": "Data Scientist", "employer": "Acme"}] * 3,
            "meta": {"total": 3}}
    assert fetch(one_query(), session=fh_session(body), errors=errors) == []
    assert len(errors) == 1 and "shape drift" in errors[0]


def full_pages(limit, total=10_000):
    """Every request answers a full page of distinct hits."""
    def answer(url, params):
        offset = params["offset"]
        data = []
        for i in range(offset, offset + limit):
            entry = copy.deepcopy(hit("mid-data-analyst-capco-hlwgfdts"))
            entry["public_slug"] = f"hit-{params.get('regions', 'pl')}-{i}"
            entry["url"] = f"https://job-boards.greenhouse.io/capco/jobs/{i}"
            data.append(entry)
        return json_response({"data": data,
                              "meta": {"limit": limit, "offset": offset, "total": total}})
    return answer


def test_the_request_cap_is_honoured_and_shared_between_queries():
    session = FakeSession([("freehire.me", full_pages(10))])
    errors: list[str] = []
    jobs = fetch(Settings(limit=10, max_requests=5), session=session, errors=errors)
    assert len(session.calls) == 5
    offsets = [(("regions" in c["params"]), c["params"]["offset"]) for c in session.calls]
    # 5 requests, 2 queries: the first gets 5 // 2, the second what is left.
    assert offsets == [(False, 0), (False, 10), (True, 0), (True, 10), (True, 20)]
    assert len(jobs) == 50
    assert errors == [], "a truncated broad facet is the coverage bound, not a fault"


def test_a_query_that_runs_out_of_results_hands_its_share_on():
    def answer(url, params):
        if "regions" in params:
            return full_pages(10)(url, params)
        return json_response(PAYLOAD)      # 9 hits < limit: one request, done
    session = FakeSession([("freehire.me", answer)])
    fetch(Settings(limit=10, max_requests=6), session=session)
    remote_calls = [c for c in session.calls if "regions" in c["params"]]
    assert len(session.calls) == 6 and len(remote_calls) == 5


def test_more_queries_than_requests_reports_the_ones_never_asked():
    errors: list[str] = []
    queries = [{"countries": [code]} for code in ("pl", "de", "es")]
    session = FakeSession([("freehire.me", full_pages(10))])
    fetch(Settings(queries=queries, limit=10, max_requests=2), session=session,
          errors=errors)
    assert len(session.calls) == 2
    assert len(errors) == 1 and "query 3" in errors[0]


def test_paging_stops_at_the_advertised_total():
    session = FakeSession([("freehire.me", full_pages(10, total=25))])
    fetch(one_query(limit=10, max_requests=6), session=session)
    assert [c["params"]["offset"] for c in session.calls] == [0, 10, 20]


def test_a_hit_returned_by_two_queries_is_emitted_once():
    session = fh_session()
    jobs = fetch(Settings(), session=session)
    assert len(session.calls) == 2
    assert len(jobs) == len({j.raw["public_slug"] for j in jobs}) == 7


@pytest.mark.parametrize("key", ["posted_within_days", "limit", "offset",
                                 "description_format"])
def test_a_query_cannot_set_what_the_adapter_owns(key):
    errors: list[str] = []
    session = fh_session()
    fetch(Settings(queries=[{"countries": ["pl"], key: "7"}]), session=session,
          errors=errors)
    sent = session.calls[0]["params"]
    assert sent.get(key) != "7"
    assert any(key in e for e in errors)


def test_without_a_config_the_defaults_are_used():
    session = fh_session()
    fetch(None, session=session)
    sent = [c["params"] for c in session.calls]
    assert sent[0]["countries"] == ["pl"]
    assert sent[1]["work_mode"] == "remote"
    assert sent[1]["regions"] == ["eu", "global"]
    for params in sent:
        assert set(params["category"]) == {"data_engineering", "ai_engineering",
                                           "data_analytics", "data_science", "ml_ai"}


# ==========================================================================
# downstream: dedupe and auto-apply
# ==========================================================================


def greenhouse_copy(**overrides):
    fields = dict(source="greenhouse", ats="greenhouse", ats_job_id="8111831",
                  company="Capco", title="Mid Data Analyst", location="Poland - Cracow",
                  url="https://job-boards.greenhouse.io/capco/jobs/8111831",
                  description="Data Analyst at Capco: SQL, Python, PySpark.",
                  hours_old=2)
    fields.update(overrides)
    return make_job(**fields)


def test_dedupe_keeps_the_greenhouse_record_over_a_longer_freehire_copy():
    aggregated = parse_job(hit("mid-data-analyst-capco-hlwgfdts"))
    direct = greenhouse_copy()
    assert len(aggregated.description) > len(direct.description)
    for batch in ([aggregated, direct], [direct, aggregated]):
        assert dedupe(batch) == [direct]


def test_a_snippet_only_direct_record_loses_to_the_full_freehire_copy():
    aggregated = parse_job(hit("mid-data-analyst-capco-hlwgfdts"))
    snippet = greenhouse_copy(source="nofluffjobs", ats=None,
                              raw={"snippet_only": True})
    for batch in ([aggregated, snippet], [snippet, aggregated]):
        assert dedupe(batch) == [aggregated]


def test_freehire_has_the_lowest_source_rank():
    assert SOURCE_RANK["freehire"] == min(SOURCE_RANK.values())


def test_a_freehire_job_is_never_auto_applied_even_on_a_greenhouse_url(tmp_path):
    cfg = write_config(tmp_path, {"apply": {"enabled": True, "dry_run": True,
                                            "min_score": 0, "require_pdf": False}})
    aggregated = parse_job(hit("mid-data-analyst-capco-hlwgfdts"))
    ok, reason = eligible(make_scored(aggregated, score=99), cfg)
    assert ok is False
    assert "aggregator" in reason and aggregated.url in reason
    # Control: the same URL from the ATS adapter is eligible, so the source
    # is what refused it.
    assert eligible(make_scored(greenhouse_copy(), score=99), cfg) == (True, "")


# ==========================================================================
# config and dispatch
# ==========================================================================


def test_the_source_is_off_by_default_and_a_cli_selection_cannot_turn_it_on(tmp_path):
    assert DEFAULTS["sources"]["freehire"] is False
    cfg = write_config(tmp_path)
    assert "freehire" not in main_module._active_sources(cfg, None)
    assert "freehire" not in main_module._active_sources(cfg, ["freehire"])


def test_the_shipped_config_turns_it_on_with_queries_that_validate():
    """Switched on 2026-10-23, with queries for the non-Polish European
    countries and for Singapore and Taiwan. A malformed query or a bound past
    freehire's maximum would surface here, not as a digest error the next
    morning."""
    shipped = Config.load(ROOT / "config.yaml", ROOT / "watchlist.yaml", root=ROOT, env={})
    assert shipped.source_enabled("freehire") is True
    assert "freehire" in main_module._active_sources(shipped, None)
    assert not [p for p in shipped.validate(require_llm=False) if "freehire" in p]
    block = shipped.watchlist["freehire"]
    assert len(block["queries"]) == 4
    assert {"sg", "tw"} <= set(block["queries"][3]["countries"])
    assert block["limit"] <= 100 and block["max_requests"] <= 20


def test_when_on_it_is_dispatched_and_counted_like_any_source(tmp_path, monkeypatch):
    job = parse_job(hit("specjalista-ds-data-governance-emagine-l4makrqu"))
    calls = []

    def fake_fetch(config, *, errors):
        calls.append(config.watchlist["freehire"]["max_requests"])
        errors.append("freehire: query 2 offset 0: HTTP 503")
        return [job]

    monkeypatch.setattr(main_module.freehire, "fetch", fake_fetch)
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "freehire": True}},
                       watchlist={})
    scored, stats = main_module.run_pipeline(
        cfg, now=datetime(2026, 10, 22, 18, 0, tzinfo=UTC), sources=["freehire"],
        skip_llm=True, skip_apply=True,
    )
    assert calls == [6]
    assert stats.source_counts == {"freehire": 1}
    assert any(e.startswith("freehire: ") for e in stats.errors)


def test_the_shipped_queries_validate_when_the_source_is_switched_on(tmp_path):
    cfg = write_config(tmp_path, {"sources": {"freehire": True}},
                       watchlist={"greenhouse": ["acme"],
                                  "freehire": WATCHLIST_DEFAULTS["freehire"]})
    assert not [p for p in cfg.validate() if "freehire" in p]


@pytest.mark.parametrize("settings,needle", [
    ({"queries": []}, "queries is empty"),
    ({"queries": ["countries=pl"]}, "queries[0] must be a mapping"),
    ({"queries": [{"countries": ["pl"], "posted_within_days": 3}]}, "re-dated"),
    ({"limit": 0}, "freehire.limit"),
    ({"limit": 500}, "freehire.limit"),
    ({"max_requests": True}, "freehire.max_requests"),
    ({"exclude_upstream": "whatjobs-pl"}, "exclude_upstream"),
])
def test_validate_rejects_a_malformed_freehire_watchlist(tmp_path, settings, needle):
    cfg = write_config(tmp_path, {"sources": {"freehire": True}},
                       watchlist={"greenhouse": ["acme"], "freehire": settings})
    assert any(needle in p for p in cfg.validate()), cfg.validate()
