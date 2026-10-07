"""Public HTTPS transport with a fail-closed robots and network boundary.

Terms permission belongs to the researched entry, not an HTTP status. Adapters
must gate that permission before calling this transport. No browser, cookies,
credentials, environment proxy configuration, or automatic redirects are used.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

USER_AGENT = "SingaporeCareerMonitor/1.0 (public personal job monitoring; 1 request/second)"


class PublicHTTPError(RuntimeError):
    """Safe, credential-free transport failure."""


class AccessDenied(PublicHTTPError):
    """A URL or its robots policy does not permit public automation."""


class _PublicHTTPSConnection(HTTPSConnection):
    """Connect only to the public IP just checked, retaining hostname TLS/SNI.

    Checking DNS before requests alone leaves a second DNS resolution inside
    urllib3. Connecting the resolved sockaddr directly closes that rebinding gap.
    """
    def _new_conn(self) -> socket.socket:
        try:
            records = socket.getaddrinfo(self._dns_host, self.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise AccessDenied("Cannot verify connection destination DNS") from None
        if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
            raise AccessDenied("Connection destination DNS includes a non-public address")
        for family, socktype, proto, _, sockaddr in records:
            connection = socket.socket(family, socktype, proto)
            try:
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    connection.settimeout(self.timeout)
                for option in self.socket_options or []:
                    connection.setsockopt(*option)
                if self.source_address:
                    connection.bind(self.source_address)
                connection.connect(sockaddr)
                return connection
            except OSError:
                connection.close()
        raise NewConnectionError(self, "Public destination connection failed")


class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **kwargs: Any) -> None:
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)
        # PoolManager otherwise shares the global class map with other clients.
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme["https"] = _PublicHTTPSPool


def validate_url(url: str) -> str:
    """Validate URL syntax without performing a request or exposing its query."""
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or "\\" in url or any(ord(c) < 33 for c in url)):
            raise ValueError
        host = parts.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if address is None and re.fullmatch(r"[\d.:]+", host):
            raise ValueError
        if any(k.casefold() in {"token", "access_token", "api_key", "apikey", "password",
                               "secret", "authorization", "auth"}
               for k, _ in parse_qsl(parts.query)):
            raise ValueError
        normalized = urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))
        # Check precisely the URL requests will send (dot-segment removal, IDNA
        # and quoting), rather than granting robots permission to a different path.
        prepared = requests.PreparedRequest()
        prepared.prepare_url(normalized, None)
        return str(prepared.url)
    except (ValueError, TypeError, requests.exceptions.InvalidURL) as exc:
        raise AccessDenied("URL must be public credential-free HTTPS on port 443") from exc


class _Robots:
    def __init__(self, text: str, user_agent: str):
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[str, str]] = []
        had_rules = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if had_rules:
                    groups.append((agents, rules))
                    agents, rules, had_rules = [], [], False
                agents.append(value.lower())
            elif agents and key in {"allow", "disallow", "crawl-delay"}:
                had_rules = True
                if value:
                    rules.append((key, value))
        if agents:
            groups.append((agents, rules))
        ua = user_agent.lower()
        matches = [(max((len(a) if a != "*" else 0) for a in aa
                        if a == "*" or a in ua), rr)
                   for aa, rr in groups if any(a == "*" or a in ua for a in aa)]
        best = max((size for size, _ in matches), default=-1)
        self.rules = [rule for size, rr in matches if size == best for rule in rr]
        self.delay = 1.0
        for key, value in self.rules:
            if key == "crawl-delay":
                try:
                    self.delay = max(self.delay, float(value))
                except ValueError:
                    pass

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        def normalize(value: str) -> str:
            value = quote(value, safe="!$&'()*+,-./:;=?@_~%")
            def octet(match: Any) -> str:
                char = chr(int(match.group(1), 16))
                return char if char.isascii() and (char.isalnum() or char in "-._~") else match.group(0).upper()
            return re.sub(r"%([0-9a-fA-F]{2})", octet, value)
        path = normalize(parts.path + ("?" + parts.query if parts.query else ""))
        matches = []
        for action, pattern in self.rules:
            if action == "crawl-delay":
                continue
            pattern = normalize(pattern)
            end = pattern.endswith("$")
            literal = pattern[:-1] if end else pattern
            expression = "^" + ".*".join(re.escape(p) for p in literal.split("*"))
            if end:
                expression += "$"
            if re.search(expression, path):
                matches.append((len(literal.replace("*", "")), action == "allow"))
        return max(matches, default=(0, True))[1]


class PublicClient:
    def __init__(self, *, session: Any = None, timeout: float = 30,
                 retries: int = 2, max_backoff: float = 60,
                 max_redirects: int = 5, user_agent: str = USER_AGENT,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 resolver: Callable[..., Any] = socket.getaddrinfo,
                 public_api_robots: Mapping[str, Mapping[str, Any]] | None = None):
        if not user_agent.strip() or retries < 0 or retries > 5 or max_backoff < 1:
            raise ValueError("Invalid public transport limits or user agent")
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.session.auth = None
        self.session.headers.clear()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json,text/html,text/plain"})
        if isinstance(self.session, requests.Session):
            self.session.params.clear()
            self.session.proxies.clear()
            self.session.mount("https://", _PublicAdapter(max_retries=0))
        self.timeout, self.retries, self.max_backoff = timeout, retries, max_backoff
        self.max_redirects, self.user_agent = max_redirects, user_agent
        self.clock, self.sleep, self.resolver = clock, sleep, resolver
        self._last: dict[str, float] = {}
        self._interval: dict[str, float] = {}
        self._robots: dict[str, _Robots | None] = {}
        self._robots_unavailable_401: set[str] = set()
        self._public_api_robots: dict[str, str] = {}
        # This optional exception is backed by a separately researched public
        # API contract. It is not an interpretation of arbitrary 401 responses.
        # The runner supplies it only from enabled, validated entry evidence.
        for origin, evidence in (public_api_robots or {}).items():
            if (origin != "https://api.ashbyhq.com" or not isinstance(evidence, Mapping)
                    or evidence.get("http_status") != 401
                    or evidence.get("policy") != "documented_public_api"
                    or evidence.get("path_prefix") != "/posting-api/job-board/"
                    or evidence.get("documentation_url") != "https://developers.ashbyhq.com/docs/public-job-posting-api"
                    or evidence.get("reference_url") != "https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3"):
                raise ValueError("Unsupported public API robots exception evidence")
            verified_at = str(evidence.get("verified_at") or "")
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified_at):
                    raise ValueError
                datetime.strptime(verified_at, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Public API robots exception requires a verified calendar date") from None
            self._public_api_robots[origin] = str(evidence["path_prefix"])
        self._lock = threading.RLock()

    def _safe(self, url: str) -> str:
        url = validate_url(url)
        host = urlsplit(url).hostname
        try:
            addresses = self.resolver(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise AccessDenied("Destination DNS includes a non-public address")
        except (socket.gaierror, ValueError) as exc:
            raise AccessDenied("Cannot verify destination DNS") from exc
        return url

    def _rate(self, host: str) -> None:
        if host in self._last:
            delay = self._interval.get(host, 1) - (self.clock() - self._last[host])
            if delay > 0:
                self.sleep(delay)
        self._last[host] = self.clock()

    def _send(self, method: str, url: str, body: Any = None) -> Any:
        host = urlsplit(url).hostname or ""
        for attempt in range(self.retries + 1):
            self._safe(url)  # recheck DNS for each retry, including robots
            self._rate(host)
            self.session.cookies.clear()
            try:
                response = self.session.request(method, url, json=body, timeout=self.timeout,
                                                allow_redirects=False, auth=None)
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise PublicHTTPError(f"Public request to {host} failed ({type(exc).__name__})") from None
                self.sleep(min(2 ** attempt, self.max_backoff))
                continue
            finally:
                self.session.cookies.clear()
            if response.status_code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                return response
            delay = min(2 ** attempt, self.max_backoff)
            value = response.headers.get("Retry-After")
            if value:
                try:
                    delay = max(delay, float(value))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(value)
                        delay = max(delay, (date - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
                if delay > self.max_backoff:
                    raise PublicHTTPError(f"Retry-After exceeds bounded retry budget on {host}")
            self.sleep(delay)
        raise AssertionError("unreachable")

    def _allowed(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"https://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = None  # failure and recursive access stay closed
            robots_url = origin + "/robots.txt"
            for _ in range(self.max_redirects + 1):
                response = self._send("GET", robots_url)
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = self._safe(urljoin(robots_url, response.headers.get("Location", "")))
                    if urlsplit(target).netloc != parts.netloc:
                        raise AccessDenied("Cross-origin robots redirect cannot establish permission")
                    robots_url = target
                    continue
                if response.status_code == 404:
                    self._robots[origin] = _Robots("", self.user_agent)
                elif (response.status_code == 401 and response.text.strip().casefold() in {"", "unauthorized"}
                      and not response.headers.get("WWW-Authenticate")
                      and robots_url == origin + "/robots.txt"):
                    self._robots_unavailable_401.add(origin)
                elif response.status_code == 200:
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "html" in content_type or re.search(r"<(?:html|!doctype)", response.text, re.I):
                        raise AccessDenied("robots.txt returned HTML rather than policy")
                    self._robots[origin] = _Robots(response.text, self.user_agent)
                break
        policy = self._robots[origin]
        if policy is None:
            prefix = self._public_api_robots.get(origin)
            if prefix and origin in self._robots_unavailable_401 and parts.path.startswith(prefix):
                self._interval[parts.hostname or ""] = 1
                return
            raise AccessDenied(f"Robots policy denies public access on {parts.hostname}")
        if not policy.allows(url):
            raise AccessDenied(f"Robots policy denies public access on {parts.hostname}")
        if policy.delay > self.max_backoff:
            raise AccessDenied("Robots crawl-delay exceeds bounded wait budget")
        self._interval[parts.hostname or ""] = policy.delay

    def request(self, method: str, url: str, *, params: Any = None, json: Any = None) -> Any:
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise AccessDenied("Only public GET and POST are supported")
        if params:
            parts = urlsplit(url)
            query = urlencode(params, doseq=True)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                             parts.query + ("&" if parts.query else "") + query, ""))
        with self._lock:
            for _ in range(self.max_redirects + 1):
                url = self._safe(url)
                self._allowed(url)
                response = self._send(method, url, json)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise PublicHTTPError("Redirect omitted Location")
                    target = self._safe(urljoin(url, location))
                    if method == "POST" and response.status_code in {307, 308} and urlsplit(target).netloc != urlsplit(url).netloc:
                        raise AccessDenied("Cross-origin redirect cannot replay a POST body")
                    if response.status_code == 303 or (method == "POST" and response.status_code in {301, 302}):
                        method, json = "GET", None
                    url = target
                    continue
                if response.status_code >= 400:
                    raise PublicHTTPError(f"HTTP {response.status_code} on {urlsplit(url).hostname}")
                return response
        raise PublicHTTPError("Public redirect limit exceeded")

    def get(self, url: str, *, params: Any = None) -> Any:
        return self.request("GET", url, params=params)

    def post(self, url: str, *, json: Any = None) -> Any:
        return self.request("POST", url, json=json)

    def get_json(self, url: str, *, params: Any = None) -> Any:
        try:
            return self.get(url, params=params).json()
        except ValueError:
            raise PublicHTTPError("Public endpoint returned invalid JSON") from None

    def post_json(self, url: str, *, json: Any = None) -> Any:
        try:
            return self.post(url, json=json).json()
        except ValueError:
            raise PublicHTTPError("Public endpoint returned invalid JSON") from None

    def get_text(self, url: str) -> str:
        return self.get(url).text

    def close(self) -> None:
        self.session.cookies.clear()
        self.session.close()

    def __enter__(self) -> PublicClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
