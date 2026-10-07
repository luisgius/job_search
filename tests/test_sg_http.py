from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from src.singapore.http import AccessDenied, PublicClient, PublicHTTPError, _PublicHTTPSConnection


def response(status=200, body="", **headers):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    result.headers.update(headers)
    return result


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.value += duration


class Session:
    def __init__(self, responses, clock):
        self.responses = iter(responses)
        self.clock = clock
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = {"Authorization": "dummy-only", "Cookie": "dummy-only"}
        self.auth = ("dummy", "dummy")
        self.trust_env = True
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        assert not self.cookies
        assert self.auth is None and self.trust_env is False
        assert "Authorization" not in self.headers and "Cookie" not in self.headers
        assert kwargs["allow_redirects"] is False
        self.calls.append((method, url, kwargs, self.clock.now()))
        self.cookies.set("discard", "unused")
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def client_for(*responses, resolver=public_dns, **kwargs):
    clock = Clock()
    session = Session(responses, clock)
    client = PublicClient(session=session, clock=clock.now, sleep=clock.sleep, resolver=resolver, **kwargs)
    return client, session, clock


def ashby_permission():
    return {"https://api.ashbyhq.com": {"http_status": 401, "policy": "documented_public_api",
            "path_prefix": "/posting-api/job-board/",
            "documentation_url": "https://developers.ashbyhq.com/docs/public-job-posting-api",
            "reference_url": "https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3",
            "verified_at": "2026-10-07"}}


@pytest.mark.parametrize("url", ["http://example.com/jobs", "https://u:p@example.com/jobs",
                                 "https://127.0.0.1/jobs", "https://[::1]/jobs", "https://[fd00::1]/jobs",
                                 "https://169.254.169.254/latest", "https://localhost/jobs",
                                 "https://foo.local/jobs", "https://example.com:8443/jobs",
                                 "https://example.com/jobs?api_key=redacted", "https://example.com\\@127.0.0.1/jobs"])
def test_unsafe_urls_fail_before_requests(url):
    client, session, _ = client_for()
    with pytest.raises(AccessDenied):
        client.get(url)
    assert session.calls == []


def test_dns_private_or_mixed_records_are_denied():
    def mixed_dns(*args, **kwargs):
        return public_dns(*args, **kwargs) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
    client, session, _ = client_for(resolver=mixed_dns)
    with pytest.raises(AccessDenied, match="non-public"):
        client.get("https://public.example/jobs")
    assert session.calls == []


def test_robots_cached_and_rate_includes_robots_and_post():
    client, session, _ = client_for(response(body="User-agent: *\nAllow: /"),
                                  response(body='{"jobs":[]}'), response(body='{"total":0}'))
    assert client.get_json("https://public.example/jobs", params={"offset": 0}) == {"jobs": []}
    assert client.post_json("https://public.example/jobs", json={"offset": 0}) == {"total": 0}
    assert [call[3] for call in session.calls] == [0, 1, 2]
    assert [call[0] for call in session.calls] == ["GET", "GET", "POST"]
    assert session.calls[1][1].endswith("?offset=0")
    assert session.calls[2][2]["json"] == {"offset": 0}
    assert not session.cookies


@pytest.mark.parametrize("status", [401, 403, 410, 500, 429])
def test_robots_fail_closed_and_cached(status):
    client, session, _ = client_for(response(status), retries=0)
    for _ in range(2):
        with pytest.raises(AccessDenied, match="Robots policy"):
            client.get("https://public.example/jobs")
    assert len(session.calls) == 1


def test_robots_404_allows_but_html_200_denies():
    client, session, _ = client_for(response(404), response(body="allowed"))
    assert client.get_text("https://public.example/jobs") == "allowed"
    client, session, _ = client_for(response(body="<!doctype html><html>not robots</html>"))
    with pytest.raises(AccessDenied, match="HTML"):
        client.get("https://public.example/jobs")
    assert len(session.calls) == 1


def test_robots_specific_agent_wildcards_and_allow_tie():
    robots = "User-agent: *\nAllow: /\nUser-agent: SingaporeCareerMonitor\nDisallow: /api/*\nAllow: /api/public$"
    client, session, _ = client_for(response(body=robots), response(body="ok"))
    with pytest.raises(AccessDenied):
        client.get("https://public.example/api/private")
    assert client.get_text("https://public.example/api/public") == "ok"
    with pytest.raises(AccessDenied):
        client.get("https://public.example/api/public?private=yes")
    assert len(session.calls) == 2


@pytest.mark.parametrize("path", ["/public/../private", "/%70rivate", "/private"])
def test_robots_matches_the_normalized_request_path(path):
    client, session, _ = client_for(response(body="User-agent: *\nDisallow: /private"))
    with pytest.raises(AccessDenied):
        client.get("https://public.example" + path)
    assert len(session.calls) == 1


def test_retry_after_and_exponential_retries_obey_host_rate():
    client, session, clock = client_for(response(404), response(429, **{"Retry-After": "3"}),
                                      response(503), response(body="ok"))
    assert client.get_text("https://public.example/jobs") == "ok"
    assert [call[3] for call in session.calls] == [0, 1, 4, 6]
    assert clock.sleeps == [1, 3, 2]


def test_retry_after_date_supported_and_long_wait_fails_without_early_retry():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    client, session, clock = client_for(response(404), response(429, **{"Retry-After": future}), response(body="ok"))
    assert client.get_text("https://public.example/jobs") == "ok"
    assert clock.sleeps[-1] >= 3
    client, session, _ = client_for(response(404), response(429, **{"Retry-After": "3600"}))
    with pytest.raises(PublicHTTPError, match="bounded retry"):
        client.get("https://public.example/jobs")
    assert len(session.calls) == 2


def test_timeout_retried_and_http_failure_sanitized():
    client, session, _ = client_for(response(404), requests.Timeout("contains dummy secret"), response(403))
    with pytest.raises(PublicHTTPError, match="HTTP 403") as error:
        client.get("https://public.example/jobs?searchText=private")
    assert "searchText" not in str(error.value)
    assert [call[3] for call in session.calls] == [0, 1, 2]


def test_safe_redirect_checks_destination_robots_and_rate():
    client, session, _ = client_for(response(404), response(302, Location="https://other.example/jobs"),
                                  response(body="User-agent: *\nDisallow: /jobs"))
    with pytest.raises(AccessDenied):
        client.get("https://public.example/jobs")
    assert [call[1] for call in session.calls] == ["https://public.example/robots.txt", "https://public.example/jobs",
                                                "https://other.example/robots.txt"]


@pytest.mark.parametrize("target", ["http://other.example/jobs", "https://user:pass@other.example/jobs", "https://127.0.0.1/jobs"])
def test_redirect_never_reaches_unsafe_target(target):
    client, session, _ = client_for(response(404), response(302, Location=target))
    with pytest.raises(AccessDenied):
        client.get("https://public.example/jobs")
    assert len(session.calls) == 2


def test_post_303_uses_get_but_cross_origin_307_never_replays_body():
    client, session, _ = client_for(response(404), response(303, Location="/result"), response(body="ok"))
    assert client.post("https://public.example/jobs", json={"offset": 0}).text == "ok"
    assert session.calls[-1][0] == "GET" and session.calls[-1][2]["json"] is None
    client, session, _ = client_for(response(404), response(307, Location="https://other.example/jobs"))
    with pytest.raises(AccessDenied, match="POST"):
        client.post("https://public.example/jobs", json={"offset": 0})
    assert len(session.calls) == 2


def test_cross_origin_robots_redirect_and_redirect_loop_fail_closed():
    client, session, _ = client_for(response(302, Location="https://other.example/robots.txt"))
    with pytest.raises(AccessDenied, match="robots redirect"):
        client.get("https://public.example/jobs")
    assert len(session.calls) == 1
    client, session, _ = client_for(response(404), response(302, Location="/jobs"), response(302, Location="/jobs"), max_redirects=1)
    with pytest.raises(PublicHTTPError, match="redirect limit"):
        client.get("https://public.example/jobs")


def test_invalid_json_is_explicit_and_context_manager_closes():
    client, session, _ = client_for(response(404), response(body="not JSON"))
    with client:
        with pytest.raises(PublicHTTPError, match="invalid JSON"):
            client.get_json("https://public.example/jobs")
    assert session.closed


def test_robots_crawl_delay_slows_requests_and_oversized_wait_is_denied():
    client, session, _ = client_for(response(body="User-agent: *\nAllow: /\nCrawl-delay: 5"), response(body="ok"), response(body="ok"))
    client.get("https://public.example/jobs")
    client.get("https://public.example/other")
    assert [call[3] for call in session.calls] == [0, 5, 10]
    client, session, _ = client_for(response(body="User-agent: *\nCrawl-delay: 3600"))
    with pytest.raises(AccessDenied, match="crawl-delay"):
        client.get("https://public.example/jobs")
    assert len(session.calls) == 1


def test_actual_socket_never_connects_private_rebound_dns(monkeypatch):
    connection = _PublicHTTPSConnection("public.example", port=443, timeout=10)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("Private socket must not be created"))
    with pytest.raises(AccessDenied, match="non-public"):
        connection._new_conn()


def test_actual_socket_connects_verified_sockaddr_without_second_dns_lookup(monkeypatch):
    class Socket:
        def __init__(self):
            self.destination = None
        def settimeout(self, value):
            pass
        def setsockopt(self, *args):
            pass
        def connect(self, destination):
            self.destination = destination
    sock = Socket()
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: sock)
    connection = _PublicHTTPSConnection("public.example", port=443, timeout=10)
    assert connection._new_conn() is sock
    assert sock.destination == ("93.184.216.34", 443)
    assert connection.host == "public.example"  # TLS cert verification uses hostname.


@pytest.mark.parametrize("body", ["", "Unauthorized"])
def test_ashby_documented_api_exception_is_exact_host_path_and_known_401_only(body):
    client, session, _ = client_for(response(401, body=body), response(body='{"jobs":[]}'), public_api_robots=ashby_permission())
    assert client.get_json("https://api.ashbyhq.com/posting-api/job-board/example") == {"jobs": []}
    with pytest.raises(AccessDenied):
        client.get("https://api.ashbyhq.com/other-api/example")
    with pytest.raises(AccessDenied):
        client.get("https://api.ashbyhq.com/posting-api/job-board")
    assert len(session.calls) == 2
    client, session, _ = client_for(response(401), public_api_robots=ashby_permission())
    with pytest.raises(AccessDenied):
        client.get("https://different.example/posting-api/job-board/example")
    assert len(session.calls) == 1


@pytest.mark.parametrize("robots", [response(403), response(500), response(401, body="User-agent: *\nDisallow: /"),
                                  response(401, body="Unauthorized", **{"WWW-Authenticate": "Basic realm=protected"}),
                                   response(body="User-agent: *\nDisallow: /posting-api/")])
def test_ashby_exception_never_overrides_disallow_or_other_robots_failures(robots):
    client, session, _ = client_for(robots, public_api_robots=ashby_permission(), retries=0)
    with pytest.raises(AccessDenied):
        client.get("https://api.ashbyhq.com/posting-api/job-board/example")
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_ashby_robots_exception_never_allows_job_authentication_or_failure(status):
    client, session, _ = client_for(response(401), response(status), public_api_robots=ashby_permission(), retries=0)
    with pytest.raises(PublicHTTPError, match=f"HTTP {status}"):
        client.get("https://api.ashbyhq.com/posting-api/job-board/example")
    assert len(session.calls) == 2


@pytest.mark.parametrize("field,value", [("path_prefix", "/"), ("http_status", 403),
                                        ("documentation_url", "https://unverified.example/docs"),
                                        ("reference_url", "https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.4"),
                                        ("verified_at", "not-a-date")])
def test_ashby_robots_exception_rejects_incomplete_or_wider_evidence(field, value):
    mapping = ashby_permission()
    mapping["https://api.ashbyhq.com"][field] = value
    with pytest.raises(ValueError):
        client_for(public_api_robots=mapping)
