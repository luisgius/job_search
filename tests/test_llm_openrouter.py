"""Tests for the OpenRouter provider in src/llm.py.

`scoring` and `tailor` must never learn which provider they are talking to,
so the property under test throughout is **behavioural equivalence**: the same
`complete()` / `complete_json()` calls, the same `LLMError` on failure, the
same retry policy. If those diverge, switching provider silently changes what
the pipeline does.

The seam here is `session=` — the same `FakeSession` the job sources use —
because OpenRouter is reached over plain HTTP rather than through an SDK.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Config
from src.llm import (
    OPENROUTER_BASE_URL,
    PROVIDER_KEYS,
    PROVIDERS,
    LLMClient,
    LLMError,
    api_key_for,
    client_from_config,
    provider_of,
)
from tests.conftest import FakeResponse, FakeSession, write_config


def completion(text: str = '{"score": 88}', **extra) -> dict:
    body = {
        "id": "gen-123",
        "model": "anthropic/claude-sonnet-5",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 90},
    }
    body.update(extra)
    return body


class PostSession(FakeSession):
    """`FakeSession` plus the `post` the OpenRouter transport needs."""

    def __init__(self, responses=None, **kwargs):
        super().__init__(**kwargs)
        if responses is None:
            responses = [FakeResponse(status_code=200, _json=completion())]
        if not isinstance(responses, list):
            responses = [responses]
        self._responses = responses
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.posts.append({"url": url, "json": json, "headers": dict(headers or {}),
                           "timeout": timeout})
        item = self._responses[min(len(self.posts) - 1, len(self._responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def client(responses=None, **kwargs):
    session = PostSession(responses)
    llm = LLMClient("or-key", provider="openrouter", session=session,
                    sleep=lambda _s: None, **kwargs)
    return llm, session


# ==========================================================================
# request shape
# ==========================================================================


def test_it_posts_to_the_chat_completions_endpoint():
    llm, session = client()
    llm.complete(model="anthropic/claude-sonnet-5", system="s", prompt="p",
                 max_tokens=100)
    assert session.posts[0]["url"] == f"{OPENROUTER_BASE_URL}/chat/completions"


def test_the_system_prompt_becomes_a_system_role_message():
    """Anthropic passes `system` separately; the chat-completions dialect
    expresses the same thing as the first message. Dropping it would quietly
    remove the calibration instructions from every score."""
    llm, session = client()
    llm.complete(model="m", system="be strict", prompt="score this", max_tokens=100)
    messages = session.posts[0]["json"]["messages"]
    assert messages[0] == {"role": "system", "content": "be strict"}
    assert messages[1] == {"role": "user", "content": "score this"}


def test_an_empty_system_prompt_sends_no_system_message():
    llm, session = client()
    llm.complete(model="m", system="", prompt="p", max_tokens=100)
    assert [m["role"] for m in session.posts[0]["json"]["messages"]] == ["user"]


def test_the_model_and_limits_are_forwarded_verbatim():
    llm, session = client()
    llm.complete(model="openai/gpt-5", system="s", prompt="p", max_tokens=1234,
                 temperature=0.4)
    body = session.posts[0]["json"]
    assert body["model"] == "openai/gpt-5"
    assert body["max_tokens"] == 1234
    assert body["temperature"] == 0.4


def test_the_key_is_sent_as_a_bearer_token():
    llm, session = client()
    llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert session.posts[0]["headers"]["Authorization"] == "Bearer or-key"


def test_the_model_name_is_never_rewritten():
    """Auto-prefixing a bare name would silently send the work to a model the
    user did not choose — and bill them for it."""
    llm, session = client()
    llm.complete(model="claude-sonnet-5", system="s", prompt="p", max_tokens=10)
    assert session.posts[0]["json"]["model"] == "claude-sonnet-5"


def test_a_custom_base_url_is_honoured():
    """The same transport serves any OpenAI-compatible gateway — LiteLLM, a
    local vLLM, a corporate proxy."""
    session = PostSession()
    llm = LLMClient("k", provider="openrouter", session=session,
                    base_url="http://localhost:4000/v1", sleep=lambda _s: None)
    llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert session.posts[0]["url"] == "http://localhost:4000/v1/chat/completions"


def test_a_completion_gets_a_generous_timeout():
    """A real completion legitimately takes a minute; the 20s default used for
    job boards would abort every call."""
    llm, session = client()
    llm.complete(model="m", system="s", prompt="p", max_tokens=4000)
    assert session.posts[0]["timeout"] >= 60


# ==========================================================================
# response shapes
# ==========================================================================


def test_a_plain_string_content_is_returned():
    llm, _ = client([FakeResponse(_json=completion("hello world"))])
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10) == "hello world"


def test_content_returned_as_a_list_of_parts_is_joined():
    """Some models on OpenRouter answer with OpenAI's multi-part content."""
    payload = completion()
    payload["choices"][0]["message"]["content"] = [
        {"type": "text", "text": "part one "},
        {"type": "text", "text": "part two"},
    ]
    llm, _ = client([FakeResponse(_json=payload)])
    assert llm.complete(model="m", system="s", prompt="p",
                        max_tokens=10) == "part one part two"


def test_a_reasoning_only_answer_is_not_lost():
    payload = completion()
    payload["choices"][0]["message"] = {"role": "assistant", "content": None,
                                        "reasoning": "the answer is 88"}
    llm, _ = client([FakeResponse(_json=payload)])
    assert "88" in llm.complete(model="m", system="s", prompt="p", max_tokens=10)


def test_an_error_object_returned_with_http_200_is_not_read_as_an_empty_answer():
    """OpenRouter returns upstream failures inside a 200. Treating that as an
    empty completion would score the job 0 with no error recorded — the worst
    possible outcome, because the digest would show it as a genuine reject."""
    payload = {"error": {"code": 502, "message": "upstream provider is down"}}
    llm, _ = client([FakeResponse(status_code=200, _json=payload)])
    with pytest.raises(LLMError, match="upstream provider is down"):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)


@pytest.mark.parametrize(
    "payload",
    [{}, {"choices": []}, {"choices": [{}]}, {"choices": "nope"}, []],
)
def test_a_malformed_body_raises_rather_than_returning_nothing(payload):
    llm, _ = client([FakeResponse(status_code=200, _json=payload)])
    with pytest.raises(LLMError):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)


def test_complete_json_works_end_to_end():
    llm, _ = client([FakeResponse(_json=completion('```json\n{"score": 77}\n```'))])
    assert llm.complete_json(model="m", system="s", prompt="p",
                             max_tokens=10) == {"score": 77}


# ==========================================================================
# failures and retries — must match the Anthropic path
# ==========================================================================


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 520, 529])
def test_transient_http_failures_are_retried(status):
    """The status only exists in the error text for this transport, so this is
    also the regression test for parsing it out of the message."""
    responses = [FakeResponse(status_code=status, text="upstream busy"),
                 FakeResponse(_json=completion("recovered"))]
    llm, session = client(responses)
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10) == "recovered"
    assert len(session.posts) == 2


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_permanent_http_failures_are_not_retried(status):
    """402 matters here: OpenRouter returns it when you are out of credit, and
    retrying that three times per job across forty jobs is pointless."""
    llm, session = client([FakeResponse(status_code=status, text="nope")])
    with pytest.raises(LLMError):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert len(session.posts) == 1


def test_the_error_body_is_surfaced_because_the_status_alone_misleads():
    llm, _ = client([FakeResponse(status_code=400,
                                  text='{"error":{"message":"model not found"}}')])
    with pytest.raises(LLMError, match="model not found"):
        llm.complete(model="bogus/model", system="s", prompt="p", max_tokens=10)


def test_a_transport_failure_is_retried_then_surfaced():
    llm, session = client([ConnectionError("reset by peer")])
    with pytest.raises(LLMError):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert len(session.posts) == 3      # 1 attempt + 2 retries, as on Anthropic


def test_every_failure_is_an_llm_error():
    """Callers catch LLMError and degrade; anything else escapes score_jobs."""
    for outcome in (ConnectionError("x"), FakeResponse(status_code=500),
                    FakeResponse(status_code=200, text="not json")):
        llm, _ = client([outcome])
        with pytest.raises(LLMError):
            llm.complete(model="m", system="s", prompt="p", max_tokens=10)


# ==========================================================================
# construction and config
# ==========================================================================


def test_an_unknown_provider_is_rejected_at_construction():
    with pytest.raises(LLMError, match="unknown llm.provider"):
        LLMClient("k", provider="ollama")


def test_a_missing_key_names_the_right_provider_and_env_var():
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        LLMClient("", provider="openrouter")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        LLMClient("", provider="anthropic")


def test_the_session_seam_removes_the_need_for_a_key():
    assert LLMClient("", provider="openrouter", session=PostSession()) is not None


def test_provider_names_are_stable():
    assert PROVIDERS == ("anthropic", "openrouter")
    assert set(PROVIDER_KEYS) == set(PROVIDERS)


def test_client_from_config_builds_the_configured_provider(tmp_path: Path):
    cfg = write_config(tmp_path, {"llm": {"provider": "openrouter"},
                                  "keys": {"openrouter": "or-key"}})
    built = client_from_config(cfg)
    assert built.provider == "openrouter"
    assert built.api_key == "or-key"
    assert built.base_url == OPENROUTER_BASE_URL


def test_client_from_config_defaults_to_anthropic(config):
    assert client_from_config(config).provider == "anthropic"


def test_the_key_lookup_follows_the_provider(tmp_path: Path):
    """Reading keys.anthropic while the provider is openrouter is exactly how
    a switched provider silently keeps using the old credential."""
    cfg = write_config(tmp_path, {"llm": {"provider": "openrouter"},
                                  "keys": {"anthropic": "sk-ant", "openrouter": "or"}})
    assert api_key_for(cfg) == "or"
    assert cfg.llm_key == "or"


def test_provider_of_rejects_nonsense(tmp_path: Path):
    cfg = write_config(tmp_path, {"llm": {"provider": "gemini"}})
    with pytest.raises(LLMError):
        provider_of(cfg)


def test_openrouter_key_comes_from_the_environment(tmp_path: Path):
    import yaml as _yaml

    (tmp_path / "config.yaml").write_text(
        _yaml.safe_dump({"llm": {"provider": "openrouter"},
                         "keys": {"openrouter": "from-file"}}), encoding="utf-8")
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / "w.yaml", root=tmp_path,
                      env={"OPENROUTER_API_KEY": "from-env"})
    assert cfg.llm_key == "from-env"


# ==========================================================================
# validation
# ==========================================================================


def test_validate_asks_for_the_right_key(tmp_path: Path):
    cfg = write_config(tmp_path, {"llm": {"provider": "openrouter"},
                                  "keys": {"anthropic": "sk-ant", "openrouter": ""},
                                  "scoring": {"model": "anthropic/claude-sonnet-5"},
                                  "tailoring": {"model": "anthropic/claude-sonnet-5"}})
    problems = cfg.validate()
    assert any("openrouter" in p and "OPENROUTER_API_KEY" in p for p in problems)


def test_validate_catches_a_bare_model_id_on_openrouter(tmp_path: Path):
    """`claude-sonnet-5` is a 404 from OpenRouter, and without this the run
    discovers that once per job, after paying for the fetch."""
    cfg = write_config(tmp_path, {"llm": {"provider": "openrouter"},
                                  "keys": {"openrouter": "or-key"},
                                  "scoring": {"model": "claude-sonnet-5"}})
    problems = cfg.validate()
    assert any("vendor-qualified" in p for p in problems)
    assert any("anthropic/claude-sonnet-5" in p for p in problems)


def test_a_vendor_qualified_model_validates_cleanly(tmp_path: Path):
    cfg = write_config(tmp_path, {"llm": {"provider": "openrouter"},
                                  "keys": {"openrouter": "or-key"},
                                  "scoring": {"model": "anthropic/claude-sonnet-5"},
                                  "tailoring": {"model": "openai/gpt-5"}})
    assert cfg.validate() == []


def test_validate_rejects_an_unknown_provider(tmp_path: Path):
    cfg = write_config(tmp_path, {"llm": {"provider": "gemini"}})
    assert any("llm.provider" in p for p in cfg.validate())


def test_the_anthropic_path_is_unaffected_by_the_new_checks(config):
    """The default configuration must keep validating exactly as before."""
    assert config.validate() == []


# ==========================================================================
# the pipeline stays provider-agnostic
# ==========================================================================


def test_scoring_runs_through_openrouter(tmp_path: Path):
    from src.scoring import score_jobs
    from tests.conftest import BASE_CV, make_job

    cfg = write_config(tmp_path, {
        "llm": {"provider": "openrouter"}, "keys": {"openrouter": "or-key"},
        "scoring": {"model": "anthropic/claude-sonnet-5", "concurrency": 1},
    })
    session = PostSession([FakeResponse(_json=completion(json.dumps(
        {"score": 91, "verdict": "fits", "reasons": [], "strengths": [], "gaps": []})))])
    llm = LLMClient("or-key", provider="openrouter", session=session,
                    sleep=lambda _s: None)

    scored = score_jobs([make_job()], BASE_CV, cfg, client=llm)
    assert scored[0].score_value == 91
    assert session.posts, "the OpenRouter transport was never used"


def test_tailoring_runs_through_openrouter(tmp_path: Path):
    from src.tailor import tailor_job
    from tests.conftest import BASE_CV, make_scored

    cfg = write_config(tmp_path, {
        "llm": {"provider": "openrouter"}, "keys": {"openrouter": "or-key"},
        "tailoring": {"model": "openai/gpt-5"},
        "output": {"dir": str(tmp_path / "output")},
    })
    session = PostSession([FakeResponse(_json=completion("# Ada Lovelace\n\nSummary."))])
    llm = LLMClient("or-key", provider="openrouter", session=session,
                    sleep=lambda _s: None)

    scored = tailor_job(make_scored(), BASE_CV, cfg, client=llm)
    assert scored.artifacts.cv_md
    assert len(session.posts) == 2      # one CV call, one cover-letter call
