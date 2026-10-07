from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.singapore.adapters import AdapterError, get_adapter

FIXTURES = Path(__file__).parent / "fixtures" / "sg"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def entry(ats, **kwargs):
    result = {"name": "Example", "ats": ats, "fs": False, "status": "verified", "automation_allowed": True,
              "verified_url": "https://careers.example.com/jobs", "robots_url": "https://careers.example.com/robots.txt",
              "terms_url": "https://careers.example.com/terms", "evidence": "Offline sanitized permission test, 2026-10-07",
              "identifiers": {"slug": "example", "token": "example", "board": "example",
                              "tenant": "example", "site": "Careers", "host": "example.wd3.myworkdayjobs.com"}}
    result.update(kwargs)
    return result


class Client:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def _reply(self, method, url, args):
        self.calls.append((method, url, args))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)

    def get_json(self, url, **kwargs):
        return self._reply("GET", url, kwargs)

    def post_json(self, url, **kwargs):
        return self._reply("POST", url, kwargs)

    def get_text(self, url):
        return self._reply("TEXT", url, {})


@pytest.mark.parametrize("ats", ["workday", "greenhouse", "lever", "smartrecruiters", "ashby", "custom"])
@pytest.mark.parametrize("gate", [{"status": "manual_check"}, {"status": "unverified"},
                                  {"automation_allowed": False}, {"automation_allowed": "true"},
                                  {"policy_hold": "defence_clarification_pending"}])
def test_runtime_gate_never_requests_manual_unverified_or_unapproved(ats, gate):
    client = Client([])
    assert get_adapter(ats).fetch(entry(ats, **gate), client) == []
    assert client.calls == []


def test_unknown_or_prohibited_boards_fail_before_requests():
    with pytest.raises(AdapterError, match="Unsupported/unapproved"):
        get_adapter("linkedin")
    with pytest.raises(AdapterError, match="Unsupported/unapproved"):
        get_adapter("mycareersfuture")


def test_terms_evidence_required_even_when_caller_skips_config_validation():
    client = Client([])
    with pytest.raises(AdapterError, match="Missing dated"):
        get_adapter("ashby").fetch(entry("ashby", evidence=""), client)
    assert client.calls == []


def test_greenhouse_reuses_parser_publication_salary_provenance():
    client = Client([fixture("greenhouse")])
    job, = get_adapter("greenhouse").fetch(entry("greenhouse", fs=True), client)
    assert job.title == "Senior Data Scientist" and job.company == "Example"
    assert job.location == "Singapore"
    assert job.posted_at == datetime(2026, 8, 4, 7, tzinfo=timezone.utc)
    assert job.salary == "SGD 12000-15000/month"
    assert "Python services" in job.description
    assert job.apply_url == job.url
    assert job.ats_job_id == "4012345"
    assert job.raw["fs"] and job.raw["established"]
    assert job.sources == ["greenhouse:https://boards-api.greenhouse.io/v1/boards/example/jobs"]
    assert job.raw["provenance"]["date_field"] == "first_published"


def test_greenhouse_updated_timestamp_does_not_fabricate_posting_date():
    payload = fixture("greenhouse")
    del payload["jobs"][0]["first_published"]
    job, = get_adapter("greenhouse").fetch(entry("greenhouse"), Client([payload]))
    assert job.posted_at is None and "posted_date_unknown" in job.flags


def test_greenhouse_truncated_listing_reports_partial_count():
    payload = fixture("greenhouse")
    payload["meta"] = {"total": 2}
    with pytest.raises(AdapterError, match="reported total") as error:
        get_adapter("greenhouse").fetch(entry("greenhouse"), Client([payload]))
    assert error.value.partial_count == 1


@pytest.mark.parametrize("date", ["2026", "2026-10", "October 2026", "Posted 3 Days Ago", "10/04"])
def test_incomplete_dates_never_fill_from_fetch_date(date):
    data = fixture("ashby")
    data["jobs"][0]["publishedAt"] = date
    job, = get_adapter("ashby").fetch(entry("ashby"), Client([data]))
    assert job.posted_at is None


def test_lever_full_description_dates_salary_and_distinct_apply_url():
    client = Client([fixture("lever")])
    job, = get_adapter("lever").fetch(entry("lever"), client)
    assert job.salary == "SGD 12000-16000 month"
    assert job.apply_url.endswith("/apply") and job.url != job.apply_url
    assert "Requirements" in job.description and "Kafka" in job.description
    assert job.location == "Singapore; Hong Kong"
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert client.calls[0][2]["params"] == {"mode": "json", "skip": 0, "limit": 100}


def test_lever_pagination_and_repeated_page_detected():
    template = fixture("lever")[0]
    first = [dict(template, id=str(i), hostedUrl=f"https://jobs.lever.co/example/{i}") for i in range(100)]
    last = [dict(template, id="100", hostedUrl="https://jobs.lever.co/example/100")]
    client = Client([first, last])
    jobs = get_adapter("lever").fetch(entry("lever"), client)
    assert len(jobs) == 101 and client.calls[1][2]["params"]["skip"] == 100
    with pytest.raises(AdapterError, match="Repeated posting") as exc:
        get_adapter("lever").fetch(entry("lever"), Client([first, first]))
    assert exc.value.partial_count == 100


def test_workday_pagination_details_absolute_date_multilocation_and_salary():
    data = fixture("workday")
    client = Client([data["pages"][0], data["details"][0], data["pages"][1], data["details"][1]])
    first, second = get_adapter("workday").fetch(entry("workday"), client)
    assert first.ats_job_id == "R100" and first.location == "Singapore; Hong Kong"
    assert first.posted_at == datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert first.salary == "SGD 12000-16000 MONTH"
    assert "Full relocation support" in first.description
    assert first.url == first.apply_url and "/Careers/job/" in first.url
    assert second.posted_at is None and "posted_date_unknown" in second.flags
    assert client.calls[0][2]["json"]["offset"] == 0
    assert client.calls[2][2]["json"]["offset"] == 1
    assert client.calls[1][1].startswith("https://example.wd3.myworkdayjobs.com/wday/cxs/example/Careers/job/")


def test_workday_partial_error_reports_prior_jobs_and_never_hides_missing_details():
    data = fixture("workday")
    client = Client([data["pages"][0], data["details"][0], data["pages"][1], RuntimeError("dummy-only")])
    with pytest.raises(AdapterError, match="incomplete") as error:
        get_adapter("workday").fetch(entry("workday"), client)
    assert error.value.partial_count == 1 and error.value.partial_jobs[0].ats_job_id == "R100"


def test_workday_paginates_unrelated_summaries_without_hydrating_them():
    data = fixture("workday")
    data["pages"][0]["jobPostings"] = [{"title": "Client Growth Director", "externalPath": "/job/Singapore/Director_R099"}]
    client = Client([data["pages"][0], data["pages"][1], data["details"][1]])
    job, = get_adapter("workday").fetch(entry("workday"), client)
    assert job.ats_job_id == "R101"
    assert [c[0] for c in client.calls] == ["POST", "POST", "GET"]
    assert job.raw["listing_total"] == 2 and job.raw["listing_offset"] == 1


def test_workday_repeated_unrelated_summaries_still_fail_explicitly():
    page = {"total": 2, "jobPostings": [{"title": "Client Growth Director", "externalPath": "/job/Singapore/Director_R099"}]}
    with pytest.raises(AdapterError, match="Repeated Workday summary"):
        get_adapter("workday").fetch(entry("workday"), Client([page, page]))


def test_workday_truncated_pagination_is_error_and_host_is_not_guessed():
    data = fixture("workday")
    client = Client([data["pages"][0], data["details"][0], {"total": 2, "jobPostings": []}])
    with pytest.raises(AdapterError, match="before total") as error:
        get_adapter("workday").fetch(entry("workday"), client)
    assert error.value.partial_count == 1
    client = Client([])
    with pytest.raises(AdapterError, match="verified identifier: host"):
        get_adapter("workday").fetch(entry("workday", identifiers={"tenant": "example", "site": "Careers"}), client)
    assert client.calls == []


def test_workday_no_guessed_id_from_path_or_relative_date():
    data = fixture("workday")
    data["pages"][0]["total"] = 1
    del data["details"][0]["jobPostingInfo"]["jobReqId"]
    job, = get_adapter("workday").fetch(entry("workday"), Client([data["pages"][0], data["details"][0]]))
    assert job.ats_job_id is None


def test_smartrecruiters_pagination_details_description_dates_and_apply():
    data = fixture("smartrecruiters")
    client = Client([data["pages"][0], data["details"][0], data["pages"][1], data["details"][1]])
    first, second = get_adapter("smartrecruiters").fetch(entry("smartrecruiters"), client)
    assert first.posted_at == datetime(2026, 10, 5, 3, 10, tzinfo=timezone.utc)
    assert first.apply_url.endswith("/apply") and first.url != first.apply_url
    assert first.salary == "SGD 10000-14000 MONTH"
    assert first.ats_job_id == "744000001234001"
    assert "Five years experience" in first.description
    assert client.calls[2][2]["params"]["offset"] == 1
    assert second.posted_at < first.posted_at


def test_smartrecruiters_missing_total_or_id_fails_explicitly():
    data = fixture("smartrecruiters")
    del data["pages"][0]["totalFound"]
    with pytest.raises(AdapterError, match="totalFound"):
        get_adapter("smartrecruiters").fetch(entry("smartrecruiters"), Client([data["pages"][0]]))


def test_ashby_real_source_uuid_salary_publication_and_apply():
    client = Client([fixture("ashby")])
    job, = get_adapter("ashby").fetch(entry("ashby"), client)
    assert job.ats_job_id == "260fdad2-74f5-4b94-b7af-caa7eec0393e"
    assert job.posted_at == datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)
    assert job.salary == "SGD 150,000 - 190,000 annually"
    assert job.apply_url.endswith("/application")
    assert job.location == "Singapore; Hong Kong" and job.remote is False
    assert client.calls[0][2]["params"] == {"includeCompensation": "true"}


def test_custom_jsonld_graph_dates_salary_and_organization():
    html = (FIXTURES / "custom.html").read_text()
    job, = get_adapter("custom").fetch(entry("custom"), Client([html]))
    assert job.ats_job_id == "DS-101" and job.location == "Singapore, SG"
    assert job.posted_at == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert job.salary == "SGD 11000-15000 MONTH"
    assert "Employment Pass sponsorship" in job.description
    assert job.apply_url.endswith("/apply")


def test_custom_explicit_verified_json_schema_maps_fields():
    schema = {"items_path": "data.jobs", "fields": {"id": "req", "title": "heading", "url": "links.posting",
              "apply_url": "links.apply", "location": "place", "description": "body", "posted_at": "published", "salary": "pay"}}
    row = entry("custom", json_schema=schema, json_schema_verified=True)
    job, = get_adapter("custom").fetch(row, Client([fixture("custom")]))
    assert job.ats_job_id == "DS-102" and job.url == "https://careers.example.com/jobs/DS-102"
    assert job.apply_url == job.url + "/apply"
    assert job.posted_at == datetime(2026, 10, 4, 4, tzinfo=timezone.utc)
    assert job.salary == "SGD 13000-17000 MONTH"
    client = Client([])
    row["json_schema_verified"] = False
    with pytest.raises(AdapterError, match="explicit verified schema"):
        get_adapter("custom").fetch(row, client)
    assert client.calls == []


def test_custom_no_unverified_html_scraping_fallback():
    with pytest.raises(AdapterError, match="no JobPosting"):
        get_adapter("custom").fetch(entry("custom"), Client(['<a href="/job/1">Data Scientist</a>']))


def test_custom_json_missing_mapped_url_is_error():
    row = entry("custom", json_schema_verified=True,
                json_schema={"fields": {"title": "title", "url": "url"}})
    with pytest.raises(AdapterError, match="missing its mapped URL"):
        get_adapter("custom").fetch(row, Client([[{"title": "Data Scientist"}]]))


@pytest.mark.parametrize("ats,payload", [("greenhouse", {"unexpected": []}), ("lever", {"error": "closed"}),
                                        ("ashby", {"jobs": "not-a-list"})])
def test_schema_drift_is_explicit_error_instead_of_empty_success(ats, payload):
    with pytest.raises(AdapterError, match="expected"):
        get_adapter(ats).fetch(entry(ats), Client([payload]))


def test_non_company_jsonld_retains_company_and_source_type():
    html = (FIXTURES / "custom.html").read_text()
    job, = get_adapter("custom").fetch(entry("custom", name="Approved board", source_type="approved_board"), Client([html]))
    assert job.company == "Example" and job.source_type == "approved_board"
    assert job.raw["established"] is False
