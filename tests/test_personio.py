"""Tests for the Personio pull in src/sources/ats_boards.py.

Personio is the ATS of German, Spanish and Italian SMBs — the mid-size
employers that never appear on Greenhouse — and it is the one board here that
speaks **XML** rather than JSON, and the one that is addressed by *subdomain*
rather than by a path segment. Both of those are places a generic slug helper
would quietly do the wrong thing, so both are pinned below.

Driven by `tests/fixtures/personio_positions.xml`, which is structurally
faithful to a `<workzag-jobs>` feed and deliberately includes an empty office,
an empty date, an unnamed position and the titled `<jobDescription>` sections.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources.ats_boards import check_slug, fetch, fetch_personio
from tests.conftest import (
    FakeResponse,
    FakeSession,
    load_fixture,
    write_config,
    xml_response,
)

UTC = timezone.utc
PERSONIO_XML = load_fixture("personio_positions.xml")


def pn_session(body=None, **kwargs):
    return FakeSession([("jobs.personio.de",
                         xml_response(PERSONIO_XML if body is None else body))],
                       **kwargs)


def by_id(jobs):
    return {j.ats_job_id: j for j in jobs}


# ==========================================================================
# addressing: the slug IS the host
# ==========================================================================


def test_personio_requests_the_tenant_xml_feed():
    session = pn_session()
    fetch_personio("vandelay", session=session)
    assert session.calls[0]["url"] == "https://vandelay.jobs.personio.de/xml"


@pytest.mark.parametrize("written", [
    "vandelay",
    "  vandelay  ",
    "vandelay.jobs.personio.de",
    "https://vandelay.jobs.personio.de/xml",
    "https://vandelay.jobs.personio.de/",
])
def test_personio_accepts_the_bare_tenant_or_the_whole_host(written):
    """Personio is per-*subdomain*, not per-path, so the generic slug rule —
    "throw away the host, keep the first path segment" — is exactly backwards
    here and would turn `vandelay.jobs.personio.de/xml` into the slug `xml`."""
    session = pn_session()
    fetch_personio(written, session=session)
    assert session.calls[0]["url"].startswith("https://vandelay.jobs.personio.de")


def test_a_com_tenant_host_is_used_verbatim_without_a_de_attempt():
    """A user who has written the full `.com` host has already answered the
    question the fallback exists to guess at — asking `.de` first would be a
    pointless request and a confusing log line."""
    session = FakeSession([("jobs.personio.com", xml_response(PERSONIO_XML))])
    jobs = fetch_personio("vandelay.jobs.personio.com", session=session)
    assert session.calls[0]["url"] == "https://vandelay.jobs.personio.com/xml"
    assert len(session.calls) == 1
    assert jobs[0].url.startswith("https://vandelay.jobs.personio.com/job/")


def test_a_bare_tenant_falls_back_from_de_to_com_on_a_404():
    """Most tenants are on `.de`; a minority were provisioned on `.com`. Making
    the user find out which by reading a 404 is a worse trade than one extra
    request on the boards where the first guess misses."""
    session = FakeSession([("jobs.personio.de", FakeResponse(status_code=404)),
                           ("jobs.personio.com", xml_response(PERSONIO_XML))])
    jobs = fetch_personio("vandelay", session=session)
    assert [c["url"] for c in session.calls] == [
        "https://vandelay.jobs.personio.de/xml",
        "https://vandelay.jobs.personio.com/xml",
    ]
    assert len(jobs) == 4
    assert jobs[0].url.startswith("https://vandelay.jobs.personio.com/job/")


def test_a_403_is_not_retried_on_the_other_domain():
    """Only "no such tenant" is worth a second host. A 403 means the tenant
    exists and something else is wrong, and trying the other domain would
    replace a useful error with a confusing one."""
    session = FakeSession([("jobs.personio.de", FakeResponse(status_code=403)),
                           ("jobs.personio.com", xml_response(PERSONIO_XML))])
    with pytest.raises(Exception):
        fetch_personio("vandelay", session=session)
    assert len(session.calls) == 1


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_slug_raises_rather_than_hitting_the_network(bad):
    with pytest.raises(ValueError):
        fetch_personio(bad, session=FakeSession())


@pytest.mark.parametrize("written", [
    "vandelay",
    "vandelay.jobs.personio.de",
    "https://vandelay.jobs.personio.de/job/12345",
    "https://vandelay.jobs.personio.de/xml",
])
def test_a_pasted_personio_url_survives_the_watchlist(written, tmp_path: Path):
    """`watchlist.yaml` promises in as many words that "Pasting the whole URL
    works too", and `_slug_for` is the only thing that keeps that promise for
    this vendor: every other board is `host/SLUG`, so the generic rule throws
    the host away and keeps the first path segment — which for Personio, where
    the slug *is* the host, turns `acme.jobs.personio.de/job/12345` into the
    tenant `job`.

    The parametrised test above proves `fetch_personio` re-normalises its own
    argument, so it passes with or without that branch. This one goes through
    `fetch()`, which is the path a watchlist entry actually takes, and it is
    the only place the branch is exercised at all."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "personio": True}},
                       watchlist={"personio": [written]})
    session = pn_session()
    jobs = fetch(cfg, session=session)

    assert session.calls[0]["url"] == "https://vandelay.jobs.personio.de/xml"
    assert len(jobs) == 4
    assert jobs[0].company == "Vandelay"


# ==========================================================================
# parsing
# ==========================================================================


def test_personio_parses_the_happy_path():
    job = by_id(fetch_personio("vandelay", session=pn_session()))["4103001"]
    assert job.source == "personio"
    assert job.title == "Senior Backend Engineer (m/w/d)"
    assert job.company == "Vandelay"
    assert job.location == "Valencia"
    assert job.url == "https://vandelay.jobs.personio.de/job/4103001"
    # 07:00+02:00 is 05:00 UTC — the feed's offset is written without a colon,
    # which is the shape `util.parse_datetime` has to survive.
    assert job.posted_at == datetime(2026, 8, 4, 5, 0, tzinfo=UTC)


def test_personio_never_claims_an_auto_appliable_ats():
    """`autoapply` only knows how to bail safely out of Greenhouse and Lever
    forms. Claiming either here would drive the bot into a form nothing has
    tested it against."""
    from src.apply.autoapply import SUPPORTED_ATS, detect_ats

    for job in fetch_personio("vandelay", session=pn_session()):
        assert job.ats == "personio"
        assert job.ats not in SUPPORTED_ATS
        assert detect_ats(job.url) is None


def test_personio_concatenates_every_titled_job_description_section():
    """A Personio ad is three or four titled HTML blocks, and "Dein Profil" is
    where every requirement lives — the same reason Lever's `lists` blocks are
    kept. Reading only the first section scores the job on its intro."""
    job = by_id(fetch_personio("vandelay", session=pn_session()))["4103001"]
    assert "Zahlungsdienste in Python" in job.description       # Deine Aufgaben
    assert "6+ Jahre Python" in job.description                 # Dein Profil
    assert "Umzugsunterstützung" in job.description             # Was wir bieten
    assert "Dein Profil" in job.description                     # the headings survive
    assert "<ul>" not in job.description                        # flattened, not HTML


def test_personio_survives_cdata_wrapped_html():
    """Every `<value>` in a real feed is CDATA-wrapped HTML. If that were read
    literally the description would be a wall of `&lt;p&gt;`."""
    job = by_id(fetch_personio("vandelay", session=pn_session()))["4103002"]
    assert job.description == "Your tasks\nOwn the lakehouse. Spark, dbt, Airflow."


def test_personio_empty_office_is_an_empty_location_not_a_crash():
    assert by_id(fetch_personio("vandelay", session=pn_session()))["4103003"].location == ""


def test_personio_undated_position_yields_none_not_a_guess():
    """`freshness.skip_undated` decides what happens to a dateless posting.
    Inventing "now" here would defeat that setting silently."""
    assert by_id(fetch_personio("vandelay", session=pn_session()))["4103003"].posted_at is None


def test_personio_ignores_updated_at_as_a_publication_date():
    """The mirror of `test_ashby_ignores_updated_at_as_a_publication_date`, and
    the test this file did not have.

    The fixture's flagship position carries both `createdAt` (07:00, when it
    went live) and `updatedAt` (08:45, when somebody fixed a typo). Reading the
    later one makes a three-month-old req look like today's news, and there is
    no way to tell from the digest that it happened."""
    job = by_id(fetch_personio("vandelay", session=pn_session()))["4103001"]
    assert job.raw["created_at"] == "2026-08-04T07:00:00+0200"
    assert job.posted_at == datetime(2026, 8, 4, 5, 0, tzinfo=UTC)


def test_personio_will_not_date_a_position_from_updated_at_alone():
    """No date is more honest than an inflated one — `skip_undated` can then
    make the decision where the user can see it."""
    body = ("<workzag-jobs><position><id>1</id><name>Engineer</name>"
            "<updatedAt>2026-08-04T08:45:00+0200</updatedAt></position>"
            "</workzag-jobs>")
    assert fetch_personio("vandelay", session=pn_session(body))[0].posted_at is None


def test_personio_skips_an_unnamed_position_without_dropping_the_feed():
    jobs = fetch_personio("vandelay", session=pn_session())
    assert "4103005" not in by_id(jobs)
    assert len(jobs) == 4


def test_personio_office_is_passed_through_for_geo_to_resolve():
    """`office` is the only geography a Personio feed states — there is no
    country field at all — so it is handed to `geo` untouched, accents and
    all. Mangling "München" here would lose every German posting."""
    from src import geo

    jobs = by_id(fetch_personio("vandelay", session=pn_session()))
    assert jobs["4103004"].location == "München"
    assert geo.country_of(jobs["4103004"].location) == "DE"
    assert geo.country_of(jobs["4103001"].location) == "ES"


def test_personio_marks_remote_positively_only():
    jobs = by_id(fetch_personio("vandelay", session=pn_session()))
    assert jobs["4103002"].remote is True      # office is literally "Remote"
    assert jobs["4103001"].remote is None
    assert all(j.remote is not False for j in jobs.values())


def test_personio_records_the_employment_type_where_the_filter_reads_it():
    """Personio states "permanent" / "intern" / "trainee" / "freelance" as
    structured data, which is the only signal that a neutrally-titled posting
    is an internship — `filters.employment_type_exclude` reads exactly the keys
    in `EMPLOYMENT_TYPE_KEYS` and never the title."""
    from src.config import DEFAULTS
    from src.filters import EMPLOYMENT_TYPE_KEYS, apply_filters

    job = by_id(fetch_personio("vandelay", session=pn_session()))["4103004"]
    assert job.raw["employment_type"] == "intern"
    assert any(k in job.raw for k in EMPLOYMENT_TYPE_KEYS)

    result = apply_filters([job], {"filters": DEFAULTS["filters"],
                                   "freshness": {"max_age_hours": 100000}})
    assert result.counts.get("employment_type_excluded") == 1


def test_personio_records_provenance_in_raw():
    job = by_id(fetch_personio("vandelay", session=pn_session()))["4103001"]
    assert job.raw["board"] == "personio"
    assert job.raw["slug"] == "vandelay.jobs.personio.de"
    assert job.raw["subcompany"] == "Vandelay Iberia S.L."
    assert job.raw["seniority"] == "experienced"
    assert job.raw["schedule"] == "full-time"


def test_personio_tolerates_a_feed_with_no_positions():
    assert fetch_personio("vandelay",
                          session=pn_session("<workzag-jobs></workzag-jobs>")) == []


def test_personio_tolerates_a_position_with_no_job_descriptions_block():
    body = ("<workzag-jobs><position><id>1</id><name>Engineer</name>"
            "<office>Madrid</office></position></workzag-jobs>")
    job = fetch_personio("vandelay", session=pn_session(body))[0]
    assert job.description == ""
    assert job.title == "Engineer"


def test_personio_drops_a_position_with_no_id():
    """The id is both the apply URL and `Job.key`. Without it the job cannot be
    linked to or tracked, and an untrackable job is one the never-apply-twice
    guarantee cannot cover."""
    body = ("<workzag-jobs><position><name>Engineer</name>"
            "<office>Madrid</office></position></workzag-jobs>")
    assert fetch_personio("vandelay", session=pn_session(body)) == []


def test_a_namespaced_feed_parses_rather_than_returning_a_quiet_zero():
    """The gate on the root element tolerates a namespace, and said so in its
    docstring — but every child lookup underneath it was `find("id")`, which
    matches a *literal* tag. On a feed served with a default namespace every
    element is really called `{…}id`, so every lookup returned None, every
    position was dropped for having no name, and the feed parsed to **zero
    jobs and raised nothing**.

    That reads as "this company is not hiring", every morning, forever. It is
    the precise silent failure the root-tag check was added to prevent, one
    level further down — a gate that admits a document and then cannot read it
    is worse than no gate at all."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<workzag-jobs xmlns="http://www.personio.de/xml">'
        "<position><id>4103001</id><name>Senior Backend Engineer</name>"
        "<office>Valencia</office><employmentType>permanent</employmentType>"
        "<createdAt>2026-08-04T07:00:00+0200</createdAt>"
        "<jobDescriptions>"
        "<jobDescription><name>Dein Profil</name>"
        "<value><![CDATA[<ul><li>6+ Jahre Python</li></ul>]]></value>"
        "</jobDescription></jobDescriptions>"
        "</position></workzag-jobs>"
    )
    jobs = fetch_personio("vandelay", session=pn_session(body))

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Backend Engineer"
    assert job.ats_job_id == "4103001"
    assert job.url == "https://vandelay.jobs.personio.de/job/4103001"
    assert job.location == "Valencia"
    assert job.posted_at == datetime(2026, 8, 4, 5, 0, tzinfo=UTC)
    assert job.raw["employment_type"] == "permanent"
    assert "6+ Jahre Python" in job.description
    assert "Dein Profil" in job.description


def test_a_prefixed_namespace_is_read_the_same_way():
    """`<p:workzag-jobs xmlns:p="…">` is the other shape the same change of
    serialiser produces."""
    body = (
        '<p:workzag-jobs xmlns:p="http://www.personio.de/xml">'
        "<p:position><p:id>7</p:id><p:name>Engineer</p:name>"
        "<p:office>Madrid</p:office></p:position></p:workzag-jobs>"
    )
    job = fetch_personio("vandelay", session=pn_session(body))[0]
    assert job.title == "Engineer" and job.location == "Madrid"


def test_a_namespaced_error_page_is_still_rejected():
    """Tolerating a namespace must not become tolerating anything. An XHTML
    error page is well-formed, namespaced XML with no positions in it, and
    returning `[]` for it is what makes a login wall look like a quiet
    company."""
    body = ('<html xmlns="http://www.w3.org/1999/xhtml">'
            "<body>Not found</body></html>")
    with pytest.raises(Exception):
        fetch_personio("vandelay", session=pn_session(body))


# ==========================================================================
# language
# ==========================================================================


def test_personio_asks_for_no_language_by_default():
    """The feed defaults to the tenant's own language, and that is the right
    default to keep: a German SMB's ad may exist *only* in German, and asking
    for a language its career site does not publish returns an empty or
    degraded description — a job made worse, or lost, to buy an English
    translation nobody needed. `_REMOTE_RE` reads German for this reason."""
    session = pn_session()
    fetch_personio("vandelay", session=session)
    assert not session.calls[0]["params"]


@pytest.mark.parametrize("language", ["de", "en", "fr", "es", "nl", "it", "pt"])
def test_personio_sends_a_language_when_the_watchlist_asks_for_one(language):
    """Documented and opt-in, per tenant: `{slug: acme, language: en}`."""
    session = pn_session()
    fetch_personio("vandelay", session=session, language=language)
    assert session.calls[0]["params"] == {"language": language}


def test_a_watchlist_entry_can_set_the_language(tmp_path: Path):
    """Through `fetch()`, which is the path the watchlist actually takes."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "personio": True}},
                       watchlist={"personio": [{"slug": "vandelay",
                                                "language": "en"}]})
    session = pn_session()
    assert len(fetch(cfg, session=session)) == 4
    assert session.calls[0]["params"] == {"language": "en"}


def test_an_undocumented_language_is_dropped_rather_than_sent(caplog):
    """The job-losing direction is asking for something the tenant cannot
    serve. An unknown value costs the option and keeps the postings."""
    import logging

    session = pn_session()
    with caplog.at_level(logging.WARNING, logger="src.sources.ats_boards"):
        jobs = fetch_personio("vandelay", session=session, language="klingon")

    assert len(jobs) == 4
    assert not session.calls[0]["params"]
    assert "klingon" in caplog.text


def test_an_unknown_watchlist_option_costs_the_option_and_not_the_company(
    tmp_path: Path,
):
    """A typo in a per-entry option must never delete a company's postings."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "personio": True}},
                       watchlist={"personio": [{"slug": "vandelay",
                                                "langauge": "en", "colour": "blue"}]})
    session = pn_session()
    assert len(fetch(cfg, session=session)) == 4
    assert not session.calls[0]["params"]


def test_malformed_xml_is_reported_rather_than_silently_empty():
    """An HTML error page served with a 200 is a real Personio failure mode.
    Returning `[]` for it would look exactly like a company that is not
    hiring — the silent failure this whole module is built to avoid."""
    session = pn_session("<html><body>Not found</body></html>")
    with pytest.raises(Exception):
        fetch_personio("vandelay", session=session)


def test_the_xml_parser_does_not_resolve_external_entities():
    """ElementTree ignores external entity declarations, which is what makes it
    safe to point at a third-party feed and why no `defusedxml` dependency is
    needed. A parser that resolved them would read `/etc/passwd` into a job
    description on request from whoever controls the feed."""
    body = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE workzag-jobs [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<workzag-jobs><position><id>1</id><name>Engineer</name>"
        "<office>&xxe;</office></position></workzag-jobs>"
    )
    with pytest.raises(Exception):
        fetch_personio("vandelay", session=pn_session(body))


# ==========================================================================
# failure isolation
# ==========================================================================


def test_check_slug_ok():
    ok, message = check_slug("personio", "vandelay", session=pn_session())
    assert ok is True
    assert "4 postings" in message


def test_check_slug_explains_a_404_on_both_hosts():
    session = FakeSession(default=FakeResponse(status_code=404))
    ok, message = check_slug("personio", "nope", session=session)
    assert ok is False
    assert "404" in message and "slug not found" in message


def test_one_dead_personio_tenant_does_not_kill_the_others(tmp_path: Path):
    """The whole point of the per-slug try/except: a renamed tenant costs that
    company's postings and nothing else."""
    cfg = write_config(tmp_path, {"sources": {"greenhouse": False, "personio": True}},
                       watchlist={"personio": ["dead", "vandelay"]})
    session = FakeSession([
        ("dead.jobs.personio", FakeResponse(status_code=404)),
        ("vandelay.jobs.personio.de", xml_response(PERSONIO_XML)),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert len(jobs) == 4
    assert len(errors) == 1
    assert "personio/dead" in errors[0] and "404" in errors[0]


def test_a_broken_personio_feed_does_not_cost_the_other_vendors(tmp_path: Path):
    """Cross-vendor isolation: the six boards share one `fetch()` call, and one
    tenant serving an HTML error page must not take the Greenhouse companies
    with it."""
    from tests.conftest import json_response, load_json_fixture

    greenhouse = load_json_fixture("greenhouse_jobs.json")
    cfg = write_config(tmp_path, {"sources": {"greenhouse": True, "personio": True}},
                       watchlist={"greenhouse": ["acme"], "personio": ["vandelay"]})
    session = FakeSession([
        ("boards-api.greenhouse.io", json_response(greenhouse)),
        ("jobs.personio", xml_response("<html>nope</html>")),
    ])
    errors: list[str] = []
    jobs = fetch(cfg, session=session, errors=errors)
    assert {j.source for j in jobs} == {"greenhouse"}
    assert len(errors) == 1
    assert "personio/vandelay" in errors[0]
