"""Provider rejection fixtures only: no account access or completion spend."""
import json
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import pytest

from src.llm import LLMClient, LLMError, ProviderLimits, UsageMeter, chain_from_config
from src.util import HttpError, http_post_json


class Clock:
    now = 0.0
    epoch = 1_790_000_000.0

    def monotonic(self):
        return self.now

    def wall(self):
        return self.epoch + self.now


class Response:
    def __init__(self, status=200, *, error=None, headers=None, text='{"score":71}'):
        self.status_code = status
        self.headers = headers or {}
        self.body = ({"error": error} if error else {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0},
        })
        self.text = json.dumps(self.body)

    def json(self):
        return self.body


def limited(*, headers=None, message="Rate limit exceeded", source=None, status=429):
    return Response(status, headers=headers, error={
        "code": status, "message": message,
        "metadata": {"limit_source": source} if source else {},
    })


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []
        self.lock = threading.Lock()

    def post(self, url, **kwargs):
        with self.lock:
            self.posts.append((url, kwargs))
            response = self.responses.pop(0) if self.responses else Response()
        return response() if callable(response) else response


@pytest.fixture
def timing():
    clock = Clock()
    return clock, ProviderLimits(clock=clock.monotonic, wall_clock=clock.wall)


def client(session, limits, **kwargs):
    return LLMClient(kwargs.pop("key", "synthetic-key"), session=session, limits=limits,
                     sleep=lambda _: pytest.fail("429 must fall back without sleeping"),
                     meter=UsageMeter(), **kwargs)


def call(llm, model="one:free"):
    return llm.complete(model=model, system="", prompt="synthetic", max_tokens=10)


def test_http_error_preserves_status_and_only_limit_response_headers():
    session = Session(limited(headers={"Retry-After": "90", "X-RateLimit-Reset": "1790000090000",
                                       "Authorization": "do-not-retain", "Set-Cookie": "private"}))
    with pytest.raises(HttpError) as caught:
        http_post_json("https://fixture.invalid", session=session)
    assert caught.value.status_code == 429
    assert caught.value.headers == {"retry-after": "90", "x-ratelimit-reset": "1790000090000"}
    assert caught.value.error["code"] == 429
    assert len(session.posts) == 1


@pytest.mark.parametrize("headers,delay", [
    ({"Retry-After": "120"}, 120),
    ({"rEtRy-AfTeR": formatdate(Clock.epoch + 120, usegmt=True)}, 120),
    ({"X-RateLimit-Reset": str(Clock.epoch + 120)}, 120),
    ({"X-RateLimit-Reset": str((Clock.epoch + 120) * 1000)}, 120),
    ({"Retry-After": "20", "X-RateLimit-Reset": str(Clock.epoch + 120)}, 120),
    ({"Retry-After": "junk", "X-RateLimit-Reset": "NaN"}, 60),
    ({"Retry-After": "inf"}, 60),
    ({"Retry-After": "-5"}, 60),
    ({"Retry-After": "0"}, 1),
    ({}, 60),
])
def test_hints_and_expiry_stop_repeated_jobs_without_sleep(timing, headers, delay):
    clock, limits = timing
    session = Session(limited(headers=headers), Response())
    llm = client(session, limits)
    with pytest.raises(LLMError) as error:
        call(llm)
    assert error.value.status_code == 429
    assert error.value.headers == {k.lower(): v for k, v in headers.items()}
    for moment in [0, delay - .01]:
        clock.now = moment
        with pytest.raises(LLMError, match="cooldown"):
            call(llm)
    assert len(session.posts) == 1
    clock.now = delay
    assert call(llm) == '{"score":71}'
    assert len(session.posts) == 2


def test_model_account_endpoint_and_custom_auth_isolation(timing):
    _, limits = timing
    failed = client(Session(limited()), limits)
    with pytest.raises(LLMError):
        call(failed)
    for kwargs, model in [({}, "two:free"), ({"key": "another-account"}, "one:free"),
                          ({"base_url": "http://localhost:11434/v1"}, "one:free"),
                          ({"headers": {"Authorization": "Bearer another"}}, "one:free")]:
        assert call(client(Session(), limits, **kwargs), model) == '{"score":71}'
    duplicate = Session()
    with pytest.raises(LLMError, match="cooldown"):
        call(client(duplicate, limits))
    assert not duplicate.posts


@pytest.mark.parametrize("message", ["Rate limit exceeded: free-models-per-day.",
                                     "Rate limit exceeded: free-models-per-min."])
def test_explicit_free_quota_skips_free_sibling_but_not_paid_or_local(timing, message):
    _, limits = timing
    session = Session(limited(message=message))
    llm = client(session, limits)
    with pytest.raises(LLMError):
        call(llm)
    with pytest.raises(LLMError, match="cooldown \\(free"):
        call(llm, "two:free")
    assert len(session.posts) == 1
    assert call(llm, "paid-model")
    assert call(client(Session(), limits, base_url="http://localhost:11434/v1"))
    assert call(client(Session(), limits, key="different-account"))


@pytest.mark.parametrize("source", ["upstream_provider_shared_pool", "openrouter_credits",
                                    "openrouter_in_flight_budget"])
def test_upstream_or_ambiguous_credit_errors_do_not_disable_other_models(timing, source):
    _, limits = timing
    status = 429 if source.startswith("upstream") else 402
    llm = client(Session(limited(source=source, status=status)), limits)
    with pytest.raises(LLMError):
        call(llm)
    assert call(llm, "two:free")


def test_explicit_key_limit_blocks_account_only(timing):
    _, limits = timing
    llm = client(Session(limited(status=402, source="openrouter_key_limit")), limits)
    with pytest.raises(LLMError):
        call(llm)
    with pytest.raises(LLMError, match="cooldown \\(account"):
        call(llm, "two")
    assert call(client(Session(), limits, key="different"))
    assert call(client(Session(), limits, base_url="http://localhost:11434/v1"))


def test_chain_shares_limits_and_keeps_local_json_validation_reasoning_and_meter(timing):
    _, limits = timing
    session = Session(limited(message="Rate limit exceeded: free-models-per-day."),
                      Response(text='{"score":200}'), Response())
    meter = UsageMeter()
    config = {"llm": {"provider": "openrouter"}, "keys": {"openrouter": "fixture"},
              "scoring": {"model": "one:free", "fallback_models": ["two:free", {
                  "model": "local", "base_url": "http://localhost:11434/v1",
                  "reasoning_effort": "none", "max_retries": 0,
              }]}}
    chain = chain_from_config(config, "scoring", session=session, limits=limits, meter=meter)
    args = dict(model="ignored", system="", prompt="synthetic", max_tokens=10,
                schema={"type": "object", "properties": {"score": {"type": "integer", "maximum": 100}},
                        "required": ["score"]})
    with pytest.raises(LLMError, match="all models failed"):
        chain.complete_json(**args)
    assert chain.complete_json(**args) == {"score": 71}
    assert chain.last_model == "local"
    assert [post[1]["json"]["model"] for post in session.posts] == ["one:free", "local", "local"]
    assert session.posts[-1][1]["json"]["reasoning_effort"] == "none"
    assert meter.snapshot()["calls"] == 2


def test_http_200_error_retains_code_and_blocks_subsequent_call(timing):
    _, limits = timing
    session = Session(Response(error={"code": 429, "message": "limited"}))
    llm = client(session, limits)
    for _ in range(2):
        with pytest.raises(LLMError) as caught:
            call(llm)
        assert caught.value.status_code == 429
    assert len(session.posts) == 1
    assert llm._meter.snapshot()["calls"] == 1


def test_concurrent_jobs_and_single_recovery_probe(timing):
    clock, limits = timing
    entered, finish = threading.Event(), threading.Event()

    def slow_success():
        entered.set()
        assert finish.wait(5)
        return Response()

    session = Session(limited(), slow_success)
    llm = client(session, limits)
    with pytest.raises(LLMError):
        call(llm)

    def blocked():
        with pytest.raises(LLMError, match="cooldown"):
            call(llm)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: blocked(), range(20)))
        assert len(session.posts) == 1
        clock.now = 60
        probe = pool.submit(call, llm)
        try:
            assert entered.wait(5)
            list(pool.map(lambda _: blocked(), range(20)))
            assert len(session.posts) == 2
        finally:
            finish.set()
        assert probe.result() == '{"score":71}'
    assert call(llm)


def test_inflight_success_cannot_clear_newer_rejection(timing):
    _, limits = timing
    entered, finish = threading.Event(), threading.Event()

    def slow_success():
        entered.set()
        assert finish.wait(5)
        return Response()

    session = Session(slow_success, limited())
    llm = client(session, limits)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(call, llm)
        try:
            assert entered.wait(5)
            with pytest.raises(LLMError):
                call(llm)
        finally:
            finish.set()
        assert pending.result()
    with pytest.raises(LLMError, match="cooldown"):
        call(llm)
    assert len(session.posts) == 2


def test_daily_quota_without_reset_waits_until_next_utc_day(timing):
    clock, limits = timing
    llm = client(Session(limited(message="Rate limit exceeded: free-models-per-day.")), limits)
    with pytest.raises(LLMError):
        call(llm)
    remaining = 86400 - clock.epoch % 86400
    clock.now = remaining - 1
    with pytest.raises(LLMError, match="cooldown"):
        call(llm, "two:free")
    clock.now = remaining
    assert call(llm, "two:free")


def test_nested_reset_metadata_and_http_header_precedence(timing):
    clock, limits = timing
    response = limited(headers={"Retry-After": "120"})
    response.body["error"]["metadata"]["headers"] = {"Retry-After": "240", "X-RateLimit-Reset": str((clock.epoch + 90) * 1000)}
    llm = client(Session(response), limits)
    with pytest.raises(LLMError):
        call(llm)
    clock.now = 119
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.error["metadata"]["headers"]["Retry-After"] == "240"
    clock.now = 120
    assert call(llm)


def test_nested_only_reset_is_used(timing):
    clock, limits = timing
    response = limited()
    response.body["error"]["metadata"]["headers"] = {"X-RateLimit-Reset": str((clock.epoch + 240) * 1000)}
    llm = client(Session(response), limits)
    with pytest.raises(LLMError):
        call(llm)
    clock.now = 239
    with pytest.raises(LLMError):
        call(llm)
    clock.now = 240
    assert call(llm)


def test_failed_probe_renews_cooldown_and_preserves_metadata(timing):
    clock, limits = timing
    session = Session(limited(), limited(headers={"Retry-After": "120"}))
    llm = client(session, limits)
    with pytest.raises(LLMError):
        call(llm)
    clock.now = 60
    with pytest.raises(LLMError):
        call(llm)
    clock.now = 179
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.headers["retry-after"] == "120"
    assert len(session.posts) == 2
    clock.now = 180
    assert call(llm)


def test_other_gateway_does_not_inherit_openrouter_account_or_reset_rules(timing):
    clock, limits = timing
    session = Session(limited(message="Rate limit exceeded: free-models-per-day.",
                              headers={"X-RateLimit-Reset": str(clock.epoch + 1000)}))
    llm = client(session, limits, base_url="http://localhost:11434/v1")
    with pytest.raises(LLMError):
        call(llm)
    assert call(llm, "two:free")
    clock.now = 60
    assert call(llm)


def test_unknown_metadata_and_plain_text_429_remain_bounded(timing):
    _, limits = timing
    response = limited()
    response.body["error"]["metadata"] = ["unexpected"]
    llm = client(Session(response), limits)
    with pytest.raises(LLMError):
        call(llm)
    response.json = lambda: (_ for _ in ()).throw(ValueError("html"))
    llm = client(Session(response), limits, key="other")
    with pytest.raises(LLMError):
        call(llm)
    with pytest.raises(LLMError, match="cooldown"):
        call(llm)


@pytest.mark.parametrize("metadata", [{"provider_code": "upstream_rate_limited"},
                                      {"provider_name": "upstream"}])
def test_upstream_evidence_prevents_free_quota_broadening(timing, metadata):
    _, limits = timing
    response = limited(message="Rate limit exceeded: free-models-per-day.")
    response.body["error"]["metadata"] = metadata
    llm = client(Session(response), limits)
    with pytest.raises(LLMError):
        call(llm)
    assert call(llm, "two:free")


def test_malformed_limit_source_cannot_hide_http_status(timing):
    _, limits = timing
    response = limited(status=402)
    response.body["error"]["metadata"] = {"limit_source": []}
    llm = client(Session(response), limits)
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.status_code == 402


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("hint", [" ", "\t", "  \t ", "\u00a0"])
def test_blank_retry_hint_preserves_429_and_default_cooldown(timing, nested, hint):
    clock, limits = timing
    response = limited(headers={} if nested else {"Retry-After": hint})
    if nested:
        response.body["error"]["metadata"]["headers"] = {"Retry-After": hint}
    session = Session(response)
    llm = client(session, limits)
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.status_code == 429
    assert caught.value.error == response.body["error"]
    clock.now = 59
    with pytest.raises(LLMError, match="cooldown"):
        call(llm)
    assert len(session.posts) == 1
    clock.now = 60
    assert call(llm)


@pytest.mark.parametrize("headers", [[("Retry-After", "30")], "Retry-After: 30", 123, None])
def test_nonmapping_response_headers_preserve_error_and_default_cooldown(timing, headers):
    clock, limits = timing
    response = limited()
    response.headers = headers
    with pytest.raises(HttpError) as caught:
        http_post_json("https://fixture.invalid", session=Session(response))
    assert caught.value.status_code == 429
    assert caught.value.headers == {}
    session = Session(response)
    llm = client(session, limits)
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.status_code == 429
    with pytest.raises(LLMError, match="cooldown"):
        call(llm)
    assert len(session.posts) == 1
    clock.now = 60
    assert call(llm)


@pytest.mark.parametrize("failure", ["500", "timeout", "empty"])
def test_failed_recovery_retains_single_probe_until_success(timing, failure):
    clock, limits = timing
    entered, finish = threading.Event(), threading.Event()

    def fail():
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        return Response(500) if failure == "500" else Response(text="")

    def slow_success():
        entered.set()
        assert finish.wait(5)
        return Response()

    session = Session(limited(), fail, slow_success)
    llm = client(session, limits, max_retries=0)
    with pytest.raises(LLMError):
        call(llm)
    clock.now = 60
    with pytest.raises(LLMError):
        call(llm)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = pool.submit(call, llm)
        try:
            assert entered.wait(5)

            def blocked(_):
                with pytest.raises(LLMError, match="cooldown"):
                    call(llm)

            list(pool.map(blocked, range(20)))
            assert len(session.posts) == 3
        finally:
            finish.set()
        assert pending.result()
    assert call(llm)  # success clears recovery; no stuck probe
    assert not limits._states


@pytest.mark.parametrize("hint", ["315360000", "1e15", str(sys.float_info.max)])
@pytest.mark.parametrize("start", [0.0, 1e308])
def test_large_finite_hints_do_not_overflow_shorten_or_block_local(timing, hint, start):
    clock, limits = timing
    clock.now = start
    session = Session(limited(headers={"Retry-After": hint}))
    llm = client(session, limits)
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.status_code == 429
    # Huge synthetic monotonic origins also exercise deadline-addition overflow.
    if start == 0:
        clock.now = math.nextafter(float(hint), 0)
    with pytest.raises(LLMError, match="cooldown"):
        call(llm)
    assert len(session.posts) == 1
    assert call(client(Session(), limits, base_url="http://localhost:11434/v1"))
    assert call(client(Session(), limits, key="another-account"))
    if start == 0:
        clock.now = float(hint)
        assert call(llm)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("max_retries", [0, 2])
def test_embedded_5xx_has_bounded_http_retry_semantics(timing, status, max_retries):
    _, limits = timing
    responses = [Response(error={"code": status, "message": "upstream failure"})
                 for _ in range(max_retries + 1)]
    for response in responses:
        response.body["usage"] = {"prompt_tokens": 3, "completion_tokens": 4}
    session = Session(*responses)
    sleeps = []
    meter = UsageMeter()
    llm = LLMClient("synthetic", session=session, limits=limits, meter=meter,
                    max_retries=max_retries, sleep=sleeps.append)
    with pytest.raises(LLMError) as caught:
        call(llm)
    assert caught.value.status_code == status
    assert len(session.posts) == max_retries + 1
    assert len(sleeps) == max_retries
    assert meter.snapshot()["calls"] == max_retries + 1


def test_embedded_400_native_schema_has_one_compatibility_fallback(timing):
    _, limits = timing
    rejection = lambda: Response(error={"code": 400, "message": "response_format is not supported"})
    session = Session(rejection(), Response())
    llm = client(session, limits, max_retries=0)
    kwargs = dict(model="one:free", system="", prompt="synthetic", max_tokens=10,
                  schema={"type": "object", "properties": {"score": {"type": "integer"}},
                          "required": ["score"]}, structured="native")
    assert llm.complete_json(**kwargs) == {"score": 71}
    assert len(session.posts) == 2
    assert "response_format" in session.posts[0][1]["json"]
    assert "response_format" not in session.posts[1][1]["json"]
    session = Session(rejection(), rejection())
    with pytest.raises(LLMError) as caught:
        client(session, limits, max_retries=0).complete_json(**kwargs)
    assert caught.value.status_code == 400
    assert len(session.posts) == 2  # no recursive compatibility retry
