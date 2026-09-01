"""Shared test scaffolding.

Everything the pipeline touches from the outside world — HTTP, the Anthropic
API, Gmail, a browser page — has exactly one injectable seam, and this module
supplies the fake for it. Consequences worth stating plainly:

  * the whole suite runs offline, with no API key and no browser;
  * no test may monkeypatch a private function to make itself pass — if a
    test needs a seam that does not exist, that is a design bug in `src`.

Time is injected, never frozen: every time-dependent function takes `now=`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pytest

# `src` lives next to `tests/`; make it importable without installing.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_MAX_AGE_HOURS  # noqa: E402
from src.models import ApplyStatus, Job, Score, ScoredJob  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A fixed clock. Every temporal assertion in the suite is relative to this.
NOW = datetime(2026, 8, 4, 9, 0, 0, tzinfo=timezone.utc)


# ==========================================================================
# time
# ==========================================================================


@pytest.fixture
def now() -> datetime:
    return NOW


def hours_ago(hours: float, *, base: datetime = NOW) -> datetime:
    return base - timedelta(hours=hours)


def ms_epoch(dt: datetime) -> int:
    """Millisecond epoch — the shape Lever uses for `createdAt`."""
    return int(dt.timestamp() * 1000)


# ==========================================================================
# jobs
# ==========================================================================


def make_job(
    *,
    source: str = "greenhouse",
    company: str = "Acme",
    title: str = "Backend Engineer",
    url: str | None = None,
    location: str = "Berlin, Germany",
    description: str = "We are looking for a backend engineer with Python and PostgreSQL.",
    posted_at: datetime | None = None,
    remote: bool | None = None,
    salary: str | None = None,
    country: str | None = None,
    ats: str | None = "greenhouse",
    ats_job_id: str | None = "1",
    raw: dict[str, Any] | None = None,
    hours_old: float | None = 2.0,
) -> Job:
    """Build a Job with sane defaults; override only what a test cares about.

    `hours_old` is a convenience over `posted_at` — pass `hours_old=None` to
    get an undated posting, which is its own important code path.

    The default URL is derived from the company and the ats_job_id, because
    that is how the world works: distinct jobs have distinct URLs, and a
    factory that gave every job the same one made `dedupe`'s URL pass merge
    strangers in half the suite. The all-defaults job still gets the exact
    historical URL (`…/acme/jobs/1`), so nothing that asserted it moved.
    """
    if url is None:
        label = re.sub(r"[^a-z0-9]+", "", company.lower()) or "acme"
        url = f"https://boards.greenhouse.io/{label}/jobs/{ats_job_id or '1'}"
    if posted_at is None and hours_old is not None:
        posted_at = hours_ago(hours_old)
    return Job(
        source=source,
        company=company,
        title=title,
        url=url,
        location=location,
        description=description,
        posted_at=posted_at,
        remote=remote,
        salary=salary,
        country=country,
        ats=ats,
        ats_job_id=ats_job_id,
        raw=raw or {},
    )


def make_scored(
    job: Job | None = None,
    *,
    score: int = 82,
    reasons: Sequence[str] = ("Python + PostgreSQL match", "Seniority aligns"),
    error: str | None = None,
    status: ApplyStatus = ApplyStatus.DIGEST,
    **job_kwargs: Any,
) -> ScoredJob:
    """A scored job, defaulting to the state it is in when it leaves scoring.

    `DIGEST` rather than `NEW`: by the time anything downstream (tailoring,
    apply, digest) sees a `ScoredJob`, `score_jobs` has already classified it.
    """
    job = job or make_job(**job_kwargs)
    return ScoredJob(
        job=job,
        status=status,
        score=Score(
            value=score,
            reasons=list(reasons),
            strengths=["5y Python"],
            gaps=["No Kafka"],
            verdict="Strong fit",
            model="test-model",
            error=error,
        ),
    )


@pytest.fixture
def job() -> Job:
    return make_job()


@pytest.fixture
def jobs() -> list[Job]:
    """A small, deliberately heterogeneous batch.

    Covers: fresh EU, stale EU, undated, US, remote-EU, remote-unqualified,
    and an excluded-by-title internship.

    Globex's age is stated relative to the shipped `freshness.max_age_hours`
    rather than as a literal, so "stale EU" stays true when that window is
    retuned. It moved from 24h to 72h once already, and a fixture whose
    comment claims one thing while its number means another is the rot this
    suite exists to avoid.

    `+ 1` and not some larger number: it says "one hour past the window", which
    is the case worth fixturing. The `+ 26` it replaces was reverse-engineered
    from the old literal 50 and expressed nothing at all.
    """
    return [
        make_job(company="Acme", title="Backend Engineer",
                 location="Berlin, Germany", hours_old=2, ats_job_id="1"),
        make_job(company="Globex", title="Data Engineer",
                 location="Amsterdam, Netherlands",
                 hours_old=DEFAULT_MAX_AGE_HOURS + 1, ats_job_id="2"),
        make_job(company="Initech", title="Platform Engineer",
                 location="Madrid, Spain", hours_old=None, ats_job_id="3"),
        make_job(company="Hooli", title="Backend Engineer",
                 location="San Francisco, CA", hours_old=1, ats_job_id="4"),
        make_job(company="Umbrella", title="Senior Python Engineer",
                 location="Remote - Europe", remote=True, hours_old=3, ats_job_id="5"),
        make_job(company="Soylent", title="Backend Engineer",
                 location="Remote", remote=True, hours_old=3, ats_job_id="6",
                 description="Work from anywhere in the United States."),
        make_job(company="Vandelay", title="Backend Engineering Intern",
                 location="Lisbon, Portugal", hours_old=1, ats_job_id="7"),
    ]


# ==========================================================================
# config
# ==========================================================================

BASE_CV = """# Ada Lovelace

Berlin, Germany · ada@example.com · +49 30 123456

## Summary
Senior backend engineer, 8 years, Python and distributed data systems.

## Skills
- **Languages:** Python, Go, SQL
- **Data/Infra:** PostgreSQL, Kafka, Airflow
- **Cloud:** AWS, Terraform, Docker

## Experience

### Senior Backend Engineer — Northwind
*Berlin, Germany · 2021 – Present*
- Cut p99 checkout latency 840ms to 210ms by batching Redis reads.
- Led the migration of 40 services from ECS to EKS with zero downtime.

### Backend Engineer — Contoso
*Remote · 2018 – 2021*
- Built an Airflow pipeline processing 2TB/day.

## Education
**BSc Computer Science** — TU Berlin, 2018

## Work authorisation
EU citizen — no sponsorship required.
"""


@pytest.fixture
def base_cv() -> str:
    return BASE_CV


def write_config(
    tmp_path: Path,
    overrides: dict[str, Any] | None = None,
    watchlist: dict[str, Any] | None = None,
    *,
    cv: str | None = BASE_CV,
):
    """Materialise a config.yaml + watchlist.yaml + CV in `tmp_path`.

    Returns a loaded `Config` rooted there, so `cfg.path(...)`, the tracker
    and every output file stay inside the test's tmpdir.
    """
    import yaml

    from src.config import Config

    data: dict[str, Any] = {
        "applicant": {
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "phone": "+49 30 123456",
            "location": "Berlin, Germany",
            "linkedin": "https://linkedin.com/in/ada",
        },
        "keys": {"anthropic": "test-key", "openrouter": "test-key"},
        "sources": {"greenhouse": True, "lever": False,
                    "adzuna": False, "linkedin_email": False},
        "output": {"dir": str(tmp_path / "output"), "open_browser": False},
        "db": {"path": str(tmp_path / "output" / "tracker.sqlite3"), "skip_seen_days": 30},
        "cv": {"path": str(tmp_path / "cv" / "base_cv.md")},
    }
    if overrides:
        from src.config import deep_merge

        data = deep_merge(data, overrides)

    watch: dict[str, Any] = watchlist if watchlist is not None else {"greenhouse": ["acme"]}

    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp_path / "watchlist.yaml").write_text(yaml.safe_dump(watch), encoding="utf-8")
    if cv is not None:
        (tmp_path / "cv").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cv" / "base_cv.md").write_text(cv, encoding="utf-8")

    return Config.load(
        tmp_path / "config.yaml", tmp_path / "watchlist.yaml", root=tmp_path, env={}
    )


@pytest.fixture
def config(tmp_path: Path):
    return write_config(tmp_path)


@pytest.fixture
def config_factory(tmp_path: Path) -> Callable[..., Any]:
    def _factory(overrides=None, watchlist=None, cv=BASE_CV):
        return write_config(tmp_path, overrides, watchlist, cv=cv)

    return _factory


@pytest.fixture
def tracker(tmp_path: Path):
    from src.db import Tracker

    t = Tracker(tmp_path / "tracker.sqlite3")
    yield t
    t.close()


@pytest.fixture
def memory_tracker():
    from src.db import Tracker

    t = Tracker(":memory:")
    yield t
    t.close()


# ==========================================================================
# HTTP
# ==========================================================================


@dataclass
class FakeResponse:
    """Minimal stand-in for `requests.Response`."""

    status_code: int = 200
    _json: Any = None
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    def json(self) -> Any:
        if self._json is None:
            return json.loads(self.text)
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def json_response(payload: Any, status: int = 200) -> FakeResponse:
    return FakeResponse(status_code=status, _json=payload, text=json.dumps(payload))


def html_response(body: str, status: int = 200) -> FakeResponse:
    return FakeResponse(status_code=status, text=body,
                        headers={"Content-Type": "text/html"})


def xml_response(body: str, status: int = 200) -> FakeResponse:
    """A response whose payload is read via `.text`, not `.json()`.

    Personio's board is XML, so its fetcher goes through `util.http_get`
    rather than `http_get_json` — a `json_response` would hide the fact that
    nothing in that path ever calls `.json()`.
    """
    return FakeResponse(status_code=status, text=body,
                        headers={"Content-Type": "application/xml"})


class FakeSession:
    """Routing fake for `requests`-style sessions.

    Routes are matched in order. A route key may be a plain substring of the
    URL or a compiled regex; the value may be a `FakeResponse`, an exception
    instance (raised), or a callable `(url, params) -> FakeResponse`.
    """

    def __init__(
        self,
        routes: Iterable[tuple[Any, Any]] | dict[Any, Any] | None = None,
        default: Any = None,
    ) -> None:
        if isinstance(routes, dict):
            routes = list(routes.items())
        self.routes: list[tuple[Any, Any]] = list(routes or [])
        self.default = default if default is not None else FakeResponse(status_code=404)
        self.calls: list[dict[str, Any]] = []

    def add(self, matcher: Any, response: Any) -> "FakeSession":
        self.routes.append((matcher, response))
        return self

    def get(self, url: str, params: Any = None, headers: Any = None,
            timeout: Any = None, **kwargs: Any) -> FakeResponse:
        self.calls.append(
            {"method": "GET", "url": url, "params": dict(params or {}),
             "headers": dict(headers or {}), "timeout": timeout}
        )
        for matcher, response in self.routes:
            if _matches(matcher, url):
                if isinstance(response, BaseException):
                    raise response
                if callable(response) and not isinstance(response, FakeResponse):
                    return response(url, params)
                return response
        if isinstance(self.default, BaseException):
            raise self.default
        return self.default

    # convenience assertions -------------------------------------------

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def params_for(self, substring: str) -> list[dict[str, Any]]:
        return [c["params"] for c in self.calls if substring in c["url"]]


def _matches(matcher: Any, url: str) -> bool:
    if isinstance(matcher, re.Pattern):
        return bool(matcher.search(url))
    if callable(matcher):
        return bool(matcher(url))
    return str(matcher) in url


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def no_sleep() -> Callable[[float], None]:
    """Drop-in for `time.sleep` so retry paths cost nothing."""
    calls: list[float] = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep


# ==========================================================================
# Anthropic
# ==========================================================================


@dataclass
class FakeTextBlock:
    """Object-shaped content block, like the real SDK returns."""

    text: str
    type: str = "text"


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str = "end_turn"
    model: str = "test-model"


class FakeMessages:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> FakeMessage:
        return self._owner._next(**kwargs)


class FakeAnthropic:
    """Stand-in for `anthropic.Anthropic`.

    `responses` may hold strings (wrapped as one text block), pre-built
    `FakeMessage`s, exceptions (raised), or callables taking the create()
    kwargs. The last entry repeats once the list is exhausted, so a test that
    scores ten jobs only has to supply one response.
    """

    def __init__(self, responses: Any = None, *, dict_blocks: bool = False) -> None:
        if responses is None:
            responses = ['{"score": 80, "verdict": "ok", "reasons": [], '
                         '"strengths": [], "gaps": []}']
        if not isinstance(responses, list):
            responses = [responses]
        self.responses = responses
        self.dict_blocks = dict_blocks
        self.calls: list[dict[str, Any]] = []
        self.messages = FakeMessages(self)

    def _next(self, **kwargs: Any) -> FakeMessage:
        index = min(len(self.calls), len(self.responses) - 1)
        self.calls.append(kwargs)
        item = self.responses[index]
        if callable(item) and not isinstance(item, (str, FakeMessage)):
            item = item(**kwargs)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeMessage):
            return item
        block: Any = ({"type": "text", "text": str(item)} if self.dict_blocks
                      else FakeTextBlock(text=str(item)))
        return FakeMessage(content=[block])

    # convenience assertions -------------------------------------------

    @property
    def prompts(self) -> list[str]:
        out = []
        for call in self.calls:
            for message in call.get("messages", []):
                out.append(str(message.get("content", "")))
        return out

    @property
    def systems(self) -> list[str]:
        return [str(c.get("system", "")) for c in self.calls]

    @property
    def models(self) -> list[str]:
        return [str(c.get("model", "")) for c in self.calls]


def llm_client(responses: Any = None, **kwargs: Any):
    """A real `LLMClient` wrapping a `FakeAnthropic` — the seam under test."""
    from src.llm import LLMClient

    return LLMClient("test-key", provider="anthropic",
                     client=FakeAnthropic(responses, **kwargs))


@pytest.fixture(autouse=True)
def _no_accidental_network(request, monkeypatch):
    """The offline suite now ENFORCES offline instead of asserting it.

    Every real HTTP client in this stack honours the proxy environment, so
    pointing it at a port nothing listens on makes any accidental egress fail
    in milliseconds with a ProxyError naming this fixture's doing -- instead
    of what actually happened once: a provider-default flip routed 54 tests
    around their injected fakes and into the real internet, where each one
    burned the transport's full retry backoff against a blocked host and the
    suite took eight minutes to say so. Tests marked `network` keep the real
    environment; they are the one place egress is the point.
    """
    if request.node.get_closest_marker("network"):
        return
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "")


@pytest.fixture(autouse=True)
def _no_ambient_credentials(request, monkeypatch):
    """The developer's real environment must not leak into the suite.

    On the user's machine OPENROUTER_API_KEY is exported — that is exactly
    how live runs are supposed to get it — and with env-wins that quietly
    SOLVED every missing-key scenario the validation tests construct: the
    suite passed in a bare container and failed... no, worse, *changed
    meaning* on the machine it exists to protect. Every `ENV_OVERRIDES`
    variable is stripped; a test that wants one injects `env=` explicitly.
    Network-marked tests keep the real environment — a live contract run
    needs the real key.
    """
    if request.node.get_closest_marker("network"):
        return
    from src.config import ENV_OVERRIDES

    for _path, var in ENV_OVERRIDES.items():
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_anthropic() -> Callable[..., FakeAnthropic]:
    return FakeAnthropic


class TransientAPIError(Exception):
    """Retryable in the eyes of llm.LLMClient (name + status_code)."""

    def __init__(self, message: str = "overloaded", status_code: int = 529) -> None:
        super().__init__(message)
        self.status_code = status_code


TransientAPIError.__name__ = "OverloadedError"


class FatalAPIError(Exception):
    """Not retryable: a 401 will not get better."""

    def __init__(self, message: str = "invalid api key") -> None:
        super().__init__(message)
        self.status_code = 401


FatalAPIError.__name__ = "AuthenticationError"


# ==========================================================================
# browser page
# ==========================================================================


class FakeElement:
    """A DOM element supporting only what `autoapply` is allowed to use."""

    def __init__(
        self,
        tag: str = "input",
        *,
        type: str = "text",
        name: str = "",
        id: str = "",
        label: str = "",
        required: bool = False,
        options: Sequence[str] | None = None,
        classes: Sequence[str] | None = None,
        text: str = "",
        **attrs: Any,
    ) -> None:
        self.tag = tag.lower()
        self.attrs: dict[str, Any] = {
            "type": type, "name": name, "id": id,
            "aria-label": label, "class": " ".join(classes or []),
        }
        self.attrs.update({k.replace("_", "-"): v for k, v in attrs.items()})
        if required:
            self.attrs["required"] = "true"
        self.attrs["data-label"] = label
        self.label = label
        self.required = required
        self.options = list(options or [])
        self.text = text or label
        self.filled: list[Any] = []
        self.files: list[Any] = []
        self.clicked = 0

    # -- playwright-ish surface ---------------------------------------

    def get_attribute(self, name: str) -> str | None:
        """Absent attribute -> None; present-but-valueless -> "".

        The distinction is load-bearing: a real DOM returns `""` for
        `<input required>`, so collapsing it to None would make the fake
        unable to express a required field at all.
        """
        if name not in self.attrs:
            return None
        value = self.attrs[name]
        return None if value is None else str(value)

    def inner_text(self) -> str:
        return self.text

    def text_content(self) -> str:
        return self.text

    def is_visible(self) -> bool:
        return True

    def fill(self, value: str) -> None:
        self.filled.append(value)

    def set_input_files(self, path: Any) -> None:
        self.files.append(str(path))

    def click(self, **_: Any) -> None:
        self.clicked += 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} name={self.attrs.get('name')!r} label={self.label!r}>"


_SELECTOR_PART = re.compile(
    r"""^(?P<tag>[a-zA-Z*]+)?
        (?P<id>\#[\w-]+)?
        (?P<classes>(?:\.[\w-]+)*)
        (?P<attrs>(?:\[[^\]]+\])*)$""",
    re.VERBOSE,
)
_ATTR_RE = re.compile(r"\[([\w-]+)(?:([~^$*]?=)\"?'?([^\]\"']*)\"?'?)?\]")


def _element_matches(el: FakeElement, part: str) -> bool:
    """Tiny CSS matcher: tag, #id, .class, [attr], [attr=v], [attr*=v]."""
    part = part.strip()
    if not part:
        return False
    m = _SELECTOR_PART.match(part.split(":")[0])
    if not m:
        return False
    tag = m.group("tag")
    if tag and tag != "*" and tag.lower() != el.tag:
        return False
    if m.group("id") and m.group("id")[1:] != str(el.attrs.get("id", "")):
        return False
    for cls in filter(None, m.group("classes").split(".")):
        if cls not in str(el.attrs.get("class", "")).split():
            return False
    for name, op, value in _ATTR_RE.findall(m.group("attrs") or ""):
        actual = el.attrs.get(name)
        if actual is None or actual == "":
            return False
        actual = str(actual)
        if not op:
            continue
        if op == "=" and actual != value:
            return False
        if op == "*=" and value not in actual:
            return False
        if op == "^=" and not actual.startswith(value):
            return False
        if op == "$=" and not actual.endswith(value):
            return False
    return True


class FakePage:
    """Implements exactly the page protocol `autoapply` documents.

    Anything `autoapply` calls that is not here is a contract violation, and
    the test will fail loudly with AttributeError — which is the point.
    """

    def __init__(
        self,
        elements: Sequence[FakeElement] | None = None,
        *,
        url: str = "https://boards.greenhouse.io/acme/jobs/1",
        html: str = "",
        confirmation: str | None = "Thank you for applying",
        goto_error: Exception | None = None,
        fill_error: Exception | None = None,
    ) -> None:
        self.elements = list(elements or [])
        self.url = url
        self.html = html
        self.confirmation = confirmation
        self.goto_error = goto_error
        self.fill_error = fill_error
        self.actions: list[tuple[str, Any]] = []
        self.filled: dict[str, str] = {}
        self.uploaded: dict[str, str] = {}
        self.clicks: list[str] = []
        self.screenshots: list[str] = []
        self.closed = False

    # -- navigation ----------------------------------------------------

    def goto(self, url: str, **kwargs: Any) -> None:
        self.actions.append(("goto", url))
        if self.goto_error:
            raise self.goto_error
        self.url = url

    def content(self) -> str:
        return self.html or self.confirmation or ""

    # -- queries -------------------------------------------------------

    def query_selector_all(self, selector: str) -> list[FakeElement]:
        self.actions.append(("query_selector_all", selector))
        parts = [p.strip() for p in selector.split(",") if p.strip()]
        out: list[FakeElement] = []
        for el in self.elements:
            for part in parts:
                leaf = part.split()[-1] if " " in part else part
                leaf = leaf.split(">")[-1].strip()
                if _element_matches(el, leaf):
                    out.append(el)
                    break
        return out

    def query_selector(self, selector: str) -> FakeElement | None:
        found = self.query_selector_all(selector)
        return found[0] if found else None

    def wait_for_selector(self, selector: str, **kwargs: Any) -> FakeElement | None:
        self.actions.append(("wait_for_selector", selector))
        found = self.query_selector_all(selector)
        if found:
            return found[0]
        if self.confirmation and any(
            token in selector.lower() for token in ("confirm", "thank", "success")
        ):
            return FakeElement("div", text=self.confirmation)
        raise TimeoutError(f"no element matched {selector}")

    # -- interaction ---------------------------------------------------

    def fill(self, selector: str, value: str, **kwargs: Any) -> None:
        self.actions.append(("fill", (selector, value)))
        if self.fill_error:
            raise self.fill_error
        self.filled[selector] = value
        for el in self.query_selector_all(selector):
            el.fill(value)

    def set_input_files(self, selector: str, path: Any, **kwargs: Any) -> None:
        self.actions.append(("set_input_files", (selector, str(path))))
        self.uploaded[selector] = str(path)
        for el in self.query_selector_all(selector):
            el.set_input_files(path)

    def click(self, selector: str, **kwargs: Any) -> None:
        self.actions.append(("click", selector))
        self.clicks.append(selector)
        for el in self.query_selector_all(selector):
            el.click()

    def check(self, selector: str, **kwargs: Any) -> None:
        self.actions.append(("check", selector))

    def screenshot(self, path: Any = None, **kwargs: Any) -> bytes:
        self.actions.append(("screenshot", str(path) if path else None))
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # A real PNG header keeps anything that sniffs the file happy.
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-screenshot")
            self.screenshots.append(str(p))
        return b"\x89PNG\r\n\x1a\n"

    def close(self) -> None:
        self.closed = True
        self.actions.append(("close", None))

    def set_default_timeout(self, timeout: Any) -> None:
        self.actions.append(("set_default_timeout", timeout))

    # -- assertions ----------------------------------------------------

    @property
    def submitted(self) -> bool:
        """True iff something that looks like a submit control was clicked."""
        return any(
            any(token in c.lower() for token in ("submit", "apply", "send"))
            for c in self.clicks
        )


class FakeBrowser:
    """`browser.new_page()` seam for `autoapply.run`."""

    def __init__(self, pages: Sequence[FakePage] | FakePage | None = None) -> None:
        if isinstance(pages, FakePage):
            pages = [pages]
        self._pages = list(pages or [])
        self.created: list[FakePage] = []
        self.closed = False

    def new_page(self, **kwargs: Any) -> FakePage:
        page = self._pages.pop(0) if self._pages else FakePage(simple_form())
        self.created.append(page)
        return page

    def new_context(self, **kwargs: Any) -> "FakeBrowser":
        return self

    def close(self) -> None:
        self.closed = True


# -- reusable form shapes --------------------------------------------------


def simple_form() -> list[FakeElement]:
    """The only shape auto-apply is allowed to submit."""
    return [
        FakeElement("input", type="text", name="first_name", id="first_name",
                    label="First Name", required=True),
        FakeElement("input", type="text", name="last_name", id="last_name",
                    label="Last Name", required=True),
        FakeElement("input", type="email", name="email", id="email",
                    label="Email", required=True),
        FakeElement("input", type="tel", name="phone", id="phone", label="Phone"),
        FakeElement("input", type="file", name="resume", id="resume",
                    label="Resume/CV", required=True),
        FakeElement("button", type="submit", name="submit", id="submit_app",
                    label="Submit Application", text="Submit Application"),
    ]


def form_with(*extra: FakeElement) -> list[FakeElement]:
    """A simple form plus whatever should trigger a bail."""
    base = simple_form()
    return base[:-1] + list(extra) + base[-1:]


@pytest.fixture
def page() -> FakePage:
    return FakePage(simple_form())


@pytest.fixture
def browser() -> FakeBrowser:
    return FakeBrowser()


# ==========================================================================
# fixture files
# ==========================================================================


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> Any:
    return json.loads(load_fixture(name))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
