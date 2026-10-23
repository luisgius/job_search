"""Tests for src/jobposting.py — schema.org JobPosting out of JSON-LD.

The shapes below are the ones that broke a one-script, one-`@type` reader in
practice: an Organization block ahead of the JobPosting, a JobPosting inside
`@graph`, a malformed block before a good one, and `@type` written as a list.
Every page is untrusted data, so the second half is about odd values giving
empty fields rather than exceptions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.jobposting import extract_job_posting, job_posting_fields


def script(value, *, attrs='type="application/ld+json"'):
    text = value if isinstance(value, str) else json.dumps(value)
    return f"<script {attrs}>{text}</script>"


def page(*scripts):
    return f"<html><head>{''.join(scripts)}</head><body><p>Hi</p></body></html>"


# ==========================================================================
# extract_job_posting
# ==========================================================================

# The cases in this section up to the marker below are adapted from
# ai-job-search (https://github.com/ai-job-search-dev/ai-job-search),
# .agents/skills/jobbank-search/cli/tests/detail-jsonld.test.ts and
# .agents/skills/jobdanmark-search/cli/tests/detail-jsonld.test.ts,
# MIT License, © 2026 Mads Lorentzen.


def test_finds_a_job_posting_inside_an_at_graph():
    html = script({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "Jobs"},
            {"@type": "JobPosting", "title": "Data Engineer"},
        ],
    })
    assert extract_job_posting(html) == {"@type": "JobPosting", "title": "Data Engineer"}


def test_top_level_object_and_array():
    assert extract_job_posting(script('{"@type":"JobPosting","title":"One"}'))["title"] == "One"
    assert extract_job_posting(
        script('[{"@type":"WebPage"},{"@type":"JobPosting","title":"Two"}]')
    )["title"] == "Two"


def test_skips_a_malformed_script_and_checks_later_ones():
    html = page(script("{not-json"), script('{"@type":"JobPosting","title":"Valid"}'))
    assert extract_job_posting(html)["title"] == "Valid"


def test_returns_none_when_no_job_posting_exists():
    assert extract_job_posting(script('{"@graph":[{"@type":"WebPage"}]}')) is None


def test_skips_malformed_and_finds_a_posting_in_an_array():
    node = extract_job_posting(page(
        script("{not-json"),
        script([{"@type": "BreadcrumbList"},
                {"@type": "JobPosting", "title": "Analyst",
                 "employmentType": ["FULL_TIME", "PART_TIME"]}]),
    ))
    fields = job_posting_fields(node)
    assert fields["title"] == "Analyst"
    assert fields["employment_type"] == ["FULL_TIME", "PART_TIME"]
    assert fields["valid_through"] is None
    assert fields["hiring_organization"] == ""


# -- end of adapted cases --------------------------------------------------


def test_at_type_may_be_a_list():
    node = extract_job_posting(script({"@type": ["JobPosting"], "title": "Listed"}))
    assert node["title"] == "Listed"
    node = extract_job_posting(script({"@type": ["Thing", "JobPosting"], "title": "Two types"}))
    assert node["title"] == "Two types"


def test_at_graph_nested_in_a_list():
    html = script([
        {"@type": "Organization", "name": "Acme"},
        {"@context": "https://schema.org",
         "@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "title": "Nested"}]},
    ])
    assert extract_job_posting(html)["title"] == "Nested"
    # JSON-LD also allows a single node, not a list, as the @graph value.
    assert extract_job_posting(script({"@graph": {"@type": "JobPosting", "title": "G"}}))["title"] == "G"


def test_organization_block_then_job_posting_block():
    html = page(
        script({"@context": "https://schema.org", "@type": "Organization", "name": "Acme"}),
        script({"@context": "https://schema.org", "@type": "JobPosting", "title": "Second"}),
    )
    assert extract_job_posting(html)["title"] == "Second"


def test_first_job_posting_in_document_order_wins():
    html = page(script({"@type": "JobPosting", "title": "First"}),
                script({"@type": "JobPosting", "title": "Second"}))
    assert extract_job_posting(html)["title"] == "First"


@pytest.mark.parametrize("attrs", [
    'type="application/ld+json"',
    "type='application/ld+json'",
    "type=application/ld+json",
    'TYPE="Application/LD+JSON"',
    'type=" application/ld+json "',
    'id="jp" type="application/ld+json" data-x="1"',
    'type="application/ld+json; charset=utf-8"',
])
def test_script_type_attribute_is_matched_tolerantly(attrs):
    html = page(f"\n  <SCRIPT {attrs}>\n  "
                + json.dumps({"@type": "JobPosting", "title": "T"}) + "\n</Script >")
    assert extract_job_posting(html)["title"] == "T"


@pytest.mark.parametrize("attrs", ['type="application/json"', 'type="text/javascript"', ""])
def test_other_scripts_are_not_json_ld(attrs):
    html = page(script({"@type": "JobPosting", "title": "T"}, attrs=attrs))
    assert extract_job_posting(html) is None


def test_commented_out_json_ld_is_not_read():
    body = json.dumps({"@type": "JobPosting", "title": "Old"})
    html = f'<!-- <script type="application/ld+json">{body}</script> -->'
    assert extract_job_posting(html) is None


def test_literal_newlines_inside_json_strings_are_tolerated():
    raw = '{"@type": "JobPosting", "title": "T", "description": "line one\nline two"}'
    assert extract_job_posting(script(raw))["description"] == "line one\nline two"


@pytest.mark.parametrize("html", [
    None, "", "<html></html>", 42, script(""), script("null"), script("[]"),
    script('"JobPosting"'), script('{"@type": 7}'), script('{"@type": "WebPage", "@graph": 3}'),
    script("[" * 5000 + "]" * 5000),
])
def test_odd_pages_give_none(html):
    assert extract_job_posting(html) is None


# ==========================================================================
# job_posting_fields
# ==========================================================================

# Adapted from ai-job-search (https://github.com/ai-job-search-dev/ai-job-search),
# .agents/skills/jobdanmark-search/cli/tests/detail-jsonld.test.ts,
# MIT License, © 2026 Mads Lorentzen.


def test_normalises_a_job_posting_and_a_scalar_employment_type():
    node = extract_job_posting(page(script({
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Data Engineer",
        "datePosted": "2026-07-13",
        "validThrough": "2026-08-13",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"name": "Acme A/S", "logo": "https://cdn.example/logo.png"},
        "jobLocation": {"address": {"addressLocality": "Odense", "addressCountry": "DK"}},
        "description": "<p>Build reliable pipelines.</p>",
    })))
    assert job_posting_fields(node) == {
        "title": "Data Engineer",
        "description": "Build reliable pipelines.",
        "date_posted": datetime(2026, 7, 13, tzinfo=timezone.utc),
        "valid_through": datetime(2026, 8, 13, tzinfo=timezone.utc),
        "remote": None,
        "applicant_location_requirements": [],
        "employment_type": ["FULL_TIME"],
        "hiring_organization": "Acme A/S",
        "url": "",
    }


def test_renders_an_html_description_to_text():
    fields = job_posting_fields({
        "@type": "JobPosting",
        "title": "Udvikler",
        "description": "<p>Vi s&oslash;ger en udvikler til R&amp;D.</p><p>Second   para"
                       "<br>line two</p><ul><li>Python &amp; Go</li><li>SQL</li></ul>",
    })
    assert fields["description"] == "Vi søger en udvikler til R&D.\nSecond para\nline two\nPython & Go\nSQL"


def test_plain_description_unchanged_and_absent_one_empty():
    assert job_posting_fields({"description": "Plain prose, no markup."})["description"] == (
        "Plain prose, no markup.")
    assert job_posting_fields({"title": "B"})["description"] == ""


# -- end of adapted cases --------------------------------------------------


def test_entity_encoded_html_description_is_rendered_too():
    fields = job_posting_fields({"description": "&lt;p&gt;Python &amp;amp; SQL&lt;/p&gt;"})
    assert fields["description"] == "Python & SQL"


def test_telecommute_is_remote_and_anything_else_is_unknown():
    assert job_posting_fields({"jobLocationType": "TELECOMMUTE"})["remote"] is True
    assert job_posting_fields({"jobLocationType": ["telecommute"]})["remote"] is True
    # Unstated or another value is not evidence of an on-site role.
    assert job_posting_fields({"jobLocationType": "ONSITE"})["remote"] is None
    assert job_posting_fields({})["remote"] is None


def test_applicant_location_requirements_become_names():
    fields = job_posting_fields({"applicantLocationRequirements": [
        {"@type": "Country", "name": "Poland"},
        {"@type": "AdministrativeArea", "name": " European Union "},
        "Germany",
        {"@type": "Country", "name": "Poland"},
        {"@type": "Country"},
        7,
    ]})
    assert fields["applicant_location_requirements"] == ["Poland", "European Union", "Germany"]
    single = job_posting_fields({"applicantLocationRequirements": {"@type": "Country", "name": "PL"}})
    assert single["applicant_location_requirements"] == ["PL"]


def test_dates_are_utc_and_partial_or_unreadable_dates_stay_unknown():
    fields = job_posting_fields({"datePosted": "2026-08-28T10:00:00+02:00",
                                 "validThrough": "2026-09-30T23:59:59Z"})
    assert fields["date_posted"] == datetime(2026, 8, 28, 8, tzinfo=timezone.utc)
    assert fields["valid_through"] == datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
    for value in ["Aug 28", "2026-08", "yesterday", "", 1756368000, None, ["2026-08-28"]]:
        assert job_posting_fields({"datePosted": value})["date_posted"] is None


def test_url_must_be_absolute_http():
    assert job_posting_fields({"url": " https://jobs.example/1 "})["url"] == "https://jobs.example/1"
    for value in ["javascript:alert(1)", "/relative/1", "ftp://x/1", 3, None]:
        assert job_posting_fields({"url": value})["url"] == ""


def test_hiring_organization_may_be_a_bare_name():
    assert job_posting_fields({"hiringOrganization": "Acme"})["hiring_organization"] == "Acme"


@pytest.mark.parametrize("node", [
    None, [], "JobPosting", 0,
    {"title": 5, "description": {"x": 1}, "datePosted": {"y": 2}, "employmentType": 9,
     "hiringOrganization": ["Acme"], "jobLocationType": {"z": 1},
     "applicantLocationRequirements": "Poland", "url": ["https://x"]},
])
def test_odd_shapes_never_raise(node):
    fields = job_posting_fields(node)
    assert set(fields) == {
        "title", "description", "date_posted", "valid_through", "remote",
        "applicant_location_requirements", "employment_type", "hiring_organization", "url",
    }
    assert fields["remote"] is None and fields["date_posted"] is None
