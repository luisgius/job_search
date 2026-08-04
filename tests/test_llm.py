"""Tests for src/llm.py — the Anthropic wrapper and JSON recovery.

`extract_json` gets most of the attention because it is the load-bearing
brittleness of the whole LLM half of the pipeline: models wrap objects in
fences, pad them with prose, and occasionally leave a trailing comma. A
parser that gives up on any of those turns a good score into a scoring
failure, and the job silently loses its ranking.

The retry policy is the other thing worth pinning: retrying a 401 wastes
time and money on a call that will never succeed, while *not* retrying a 529
throws away a job for no reason.
"""

from __future__ import annotations

import json

import pytest

from src.llm import (
    RETRYABLE_STATUS,
    LLMClient,
    LLMError,
    extract_json,
)
from tests.conftest import (
    FakeAnthropic,
    FakeMessage,
    FakeTextBlock,
    FatalAPIError,
    TransientAPIError,
)


def client(responses=None, **kwargs):
    fake = FakeAnthropic(responses, **kwargs)
    return LLMClient("test-key", client=fake, sleep=lambda _s: None), fake


# ==========================================================================
# extract_json
# ==========================================================================


def test_plain_json():
    assert extract_json('{"score": 82}') == {"score": 82}


def test_json_fenced_block():
    text = 'Here you go:\n```json\n{"score": 82, "verdict": "good"}\n```\nHope that helps!'
    assert extract_json(text)["score"] == 82


def test_bare_fenced_block():
    assert extract_json('```\n{"score": 82}\n```') == {"score": 82}


def test_prose_before_and_after():
    text = 'Based on the CV, my assessment is {"score": 71} — let me know.'
    assert extract_json(text) == {"score": 71}


def test_nested_objects_and_arrays():
    payload = {"score": 82, "reasons": ["a", "b"],
               "detail": {"seniority": {"match": True, "note": "ok"}}}
    assert extract_json(f"prefix {json.dumps(payload)} suffix") == payload


def test_braces_inside_string_literals_do_not_confuse_the_scanner():
    """A naive `text[text.find('{'):text.rfind('}')+1]` slice gets this wrong
    the moment a reason quotes code or an f-string."""
    payload = {"score": 60, "verdict": 'they want f"{user}" templating'}
    assert extract_json(f"noise {json.dumps(payload)} noise") == payload


def test_escaped_quotes_inside_strings():
    raw = r'{"verdict": "they said \"no sponsorship\" explicitly", "score": 10}'
    assert extract_json(raw)["score"] == 10


def test_trailing_commas_are_repaired():
    """Models emit these often enough that failing here would be a real loss."""
    assert extract_json('{"score": 82, "reasons": ["a", "b",],}')["score"] == 82


def test_the_first_valid_object_wins_over_later_noise():
    text = '{"score": 82} and then some rubbish {not json'
    assert extract_json(text) == {"score": 82}


def test_an_object_after_a_broken_one_is_still_found():
    text = 'oops {"score": broken} but really {"score": 82}'
    assert extract_json(text)["score"] == 82


def test_unicode_survives():
    assert extract_json('{"verdict": "München — sehr gut", "score": 90}')["score"] == 90


@pytest.mark.parametrize(
    "text",
    ["", "   ", "no json here at all", "{", "}", "{unclosed: ", "[1, 2, 3]",
     "null", '"just a string"', None],
)
def test_unrecoverable_input_raises_llm_error(text):
    with pytest.raises(LLMError):
        extract_json(text)


def test_an_object_wrapped_in_an_array_is_unwrapped():
    """The prompts ask for a bare object, but models sometimes wrap it in a
    list anyway. Recovering the first object is the whole point of this
    function — rejecting it would turn a perfectly good score into a scoring
    failure."""
    assert extract_json('[{"score": 82}]') == {"score": 82}


def test_a_list_of_scalars_has_nothing_to_recover():
    with pytest.raises(LLMError):
        extract_json("[1, 2, 3]")


def test_extract_json_does_not_hang_on_pathological_input():
    # Unbalanced braces at scale: the scanner must bail, not backtrack forever.
    with pytest.raises(LLMError):
        extract_json("{" * 5000)


# ==========================================================================
# LLMClient construction
# ==========================================================================


def test_missing_key_fails_at_construction_not_three_stages_later():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        LLMClient("")


def test_an_injected_client_needs_no_key():
    assert LLMClient("", client=FakeAnthropic()) is not None


def test_the_injected_client_is_used_verbatim():
    fake = FakeAnthropic(['{"score": 1}'])
    llm = LLMClient("", client=fake)
    assert llm.client is fake


# ==========================================================================
# complete
# ==========================================================================


def test_complete_returns_the_text():
    llm, _ = client(["hello world"])
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10) == "hello world"


def test_complete_forwards_every_parameter():
    llm, fake = client(["ok"])
    llm.complete(model="claude-sonnet-5", system="be strict", prompt="score this",
                 max_tokens=1234, temperature=0.3)
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["system"] == "be strict"
    assert call["max_tokens"] == 1234
    assert call["temperature"] == 0.3
    assert call["messages"] == [{"role": "user", "content": "score this"}]


def test_complete_joins_multiple_text_blocks():
    message = FakeMessage(content=[FakeTextBlock(text="part one "),
                                   FakeTextBlock(text="part two")])
    llm, _ = client([message])
    assert llm.complete(model="m", system="s", prompt="p",
                        max_tokens=10) == "part one part two"


def test_complete_ignores_non_text_blocks():
    """Thinking and tool_use blocks must not end up concatenated into the
    JSON payload."""
    message = FakeMessage(content=[
        {"type": "thinking", "thinking": "hmm"},
        FakeTextBlock(text='{"score": 82}'),
    ])
    llm, _ = client([message])
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10) == '{"score": 82}'


def test_complete_handles_dict_shaped_blocks():
    """The SDK returns objects; some proxies and recorded fixtures return
    plain dicts. Both have to work."""
    llm, _ = client(["dict shaped"], dict_blocks=True)
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10) == "dict shaped"


def test_empty_content_is_returned_rather_than_raising():
    llm, _ = client([FakeMessage(content=[])])
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10) == ""


# ==========================================================================
# retries
# ==========================================================================


def test_transient_errors_are_retried_then_succeed():
    llm, fake = client([TransientAPIError(), '{"score": 82}'])
    assert llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert len(fake.calls) == 2


def test_retries_are_bounded():
    llm, fake = client([TransientAPIError()], )
    llm.max_retries = 2
    with pytest.raises(LLMError):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert len(fake.calls) == 3       # 1 attempt + 2 retries


def test_auth_errors_are_not_retried():
    """A 401 will not get better, and retrying it three times per job across
    forty jobs is 120 pointless round trips."""
    llm, fake = client([FatalAPIError()])
    with pytest.raises(LLMError, match="invalid api key"):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert len(fake.calls) == 1


def test_retry_backoff_grows_and_never_precedes_the_first_attempt():
    waits: list[float] = []
    llm = LLMClient("k", client=FakeAnthropic([TransientAPIError()]),
                    max_retries=3, sleep=waits.append)
    with pytest.raises(LLMError):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert len(waits) == 3
    assert waits == sorted(waits)
    assert waits[0] > 0


def test_retryable_statuses_cover_the_ones_that_matter():
    for status in (429, 500, 502, 503, 529):
        assert status in RETRYABLE_STATUS
    for status in (400, 401, 403, 404, 422):
        assert status not in RETRYABLE_STATUS


def test_every_failure_surfaces_as_llm_error():
    """Callers catch LLMError and degrade; a raw SDK exception would escape
    score_jobs and take down the run."""
    llm, _ = client([ValueError("something weird")])
    with pytest.raises(LLMError):
        llm.complete(model="m", system="s", prompt="p", max_tokens=10)


# ==========================================================================
# complete_json
# ==========================================================================


def test_complete_json_parses_a_fenced_response():
    llm, _ = client(['```json\n{"score": 77}\n```'])
    assert llm.complete_json(model="m", system="s", prompt="p",
                             max_tokens=10) == {"score": 77}


def test_complete_json_appends_the_schema_hint_to_the_prompt():
    llm, fake = client(['{"score": 1}'])
    llm.complete_json(model="m", system="s", prompt="score this", max_tokens=10,
                      schema_hint='{"score": 0}')
    sent = fake.calls[0]["messages"][0]["content"]
    assert "score this" in sent
    assert '{"score": 0}' in sent


def test_complete_json_without_a_hint_sends_the_prompt_unchanged():
    llm, fake = client(['{"score": 1}'])
    llm.complete_json(model="m", system="s", prompt="score this", max_tokens=10)
    assert fake.calls[0]["messages"][0]["content"] == "score this"


def test_complete_json_raises_on_unparseable_output():
    llm, _ = client(["I'd rather not answer that."])
    with pytest.raises(LLMError):
        llm.complete_json(model="m", system="s", prompt="p", max_tokens=10)
