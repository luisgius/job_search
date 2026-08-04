"""Contract tests against the REAL APIs. Deselected by default.

    pytest -m network -q            # run these
    pytest -q                       # the normal suite; these are skipped

Why this file exists
--------------------
Every other test in this suite runs offline against fixtures in
`tests/fixtures/`. That proves our parsers handle *those* payloads — it proves
nothing about whether the live APIs still emit them. If Greenhouse renamed a
field tomorrow, the fixtures and the parser would agree with each other and
both be wrong, and 958 green tests would not notice.

These tests close that gap. They do the one thing the offline suite cannot:
fetch a real payload and assert that the specific fields the parsers depend on
are still there, with the shapes they expect.

They are NOT in the default run on purpose. A suite that goes red because
somebody else's API had a bad afternoon trains you to ignore failures. Run
them deliberately: on setup, and again whenever a source mysteriously returns
nothing.

    pytest -m network -q                          everything
    pytest -m network -q -k greenhouse            one board

Each test skips (rather than fails) when the network is unreachable, so a
train journey does not look like a broken API.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from src.models import utcnow
from src.sources.ats_boards import fetch_greenhouse, fetch_lever
from src.util import HttpError

pytestmark = pytest.mark.network

# Long-lived public boards. If one of these 404s, the slug moved — which is
# itself worth knowing, and is exactly the failure `--check-all` exists for.
GREENHOUSE_SLUGS = ["gitlab", "datadog"]
LEVER_SLUGS = ["plaid"]

#: Fields the parsers actually read. Losing any one of them silently degrades
#: the pipeline rather than crashing it, which is why they are asserted here
#: rather than left to a try/except.
GREENHOUSE_REQUIRED = ("id", "title", "absolute_url")
GREENHOUSE_EXPECTED = ("location", "content", "updated_at")
LEVER_REQUIRED = ("id", "text", "hostedUrl")
LEVER_EXPECTED = ("categories", "createdAt")


#: An actual status code, not the letters "HTTP" — `HTTPSConnectionPool`
#: contains those and would turn every offline machine into a red suite.
_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def _reachable(fn, *args, **kwargs):
    """Run a fetch, skipping the test when the network is the problem.

    A 404/403 means the API answered and rejected us — a real finding, so it
    fails. A DNS or connection failure is a train tunnel, so it skips.
    """
    try:
        return fn(*args, **kwargs)
    except HttpError as exc:
        if _STATUS_RE.search(str(exc)):
            pytest.fail(f"the API answered but rejected us: {exc}")
        pytest.skip(f"network unreachable: {exc}")
    except Exception as exc:  # transport-level
        pytest.skip(f"network unreachable: {exc}")


def _raw_payload(url: str, params: dict | None = None):
    from src.util import http_get_json

    try:
        return http_get_json(url, params=params)
    except Exception as exc:
        pytest.skip(f"network unreachable: {exc}")


# ==========================================================================
# Greenhouse
# ==========================================================================


@pytest.mark.parametrize("slug", GREENHOUSE_SLUGS)
def test_greenhouse_board_still_answers(slug):
    jobs = _reachable(fetch_greenhouse, slug)
    assert jobs, f"greenhouse/{slug} returned zero postings — has the slug moved?"


@pytest.mark.parametrize("slug", GREENHOUSE_SLUGS[:1])
def test_greenhouse_payload_still_has_the_fields_we_parse(slug):
    """The test that would have caught a silent schema change."""
    payload = _raw_payload(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        {"content": "true"},
    )
    assert isinstance(payload, dict) and isinstance(payload.get("jobs"), list)
    posting = payload["jobs"][0]

    missing = [f for f in GREENHOUSE_REQUIRED if f not in posting]
    assert not missing, f"greenhouse dropped required field(s): {missing}"

    absent = [f for f in GREENHOUSE_EXPECTED if f not in posting]
    assert not absent, (
        f"greenhouse no longer returns {absent} — the parser degrades silently "
        "when these vanish (no location, no description, or no date at all)"
    )


@pytest.mark.parametrize("slug", GREENHOUSE_SLUGS[:1])
def test_greenhouse_content_is_still_entity_escaped(slug):
    """The single most fragile assumption in the Greenhouse parser.

    `content` is HTML that has been entity-escaped *again*, so it arrives as
    `&lt;p&gt;`. We unescape exactly once. If Greenhouse ever stops
    double-encoding, that single unescape starts mangling real text.
    """
    payload = _raw_payload(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        {"content": "true"},
    )
    contents = [str(j.get("content") or "") for j in payload["jobs"][:20]]
    sample = next((c for c in contents if c.strip()), "")
    if not sample:
        pytest.skip("no posting on this board carries a description")
    assert "&lt;" in sample or "&amp;" in sample, (
        "greenhouse `content` no longer looks entity-escaped — "
        "src/sources/ats_boards.py unescapes it once and would now corrupt text"
    )


@pytest.mark.parametrize("slug", GREENHOUSE_SLUGS[:1])
def test_greenhouse_still_publishes_first_published(slug):
    """We prefer `first_published` because `updated_at` moves on any edit and
    badly overstates freshness. If it disappears, every posting silently falls
    back to the inflated date."""
    payload = _raw_payload(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        {"content": "true"},
    )
    have = sum(1 for j in payload["jobs"][:50] if j.get("first_published"))
    assert have, (
        "no posting carries `first_published` — freshness now rests entirely "
        "on `updated_at`, which is not a publication date"
    )


@pytest.mark.parametrize("slug", GREENHOUSE_SLUGS[:1])
def test_greenhouse_jobs_parse_into_usable_records(slug):
    jobs = _reachable(fetch_greenhouse, slug)
    assert all(j.title and j.url for j in jobs)
    assert all(j.ats == "greenhouse" and j.ats_job_id for j in jobs)
    dated = [j for j in jobs if j.posted_at]
    assert dated, "not one posting carried a parseable date"
    # A date in the future, or older than the company, means we misread it.
    now = utcnow()
    for job in dated[:50]:
        assert job.posted_at < now + timedelta(days=2), f"future date: {job.posted_at}"
        assert job.posted_at > now - timedelta(days=365 * 20)


# ==========================================================================
# Lever
# ==========================================================================


@pytest.mark.parametrize("slug", LEVER_SLUGS)
def test_lever_board_still_answers(slug):
    jobs = _reachable(fetch_lever, slug)
    assert jobs, f"lever/{slug} returned zero postings — has the slug moved?"


@pytest.mark.parametrize("slug", LEVER_SLUGS[:1])
def test_lever_payload_still_has_the_fields_we_parse(slug):
    payload = _raw_payload(f"https://api.lever.co/v0/postings/{slug}",
                           {"mode": "json"})
    assert isinstance(payload, list) and payload, "lever no longer returns a list"
    posting = payload[0]

    missing = [f for f in LEVER_REQUIRED if f not in posting]
    assert not missing, f"lever dropped required field(s): {missing}"
    absent = [f for f in LEVER_EXPECTED if f not in posting]
    assert not absent, f"lever no longer returns {absent}"


@pytest.mark.parametrize("slug", LEVER_SLUGS[:1])
def test_lever_created_at_is_still_a_millisecond_epoch(slug):
    """We rely on `parse_datetime` detecting ms-vs-seconds by magnitude. If
    Lever switched to seconds, every posting would be dated 1970 and dropped
    as stale — silently, since undated/stale jobs just vanish."""
    payload = _raw_payload(f"https://api.lever.co/v0/postings/{slug}",
                           {"mode": "json"})
    stamps = [p.get("createdAt") for p in payload[:20] if p.get("createdAt")]
    if not stamps:
        pytest.skip("no posting carries createdAt")
    assert all(isinstance(s, (int, float)) and s > 1e11 for s in stamps), (
        f"lever createdAt no longer looks like a millisecond epoch: {stamps[:3]}"
    )


@pytest.mark.parametrize("slug", LEVER_SLUGS[:1])
def test_lever_still_splits_requirements_into_lists(slug):
    """`lists` is where the requirements live. Dropping it would leave the
    scorer judging an intro paragraph."""
    payload = _raw_payload(f"https://api.lever.co/v0/postings/{slug}",
                           {"mode": "json"})
    assert any(p.get("lists") for p in payload[:20]), (
        "no posting carries `lists` — descriptions are now intro-only and "
        "scores will be based on far less evidence"
    )


@pytest.mark.parametrize("slug", LEVER_SLUGS[:1])
def test_lever_jobs_parse_into_usable_records(slug):
    jobs = _reachable(fetch_lever, slug)
    assert all(j.title and j.url and j.ats == "lever" for j in jobs)
    assert any(len(j.description) > 200 for j in jobs), (
        "every description came back nearly empty — the parser is reading the "
        "wrong fields"
    )


# ==========================================================================
# our fixtures vs reality
# ==========================================================================


def test_the_offline_fixtures_match_the_shape_of_the_live_payload():
    """The point of this whole file, in one test.

    The offline suite is only as trustworthy as the fixtures it runs on. This
    compares the keys in `tests/fixtures/greenhouse_jobs.json` against a live
    posting and fails when reality has moved on — which is the moment those
    958 green tests stop meaning what they appear to mean.
    """
    from tests.conftest import load_json_fixture

    live = _raw_payload(
        f"https://boards-api.greenhouse.io/v1/boards/{GREENHOUSE_SLUGS[0]}/jobs",
        {"content": "true"},
    )["jobs"][0]
    fixture = load_json_fixture("greenhouse_jobs.json")["jobs"][0]

    invented = set(fixture) - set(live)
    assert not invented, (
        f"the fixture claims field(s) the live API does not return: {sorted(invented)} "
        "— the offline tests are validating a payload that does not exist"
    )
