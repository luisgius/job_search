"""Tests for the Teamtailor pull in src/sources/ats_boards.py.

Teamtailor is the Nordic default, and it is unusual twice over: the only
keyless tenant surface is the RSS feed (`jobs.rss` appended to the careers
site's jobs page), and custom domains are the *norm* for its larger tenants
— so a watchlist entry may be a whole careers URL, not a slug, and the
fetcher must follow whatever redirects the tenant's domain setup involves.

Driven by `tests/fixtures/teamtailor_jobs.rss`. The fixture is
**spec-derived, not recorded** (RSS 2.0 core elements, plus `location` /
`remote-status` / `department` as optional per-item extras): it was written
while this environment had no network route to teamtailor.com. The
assumptions are pinned against a live feed by the `network`-marked tests in
`test_live_contract.py` — run `pytest -m network` once the network allows,
and re-record the fixture if those fail. Until then the honest caveat
stands: an item that carries no location element parses with `location=""`,
which the geo filter treats as unresolvable and drops.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sources.ats_boards import (
    _teamtailor_feed_url,
    check_slug,
    fetch_teamtailor,
)
from tests.conftest import (
    FakeResponse,
    FakeSession,
    load_fixture,
    xml_response,
)

UTC = timezone.utc
FEED = load_fixture("teamtailor_jobs.rss")


def tt_session(body=None, **kwargs):
    return FakeSession(
        [(".rss", xml_response(FEED if body is None else body))], **kwargs
    )


def by_id(jobs):
    return {j.ats_job_id: j for j in jobs}


# ==========================================================================
# addressing: slug, host, or a whole careers URL
# ==========================================================================


@pytest.mark.parametrize("written,expected", [
    ("acme", "https://acme.teamtailor.com/jobs.rss"),
    ("  acme  ", "https://acme.teamtailor.com/jobs.rss"),
    ("acme.teamtailor.com", "https://acme.teamtailor.com/jobs.rss"),
    ("https://acme.teamtailor.com/jobs", "https://acme.teamtailor.com/jobs.rss"),
    ("careers.acme.com", "https://careers.acme.com/jobs.rss"),
    ("https://careers.acme.com/jobs", "https://careers.acme.com/jobs.rss"),
    ("https://careers.acme.com/jobs/", "https://careers.acme.com/jobs.rss"),
    ("https://careers.acme.com/jobs.rss", "https://careers.acme.com/jobs.rss"),
    ("https://careers.acme.com", "https://careers.acme.com/jobs.rss"),
])
def test_every_entry_shape_resolves_to_the_feed(written, expected):
    assert _teamtailor_feed_url(written) == expected


def test_query_and_fragment_are_dropped_from_a_pasted_url():
    """`?department=…` on the feed would silently narrow what the pipeline
    sees, every morning, to whatever filter happened to be in the browser."""
    url = _teamtailor_feed_url("https://careers.acme.com/jobs?department=data#top")
    assert url == "https://careers.acme.com/jobs.rss"


def test_fetch_asks_for_rss():
    session = tt_session()
    fetch_teamtailor("acme", session=session)
    assert session.calls[0]["url"] == "https://acme.teamtailor.com/jobs.rss"
    assert session.calls[0]["headers"]["Accept"].startswith("application/rss+xml")


# ==========================================================================
# parsing
# ==========================================================================


def test_parses_items_and_skips_the_linkless_one():
    jobs = fetch_teamtailor("acme", session=tt_session())
    assert len(jobs) == 3  # the "Ghost entry" has no link
    assert all(j.source == "teamtailor" and j.ats == "teamtailor" for j in jobs)


def test_company_is_the_channels_own_title():
    """The channel `<title>` is the vendor-published name — the same evidence
    Workable's envelope name is trusted for — and it beats deriving "Career"
    out of a custom domain's first label."""
    jobs = fetch_teamtailor("https://career.vandelay.example/jobs", session=tt_session())
    assert jobs[0].company == "Vandelay Industries"


def test_the_posting_id_is_the_number_in_the_job_url():
    jobs = by_id(fetch_teamtailor("acme", session=tt_session()))
    assert set(jobs) == {"4471001", "4471002", "4471003"}


def test_the_full_item_maps_every_field():
    job = by_id(fetch_teamtailor("acme", session=tt_session()))["4471001"]
    assert job.title == "Data Scientist"
    assert job.url == "https://career.vandelay.example/jobs/4471001-data-scientist"
    assert job.location == "Stockholm"
    assert job.posted_at == datetime(2026, 8, 28, 10, 12, tzinfo=UTC)
    assert "SQL and Python required" in job.description
    assert "<p>" not in job.description
    assert job.raw["department"] == "Analytics"


def test_fully_remote_status_reads_remote():
    job = by_id(fetch_teamtailor("acme", session=tt_session()))["4471002"]
    assert job.remote is True
    assert job.location == "Remote"


def test_an_item_without_pubdate_has_freshness_unknown():
    """None, not today's date: an invented timestamp would smuggle stale
    postings through the freshness window forever."""
    job = by_id(fetch_teamtailor("acme", session=tt_session()))["4471003"]
    assert job.posted_at is None
    assert job.location == ""      # the feed said nothing; nothing is invented
    assert job.remote is None


# ==========================================================================
# the root gate and --check
# ==========================================================================


def test_a_200_that_is_not_a_feed_raises():
    """A well-formed non-RSS answer (a maintenance page, some other XML) must
    fail loudly rather than parse into a company that is not hiring."""
    session = tt_session("<html><body>Sign in</body></html>")
    with pytest.raises(Exception, match="did not return a parseable RSS feed"):
        fetch_teamtailor("acme", session=session)


def test_check_slug_passes_a_real_feed():
    ok, message = check_slug("teamtailor", "acme", session=tt_session())
    assert ok
    assert "3" in message


def test_check_slug_accepts_a_custom_domain_url():
    ok, _ = check_slug(
        "teamtailor", "https://career.vandelay.example/jobs", session=tt_session()
    )
    assert ok


def test_check_slug_fails_a_missing_tenant():
    ok, _ = check_slug("teamtailor", "acme",
                       session=FakeSession(default=FakeResponse(status_code=404)))
    assert not ok


def test_an_empty_feed_is_ok_not_broken():
    empty = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0">'
        "<channel><title>Acme</title></channel></rss>"
    )
    ok, message = check_slug("teamtailor", "acme", session=tt_session(empty))
    assert ok
    assert "0 postings" in message
