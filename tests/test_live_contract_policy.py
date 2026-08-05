"""The skip-or-fail policy of `test_live_contract.py`, proved offline.

`test_live_contract.py` is the only file that talks to the real internet, and
the only one that can settle whether the four European parsers read fields that
actually exist. Every test in it is gated twice: once by a session-wide network
probe, and once by the helper that makes the request.

Both gates can *skip*. A skip prints green. So a bug in either gate does not
look like a bug — it looks like a pass, and it silently disarms forty contract
tests at once. That is the failure this module exists to catch, and it is why
these tests are offline: they must run in the default suite, on the machine
where nobody is looking, rather than only when someone remembers `-m network`.

The rule under test, in one sentence: **an API that answered and rejected us is
a finding and must fail; a connection that never happened is a train tunnel and
must skip.** `_reachable` already got this right; the raw helpers and the probe
did not.
"""

from __future__ import annotations

import pytest

from src.util import HttpError
from tests.test_live_contract import (
    answered_and_rejected,
    fetch_raw,
    probe_network,
)

SKIPPED = pytest.skip.Exception
FAILED = pytest.fail.Exception


def outcome(fn, *args, **kwargs) -> tuple[str, str]:
    """Run `fn` and report which pytest outcome it raised, as plain data.

    `pytest.raises(pytest.fail.Exception)` cannot be used for this, and the
    reason is the whole subject of the file: when the code under test *skips*
    instead of failing, the `Skipped` exception escapes `pytest.raises` and
    pytest marks **this** test skipped — which prints green and reports
    nothing. A test that cannot tell a skip from a failure is no use at all for
    proving that something else can.
    """
    try:
        fn(*args, **kwargs)
    except SKIPPED as exc:
        return "skip", str(exc)
    except FAILED as exc:
        return "fail", str(exc)
    return "returned", ""


class FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def json(self):
        return self._payload


def raising(exc):
    def _get(url, params=None, **kwargs):
        raise exc

    return _get


def answering(response=None):
    calls: list[str] = []

    def _get(url, params=None, **kwargs):
        calls.append(url)
        return response if response is not None else FakeResponse()

    _get.calls = calls
    return _get


# ==========================================================================
# the shared rule
# ==========================================================================


@pytest.mark.parametrize("message", [
    "https://api.example.com/jobs -> HTTP 404",
    "https://api.example.com/jobs -> HTTP 403",
    "https://api.example.com/jobs -> HTTP 500",
    "https://api.example.com/jobs -> HTTP 429 after 3 attempts",
])
def test_a_status_code_means_the_api_answered_us(message):
    assert answered_and_rejected(HttpError(message)) is True


@pytest.mark.parametrize("exc", [
    HttpError("HTTPSConnectionPool(host='api.example.com'): Max retries exceeded"),
    HttpError("[Errno -2] Name or service not known"),
    ConnectionError("connection refused"),
    TimeoutError("timed out"),
])
def test_a_transport_failure_is_not_an_answer(exc):
    """`HTTPSConnectionPool` contains the letters "HTTP" and would turn every
    offline machine into a red suite if the match were on the word rather than
    on an actual three-digit status."""
    assert answered_and_rejected(exc) is False


# ==========================================================================
# the raw request helper — finding 12a
# ==========================================================================


@pytest.mark.parametrize("status", [404, 403, 410, 500])
def test_a_raw_request_that_is_answered_and_rejected_fails(status):
    """`_raw_payload` and `_raw_text` back the field-shape tests, which are the
    only tests that can tell us a parser is reading fields the API still sends.

    They used to skip on *any* exception. A board that moved, a slug that
    rotted, an endpoint that started requiring a key — all of it came out as
    "skipped", which reads as green, in the one file whose entire job is to
    notice that kind of change."""
    verdict, message = outcome(
        fetch_raw, "https://api.example.com/jobs",
        get=raising(HttpError(f"https://api.example.com/jobs -> HTTP {status}")),
    )
    assert verdict == "fail", f"a {status} came out as {verdict!r}: {message}"
    assert "answered but rejected" in message
    assert str(status) in message


@pytest.mark.parametrize("exc", [
    HttpError("HTTPSConnectionPool(host='api.example.com'): Max retries exceeded"),
    OSError("[Errno -3] Temporary failure in name resolution"),
])
def test_a_raw_request_that_never_reached_anyone_skips(exc):
    """The other half of the pair. A suite that goes red on a train journey
    trains you to ignore it."""
    verdict, message = outcome(fetch_raw, "https://api.example.com/jobs",
                               get=raising(exc))
    assert verdict == "skip", f"a train tunnel came out as {verdict!r}: {message}"
    assert "unreachable" in message


def test_a_raw_request_that_works_returns_the_response():
    response = FakeResponse({"jobs": []})
    assert fetch_raw("https://api.example.com/jobs", get=answering(response)) is response


def test_the_raw_helper_and_reachable_agree():
    """The bug was that two helpers three lines apart disagreed about the same
    exception. Asserting the shared rule is what keeps them from drifting."""
    from tests.test_live_contract import _reachable

    exc = HttpError("https://api.example.com/jobs -> HTTP 404")
    via_reachable, _ = outcome(_reachable, raising(exc), "https://api.example.com/jobs")
    via_raw, _ = outcome(fetch_raw, "https://api.example.com/jobs", get=raising(exc))
    assert via_reachable == via_raw == "fail"


# ==========================================================================
# the session probe — finding 12b
# ==========================================================================

GONE = HttpError("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs -> HTTP 404")
TUNNEL = HttpError("HTTPSConnectionPool(host='x'): Max retries exceeded")

PROBES = (("https://a.example/1", None),
          ("https://b.example/2", None),
          ("https://c.example/3", None))


def test_the_probe_passes_as_soon_as_anything_answers():
    get = answering()
    verdict, message = outcome(probe_network, PROBES, get=get)
    assert verdict == "returned", message
    assert get.calls == ["https://a.example/1"]   # no reason to ask twice


def test_one_dead_probe_host_does_not_disarm_the_file():
    """The probe used to be a single third party's board. If GitLab leaves
    Greenhouse — a decision GitLab is entitled to make without telling us —
    that one 404 skipped all forty contract tests, including the four European
    boards this file exists to settle, and printed green."""
    def get(url, params=None, **kwargs):
        if url == "https://a.example/1":
            raise GONE
        return FakeResponse()

    verdict, message = outcome(probe_network, PROBES, get=get)
    assert verdict == "returned", (
        f"one dead probe host {verdict}ped the whole file: {message}"
    )


def test_no_route_to_anywhere_skips():
    verdict, message = outcome(probe_network, PROBES, get=raising(TUNNEL))
    assert verdict == "skip", message
    assert "unreachable" in message


def test_every_probe_answering_and_rejecting_fails_rather_than_skipping():
    """"The probe itself is broken" and "there is no network" must be
    distinguishable. Every host answering with a 404 is not an offline machine:
    it is a set of rotted probe URLs, or something intercepting the connection,
    and skipping on it disarms the whole file."""
    verdict, message = outcome(probe_network, PROBES, get=raising(GONE))
    assert verdict == "fail", (
        f"a wholly rotted probe list came out as {verdict!r} — which prints "
        f"green and takes forty contract tests with it: {message}"
    )
    assert "rotted" in message or "intercepting" in message
    assert "https://a.example/1" in message
    assert "print green" in message


def test_a_mixture_of_dead_and_unreachable_still_skips():
    """One rotted probe plus a genuine outage is still, on the evidence, an
    offline machine — and a red suite on a train is how a file like this gets
    ignored."""
    def get(url, params=None, **kwargs):
        raise GONE if url.endswith("/1") else TUNNEL

    verdict, message = outcome(probe_network, PROBES, get=get)
    assert verdict == "skip", message


def test_the_shipped_probe_list_does_not_rest_on_one_company():
    """The actual constant, not a fixture of it: several independent hosts, so
    no single company's hiring decision can silence this file."""
    from tests.test_live_contract import _PROBE_URLS

    assert len(_PROBE_URLS) >= 3
    hosts = {url.split("/")[2] for url, _params in _PROBE_URLS}
    assert len(hosts) == len(_PROBE_URLS), f"probe hosts repeat: {sorted(hosts)}"
