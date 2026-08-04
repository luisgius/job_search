"""Tests for src/sources/linkedin_email.py.

This is the most brittle module in the repo by construction: it parses
marketing HTML that LinkedIn redesigns on its own schedule, with no
versioning and no notice. The tests therefore pin *leniency* as much as
correctness — a template change should degrade the yield, never raise, and
never take the run down.

`tests/fixtures/linkedin_alert.html` is a trimmed but structurally faithful
alert, including the things that actually break parsers: nested tables,
tracking params on every href, the same job repeated further down, and one
entry with no company block at all.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.sources.linkedin_email import (
    SCOPES,
    build_service,
    canonical_linkedin_url,
    extract_jobs_from_html,
    fetch,
    fetch_description,
    parse_message,
)
from tests.conftest import (
    FakeResponse,
    FakeSession,
    html_response,
    load_fixture,
    write_config,
)

UTC = timezone.utc
ALERT_HTML = load_fixture("linkedin_alert.html")
RECEIVED = datetime(2026, 8, 4, 7, 0, tzinfo=UTC)


def b64(text: str) -> str:
    """Encode like Gmail does: URL-safe alphabet, padding stripped."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def gmail_message(html: str = ALERT_HTML, *, message_id: str = "m1",
                  internal_date: int = int(RECEIVED.timestamp() * 1000),
                  multipart: bool = True) -> dict[str, Any]:
    if multipart:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("plain text fallback")}},
                {"mimeType": "text/html", "body": {"data": b64(html)}},
            ],
        }
    else:
        payload = {"mimeType": "text/html", "body": {"data": b64(html)}}
    return {"id": message_id, "internalDate": str(internal_date), "payload": payload}


class FakeGmail:
    """Minimal stand-in for the googleapiclient resource chain.

    `service.users().messages().list(...).execute()` is four calls deep, which
    is exactly why `fetch` takes a `service=` seam instead of building one.
    """

    def __init__(self, messages: list[dict[str, Any]] | None = None,
                 *, list_error: Exception | None = None,
                 get_errors: dict[str, Exception] | None = None) -> None:
        self._store = {m["id"]: m for m in (messages or [])}
        # NB: the attribute cannot be called `messages` — it would shadow the
        # `messages()` method that the Gmail resource chain calls.
        self.list_error = list_error
        self.get_errors = get_errors or {}
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    # -- resource chain ------------------------------------------------

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Exec(lambda: self._list())

    def get(self, *, userId, id, format=None):
        self.get_calls.append(id)
        return _Exec(lambda: self._get(id))

    # -- behaviour -----------------------------------------------------

    def _list(self):
        if self.list_error:
            raise self.list_error
        return {"messages": [{"id": mid} for mid in self._store]}

    def _get(self, message_id):
        if message_id in self.get_errors:
            raise self.get_errors[message_id]
        return self._store[message_id]


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def linkedin_config(tmp_path: Path, watch=None, enabled=True):
    return write_config(
        tmp_path,
        {"sources": {"greenhouse": False, "linkedin_email": enabled}},
        watchlist={"linkedin_email": watch if watch is not None
                   else {"gmail_query": "from:jobalerts-noreply@linkedin.com",
                         "max_messages": 25, "fetch_descriptions": False}},
    )


# ==========================================================================
# URL canonicalisation
# ==========================================================================


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/comm/jobs/view/3987654321/?trackingId=AbC%3D%3D&refId=x",
        "https://www.linkedin.com/jobs/view/3987654321/",
        "https://www.linkedin.com/jobs/view/3987654321?trk=alert",
        "https://linkedin.com/comm/jobs/view/3987654321",
    ],
)
def test_canonical_url_strips_tracking_and_the_comm_prefix(raw):
    """Tracking params differ per email, so without this the same job looks
    new every single day."""
    assert canonical_linkedin_url(raw) == "https://www.linkedin.com/jobs/view/3987654321"


def test_canonical_url_leaves_unrecognised_urls_alone():
    assert canonical_linkedin_url("https://example.com/x") == "https://example.com/x"
    assert canonical_linkedin_url("") == ""


# ==========================================================================
# HTML extraction
# ==========================================================================


def test_extracts_every_distinct_job():
    jobs = extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)
    ids = {j.raw["linkedin_job_id"] for j in jobs}
    assert ids == {"3987654321", "3987654322", "3987654323", "3987654324"}


def test_reads_title_company_and_location():
    jobs = {j.raw["linkedin_job_id"]: j for j in
            extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)}
    job = jobs["3987654321"]
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Northwind"
    assert "Berlin" in job.location


def test_the_same_job_repeated_later_in_the_email_is_one_job():
    jobs = extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)
    assert sum(1 for j in jobs if j.raw["linkedin_job_id"] == "3987654321") == 1


def test_a_job_with_no_company_block_is_still_emitted():
    """Dropping it would make the job invisible forever; filters and scoring
    can still judge a title."""
    jobs = {j.raw["linkedin_job_id"]: j for j in
            extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)}
    orphan = jobs["3987654324"]
    assert orphan.title == "Backend Engineer"
    assert orphan.company == ""


def test_navigation_links_are_not_mistaken_for_jobs():
    """"See all jobs", "Unsubscribe" and "Help" are anchors too."""
    titles = {j.title for j in extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)}
    assert "See all jobs" not in titles
    assert "Unsubscribe" not in titles
    assert "Help" not in titles


def test_every_job_inherits_the_alert_freshness():
    """LinkedIn alerts carry no per-posting date. This is a documented ceiling
    on precision, not a bug — but it must be applied consistently."""
    jobs = extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)
    assert all(j.posted_at == RECEIVED for j in jobs)


def test_urls_are_canonicalised_at_extraction_time():
    jobs = extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)
    assert all(j.url.startswith("https://www.linkedin.com/jobs/view/") for j in jobs)
    assert all("trackingId" not in j.url for j in jobs)


def test_remote_and_hybrid_markers_are_read():
    jobs = {j.raw["linkedin_job_id"]: j for j in
            extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)}
    assert jobs["3987654322"].remote is True   # "(Remote)"
    assert jobs["3987654323"].remote is None   # nothing said


def test_jobs_carry_no_ats_because_linkedin_is_an_aggregator():
    """Which ATS the posting redirects to is a later stage's problem; claiming
    one here would let auto-apply fire on a URL it cannot handle."""
    jobs = extract_jobs_from_html(ALERT_HTML, received_at=RECEIVED)
    assert all(j.ats is None and j.ats_job_id is None for j in jobs)


@pytest.mark.parametrize(
    "html",
    ["", "<html><body>no jobs here</body></html>", "<a href='https://x'>x</a>",
     "<html><body", "<<<>>>", None],
)
def test_unparseable_or_empty_html_yields_nothing_rather_than_raising(html):
    assert extract_jobs_from_html(html, received_at=RECEIVED) == []


def test_a_template_redesign_degrades_instead_of_exploding():
    """The realistic failure: LinkedIn changes the surrounding markup and the
    company/location blocks vanish. Titles and links must survive."""
    stripped = (
        "<div><a href='https://www.linkedin.com/comm/jobs/view/111/'>Engineer</a></div>"
        "<div><a href='https://www.linkedin.com/comm/jobs/view/222/'>Analyst</a></div>"
    )
    jobs = extract_jobs_from_html(stripped, received_at=RECEIVED)
    assert {j.title for j in jobs} == {"Engineer", "Analyst"}


# ==========================================================================
# Gmail message decoding
# ==========================================================================


def test_parse_message_prefers_the_html_part():
    jobs = parse_message(gmail_message())
    assert len(jobs) == 4


def test_parse_message_handles_a_single_part_message():
    assert len(parse_message(gmail_message(multipart=False))) == 4


def test_parse_message_finds_a_nested_html_part():
    """Gmail nests multipart/alternative inside multipart/mixed when there are
    attachments or inline images — which LinkedIn alerts have."""
    payload = {
        "id": "m1",
        "internalDate": str(int(RECEIVED.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "image/png", "body": {"attachmentId": "a1"}},
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/html", "body": {"data": b64(ALERT_HTML)}},
                ]},
            ],
        },
    }
    assert len(parse_message(payload)) == 4


def test_parse_message_decodes_unpadded_base64url():
    """Gmail uses '-'/'_' and strips '=' padding; `urlsafe_b64decode` refuses
    the latter, so the padding has to be restored by hand."""
    html = "<a href='https://www.linkedin.com/comm/jobs/view/999/'>Engineer</a>"
    for pad in range(4):
        body = html + " " * pad          # shifts the padding requirement
        jobs = parse_message(gmail_message(body, multipart=False))
        assert len(jobs) == 1, pad


def test_parse_message_uses_internal_date_as_posted_at():
    jobs = parse_message(gmail_message())
    assert all(j.posted_at == RECEIVED for j in jobs)


@pytest.mark.parametrize(
    "payload",
    [{}, None, "nonsense", {"payload": {}}, {"payload": {"parts": []}},
     {"payload": {"mimeType": "text/html", "body": {}}}],
)
def test_parse_message_on_junk_returns_nothing(payload):
    assert parse_message(payload) == []


# ==========================================================================
# fetch
# ==========================================================================


def test_fetch_uses_the_injected_service_and_never_authorises(tmp_path: Path):
    service = FakeGmail([gmail_message()])
    jobs = fetch(linkedin_config(tmp_path), service=service)
    assert len(jobs) == 4
    assert service.get_calls == ["m1"]


def test_fetch_passes_the_configured_query_and_cap(tmp_path: Path):
    cfg = linkedin_config(tmp_path, watch={"gmail_query": "from:someone newer_than:1d",
                                           "max_messages": 7,
                                           "fetch_descriptions": False})
    service = FakeGmail([gmail_message()])
    fetch(cfg, service=service)
    assert service.list_calls[0]["q"] == "from:someone newer_than:1d"
    assert service.list_calls[0]["maxResults"] == 7
    assert service.list_calls[0]["userId"] == "me"


def test_fetch_deduplicates_the_same_job_across_two_alerts(tmp_path: Path):
    """Consecutive daily alerts repeat yesterday's jobs; without this the
    digest fills up with the same five roles."""
    service = FakeGmail([gmail_message(message_id="m1"),
                         gmail_message(message_id="m2")])
    assert len(fetch(linkedin_config(tmp_path), service=service)) == 4


def test_fetch_returns_nothing_when_disabled(tmp_path: Path):
    service = FakeGmail([gmail_message()])
    assert fetch(linkedin_config(tmp_path, enabled=False), service=service) == []
    assert service.list_calls == []


def test_fetch_with_no_matching_mail_is_quiet(tmp_path: Path):
    assert fetch(linkedin_config(tmp_path), service=FakeGmail([])) == []


def test_a_gmail_outage_is_reported_not_raised(tmp_path: Path):
    service = FakeGmail(list_error=RuntimeError("quota exceeded"))
    errors: list[str] = []
    assert fetch(linkedin_config(tmp_path), service=service, errors=errors) == []
    assert any("Gmail search failed" in e for e in errors)


def test_one_unreadable_message_does_not_lose_the_others(tmp_path: Path):
    service = FakeGmail(
        [gmail_message(message_id="m1"),
         gmail_message(html="<a href='https://www.linkedin.com/comm/jobs/view/555/'>X</a>",
                       message_id="m2")],
        get_errors={"m1": RuntimeError("message not found")},
    )
    jobs = fetch(linkedin_config(tmp_path), service=service)
    assert [j.raw["linkedin_job_id"] for j in jobs] == ["555"]


def test_fetch_never_raises(tmp_path: Path):
    class Exploding:
        def users(self):
            raise RuntimeError("boom")

    errors: list[str] = []
    assert fetch(linkedin_config(tmp_path), service=Exploding(), errors=errors) == []
    assert errors


# ==========================================================================
# guest description fetch
# ==========================================================================


def test_fetch_description_flattens_the_guest_html():
    session = FakeSession([("jobs-guest", html_response(
        "<div class='description'><p>We need Python and Kafka.</p></div>"
    ))])
    text = fetch_description("https://www.linkedin.com/jobs/view/3987654321", session=session)
    assert "We need Python and Kafka." in text
    assert "<p>" not in text


def test_fetch_description_hits_the_guest_endpoint_with_the_job_id():
    session = FakeSession([("jobs-guest", html_response("<p>x</p>"))])
    fetch_description("https://www.linkedin.com/jobs/view/3987654321", session=session)
    assert session.calls[0]["url"].endswith("/jobPosting/3987654321")


@pytest.mark.parametrize(
    "outcome",
    [FakeResponse(status_code=404), FakeResponse(status_code=429),
     ConnectionError("reset"), FakeResponse(status_code=999)],
)
def test_a_broken_guest_endpoint_degrades_to_empty(outcome):
    """This endpoint breaks and rate-limits periodically. Every failure must
    return "" so the job still reaches the digest with its email-derived info."""
    session = FakeSession([("jobs-guest", outcome)])
    assert fetch_description("https://www.linkedin.com/jobs/view/1", session=session) == ""


def test_fetch_description_does_not_retry_into_a_rate_limiter():
    session = FakeSession([("jobs-guest", FakeResponse(status_code=429))])
    fetch_description("https://www.linkedin.com/jobs/view/1", session=session)
    assert len(session.calls) == 1


def test_fetch_description_without_a_job_id_makes_no_request():
    session = FakeSession()
    assert fetch_description("https://example.com/whatever", session=session) == ""
    assert session.calls == []


def test_descriptions_are_attached_when_enabled(tmp_path: Path):
    cfg = linkedin_config(tmp_path, watch={"gmail_query": "q", "max_messages": 5,
                                           "fetch_descriptions": True})
    session = FakeSession([("jobs-guest", html_response("<p>Python and Kafka.</p>"))])
    jobs = fetch(cfg, service=FakeGmail([gmail_message()]), session=session)
    assert all("Python and Kafka." in j.description for j in jobs)


def test_descriptions_are_skipped_when_disabled(tmp_path: Path):
    session = FakeSession()
    jobs = fetch(linkedin_config(tmp_path), service=FakeGmail([gmail_message()]),
                 session=session)
    assert session.calls == []
    assert all(j.description == "" for j in jobs)


# ==========================================================================
# auth
# ==========================================================================


def test_the_gmail_scope_is_read_only():
    """Anything broader would let a job-search script mutate the user's inbox."""
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_build_service_names_the_missing_credentials_file(tmp_path: Path):
    cfg = linkedin_config(tmp_path, watch={"credentials_file": "gmail_credentials.json"})
    with pytest.raises(Exception) as exc:
        build_service(cfg)
    assert "gmail_credentials.json" in str(exc.value)
