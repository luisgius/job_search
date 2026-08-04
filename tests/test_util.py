"""Tests for src/util.py — HTTP retries, HTML flattening, date parsing.

`parse_datetime` deserves the attention it gets here: it decides whether a
posting counts as "fresh", and every ATS emits a different date shape. A
wrong answer either floods the digest with month-old jobs or silently drops
everything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.util import (
    HttpError,
    chunked,
    ensure_dir,
    env_flag,
    html_to_text,
    http_get,
    http_get_json,
    parse_datetime,
    safe_call,
    setup_logging,
    slugify,
    truncate,
)
from tests.conftest import FakeResponse, FakeSession, json_response


# ==========================================================================
# html_to_text
# ==========================================================================


def test_html_to_text_flattens_structure_into_lines():
    text = html_to_text(
        "<h3>Requirements</h3><ul><li>Python</li><li>PostgreSQL</li></ul>"
    )
    # Each block becomes its own line; running list items together would
    # produce "PythonPostgreSQL" and corrupt keyword matching downstream.
    assert text.splitlines() == ["Requirements", "Python", "PostgreSQL"]


def test_html_to_text_separates_block_elements():
    assert html_to_text("<p>One</p><p>Two</p>").splitlines() == ["One", "Two"]


def test_html_to_text_unescapes_entities():
    assert html_to_text("<p>R&amp;D &mdash; you&#39;ll ship</p>") == "R&D — you'll ship"


def test_html_to_text_drops_script_and_style():
    out = html_to_text("<style>.a{color:red}</style><p>Hi</p><script>alert(1)</script>")
    assert "color" not in out
    assert "alert" not in out
    assert out.strip() == "Hi"


def test_html_to_text_collapses_runs_of_blank_lines():
    assert "\n\n\n" not in html_to_text("<p>a</p><br/><br/><br/><br/><p>b</p>")


def test_html_to_text_handles_nbsp_and_empty_input():
    assert html_to_text("<p>a&nbsp;b</p>") == "a b"
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


def test_html_to_text_on_plain_text_is_a_noop():
    assert html_to_text("Just words.") == "Just words."


# ==========================================================================
# truncate / slugify
# ==========================================================================


def test_truncate_leaves_short_text_alone():
    assert truncate("short", 100) == "short"


def test_truncate_marks_what_it_cut():
    out = truncate("word " * 500, 100)
    assert len(out) < 200
    assert out.endswith("[...truncated]")


def test_truncate_prefers_a_word_boundary():
    out = truncate("alpha beta gamma delta epsilon", 20, suffix="")
    assert not out.endswith("del")
    assert out == out.rstrip()


def test_truncate_handles_empty():
    assert truncate("", 10) == ""
    assert truncate(None, 10) == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Backend Engineer", "acme-backend-engineer"),
        ("Zürich — Senior Engineer (m/f/d)", "zurich-senior-engineer-m-f-d"),
        ("///", "untitled"),
        ("", "untitled"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_respects_max_length_and_never_ends_in_a_dash():
    out = slugify("a " * 200, max_length=20)
    assert len(out) <= 20
    assert not out.endswith("-")


# ==========================================================================
# parse_datetime
# ==========================================================================

UTC = timezone.utc


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Greenhouse: ISO with an offset
        ("2026-08-04T07:30:00-04:00", datetime(2026, 8, 4, 11, 30, tzinfo=UTC)),
        # Adzuna: ISO with a Z
        ("2026-08-04T06:12:00Z", datetime(2026, 8, 4, 6, 12, tzinfo=UTC)),
        # naive ISO -> assumed UTC
        ("2026-08-04T06:12:00", datetime(2026, 8, 4, 6, 12, tzinfo=UTC)),
        ("2026-08-04", datetime(2026, 8, 4, 0, 0, tzinfo=UTC)),
        # Lever / Gmail: millisecond epoch, as int and as string
        (1785826800000, datetime(2026, 8, 4, 7, 0, tzinfo=UTC)),
        ("1785826800000", datetime(2026, 8, 4, 7, 0, tzinfo=UTC)),
        # second epoch
        (1785826800, datetime(2026, 8, 4, 7, 0, tzinfo=UTC)),
    ],
)
def test_parse_datetime_known_shapes(raw, expected):
    assert parse_datetime(raw) == expected


def test_parse_datetime_passes_through_datetimes():
    aware = datetime(2026, 8, 4, 9, tzinfo=UTC)
    assert parse_datetime(aware) == aware
    naive = datetime(2026, 8, 4, 9)
    assert parse_datetime(naive) == aware


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", [], {}])
def test_parse_datetime_returns_none_rather_than_guessing(raw):
    # None means "freshness unknown", which the filter treats as skip-worthy.
    # Guessing here would quietly let stale postings through.
    assert parse_datetime(raw) is None


def test_parse_datetime_survives_absurd_epochs():
    assert parse_datetime(10**20) is None


# ==========================================================================
# http_get
# ==========================================================================


def test_http_get_returns_the_response(no_sleep):
    session = FakeSession([("example.com", json_response({"ok": True}))])
    response = http_get("https://example.com/x", session=session, sleep=no_sleep)
    assert response.json() == {"ok": True}


def test_http_get_sends_a_user_agent(no_sleep):
    session = FakeSession([("example.com", json_response({}))])
    http_get("https://example.com/x", session=session, sleep=no_sleep)
    assert "job-hunter" in session.calls[0]["headers"]["User-Agent"]


def test_http_get_passes_params_through(no_sleep):
    session = FakeSession([("adzuna", json_response({}))])
    http_get("https://api.adzuna.com/x", params={"app_id": "1"},
             session=session, sleep=no_sleep)
    assert session.calls[0]["params"] == {"app_id": "1"}


def test_http_get_retries_on_500_then_succeeds(no_sleep):
    responses = [FakeResponse(status_code=500), json_response({"ok": 1})]
    session = FakeSession([("x", lambda url, params: responses.pop(0))])
    assert http_get("https://x/", session=session, sleep=no_sleep).json() == {"ok": 1}
    assert len(session.calls) == 2


def test_http_get_retries_on_429(no_sleep):
    responses = [FakeResponse(status_code=429), json_response({"ok": 1})]
    session = FakeSession([("x", lambda url, params: responses.pop(0))])
    http_get("https://x/", session=session, sleep=no_sleep)
    assert len(session.calls) == 2


def test_http_get_retries_on_transport_errors(no_sleep):
    calls = {"n": 0}

    def flaky(url, params):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("connection reset")
        return json_response({"ok": 1})

    session = FakeSession([("x", flaky)])
    assert http_get("https://x/", session=session, sleep=no_sleep).json() == {"ok": 1}


def test_http_get_gives_up_after_the_retry_budget(no_sleep):
    session = FakeSession([("x", FakeResponse(status_code=503))])
    with pytest.raises(HttpError):
        http_get("https://x/", session=session, retries=3, sleep=no_sleep)
    assert len(session.calls) == 3


def test_http_get_does_not_retry_a_404(no_sleep):
    """A bad Greenhouse slug returns 404 forever; retrying wastes 3x the time
    on every run, for every wrong slug in the watchlist."""
    session = FakeSession([("x", FakeResponse(status_code=404))])
    with pytest.raises(HttpError):
        http_get("https://x/", session=session, sleep=no_sleep)
    assert len(session.calls) == 1


def test_http_get_does_not_retry_a_403(no_sleep):
    session = FakeSession([("x", FakeResponse(status_code=403))])
    with pytest.raises(HttpError):
        http_get("https://x/", session=session, sleep=no_sleep)
    assert len(session.calls) == 1


def test_http_get_backs_off_between_attempts(no_sleep):
    session = FakeSession([("x", FakeResponse(status_code=500))])
    with pytest.raises(HttpError):
        http_get("https://x/", session=session, retries=3, backoff=2.0, sleep=no_sleep)
    # Increasing waits, and never a wait before the first attempt.
    assert no_sleep.calls == [2.0, 4.0]


def test_http_get_json_raises_on_non_json(no_sleep):
    session = FakeSession([("x", FakeResponse(status_code=200, text="<html>nope"))])
    with pytest.raises(HttpError):
        http_get_json("https://x/", session=session, sleep=no_sleep)


def test_http_get_error_message_names_the_url(no_sleep):
    session = FakeSession([("boards-api", FakeResponse(status_code=404))])
    with pytest.raises(HttpError, match="boards-api"):
        http_get("https://boards-api.greenhouse.io/v1/boards/nope/jobs",
                 session=session, sleep=no_sleep)


# ==========================================================================
# safe_call
# ==========================================================================


def test_safe_call_returns_the_value():
    assert safe_call(lambda x: x * 2, 21) == 42


def test_safe_call_swallows_and_records():
    errors: list[str] = []
    def boom():
        raise ValueError("kaboom")

    assert safe_call(boom, default=[], errors=errors, label="greenhouse/acme") == []
    assert len(errors) == 1
    assert "greenhouse/acme" in errors[0]
    assert "kaboom" in errors[0]


def test_safe_call_without_an_error_list_still_works():
    assert safe_call(lambda: 1 / 0, default="fallback") == "fallback"


# ==========================================================================
# misc
# ==========================================================================


def test_chunked():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 3)) == []


def test_ensure_dir_is_idempotent(tmp_path: Path):
    target = tmp_path / "a" / "b"
    assert ensure_dir(target) == target
    assert ensure_dir(target) == target
    assert target.is_dir()


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
     ("0", False), ("false", False), ("", False), ("nonsense", False)],
)
def test_env_flag(monkeypatch, value, expected):
    monkeypatch.setenv("JH_TEST_FLAG", value)
    assert env_flag("JH_TEST_FLAG") is expected


def test_env_flag_default_when_unset(monkeypatch):
    monkeypatch.delenv("JH_TEST_FLAG", raising=False)
    assert env_flag("JH_TEST_FLAG", default=True) is True


def test_setup_logging_is_safe_to_call_twice():
    import io

    setup_logging("DEBUG", stream=io.StringIO())
    setup_logging("INFO", stream=io.StringIO())
