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
from src.sources.ats_boards import (
    ASHBY_JOB_BOARD_URL,
    RequestBudget,
    PERSONIO_XML_URL,
    SMARTRECRUITERS_POSTING_URL,
    SMARTRECRUITERS_POSTINGS_URL,
    WORKABLE_ACCOUNT_URL,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
    fetch_personio,
    fetch_smartrecruiters,
    fetch_workable,
)
from src.util import HttpError

pytestmark = pytest.mark.network

# Long-lived public boards. If one of these 404s, the slug moved — which is
# itself worth knowing, and is exactly the failure `--check-all` exists for.
GREENHOUSE_SLUGS = ["gitlab", "datadog"]
# The Lever anchor is UNPINNED: plaid left Lever, kraken exists but lists
# zero postings, spendesk 404s (all verified live 2026-09-01). The fixture
# below tries these candidates in order and SKIPS the vendor's tests when
# none answers with postings — a loud skip, not a red suite, until Phase 3's
# --discover pins a verified tenant at the FRONT of this list.
LEVER_SLUG_CANDIDATES = ["backmarket", "voodoo", "welocalize", "kraken"]

# --------------------------------------------------------------------------
# The four European boards.
#
# Read this before trusting a green run here. These parsers were written
# offline, against vendor documentation and known payload shapes, on a machine
# with **no outbound network** — not one byte of a real response was ever seen.
# Every assertion below is therefore a *hypothesis about the payload*, and this
# file is where those hypotheses get tested for the first time.
#
# The slugs are each vendor's own careers board, chosen because a vendor
# self-hosting is the likeliest slug to still exist — but they too are
# unverified. A 404 here means "fix the slug", not "the parser is broken";
# `--check` distinguishes the two in one command:
#
#     python -m src.sources.ats_boards --check workable <slug>
# --------------------------------------------------------------------------
# blueground answered WITH postings on the 2026-09-01 live run — the one
# verified Workable anchor; netdata exists but listed zero that day. Same
# skip-not-fail protocol as Lever while anchors settle.
WORKABLE_SLUG_CANDIDATES = ["blueground", "netdata"]
#: Collected-at-import users (the fixture-shape test, the autoapply sweep)
#: read the plain lists; the runtime anchor is picked by the fixtures below.
WORKABLE_SLUGS = WORKABLE_SLUG_CANDIDATES


def _live_anchor(board: str, candidates: list[str]) -> str:
    """First candidate the board answers with postings for, else a loud skip.

    Anchors rot — three rotted in the first two live runs alone — and a
    vendor's whole test block failing over a dead example trains the reader
    to ignore red. The skip message says exactly what to do instead.
    """
    from src.sources.ats_boards import PROBE_FOUND

    for candidate in candidates:
        probe = live_probe(board, candidate)
        if probe.status == PROBE_FOUND:
            return candidate
    pytest.skip(
        f"no {board} candidate answered with postings ({', '.join(candidates)})"
        " — pin a verified tenant here (Phase 3's --discover supplies them)"
    )


@pytest.fixture(scope="session")
def lever_slug():
    return _live_anchor("lever", LEVER_SLUG_CANDIDATES)


@pytest.fixture(scope="session")
def workable_slug():
    return _live_anchor("workable", WORKABLE_SLUG_CANDIDATES)
ASHBY_SLUGS = ["ashby"]
SMARTRECRUITERS_SLUGS = ["smartrecruiters"]
PERSONIO_SLUGS = ["personio"]

#: Fields the parsers actually read. Losing any one of them silently degrades
#: the pipeline rather than crashing it, which is why they are asserted here
#: rather than left to a try/except.
GREENHOUSE_REQUIRED = ("id", "title", "absolute_url")
GREENHOUSE_EXPECTED = ("location", "content", "updated_at")
LEVER_REQUIRED = ("id", "text", "hostedUrl")
LEVER_EXPECTED = ("categories", "createdAt")

#: `shortcode` is `Job.key`; without a title or a URL the posting is unusable.
WORKABLE_REQUIRED = ("title", "shortcode")
#: Everything the parser bets on beyond bare usability. `requirements` and
#: `benefits` only appear with `?details=true`, and they are half the ad.
# The live widget's shape as of 2026-09-01: requirements/benefits live
# inside `description`, geography inside the `locations` list.
WORKABLE_EXPECTED = ("published_on", "description", "locations",
                     "department", "url")

#: Every spelling the parser will accept for a Workable posting's *extra*
#: offices. Which one is live has never been seen; the test below records it.
WORKABLE_LOCATION_LIST_KEYS = ("locations", "secondary_locations",
                               "secondaryLocations", "additional_locations",
                               "additionalLocations", "other_locations")

ASHBY_REQUIRED = ("id", "title")
ASHBY_EXPECTED = ("location", "secondaryLocations", "isListed", "publishedAt",
                  "employmentType", "descriptionPlain", "jobUrl", "applyUrl")

SMARTRECRUITERS_REQUIRED = ("id", "name")
SMARTRECRUITERS_EXPECTED = ("location", "typeOfEmployment", "releasedDate",
                            "company")
#: The `jobAd.sections` keys the description is assembled from.
SMARTRECRUITERS_SECTION_KEYS = ("jobDescription", "qualifications")

#: Elements on a `<position>` the Personio parser reads.
PERSONIO_REQUIRED = ("id", "name")
PERSONIO_EXPECTED = ("office", "employmentType", "createdAt", "jobDescriptions")


#: An actual status code, not the letters "HTTP" — `HTTPSConnectionPool`
#: contains those and would turn every offline machine into a red suite.
_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


#: One cheap probe answers "is there a network at all?" for the whole file.
#: Without it, every test in here pays `util.http_get`'s full retry budget —
#: three attempts with exponential backoff — before concluding what the first
#: one already knew, and `pytest -m network` on a train takes minutes to tell
#: you it has nothing to say. `retries=1` because a probe that retries is
#: measuring the retry policy, not the network.
#:
#: **Several unrelated hosts, not one company's board.** The probe used to be
#: `boards-api.greenhouse.io/v1/boards/gitlab/jobs` alone, and it skipped the
#: whole file on *any* exception — a 404 included. The day GitLab leaves
#: Greenhouse, or renames its board, `pytest -m network` skips all forty
#: contract tests, prints "skipped", and reads exactly like a pass. The four
#: European boards this file exists to settle would go unchecked and nobody
#: would be told. One company's hiring decision must not be able to do that.
#:
#: These are chosen to be independent: different companies, different vendors,
#: different DNS. Any one of them answering proves there is a route out.
_PROBE_URLS: tuple[tuple[str, dict | None], ...] = (
    ("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs", {"content": "false"}),
    ("https://api.lever.co/v0/postings/plaid", {"mode": "json", "limit": "1"}),
    ("https://api.ashbyhq.com/posting-api/job-board/ashby", None),
)


def answered_and_rejected(exc: BaseException) -> bool:
    """True when the API *answered* — a status code came back — and said no.

    The one rule this file's skip-or-fail policy turns on, written once so that
    every helper below obeys the same one. A 404, a 403 or a 500 means the
    endpoint exists, heard us and refused: a finding, and a test that skips on
    it is a test that lies. A DNS failure or a refused connection is a train
    tunnel, and failing on that trains people to ignore this file.

    `HTTPSConnectionPool` contains the letters "HTTP", which is why the match
    is on an actual three-digit status and not on the word.
    """
    return isinstance(exc, HttpError) and bool(_STATUS_RE.search(str(exc)))


def probe_network(probes=_PROBE_URLS, *, get=None) -> None:
    """Decide, once for the file, whether there is a network to talk to.

    Three outcomes, deliberately distinguishable:

      * one probe answered -> there is a network; run the file. A *single*
        blocked or moved host is still reported per test by `_reachable`.
      * every probe failed at the transport level -> no route to anywhere.
        Skip; a train tunnel is not a broken API.
      * every probe was *answered* and rejected -> the probe URLs have rotted,
        or something is intercepting them. **Fail**, loudly. "The probe is
        broken" and "there is no network" must never look the same, because
        one of them silently disarms every test in this file and prints green.

    `get` is the seam: the meta-tests in `test_live_contract_policy.py` drive
    this offline, which is the only way to prove the policy without a network.
    """
    if get is None:
        from src.util import http_get as get

    rejected: list[str] = []
    unreachable: list[str] = []
    for url, params in probes:
        try:
            get(url, params=params, retries=1, timeout=10)
            return
        except Exception as exc:
            (rejected if answered_and_rejected(exc) else unreachable).append(
                f"{url} -> {exc}"
            )

    if rejected and not unreachable:
        pytest.fail(
            "every network probe was answered and rejected, so this is not an "
            "offline machine — the probe URLs have rotted, or something is "
            "intercepting them. Skipping here would disarm every test in this "
            "file and print green:\n  " + "\n  ".join(rejected)
        )
    pytest.skip("network unreachable: " + "; ".join(unreachable + rejected))


@pytest.fixture(scope="session", autouse=True)
def _network_or_skip():
    probe_network()


def _reachable(fn, *args, **kwargs):
    """Run a fetch, skipping the test when the network is the problem.

    A 404/403 means the API answered and rejected us — a real finding, so it
    fails. A DNS or connection failure is a train tunnel, so it skips.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if answered_and_rejected(exc):
            pytest.fail(f"the API answered but rejected us: {exc}")
        pytest.skip(f"network unreachable: {exc}")


def fetch_raw(url: str, params: dict | None = None, *, get=None):
    """One raw request, under exactly the policy `_reachable` uses.

    These helpers back the field-shape tests — the only tests in the whole
    suite that can settle whether the four European parsers read fields that
    exist. They used to `pytest.skip` on *any* exception, 404 and 500 included,
    so a moved slug or a revoked endpoint made every one of them read as green:
    a test that skips when it should fail is worse than no test at all.
    """
    if get is None:
        from src.util import http_get as get

    try:
        return get(url, params=params)
    except Exception as exc:
        if answered_and_rejected(exc):
            pytest.fail(f"the API answered but rejected us: {exc}")
        pytest.skip(f"network unreachable: {exc}")


def _raw_payload(url: str, params: dict | None = None):
    response = fetch_raw(url, params)
    try:
        return response.json()
    except Exception as exc:
        pytest.fail(f"{url} did not return JSON: {exc}")


def _raw_text(url: str, params: dict | None = None) -> str:
    """The response body as text — Personio's feed is XML, not JSON."""
    return getattr(fetch_raw(url, params), "text", "") or ""


def _union_of_keys(postings, limit: int = 25) -> set[str]:
    """Every field name seen across the first `limit` live postings.

    A single posting legitimately omits optional fields, so comparing a fixture
    against `postings[0]` alone reports fields as "invented" that the API does
    return — on the next posting. The union is the honest comparison.
    """
    keys: set[str] = set()
    for posting in postings[:limit]:
        if isinstance(posting, dict):
            keys.update(posting.keys())
    return keys


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


def test_lever_board_still_answers(lever_slug):
    slug = lever_slug
    jobs = _reachable(fetch_lever, slug)
    assert jobs, f"lever/{slug} returned zero postings — has the slug moved?"


def test_lever_payload_still_has_the_fields_we_parse(lever_slug):
    slug = lever_slug
    payload = _raw_payload(f"https://api.lever.co/v0/postings/{slug}",
                           {"mode": "json"})
    assert isinstance(payload, list) and payload, "lever no longer returns a list"
    posting = payload[0]

    missing = [f for f in LEVER_REQUIRED if f not in posting]
    assert not missing, f"lever dropped required field(s): {missing}"
    absent = [f for f in LEVER_EXPECTED if f not in posting]
    assert not absent, f"lever no longer returns {absent}"


def test_lever_created_at_is_still_a_millisecond_epoch(lever_slug):
    slug = lever_slug
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


def test_lever_still_splits_requirements_into_lists(lever_slug):
    slug = lever_slug
    """`lists` is where the requirements live. Dropping it would leave the
    scorer judging an intro paragraph."""
    payload = _raw_payload(f"https://api.lever.co/v0/postings/{slug}",
                           {"mode": "json"})
    assert any(p.get("lists") for p in payload[:20]), (
        "no posting carries `lists` — descriptions are now intro-only and "
        "scores will be based on far less evidence"
    )


def test_lever_jobs_parse_into_usable_records(lever_slug):
    slug = lever_slug
    jobs = _reachable(fetch_lever, slug)
    assert all(j.title and j.url and j.ats == "lever" for j in jobs)
    assert any(len(j.description) > 200 for j in jobs), (
        "every description came back nearly empty — the parser is reading the "
        "wrong fields"
    )


# ==========================================================================
# Workable
#
# Everything below is a hypothesis written without ever seeing a real payload.
# ==========================================================================


def test_workable_account_still_answers(workable_slug):
    slug = workable_slug
    jobs = _reachable(fetch_workable, slug)
    assert jobs, f"workable/{slug} returned zero postings — has the slug moved?"


def test_workable_payload_still_has_the_fields_we_parse(workable_slug):
    slug = workable_slug
    payload = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug), {"details": "true"})
    assert isinstance(payload, dict), "workable no longer returns an object"
    assert isinstance(payload.get("jobs"), list), "workable no longer returns `jobs`"
    postings = payload["jobs"]
    assert postings, "the account answered with no jobs at all"

    seen = _union_of_keys(postings)
    missing = [f for f in WORKABLE_REQUIRED if f not in seen]
    assert not missing, (
        f"workable dropped required field(s): {missing} — `shortcode` is "
        "`Job.key`, so losing it re-keys every posting on the board"
    )
    absent = [f for f in WORKABLE_EXPECTED if f not in seen]
    assert not absent, (
        f"workable no longer returns {absent} — the parser degrades silently "
        "when these vanish (no date, no location, or half the ad missing)"
    )


def test_workable_details_flag_is_what_produces_the_description(workable_slug):
    slug = workable_slug
    """The whole reason `?details=true` is sent. Without it the payload is
    title-and-location only, and every score would be based on a job title —
    a failure that produces plausible-looking numbers rather than an error."""
    thin = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug))
    fat = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug), {"details": "true"})
    with_text = [j for j in fat.get("jobs", []) if str(j.get("description") or "").strip()]
    assert with_text, (
        "`?details=true` no longer returns descriptions — the scorer would be "
        "judging job titles"
    )
    without = [j for j in thin.get("jobs", []) if str(j.get("description") or "").strip()]
    if without:
        pytest.skip("workable now returns descriptions without ?details=true too")


def test_workable_requirements_and_benefits_are_separate_blocks(workable_slug):
    slug = workable_slug
    """The parser concatenates description + requirements + benefits, the way
    the Lever parser concatenates `lists`. If Workable folded them into
    `description` the concatenation is harmless; if it renamed them, half of
    every ad — the half with the years-of-experience in it — disappears."""
    payload = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug), {"details": "true"})
    postings = payload.get("jobs", [])
    if not any("requirements" in j for j in postings[:25]):
        # Verified 2026-09-01: the widget folded requirements/benefits into
        # `description` — the harmless direction, since the parser
        # concatenates all three. The details-flag test above is what proves
        # the substance still arrives.
        pytest.skip("requirements folded into description — harmless, documented")
    assert any(str(j.get("requirements") or "").strip() for j in postings[:25]), (
        "no posting carries `requirements` — the most scoring-relevant part of "
        "the ad is no longer being read"
    )


def test_workable_location_is_still_structured_parts(workable_slug):
    slug = workable_slug
    """`Job.location` is assembled here from city/region/country because
    Workable never sends a sentence. The country key has two known spellings
    (`countryCode` and `country_code`) and the parser reads both — this test
    exists to record which one is actually live."""
    payload = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug), {"details": "true"})
    postings = payload.get("jobs", [])[:25]
    objects = [j.get("location") for j in postings if isinstance(j.get("location"), dict)]
    if not objects:
        # Verified 2026-09-01: the singular object is gone; geography now
        # arrives only in the `locations` list, whose entries must still be
        # structured for the parser's city/country assembly to work.
        listed = [
            node
            for j in postings
            for node in (j.get("locations") or [])
            if isinstance(node, dict)
        ]
        assert listed, (
            "neither `location` objects nor structured `locations` entries — "
            "the parser has no geography to assemble"
        )
        return
    keys = set()
    for loc in objects:
        keys.update(loc.keys())
    assert "city" in keys or "country" in keys, (
        f"workable location no longer carries city/country, only {sorted(keys)} — "
        "Job.location would be empty and every posting would fail the geo filter"
    )
    assert "telecommuting" in keys, (
        "workable no longer states `telecommuting` — remote postings would have "
        "an empty location and be dropped as unresolvable"
    )


def test_workable_still_publishes_published_on(workable_slug):
    slug = workable_slug
    """The parser dates a posting by `published_on`, never by `created_at`.

    `created_at` is when the requisition *record* was made — the day someone
    opened the draft — and drafting weeks ahead is ordinary recruiting. A req
    begun on 6 July and published on 4 August is brand new and would be dated a
    month old, rejected as stale, and never seen. If `published_on` disappears
    the parser falls back to exactly that, so this is the test that has to
    notice."""
    payload = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug), {"details": "true"})
    postings = payload.get("jobs", [])
    have = sum(1 for j in postings[:50] if j.get("published_on"))
    assert have, (
        "no posting carries `published_on` — freshness now rests on "
        "`created_at`, which dates a posting by when its draft was opened and "
        "silently ages every req that was written in advance"
    )


def test_workable_says_somewhere_which_offices_a_posting_is_open_in(workable_slug):
    slug = workable_slug
    """A posting open in San Francisco *and* Valencia must not be pinned to the
    first one: US companies list their offices home-first, so `location` alone
    reads as unambiguously American and the geo veto deletes a European role.
    That is the same failure `allLocations` fixes on Lever and
    `secondaryLocations` on Ashby.

    The key name here is a hypothesis — the parser reads six spellings for that
    reason — and this test is the only thing that can say which is real."""
    payload = _raw_payload(WORKABLE_ACCOUNT_URL.format(slug=slug), {"details": "true"})
    postings = payload.get("jobs", [])
    seen = _union_of_keys(postings, limit=50)
    found = [k for k in WORKABLE_LOCATION_LIST_KEYS if k in seen]
    assert found, (
        "no posting carries any of "
        f"{list(WORKABLE_LOCATION_LIST_KEYS)}, so a multi-office posting is "
        f"pinned to its primary location. Keys actually present: {sorted(seen)} "
        "— add the real one to `_WORKABLE_LOCATION_LIST_KEYS`"
    )
    for key in found:
        for posting in postings[:50]:
            for entry in posting.get(key) or []:
                assert isinstance(entry, dict), (
                    f"workable `{key}` entries are now {type(entry).__name__}; "
                    "the parser assembles them from city/region/country"
                )


def test_workable_jobs_parse_into_usable_records(workable_slug):
    slug = workable_slug
    jobs = _reachable(fetch_workable, slug)
    assert all(j.title and j.url for j in jobs)
    assert all(j.ats == "workable" and j.ats_job_id for j in jobs)
    assert any(len(j.description) > 200 for j in jobs), (
        "every description came back nearly empty — the parser is reading the "
        "wrong fields"
    )
    dated = [j for j in jobs if j.posted_at]
    assert dated, "not one posting carried a parseable date"
    now = utcnow()
    for job in dated[:50]:
        assert job.posted_at < now + timedelta(days=2), f"future date: {job.posted_at}"
        assert job.posted_at > now - timedelta(days=365 * 20)


# ==========================================================================
# Ashby
# ==========================================================================


@pytest.mark.parametrize("slug", ASHBY_SLUGS)
def test_ashby_board_still_answers(slug):
    jobs = _reachable(fetch_ashby, slug)
    assert jobs, f"ashby/{slug} returned zero postings — has the slug moved?"


@pytest.mark.parametrize("slug", ASHBY_SLUGS[:1])
def test_ashby_payload_still_has_the_fields_we_parse(slug):
    payload = _raw_payload(ASHBY_JOB_BOARD_URL.format(slug=slug),
                           {"includeCompensation": "true"})
    assert isinstance(payload, dict), "ashby no longer returns an object"
    assert isinstance(payload.get("jobs"), list), "ashby no longer returns `jobs`"
    postings = payload["jobs"]
    assert postings, "the board answered with no jobs at all"

    seen = _union_of_keys(postings)
    missing = [f for f in ASHBY_REQUIRED if f not in seen]
    assert not missing, f"ashby dropped required field(s): {missing}"
    absent = [f for f in ASHBY_EXPECTED if f not in seen]
    assert not absent, (
        f"ashby no longer returns {absent} — losing `isListed` would surface "
        "drafts, and losing `secondaryLocations` pins every multi-city role to "
        "its primary office"
    )


@pytest.mark.parametrize("slug", ASHBY_SLUGS[:1])
def test_ashby_secondary_locations_are_objects_with_a_location_field(slug):
    """The parser accepts both objects and bare strings, because the API
    changed shape once already. This test records which one is live, so the
    day the other disappears is a day someone finds out."""
    payload = _raw_payload(ASHBY_JOB_BOARD_URL.format(slug=slug),
                           {"includeCompensation": "true"})
    entries = [
        entry
        for job in payload.get("jobs", [])[:40]
        for entry in (job.get("secondaryLocations") or [])
    ]
    if not entries:
        pytest.skip("no posting on this board is open in more than one place")
    for entry in entries[:10]:
        assert isinstance(entry, (dict, str)), (
            f"secondaryLocations entries are now {type(entry).__name__} — the "
            "parser reads objects and strings and would drop every extra city"
        )
        if isinstance(entry, dict):
            assert entry.get("location") or entry.get("name"), (
                f"a secondaryLocations object has no readable name: {sorted(entry)}"
            )


@pytest.mark.parametrize("slug", ASHBY_SLUGS[:1])
def test_ashby_still_publishes_published_at(slug):
    """The parser deliberately refuses `updatedAt`, which moves on any edit.
    If `publishedAt` disappears every posting becomes undated, and
    `freshness.skip_undated` (on by default) then drops the entire board —
    silently, because undated jobs just vanish."""
    payload = _raw_payload(ASHBY_JOB_BOARD_URL.format(slug=slug),
                           {"includeCompensation": "true"})
    have = sum(1 for j in payload.get("jobs", [])[:50] if j.get("publishedAt"))
    assert have, (
        "no posting carries `publishedAt` — with skip_undated on, this board "
        "now contributes nothing at all"
    )


@pytest.mark.parametrize("slug", ASHBY_SLUGS[:1])
def test_ashby_jobs_parse_into_usable_records(slug):
    jobs = _reachable(fetch_ashby, slug)
    assert all(j.title and j.url for j in jobs)
    assert all(j.ats == "ashby" and j.ats_job_id for j in jobs)
    assert any(len(j.description) > 200 for j in jobs), (
        "every description came back nearly empty — the parser is reading the "
        "wrong fields"
    )


# ==========================================================================
# SmartRecruiters
# ==========================================================================


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS)
def test_smartrecruiters_company_still_answers(slug):
    jobs = _reachable(fetch_smartrecruiters, slug, details=False)
    assert jobs, f"smartrecruiters/{slug} returned zero postings — slug moved?"


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS[:1])
def test_smartrecruiters_listing_still_has_the_fields_we_parse(slug):
    payload = _raw_payload(SMARTRECRUITERS_POSTINGS_URL.format(slug=slug),
                           {"limit": 100})
    assert isinstance(payload, dict), "smartrecruiters no longer returns an object"
    assert isinstance(payload.get("content"), list), (
        "smartrecruiters no longer returns `content` — the parser would see "
        "zero postings on a company that is hiring"
    )
    postings = payload["content"]
    assert postings, "the company answered with no postings at all"

    seen = _union_of_keys(postings)
    missing = [f for f in SMARTRECRUITERS_REQUIRED if f not in seen]
    assert not missing, f"smartrecruiters dropped required field(s): {missing}"
    absent = [f for f in SMARTRECRUITERS_EXPECTED if f not in seen]
    assert not absent, f"smartrecruiters no longer returns {absent}"


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS[:1])
def test_smartrecruiters_still_pages_by_offset_and_says_how_many_there_are(slug):
    """The listing is paged and the fetcher walks it with `?offset=`.

    Two things have to stay true for that walk to terminate on the right page:
    the envelope still reports `totalFound`, and `offset` still means what
    https://developers.smartrecruiters.com/docs/pagination says it means. If
    `offset` were ignored, page two would be page one again and a large company
    would be read as its first hundred roles over and over."""
    first = _raw_payload(SMARTRECRUITERS_POSTINGS_URL.format(slug=slug),
                         {"limit": 10, "offset": 0})
    assert isinstance(first, dict)
    total = first.get("totalFound")
    assert isinstance(total, int), (
        "the listing envelope no longer reports `totalFound` — the fetcher can "
        "no longer tell a finished board from a truncated one"
    )
    page_one = [p.get("id") for p in first.get("content", [])]
    if total <= len(page_one) or len(page_one) < 10:
        pytest.skip("this company has too few postings to prove paging")

    second = _raw_payload(SMARTRECRUITERS_POSTINGS_URL.format(slug=slug),
                          {"limit": 10, "offset": 10})
    page_two = [p.get("id") for p in second.get("content", [])]
    assert page_two, f"offset=10 returned nothing on a board of {total} postings"
    assert not (set(page_one) & set(page_two)), (
        "`offset` no longer advances the window — every page is page one, so a "
        "company with 250 roles would contribute the same 100 forever"
    )


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS[:1])
def test_the_smartrecruiters_listing_still_carries_no_description(slug):
    """The single fact that shapes this source. If SmartRecruiters ever put the
    ad in the listing, the per-posting detail call — one HTTP request per job,
    capped, and the most expensive thing in the fetch stage — becomes pure
    waste and should be deleted."""
    payload = _raw_payload(SMARTRECRUITERS_POSTINGS_URL.format(slug=slug),
                           {"limit": 100})
    postings = payload.get("content", [])
    if any(p.get("jobAd") for p in postings[:25]):
        pytest.skip(
            "the listing now carries `jobAd` — the per-posting detail fetch in "
            "src/sources/ats_boards.py is now unnecessary and can be removed"
        )


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS[:1])
def test_smartrecruiters_detail_still_carries_jobad_sections(slug):
    """Where the entire description comes from. `jobAd.sections.*.text` is the
    only route to an ad on this vendor, so a rename here means every
    SmartRecruiters job is scored on its title."""
    listing = _raw_payload(SMARTRECRUITERS_POSTINGS_URL.format(slug=slug),
                           {"limit": 100})
    postings = [p for p in listing.get("content", []) if p.get("id")]
    if not postings:
        pytest.skip("no posting carries an id")

    detail = _raw_payload(SMARTRECRUITERS_POSTING_URL.format(
        slug=slug, posting_id=postings[0]["id"]))
    assert isinstance(detail, dict)
    job_ad = detail.get("jobAd")
    assert isinstance(job_ad, dict), "the detail payload no longer carries `jobAd`"
    sections = job_ad.get("sections")
    assert isinstance(sections, dict), "`jobAd.sections` is no longer an object"

    present = [k for k in SMARTRECRUITERS_SECTION_KEYS if isinstance(sections.get(k), dict)]
    assert present, (
        f"none of {list(SMARTRECRUITERS_SECTION_KEYS)} is in "
        f"jobAd.sections ({sorted(sections)}) — every description is now empty"
    )
    assert any(str(sections[k].get("text") or "").strip() for k in present), (
        "the section objects no longer carry `text`"
    )


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS[:1])
def test_smartrecruiters_apply_url_pattern_still_resolves(slug):
    """`jobs.smartrecruiters.com/{company}/{id}` is constructed rather than read
    out of the payload — the listing's `ref` is the API URL, which is JSON and
    useless to a human. A change here means every digest link 404s."""
    from src.util import http_get

    jobs = _reachable(fetch_smartrecruiters, slug, details=False)
    assert jobs
    url = jobs[0].url
    assert url.startswith("https://jobs.smartrecruiters.com/")
    try:
        response = http_get(url)
    except Exception as exc:
        pytest.skip(f"could not reach the applicant-facing host: {exc}")
    assert getattr(response, "status_code", 0) < 400, (
        f"the constructed apply URL is dead: {url} — every SmartRecruiters card "
        "in the digest links nowhere"
    )


@pytest.mark.parametrize("slug", SMARTRECRUITERS_SLUGS[:1])
def test_smartrecruiters_jobs_parse_into_usable_records(slug):
    jobs = _reachable(fetch_smartrecruiters, slug, max_descriptions=3)
    assert all(j.title and j.url for j in jobs)
    assert all(j.ats == "smartrecruiters" and j.ats_job_id for j in jobs)
    assert any(len(j.description) > 200 for j in jobs[:3]), (
        "not one of the first three postings got a description — the detail "
        "call or the section parsing is reading the wrong fields"
    )


# ==========================================================================
# Personio
# ==========================================================================


@pytest.mark.parametrize("slug", PERSONIO_SLUGS)
def test_personio_feed_still_answers(slug):
    jobs = _reachable(fetch_personio, slug)
    assert jobs, f"personio/{slug} returned zero positions — has the tenant moved?"


@pytest.mark.parametrize("slug", PERSONIO_SLUGS[:1])
def test_personio_still_serves_workzag_jobs_xml(slug):
    """The feed is XML with a `<workzag-jobs>` root — a name from Personio's
    pre-rebrand days that has outlived it. The parser rejects any other root
    outright, because an HTML error page is well-formed XML and would otherwise
    parse into zero positions and read as a company that is not hiring."""
    import xml.etree.ElementTree as ElementTree

    body = _raw_text(PERSONIO_XML_URL.format(host=f"{slug}.jobs.personio.de"))
    assert body.strip(), "the feed is empty"
    root = ElementTree.fromstring(body)
    assert str(root.tag).rsplit("}", 1)[-1] == "workzag-jobs", (
        f"the Personio feed root is now <{root.tag}> — src/sources/ats_boards.py "
        "rejects anything else and this board now yields nothing"
    )


@pytest.mark.parametrize("slug", PERSONIO_SLUGS[:1])
def test_personio_still_accepts_the_documented_language_parameter(slug):
    """`?language=` is documented (de/en/fr/es/nl/it/pt) and is opt-in per
    watchlist entry. It is *not* sent by default, on purpose — a tenant only
    publishes the languages its career site is configured for, and asking for
    one it does not have is how a real ad comes back empty. This test says only
    that a tenant which does publish a language still answers when asked."""
    body = _raw_text(
        PERSONIO_XML_URL.format(host=f"{slug}.jobs.personio.de"), {"language": "en"}
    )
    assert body.strip(), (
        "the feed answered empty for ?language=en — a watchlist entry that "
        "sets `language` would silently contribute nothing"
    )
    assert "<position" in body, (
        "?language=en returned a document with no positions in it; either this "
        "tenant does not publish English or the parameter has changed meaning"
    )


@pytest.mark.parametrize("slug", PERSONIO_SLUGS[:1])
def test_personio_position_still_has_the_elements_we_parse(slug):
    import xml.etree.ElementTree as ElementTree

    body = _raw_text(PERSONIO_XML_URL.format(host=f"{slug}.jobs.personio.de"))
    root = ElementTree.fromstring(body)
    positions = list(root.iter("position"))
    assert positions, "the feed carries no <position> elements"

    seen: set[str] = set()
    for position in positions[:25]:
        seen.update(str(child.tag).rsplit("}", 1)[-1] for child in position)

    missing = [f for f in PERSONIO_REQUIRED if f not in seen]
    assert not missing, (
        f"personio dropped required element(s): {missing} — `id` is `Job.key` "
        "and the apply URL, `name` is the title"
    )
    absent = [f for f in PERSONIO_EXPECTED if f not in seen]
    assert not absent, (
        f"personio no longer sends {absent} — `office` is the ONLY geography "
        "this feed states, so losing it fails every posting at the geo filter"
    )


@pytest.mark.parametrize("slug", PERSONIO_SLUGS[:1])
def test_personio_job_descriptions_are_still_titled_html_sections(slug):
    """Each `<jobDescription>` is a `<name>` heading plus a CDATA-wrapped HTML
    `<value>`, and the profile section is where every requirement lives. The
    parser concatenates them the way it concatenates Lever's `lists`."""
    import xml.etree.ElementTree as ElementTree

    body = _raw_text(PERSONIO_XML_URL.format(host=f"{slug}.jobs.personio.de"))
    root = ElementTree.fromstring(body)
    positions = list(root.iter("position"))
    sections = [
        section
        for position in positions[:25]
        for section in position.iter("jobDescription")
    ]
    if not sections and len(positions) < 3:
        pytest.skip(
            f"{slug} lists only {len(positions)} position(s), none with "
            "sections — a board this small cannot prove the structure; swap "
            "in a bigger live tenant (Phase 3's --discover will supply "
            "verified ones)"
        )
    assert sections, (
        "no position carries a <jobDescription> — every ad would be empty"
    )
    assert any((s.findtext("value") or "").strip() for s in sections), (
        "no <jobDescription> carries a <value> — the description is the field, "
        "not the heading"
    )


@pytest.mark.parametrize("slug", PERSONIO_SLUGS[:1])
def test_personio_created_at_offset_is_still_parseable(slug):
    """Personio writes the offset without a colon ("+0200"), which
    `datetime.fromisoformat` only accepts on Python 3.11+. If that ever became
    a format `util.parse_datetime` could not read, every position would be
    undated and `skip_undated` would drop the whole tenant in silence."""
    from src.util import parse_datetime

    import xml.etree.ElementTree as ElementTree

    body = _raw_text(PERSONIO_XML_URL.format(host=f"{slug}.jobs.personio.de"))
    root = ElementTree.fromstring(body)
    stamps = [
        (p.findtext("createdAt") or "").strip()
        for p in list(root.iter("position"))[:25]
    ]
    stamps = [s for s in stamps if s]
    if not stamps:
        pytest.skip("no position carries createdAt")
    unparseable = [s for s in stamps if parse_datetime(s) is None]
    assert not unparseable, (
        f"util.parse_datetime cannot read Personio's dates any more: "
        f"{unparseable[:3]} — every position becomes undated and is dropped"
    )


@pytest.mark.parametrize("slug", PERSONIO_SLUGS[:1])
def test_personio_jobs_parse_into_usable_records(slug):
    jobs = _reachable(fetch_personio, slug)
    assert all(j.title and j.url for j in jobs)
    assert all(j.ats == "personio" and j.ats_job_id for j in jobs)
    assert all(".jobs.personio." in j.url for j in jobs)
    if len(jobs) < 3 and not any(len(j.description) > 200 for j in jobs):
        pytest.skip(
            f"{slug} lists only {len(jobs)} position(s) with no real body — "
            "too small to prove description parsing; swap in a bigger tenant"
        )
    assert any(len(j.description) > 200 for j in jobs), (
        "every description came back nearly empty — the parser is reading the "
        "wrong elements"
    )


# ==========================================================================
# none of these may be auto-appliable
# ==========================================================================


def test_no_live_board_produces_a_job_autoapply_would_submit():
    """The safety claim, checked against real URLs rather than fixtures.

    `autoapply` has only ever been hardened against Greenhouse and Lever forms.
    A live posting whose URL `detect_ats` accepts — a company that redirects its
    Ashby board to a Greenhouse form, say — would put the bot in front of a form
    no test has ever seen.
    """
    from src.apply.autoapply import SUPPORTED_ATS, detect_ats

    fetchers = [
        (fetch_workable, WORKABLE_SLUGS), (fetch_ashby, ASHBY_SLUGS),
        (fetch_smartrecruiters, SMARTRECRUITERS_SLUGS),
        (fetch_personio, PERSONIO_SLUGS),
    ]
    checked = 0
    for fetcher, slugs in fetchers:
        try:
            jobs = fetcher(slugs[0])
        except Exception:
            continue          # covered by that board's own test above
        checked += 1
        for job in jobs[:50]:
            assert job.ats not in SUPPORTED_ATS, (
                f"{job.ats} now claims to be an auto-appliable ATS"
            )
            assert detect_ats(job.url) is None, (
                f"{job.url} would be auto-applied to by a bot that has never "
                "been tested against this vendor's form"
            )
    if not checked:
        pytest.skip("no European board was reachable")


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


#: Fields a fixture carries **on purpose to prove the parser ignores them**.
#:
#: A negative test needs the field present to be a test at all — "we do not
#: date a posting from `updated_at`" is unprovable against a payload with no
#: `updated_at` in it. But the parser never reads them, so their absence from a
#: live response says nothing about whether the parser is exercised against a
#: payload that exists, which is the only thing the test below is asking. They
#: are excluded from the comparison rather than removed from the fixtures.
_DELIBERATELY_IGNORED_FIELDS: dict[str, frozenset[str]] = {
    # Modified dates. Reading one overstates freshness — a typo fix on a
    # three-month-old req looks like today's news.
    "workable_jobs.json": frozenset({
        "updated_at",
        # The live widget slimmed down (verified 2026-09-01): requirements
        # and benefits folded into `description`, the singular `location`
        # object replaced by the `locations` list, workplace_type gone. The
        # parser deliberately still reads every old spelling — tenants and
        # gateways lag API changes — and the offline tests document that
        # path, so the fixture keeps the fields on purpose.
        "requirements", "benefits", "location", "workplace_type",
    }),
    "smartrecruiters_postings.json": frozenset({
        "updatedOn",
        # Live payloads stopped sending createdOn (verified 2026-09-01); the
        # parser keeps it as a deliberate *fallback floor* for tenants that
        # still do, and the unit tests document that semantics — the fixture
        # carries it so those tests stay honest about the fallback.
        "createdOn",
    }),
    # The one-line OG-card teaser. Scoring it instead of the ad produces a
    # perfectly reasonable-looking number from a sentence of marketing.
    "ashby_jobs.json": frozenset({"descriptionSocial"}),
}


@pytest.mark.parametrize(
    "fixture_name,fixture_key,url,params,live_key,slug",
    [
        ("workable_jobs.json", "jobs",
         WORKABLE_ACCOUNT_URL, {"details": "true"}, "jobs", WORKABLE_SLUGS[0]),
        ("ashby_jobs.json", "jobs",
         ASHBY_JOB_BOARD_URL, {"includeCompensation": "true"}, "jobs", ASHBY_SLUGS[0]),
        ("smartrecruiters_postings.json", "content",
         SMARTRECRUITERS_POSTINGS_URL, {"limit": 100, "offset": 0}, "content",
         SMARTRECRUITERS_SLUGS[0]),
    ],
)
def test_the_european_fixtures_do_not_claim_fields_reality_never_sends(
    fixture_name, fixture_key, url, params, live_key, slug
):
    """The most important test in this file for the four new boards.

    Their fixtures were written from vendor documentation on a machine with no
    network, so unlike `greenhouse_jobs.json` they have never once been checked
    against a real response. Every offline test for Workable, Ashby and
    SmartRecruiters is only as true as these files. A field here that the live
    API does not send means the parser is being exercised against a payload
    that does not exist — green tests proving nothing.

    Compared against the *union* of keys across many live postings, because one
    posting legitimately omits optional fields.
    """
    from tests.conftest import load_json_fixture

    live = _raw_payload(url.format(slug=slug), params)
    postings = live.get(live_key) if isinstance(live, dict) else None
    if not isinstance(postings, list) or not postings:
        pytest.skip(f"{slug} returned no postings to compare against")

    fixture = load_json_fixture(fixture_name)[fixture_key]
    invented = (
        _union_of_keys(fixture, limit=50)
        - _union_of_keys(postings, limit=50)
        - _DELIBERATELY_IGNORED_FIELDS.get(fixture_name, frozenset())
    )
    assert not invented, (
        f"{fixture_name} claims field(s) the live API does not return: "
        f"{sorted(invented)} — every offline test for this source is validating "
        "a payload that does not exist"
    )


def test_the_personio_fixture_does_not_claim_elements_reality_never_sends():
    """The XML twin of the test above, for the same reason."""
    import xml.etree.ElementTree as ElementTree

    from tests.conftest import load_fixture

    body = _raw_text(
        PERSONIO_XML_URL.format(host=f"{PERSONIO_SLUGS[0]}.jobs.personio.de")
    )
    live_positions = list(ElementTree.fromstring(body).iter("position"))
    if not live_positions:
        pytest.skip("the live feed carries no positions to compare against")

    def tags(positions):
        return {
            str(child.tag).rsplit("}", 1)[-1]
            for position in positions[:50] for child in position
        }

    fixture_positions = list(
        ElementTree.fromstring(load_fixture("personio_positions.xml")).iter("position")
    )
    # `<updatedAt>` is in the fixture only so that "we never date a position
    # from it" is a test rather than an aspiration; the parser never reads it.
    invented = tags(fixture_positions) - tags(live_positions) - {"updatedAt"}
    assert not invented, (
        f"personio_positions.xml claims element(s) the live feed does not send: "
        f"{sorted(invented)} — the offline Personio tests are validating a "
        "document that does not exist"
    )


# ==========================================================================
# discovery
#
# `--discover` guesses a slug and asks six vendors about it, so its whole
# confidence model rests on one assumption about the real internet: **a slug
# nobody owns is answered with a 404, not with an empty board.** If any vendor
# ever starts serving 200 + zero postings for an unknown tenant, every guess
# comes back "reachable, nothing open", and the tool starts recommending
# spellings that do not exist — which is the exact failure it was built to
# avoid, since a wrong slug in `watchlist.yaml` produces an empty board every
# morning that reads as a quiet market.
#
# That cannot be settled offline: the fixture and the parser would agree with
# each other and both be wrong. It is settled here.
# ==========================================================================

#: A slug no tenant will ever own. Deliberately not random: a rerun must ask
#: the same question, and a random string would make a failure unreproducible.
NONEXISTENT_SLUG = "no-such-company-job-hunter-probe-9f3a"


def live_probe(board: str, slug: str, *, probe=None):
    """One discovery probe, under this file's skip-or-fail policy.

    `probe_board` never raises — it classifies — so the policy is applied to the
    classification instead of to an exception. Only `unreachable` (no answer at
    all: DNS, refused connection, timeout) skips. Everything else, `error`
    included, is an answer and is handed back for the test to assert on: a 403
    or a 429 means the board heard us and refused, which is a finding, and a
    test that skips on it prints green while proving nothing.

    `probe` is the seam: `test_live_contract_policy.py` drives this gate
    offline, in the default run, which is the only way to prove the policy on
    the machine where nobody is looking.
    """
    from src.sources.ats_boards import PROBE_UNREACHABLE, probe_board

    result = (probe or probe_board)(board, slug)
    if result.status == PROBE_UNREACHABLE:
        pytest.skip(f"network unreachable: {board}/{slug}: {result.message}")
    return result


def swept_or_skip(result):
    """The same policy for a whole discovery sweep instead of one probe.

    Skip only when nothing was asked (the budget was already spent) or when
    *nothing at all* answered — which is an offline machine. A single answered
    probe among the failures, a 403 included, means there is a network and an
    API spoke; the sweep's result is handed back so the test's assertions run
    and fail loudly on it. In particular a sweep of pure 404s must reach the
    assertions: all-absent is not a train tunnel, it is the derivation failing
    to produce the real slug — the exact finding the caller exists to make.
    """
    from src.sources.ats_boards import PROBE_UNREACHABLE

    if not result.probes:
        pytest.skip("the request budget was spent before anything was asked")
    if all(p.status == PROBE_UNREACHABLE for p in result.probes):
        pytest.skip(f"network unreachable: {result.probes[0].message}")
    return result


@pytest.mark.parametrize("board", [
    "greenhouse", "lever", "workable", "ashby", "smartrecruiters", "personio",
])
def test_a_slug_nobody_owns_is_answered_with_a_404(board):
    """The assumption every confidence in the discovery module rests on.

    An `absent` probe is the one that lets discovery rule a board *out*. If this
    ever comes back `empty`, the sweep can no longer tell "this company is not
    here" from "this company is here and is not hiring", and it will happily
    recommend a slug that belongs to nobody.
    """
    from src.sources.ats_boards import PROBE_ABSENT, PROBE_EMPTY

    probe = live_probe(board, NONEXISTENT_SLUG)
    if "429" in (probe.message or ""):
        pytest.skip(f"{board} rate-limited the probe — rerun to settle it")
    if board == "smartrecruiters":
        # Verified 2026-09-01: SmartRecruiters answers 200 with an empty
        # `content` for a slug nobody owns — absence is NOT detectable on
        # this vendor. Discovery already treats an empty twin as a named
        # hole (confidence: medium), so the sweep stays honest; what it can
        # never do is rule SmartRecruiters out. Documented so a future 404
        # (them fixing it) fails here and upgrades discovery.
        assert probe.status == PROBE_EMPTY, (
            f"smartrecruiters now answers {probe.status!r} for a nonexistent "
            "slug — if that is 'absent', discovery can start ruling it out: "
            "remove this carve-out"
        )
        return
    assert probe.status != PROBE_EMPTY, (
        f"{board} answered 200 with an empty board for a slug that cannot "
        "exist — discovery can no longer rule this board out for any company, "
        "and every guess will look reachable"
    )
    assert probe.status == PROBE_ABSENT, (
        f"{board} answered a nonexistent slug with {probe.status!r} "
        f"({probe.message}) rather than a 404 — the discovery sweep classifies "
        "that as 'we do not know', so every company will come back qualified"
    )


@pytest.mark.parametrize("board,slug", [
    ("greenhouse", GREENHOUSE_SLUGS[0]),
])
def test_a_real_board_probes_as_found(board, slug):
    """The other half of the pair: a slug that does exist must come back
    `found`, with a posting count, from the cheap probe kwargs discovery uses
    (no descriptions, one SmartRecruiters page). If the cheap path stopped
    returning postings, every real company would read as an empty board."""
    from src.sources.ats_boards import PROBE_FOUND

    probe = live_probe(board, slug)
    assert probe.status == PROBE_FOUND, f"{board}/{slug}: {probe.message}"
    assert probe.count and probe.count > 0


def test_a_real_lever_board_probes_as_found(lever_slug):
    """The Lever half of the pair above, riding the anchor fixture so an
    unpinned anchor skips loudly instead of failing on a dead example."""
    from src.sources.ats_boards import PROBE_FOUND

    probe = live_probe("lever", lever_slug)
    assert probe.status == PROBE_FOUND, f"lever/{lever_slug}: {probe.message}"
    assert probe.count and probe.count > 0


def test_discovery_finds_a_company_from_its_name_alone():
    """End to end against the real internet: a company name in, the board and
    slug to paste out. This is the only test that exercises slug derivation,
    the sweep and the live boards together.

    Bounded explicitly, because this is the one feature that makes unsolicited
    requests: a deliberate `-m network` run must not turn into a tenant scan.
    """
    from src.sources.ats_boards import discover_company

    budget = RequestBudget(12)
    result = swept_or_skip(discover_company(GREENHOUSE_SLUGS[0].title(), budget=budget))

    hits = {(p.board, p.slug) for p in result.matches}
    assert ("greenhouse", GREENHOUSE_SLUGS[0]) in hits, (
        f"discovery did not find greenhouse/{GREENHOUSE_SLUGS[0]} from the "
        f"company name alone — it reported {result.confidence!r} with "
        f"{sorted(hits)}. Either the slug moved (which `--check` will confirm) "
        "or the derivation no longer produces it"
    )
    assert budget.spent <= budget.limit


def test_a_discovery_sweep_stays_inside_its_request_budget():
    """The bound is the feature. A budget that a real payload can walk past —
    SmartRecruiters' offset pagination is the obvious way — is not a bound."""
    from src.sources.ats_boards import discover

    budget = RequestBudget(8)
    results, budget = discover([NONEXISTENT_SLUG, "Some Other Company"],
                               budget=budget)
    if not any(r.probes for r in results):
        pytest.skip("nothing was probed")
    assert budget.spent <= 8
    assert budget.skipped, (
        "eight probes covered two whole companies, so the cap never engaged — "
        "this test is no longer testing anything"
    )


# ==========================================================================
# Recruitee (fixture is spec-derived: these tests are what validates it)
# ==========================================================================

# UNVERIFIED — chosen while this environment had no network route: long-lived
# Dutch tech tenants believed to run public Recruitee careers sites. If one
# 404s on the first `-m network` run, the slug moved (or the guess was wrong):
# swap in any tenant that `--check recruitee <slug>` passes and re-run.
# framer 404'd on the first live run; channable answered and parsed — the
# Recruitee parser is live-verified through it (2026-09-01).
RECRUITEE_SLUGS = ["channable"]
RECRUITEE_REQUIRED = ("id", "title")
RECRUITEE_EXPECTED = (
    "slug", "careers_url", "city", "country", "country_code",
    "created_at", "description", "requirements", "employment_type_code",
)


@pytest.mark.parametrize("slug", RECRUITEE_SLUGS)
def test_recruitee_board_still_answers(slug):
    from src.sources.ats_boards import fetch_recruitee

    jobs = _reachable(fetch_recruitee, slug)
    assert jobs, f"recruitee/{slug} returned zero offers — has the tenant moved?"
    assert all(j.ats == "recruitee" and j.url for j in jobs)


@pytest.mark.parametrize("slug", RECRUITEE_SLUGS[:1])
def test_recruitee_offers_still_have_the_fields_we_parse(slug):
    from src.sources.ats_boards import RECRUITEE_OFFERS_URL

    payload = _raw_payload(RECRUITEE_OFFERS_URL.format(slug=slug))
    offers = payload.get("offers")
    assert isinstance(offers, list) and offers, (
        "the payload no longer carries an 'offers' list — the envelope gate "
        "in fetch_recruitee will fail every tenant"
    )
    seen = _union_of_keys(offers)
    missing = [f for f in RECRUITEE_REQUIRED if f not in seen]
    assert not missing, (
        f"recruitee dropped required field(s): {missing} — `id` is the job "
        "identity, `title` is the title"
    )
    absent = [f for f in RECRUITEE_EXPECTED if f not in seen]
    assert not absent, (
        f"recruitee no longer sends {absent} — the offline fixture "
        "`recruitee_offers.json` was written from the documented shape and "
        "must be re-recorded from this live payload"
    )


@pytest.mark.parametrize("slug", RECRUITEE_SLUGS[:1])
def test_recruitee_salary_shape_when_published(slug):
    """`salary: {min, max, currency, period}` is the one structured salary any
    board here publishes. Tenants opt in per offer, so an absent block is not
    drift — but a present block with different keys is."""
    from src.sources.ats_boards import RECRUITEE_OFFERS_URL

    payload = _raw_payload(RECRUITEE_OFFERS_URL.format(slug=slug))
    published = [
        o["salary"] for o in payload.get("offers", [])
        if isinstance(o.get("salary"), dict) and o["salary"]
    ]
    if not published:
        pytest.skip("no offer on this tenant publishes a salary")
    keys = set().union(*(s.keys() for s in published))
    assert {"min", "max"} & keys, (
        f"recruitee salary objects now carry {sorted(keys)} — "
        "_recruitee_salary reads min/max/currency/period"
    )


# ==========================================================================
# Teamtailor (fixture is spec-derived: these tests are what validates it)
# ==========================================================================

# UNVERIFIED — chosen while this environment had no network route: a hosted
# tenant believed stable, plus Teamtailor's own careers site as the
# custom-domain shape. Same rule as Recruitee: a 404 means swap and re-run.
# mentimeter is not a hosted tenant (404 on the first live run); Teamtailor's
# own careers site answered and parsed — the RSS parser is live-verified
# through it (2026-09-01).
TEAMTAILOR_ENTRIES = ["https://career.teamtailor.com/jobs"]


@pytest.mark.parametrize("entry", TEAMTAILOR_ENTRIES)
def test_teamtailor_feed_still_answers(entry):
    from src.sources.ats_boards import fetch_teamtailor

    jobs = _reachable(fetch_teamtailor, entry)
    assert jobs, f"teamtailor/{entry} returned zero items — has the site moved?"
    assert all(j.ats == "teamtailor" and j.url for j in jobs)


@pytest.mark.parametrize("entry", TEAMTAILOR_ENTRIES[:1])
def test_teamtailor_still_serves_rss_with_the_elements_we_parse(entry):
    import xml.etree.ElementTree as ElementTree

    from src.sources.ats_boards import _teamtailor_feed_url

    body = _raw_text(_teamtailor_feed_url(entry))
    assert body.strip(), "the feed is empty"
    root = ElementTree.fromstring(body)
    assert str(root.tag).rsplit("}", 1)[-1].lower() == "rss", (
        f"the Teamtailor feed root is now <{root.tag}> — the parser rejects "
        "anything but <rss> and this board now yields nothing"
    )
    items = [el for el in root.iter() if str(el.tag).rsplit("}", 1)[-1] == "item"]
    assert items, "the feed carries no <item> elements"

    seen: set[str] = set()
    for item in items[:25]:
        seen.update(str(child.tag).rsplit("}", 1)[-1] for child in item)
    missing = [f for f in ("title", "link") if f not in seen]
    assert not missing, f"teamtailor items dropped {missing}"
    # The offline fixture assumes items MAY carry these; record what the live
    # feed actually does, because an item without any location element parses
    # with location="" and the geo filter drops it.
    informational = [f for f in ("pubDate", "location", "department") if f not in seen]
    if informational:
        pytest.skip(
            f"live feed omits {informational} — expected for some tenants; "
            "re-record teamtailor_jobs.rss from this feed if location is "
            "missing across ALL tenants you watch, and plan on the geo "
            "filter dropping location-less items"
        )


# ==========================================================================
# Arbeitnow (fixture is spec-derived: these tests are what validates it)
# ==========================================================================

ARBEITNOW_REQUIRED = ("title", "company_name", "url", "created_at")
# visa_sponsorship left the live payload (verified 2026-09-01); the parser
# still reads it defensively for the day it returns.
ARBEITNOW_EXPECTED = ("slug", "remote", "location", "tags", "job_types",
                      "description")


def test_arbeitnow_feed_still_answers_and_parses():
    from src.sources.arbeitnow import fetch

    jobs = _reachable(fetch, None)
    assert jobs, "arbeitnow returned zero postings"
    assert all(j.company and j.url for j in jobs)
    assert any(j.posted_at for j in jobs), (
        "no posting carries a usable created_at — the epoch parsing or the "
        "field name drifted, and freshness will drop everything"
    )


def test_arbeitnow_entries_still_have_the_fields_we_parse():
    from src.sources.arbeitnow import API_URL

    payload = _raw_payload(API_URL, {"page": 1})
    data = payload.get("data")
    assert isinstance(data, list) and data, (
        "the payload no longer carries a 'data' list — fetch() reports this "
        "as shape drift and the source degrades"
    )
    seen = _union_of_keys(data)
    missing = [f for f in ARBEITNOW_REQUIRED if f not in seen]
    assert not missing, f"arbeitnow dropped required field(s): {missing}"
    absent = [f for f in ARBEITNOW_EXPECTED if f not in seen]
    assert not absent, (
        f"arbeitnow no longer sends {absent} — re-record arbeitnow_page.json "
        "from this live payload"
    )
    assert isinstance(payload.get("links"), dict), (
        "pagination no longer speaks links.next — fetch() will stop at page 1"
    )


# ==========================================================================
# Landing.jobs (fixture is spec-derived: these tests are what validates it)
# ==========================================================================

LANDING_REQUIRED = ("id", "title")
#: Groups where ANY member satisfies the parser — the API has served several
#: spellings over time and `parse_job` reads all of them.
# The live listing schema, recorded 2026-09-01 from the API itself: no
# company field AT ALL (resolution goes through the per-job detail endpoint,
# tested separately below), geography in `locations`, salary as
# gross_salary_low/high + currency_code.
LANDING_EXPECTED_GROUPS = (
    ("url", "share_url", "landing_page"),
    ("published_at", "created_at"),
    ("locations", "city", "location"),
    ("gross_salary_low", "salary_low"),
)


def test_landing_jobs_feed_still_answers_and_parses():
    from src.sources.landing_jobs import fetch

    jobs = _reachable(fetch, None)
    # The DS/ML gate may legitimately leave few postings; zero *fetched* pages
    # would surface as shape drift below, so an empty parse here only warrants
    # a skip, not a fail.
    if not jobs:
        pytest.skip("no DS/ML-titled postings on the board today")
    assert all(j.company and j.url for j in jobs)


def test_landing_jobs_listings_still_have_the_fields_we_parse():
    from src.sources.landing_jobs import API_URL, PAGE_LIMIT

    payload = _raw_payload(API_URL, {"offset": 0, "limit": PAGE_LIMIT})
    # Guarded lookup: a JSON scalar must land on the drift message below,
    # not on an AttributeError.
    batch = payload if isinstance(payload, list) else (
        payload.get("jobs") if isinstance(payload, dict) else None
    )
    assert isinstance(batch, list) and batch, (
        "the payload is neither a bare list nor a 'jobs' list — fetch() "
        "reports this as shape drift and the source degrades"
    )
    seen = _union_of_keys(batch)
    missing = [f for f in LANDING_REQUIRED if f not in seen]
    assert not missing, f"landing.jobs dropped required field(s): {missing}"
    for group in LANDING_EXPECTED_GROUPS:
        assert any(f in seen for f in group), (
            f"landing.jobs sends none of {group} — parse_job cannot fill that "
            "field any more. Keys the live listing DOES send: "
            f"{sorted(seen)} — re-shape the parser/fixture from these"
        )


def test_landing_jobs_detail_carries_the_employer():
    """The listing names no company at all (live schema 2026-09-01), so
    parse-ability of an employer rests entirely on the per-job detail
    endpoint. Self-revealing on failure: the message carries the detail's
    real keys so the next paste re-shapes the resolver without a round trip."""
    from src.sources.landing_jobs import API_URL, JOB_DETAIL_URL

    listing = _raw_payload(API_URL, {"offset": 0, "limit": 5})
    batch = listing if isinstance(listing, list) else (
        listing.get("jobs") if isinstance(listing, dict) else None
    )
    entries = [e for e in batch or [] if isinstance(e, dict)]
    if not entries:
        pytest.skip("the board listed nothing to detail")
    job_id = entries[0].get("id")
    if not isinstance(job_id, int):
        pytest.skip("listing entries carry no integer id to detail")
    detail = _raw_payload(JOB_DETAIL_URL.format(job_id=job_id))
    assert isinstance(detail, dict), "detail endpoint did not return an object"
    keys = set(detail)
    assert keys & {"company_id", "company_name", "company", "company_slug"}, (
        "the detail endpoint names no employer either — keys it DOES send: "
        f"{sorted(keys)} — re-shape the resolver from these"
    )


# ==========================================================================
# Just Join IT — Tier 2 (fixture is spec-derived: these tests validate it)
# ==========================================================================

JUSTJOIN_REQUIRED = ("slug", "title", "companyName")
JUSTJOIN_EXPECTED = ("city", "workplaceType", "experienceLevel", "publishedAt",
                     "requiredSkills", "employmentTypes", "multilocation")


def _skip_if_justjoin_blocks(exc_or_message) -> None:
    """503 from api.justjoin.it even with browser-shaped headers (verified
    2026-09-01) — the block is TLS-fingerprint-level, below anything plain
    `requests` can spoof. The source self-reports as degraded in the digest's
    health table; these tests skip rather than paint the suite red over an
    accepted degradation. Re-scout the endpoint in the site's devtools
    (Network tab -> the offers request -> Copy as cURL) to revive it."""
    if "503" in str(exc_or_message):
        pytest.skip("justjoin.it 503s non-browser TLS — accepted degradation")


def test_justjoin_feed_still_answers_and_parses():
    from src.sources.justjoin_it import fetch

    errors: list[str] = []
    jobs = fetch(None, errors=errors)
    if errors:
        _skip_if_justjoin_blocks(errors[0])
    if not jobs:
        pytest.skip("no junior/mid DS-ML offers on the board right now")
    assert all(j.company and j.url for j in jobs)
    assert any(j.posted_at for j in jobs)


def test_justjoin_offers_still_have_the_fields_we_parse():
    from src.sources.justjoin_it import _BROWSER_HEADERS, API_URL, PER_PAGE
    from src.util import http_get

    try:
        response = http_get(API_URL, params={"page": 1, "perPage": PER_PAGE},
                            headers=dict(_BROWSER_HEADERS))
    except Exception as exc:
        _skip_if_justjoin_blocks(exc)
        raise
    payload = response.json()
    data = payload.get("data")
    assert isinstance(data, list) and data, (
        "the payload no longer carries a 'data' list — fetch() reports this "
        "as shape drift and the source degrades. This is the Tier 2 risk "
        "arriving; find the new endpoint in the site's devtools"
    )
    seen = _union_of_keys(data)
    missing = [f for f in JUSTJOIN_REQUIRED if f not in seen]
    assert not missing, f"justjoin.it dropped required field(s): {missing}"
    absent = [f for f in JUSTJOIN_EXPECTED if f not in seen]
    assert not absent, (
        f"justjoin.it no longer sends {absent} — re-record "
        "justjoin_offers.json from this live payload"
    )


def test_justjoin_still_honours_the_experience_param():
    """The request narrows to junior/mid. If the param dies the adapter still
    re-filters client-side, but silently fetching every seniority triples the
    pages walked for the same yield — worth knowing, not worth failing."""
    from src.sources.justjoin_it import API_URL

    from src.sources.justjoin_it import _BROWSER_HEADERS
    from src.util import http_get

    try:
        response = http_get(
            API_URL,
            params={"page": 1, "perPage": 50,
                    "experienceLevels[]": ["junior", "mid"]},
            headers=dict(_BROWSER_HEADERS),
        )
    except Exception as exc:
        _skip_if_justjoin_blocks(exc)
        raise
    payload = response.json()
    levels = {
        str(entry.get("experienceLevel", "")).lower()
        for entry in payload.get("data", [])
        if isinstance(entry, dict)
    } - {""}
    if levels and not levels <= {"junior", "mid"}:
        pytest.skip(
            f"experienceLevels[] is no longer honoured (saw {sorted(levels)}) "
            "— the client-side re-check is carrying the spec alone now"
        )


# ==========================================================================
# No Fluff Jobs — Tier 2 (fixture is spec-derived: these tests validate it)
# ==========================================================================

NOFLUFF_REQUIRED = ("id", "title", "name", "url")
NOFLUFF_EXPECTED = ("category", "seniority", "salary", "location", "posted")


def test_nofluff_listing_still_answers_and_parses():
    from src.sources.nofluffjobs import fetch

    jobs = _reachable(fetch, None)
    if not jobs:
        pytest.skip("no data/AI junior-mid postings on the board right now")
    assert all(j.company and j.url for j in jobs)
    assert any(j.posted_at for j in jobs)


def test_nofluff_postings_still_have_the_fields_we_parse():
    from src.sources.nofluffjobs import API_URL

    payload = _raw_payload(API_URL)
    postings = payload.get("postings")
    assert isinstance(postings, list) and postings, (
        "the payload no longer carries a 'postings' list — fetch() reports "
        "this as shape drift and the source degrades. This is the Tier 2 "
        "risk arriving; find the new endpoint in the site's devtools"
    )
    seen = _union_of_keys(postings)
    missing = [f for f in NOFLUFF_REQUIRED if f not in seen]
    assert not missing, f"nofluffjobs dropped required field(s): {missing}"
    absent = [f for f in NOFLUFF_EXPECTED if f not in seen]
    assert not absent, (
        f"nofluffjobs no longer sends {absent} — re-record "
        "nofluffjobs_postings.json from this live payload"
    )
    categories = {
        str(p.get("category", "")).lower()
        for p in postings if isinstance(p, dict)
    } - {""}
    if categories and not categories & {"data", "artificial-intelligence"}:
        pytest.fail(
            f"no posting on the whole board carries category 'data' or "
            f"'artificial-intelligence' (saw e.g. {sorted(categories)[:8]}) — "
            "the category vocabulary moved and DATA_CATEGORIES needs updating"
        )
