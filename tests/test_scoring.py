"""Tests for src/scoring.py — the LLM fit-scoring stage.

Two properties matter more than the arithmetic:

  * **A job is never silently lost.** If the model is down, or returns
    nonsense, or the key is missing, the job must still reach the digest with
    an explanation. Dropping it would hide a real match behind an outage, and
    the user would never know it happened.
  * **The cost ceiling is real.** `scoring.max_jobs` bounds spend, and a cap
    that truncates silently reads as "there was nothing else today".

The prompt itself is tested for the instructions that change behaviour —
strict JSON, the hard-requirement penalty, the no-inventing rule — not for
its prose.
"""

from __future__ import annotations

import json

import pytest

from src.models import ApplyStatus, Score
from src.scoring import (
    RESPONSE_KEYS,
    SYSTEM_PROMPT,
    build_prompt,
    parse_score,
    score_job,
    score_jobs,
)
from tests.conftest import (
    BASE_CV,
    FakeAnthropic,
    TransientAPIError,
    llm_client,
    make_job,
    write_config,
)


def payload(score=82, **extra):
    body = {"score": score, "verdict": "Strong fit",
            "reasons": ["8y Python vs 5+ asked"], "strengths": ["Python"],
            "gaps": ["No Kafka"]}
    body.update(extra)
    return json.dumps(body)


# ==========================================================================
# build_prompt
# ==========================================================================


def test_prompt_contains_the_cv_verbatim():
    """The CV is the only source of truth about the candidate; truncating it
    would silently change every score."""
    prompt = build_prompt(make_job(), BASE_CV, {})
    assert BASE_CV in prompt


def test_prompt_contains_the_job_facts():
    job = make_job(company="Northwind", title="Senior Backend Engineer",
                   location="Berlin, Germany", salary="€70k–€90k")
    prompt = build_prompt(job, BASE_CV, {})
    for fragment in ("Northwind", "Senior Backend Engineer", "Berlin, Germany",
                     "€70k–€90k", job.url):
        assert fragment in prompt


def test_prompt_says_when_the_posting_has_no_date():
    prompt = build_prompt(make_job(hours_old=None), BASE_CV, {})
    assert "unknown" in prompt.lower()


def test_prompt_truncates_a_huge_description_but_keeps_the_cv():
    job = make_job(description="x " * 20000)
    prompt = build_prompt(job, BASE_CV, {})
    assert "truncated" in prompt
    assert BASE_CV in prompt


def test_prompt_warns_when_only_a_snippet_is_available():
    """Adzuna descriptions are teasers. Without this the model confidently
    scores requirements it has not seen."""
    job = make_job(source="adzuna", raw={"snippet_only": True})
    assert "snippet" in build_prompt(job, BASE_CV, {}).lower()


def test_prompt_handles_a_missing_description():
    prompt = build_prompt(make_job(description=""), BASE_CV, {})
    assert "no description" in prompt.lower()


def test_prompt_demands_strict_json_with_the_exact_keys():
    prompt = build_prompt(make_job(), BASE_CV, {})
    assert "STRICT JSON" in prompt
    for key in RESPONSE_KEYS:
        assert f'"{key}"' in prompt or key in prompt


def test_prompt_carries_the_instructions_that_change_the_score():
    prompt = build_prompt(make_job(), BASE_CV, {})
    lowered = prompt.lower()
    assert "penalise hard" in lowered or "penalize hard" in lowered
    assert "work authorisation" in lowered or "work authorization" in lowered
    assert "never invent" in lowered
    assert "concrete evidence" in lowered


def test_title_alignment_outranks_the_rest_of_the_rubric():
    """The rubric is an ordered list and the order is the instruction.

    Title alignment used to be item 6 — a floor check tacked on after the
    technical criteria. It is the strongest thing observable about an
    application before a human reads the CV, so it leads. Asserting the
    ordering rather than mere presence is deliberate: putting the clause back
    at the bottom would keep every substring in this file green while undoing
    the whole point of it.
    """
    prompt = build_prompt(make_job(), BASE_CV, {}).lower()
    family = prompt.index("job family and title alignment")
    tech = prompt.index("must-have technologies")
    seniority = prompt.index("years of experience")
    assert family < tech < seniority


def test_title_alignment_is_judged_on_the_work_not_the_string():
    """Ranking on title is one instruction away from keyword matching.

    A candidate whose employer called them a "Backend Developer" is not a
    worse fit for a "Backend Engineer" role, and a rubric that ranks on the
    literal title would score them down for their previous company's word
    choice. The counter-example matters as much as the rule: "Data Engineer"
    and "Data Scientist" overlap by a word and are different jobs.
    """
    prompt = build_prompt(make_job(), BASE_CV, {}).lower()
    assert "judge the work, not the string" in prompt
    assert "backend developer" in prompt
    assert "data scientist" in prompt


def test_a_different_job_family_is_still_a_ceiling_not_a_deduction():
    """Promoting this clause must not have softened it.

    The old wording made a wrong-family posting a 5. If reordering had turned
    that into "weigh title heavily", a strong backend CV would start scoring
    60 on sales roles — which is exactly the noise the threshold exists to
    keep out.
    """
    prompt = build_prompt(make_job(), BASE_CV, {}).lower()
    assert "it is a 5" in prompt
    assert "ceiling, not a deduction" in prompt


def test_prompt_includes_the_applicant_header_when_given():
    prompt = build_prompt(make_job(), BASE_CV,
                          {"name": "Ada Lovelace", "location": "Berlin, Germany"})
    assert "Ada Lovelace" in prompt


def test_system_prompt_asks_for_calibration_not_enthusiasm():
    lowered = SYSTEM_PROMPT.lower()
    assert "recruiter" in lowered or "calibrated" in lowered
    assert "under-score" in lowered or "under score" in lowered or "strict" in lowered


# ==========================================================================
# parse_score
# ==========================================================================


def test_parse_score_happy_path():
    score = parse_score({"score": 82, "verdict": "good", "reasons": ["a"],
                         "strengths": ["b"], "gaps": ["c"]})
    assert score.value == 82
    assert score.ok is True
    assert score.reasons == ["a"]
    assert score.strengths == ["b"]
    assert score.gaps == ["c"]
    assert score.verdict == "good"


@pytest.mark.parametrize(
    "raw,expected",
    [(82, 82), (82.4, 82), (82.6, 83), ("82", 82), ("82.5", 83),
     ("82/100", 82), (" 82 ", 82), ("score: 82", 82)],
)
def test_parse_score_coerces_the_shapes_models_emit(raw, expected):
    assert parse_score({"score": raw}).value == expected


@pytest.mark.parametrize("raw,expected", [(150, 100), (-20, 0), (101, 100)])
def test_parse_score_clamps_to_the_range(raw, expected):
    assert parse_score({"score": raw}).value == expected


@pytest.mark.parametrize("raw", [None, "", "high", {}, [], "n/a"])
def test_a_missing_score_is_an_error_not_a_zero(raw):
    """A real zero means "terrible fit"; an error means "we do not know".
    They route differently — the second still reaches the digest."""
    score = parse_score({"score": raw})
    assert score.value == 0
    assert score.ok is False
    assert score.error


def test_parse_score_keeps_the_reasons_from_a_scoreless_payload():
    score = parse_score({"reasons": ["seniority mismatch"], "verdict": "no"})
    assert score.error
    assert score.reasons == ["seniority mismatch"]
    assert score.verdict == "no"


def test_parse_score_coerces_a_string_reason_to_a_list():
    assert parse_score({"score": 80, "reasons": "just the one"}).reasons == ["just the one"]


def test_parse_score_drops_non_string_list_entries():
    score = parse_score({"score": 80, "reasons": ["ok", None, 42, {"a": 1}, "fine"]})
    assert score.reasons == ["ok", "fine"] or all(isinstance(r, str) for r in score.reasons)
    assert all(isinstance(r, str) for r in score.reasons)


def test_parse_score_tolerates_missing_lists():
    score = parse_score({"score": 80})
    assert score.reasons == [] and score.strengths == [] and score.gaps == []


def test_parse_score_rejects_a_non_mapping():
    for value in ("a string", ["a", "list"], None, 42):
        assert parse_score(value).ok is False


# ==========================================================================
# score_job
# ==========================================================================


def test_score_job_returns_the_parsed_score(tmp_path):
    cfg = write_config(tmp_path)
    score = score_job(make_job(), BASE_CV, cfg, client=llm_client([payload(88)]))
    assert score.value == 88
    assert score.ok is True


def test_score_job_records_the_model_used(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"model": "claude-haiku-4-5-20251001"}})
    score = score_job(make_job(), BASE_CV, cfg, client=llm_client([payload()]))
    assert score.model == "claude-haiku-4-5-20251001"


def test_score_job_honours_the_configured_model_and_limits(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"model": "test-model", "max_tokens": 999,
                                              "temperature": 0.4}})
    fake = FakeAnthropic([payload()])
    from src.llm import LLMClient

    score_job(make_job(), BASE_CV, cfg, client=LLMClient("k", client=fake))
    assert fake.calls[0]["model"] == "test-model"
    assert fake.calls[0]["max_tokens"] == 999
    assert fake.calls[0]["temperature"] == 0.4


def test_score_job_never_raises_on_an_api_failure(tmp_path):
    cfg = write_config(tmp_path)
    client = llm_client([TransientAPIError()] * 5)
    score = score_job(make_job(), BASE_CV, cfg, client=client)
    assert score.ok is False
    assert score.error


def test_score_job_never_raises_on_unparseable_output(tmp_path):
    cfg = write_config(tmp_path)
    score = score_job(make_job(), BASE_CV, cfg, client=llm_client(["I decline."]))
    assert score.ok is False


def test_score_job_survives_a_completely_broken_client(tmp_path):
    class Broken:
        def complete_json(self, **kwargs):
            raise RuntimeError("not an LLMError at all")

    score = score_job(make_job(), BASE_CV, write_config(tmp_path), client=Broken())
    assert score.ok is False
    assert "not an LLMError" in score.error


# ==========================================================================
# score_jobs
# ==========================================================================


def test_score_jobs_scores_everything_and_sorts_by_score(tmp_path):
    cfg = write_config(tmp_path)
    jobs = [make_job(ats_job_id=str(i)) for i in range(3)]
    client = llm_client([payload(50), payload(90), payload(70)])
    scored = score_jobs(jobs, BASE_CV, cfg, client=client)
    assert [s.score_value for s in scored] == [90, 70, 50]


def test_score_jobs_classifies_against_the_threshold(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"threshold": 75, "concurrency": 1}})
    jobs = [make_job(ats_job_id="a"), make_job(ats_job_id="b")]
    scored = score_jobs(jobs, BASE_CV, cfg, client=llm_client([payload(80), payload(60)]))
    by_status = {s.status for s in scored}
    assert ApplyStatus.DIGEST in by_status
    assert ApplyStatus.SCORED_BELOW in by_status
    below = next(s for s in scored if s.status is ApplyStatus.SCORED_BELOW)
    assert "below threshold 75" in below.status_detail


def test_a_score_exactly_at_the_threshold_is_a_match(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"threshold": 75}})
    scored = score_jobs([make_job()], BASE_CV, cfg, client=llm_client([payload(75)]))
    assert scored[0].status is ApplyStatus.DIGEST


def test_a_failed_score_still_reaches_the_digest(tmp_path):
    """The property that keeps an API outage from hiding a real match: an
    unscored job is a job the human has to judge, not a job to drop."""
    cfg = write_config(tmp_path)
    scored = score_jobs([make_job()], BASE_CV, cfg, client=llm_client(["nonsense"]))
    assert len(scored) == 1
    assert scored[0].status is ApplyStatus.SCORING_PENDING
    assert "evaluation pending" in scored[0].status_detail


def test_max_jobs_caps_spend_and_says_so(tmp_path, caplog):
    """Silent truncation reads as "nothing else was posted today"."""
    import logging

    cfg = write_config(tmp_path, {"scoring": {"max_jobs": 2, "concurrency": 1}})
    jobs = [make_job(ats_job_id=str(i)) for i in range(10)]
    fake = FakeAnthropic([payload()])
    from src.llm import LLMClient

    with caplog.at_level(logging.WARNING):
        scored = score_jobs(jobs, BASE_CV, cfg, client=LLMClient("k", client=fake))
    assert len(scored) == 2
    assert len(fake.calls) == 2
    assert "max_jobs" in caplog.text
    assert "8 not scored" in caplog.text


def test_a_missing_api_key_costs_one_error_not_one_per_job(tmp_path):
    cfg = write_config(tmp_path, {"keys": {"openrouter": ""}})
    jobs = [make_job(ats_job_id=str(i)) for i in range(5)]
    errors: list[str] = []
    scored = score_jobs(jobs, BASE_CV, cfg, client=None, errors=errors)
    assert len(scored) == 5
    assert all(s.status is ApplyStatus.SCORING_PENDING for s in scored)
    assert len(errors) == 1


def test_score_jobs_records_per_job_failures_in_errors(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"concurrency": 1}})
    errors: list[str] = []
    score_jobs([make_job()], BASE_CV, write_config(tmp_path), client=llm_client(["junk"]),
               errors=errors)
    assert len(errors) == 1


def test_score_jobs_on_an_empty_batch(tmp_path):
    assert score_jobs([], BASE_CV, write_config(tmp_path), client=llm_client()) == []


def test_concurrency_preserves_a_deterministic_result(tmp_path):
    """Threads must not reshuffle the digest between two runs over the same
    input — a digest that reorders for no reason is a digest you stop reading."""
    cfg = write_config(tmp_path, {"scoring": {"concurrency": 4}})
    jobs = [make_job(company=f"C{i}", ats_job_id=str(i)) for i in range(8)]
    scores = [payload(50 + i) for i in range(8)]
    first = score_jobs(jobs, BASE_CV, cfg, client=llm_client(list(scores)))
    second = score_jobs(jobs, BASE_CV, cfg, client=llm_client(list(scores)))
    assert [s.key for s in first] == [s.key for s in second]


def test_concurrency_of_one_takes_the_plain_loop(tmp_path):
    cfg = write_config(tmp_path, {"scoring": {"concurrency": 1}})
    jobs = [make_job(ats_job_id=str(i)) for i in range(3)]
    assert len(score_jobs(jobs, BASE_CV, cfg, client=llm_client([payload()]))) == 3


# ==========================================================================
# Phase 5 — per-candidate rules: positioning, signals, hard caps
# ==========================================================================

from src.scoring import _candidate_rules  # noqa: E402


_RULES_CFG = {"scoring": {
    "candidate_context": "Total experience: ~1.5 years. Gap: no NLP research.",
    "positive_signals": ["forecasting", "causal inference"],
    "score_caps": [
        {"when": "requires deep NLP research", "cap": 60},
    ],
}}


def test_candidate_rules_render_all_three_blocks():
    rules = _candidate_rules(_RULES_CFG)
    assert "1.5 years" in rules
    assert "POSITIVE SIGNALS" in rules and "- forecasting" in rules
    assert "MUST NOT exceed 60" in rules
    assert "requires deep NLP research" in rules


def test_empty_config_renders_no_rules():
    assert _candidate_rules({"scoring": {}}) == ""
    assert _candidate_rules(None) == ""


def test_a_bare_string_signal_is_one_signal_not_zero():
    """`positive_signals: forecasting` (a YAML scalar, not a list) must mean
    one signal — the same bare-string coercion `_string_list` applies —
    rather than being dropped without a word."""
    cfg = {"scoring": {"positive_signals": "time-series forecasting"}}
    rules = _candidate_rules(cfg)
    assert "- time-series forecasting" in rules


def test_malformed_caps_are_skipped_not_fatal():
    cfg = {"scoring": {"score_caps": [
        "not-a-mapping",
        {"when": "", "cap": 60},          # no condition
        {"when": "x", "cap": 300},        # cap out of range
        {"when": "requires magic", "cap": 55},
    ]}}
    rules = _candidate_rules(cfg)
    assert "MUST NOT exceed 55" in rules
    assert "300" not in rules


def test_the_rules_reach_the_model(tmp_path):
    """The whole point: what the config states must arrive in the prompt the
    model actually sees — threshold and max_jobs stay untouched."""
    from src.models import Job

    captured = {}

    class Client:
        def complete_json(self, *, model, system, require_keys, forbid_verbatim,
                          prompt, max_tokens, temperature, schema=None,
                          structured="auto", accept_schema=None):
            captured["prompt"] = prompt
            return {"score": 70, "reasons": ["ok"], "strengths": [],
                    "gaps": [], "verdict": "fine"}

    from src.scoring import score_job
    cfg = dict(_RULES_CFG)
    job = Job(source="greenhouse", company="Acme", title="Data Scientist",
              url="https://x.example/1", description="Forecasting role.")
    score = score_job(job, "# CV\nSome experience.", cfg, client=Client())
    assert score.value == 70
    assert "MUST NOT exceed 60" in captured["prompt"]
    assert "1.5 years" in captured["prompt"]


def test_the_shipped_config_carries_the_samba_tv_cap():
    """The lesson that motivated Phase 5, pinned to the shipped file."""
    import yaml as _yaml
    from pathlib import Path as _P

    shipped = _yaml.safe_load(
        (_P(__file__).resolve().parent.parent / "config.yaml").read_text())
    caps = shipped["scoring"]["score_caps"]
    assert any(int(c["cap"]) == 60 and "fine-tuning" in c["when"] for c in caps)
    assert shipped["scoring"]["threshold"] == 65   # untouched by design
    assert shipped["scoring"]["max_jobs"] == 40    # untouched by design


# ==========================================================================
# Every score shape parse_score reads survives validation
# ==========================================================================


def _reply(score=82, **extra):
    body = {"score": score, "verdict": "Fit", "reasons": ["8y Python vs 5+ asked"],
            "strengths": [], "gaps": []}
    body.update(extra)
    return json.dumps(body)


def _structured(mode, scoring=None):
    return {"scoring": dict(scoring or {}), "llm": {"structured_output": mode}}


@pytest.mark.parametrize("mode", ["native", "prompt"])
@pytest.mark.parametrize("raw,expected", [(72.0, 72), ("72", 72), ("82/100", 82), (64.5, 65),
                                          ("85%", 85), (0, 0), (5, 5)])
def test_score_job_reads_every_shape_parse_score_documents(mode, raw, expected):
    """These shapes used to be validated against "integer" before
    `parse_score` saw them, and came back as "response.score has wrong type"
    — a burned fallback attempt, or a job stuck in SCORING_PENDING."""
    score = score_job(make_job(), BASE_CV, _structured(mode),
                      client=llm_client([_reply(raw)]))
    assert score.ok is True, score.error
    assert score.value == expected


@pytest.mark.parametrize("mode", ["native", "prompt"])
def test_score_job_turns_a_score_with_no_number_into_an_error(mode):
    score = score_job(make_job(), BASE_CV, _structured(mode),
                      client=llm_client([_reply("excellent")]))
    assert score.ok is False
    assert score.value == 0
    assert "response.score" in score.error


@pytest.mark.parametrize("mode", ["native", "prompt"])
@pytest.mark.parametrize("raw", ["8/10", "4/5", "N/A (0 of 3 requirements)", 0.85, 8.5,
                                 "-20", "850", "0.85", 0.5])
def test_a_score_on_another_scale_is_an_error_not_a_low_score(mode, raw):
    """Each of these was an error before the accept schema loosened, and then
    a valid score: "8/10" became 8, 0.85 became 1, "850" became 100. A score
    the model wrote on some other scale is unparseable, not low — the job
    stays pending, and a fallback model gets to answer."""
    score = score_job(make_job(), BASE_CV, _structured(mode),
                      client=llm_client([_reply(raw)]))
    assert score.ok is False
    assert score.value == 0


def test_a_score_on_another_scale_lets_the_fallback_answer():
    from src.llm import chain_from_config

    cfg = {"scoring": {"model": "first", "fallback_models": ["second"]},
           "llm": {"structured_output": "prompt"}}
    chain = chain_from_config(cfg, "scoring", clients=[llm_client([_reply("8/10")]),
                                                       llm_client([_reply(72)])])
    score = score_job(make_job(), BASE_CV, cfg, client=chain)
    assert score.ok is True and score.value == 72
    assert score.model == "second"


def test_a_half_point_reply_at_the_threshold_is_a_match():
    """64.5 rounds half-up to 65 — not to 64, under the default threshold."""
    cfg = _structured("prompt", {"threshold": 65})
    scored = score_jobs([make_job()], BASE_CV, cfg, client=llm_client([_reply(64.5)]))
    assert scored[0].score_value == 65
    assert scored[0].status is ApplyStatus.DIGEST


@pytest.mark.parametrize("raw,expected", [(64.5, 65), (12.5, 13), (99.5, 100),
                                          (64.49, 64), ("64,5", 65), (72.5, 73)])
def test_parse_score_rounds_half_up(raw, expected):
    assert parse_score({"score": raw}).value == expected


def test_native_structured_output_still_asks_the_provider_for_an_integer():
    from src.llm import LLMClient
    from src.scoring import RESPONSE_SCHEMA

    fake = FakeAnthropic([_reply(72.0)])
    score = score_job(make_job(), BASE_CV, _structured("native"),
                      client=LLMClient("k", provider="anthropic", client=fake))
    sent = fake.calls[0]["output_config"]["format"]["schema"]
    assert sent == RESPONSE_SCHEMA
    assert sent["properties"]["score"] == {"type": "integer", "minimum": 0, "maximum": 100}
    assert score.ok is True and score.value == 72


# ==========================================================================
# scoring.enforce_caps — caps held in code, off by default
# ==========================================================================

from src.scoring import RESPONSE_SCHEMA, _apply_caps, _score_caps  # noqa: E402

_CAPS = [{"when": "requires German", "cap": 60, "id": "german"},
         {"when": "requires a PhD", "cap": 40}]

#: Today's rendering, written out: with the flag off, ids must not leak into
#: the prompt and nothing else may move.
_CAPS_RULES_TODAY = (
    "HARD CAPS — non-negotiable ceilings:\n"
    "- If the posting requires German: the score MUST NOT exceed 60. "
    "Say which cap fired in `reasons`.\n"
    "- If the posting requires a PhD: the score MUST NOT exceed 40. "
    "Say which cap fired in `reasons`."
)

#: Today's schema, written out, for the same reason.
_SCHEMA_TODAY = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "verdict", "reasons", "strengths", "gaps"],
    "additionalProperties": False,
}


def _enforcing(on=True, caps=_CAPS):
    return _structured("native", {"score_caps": caps, "enforce_caps": on})


def _capped(score, cfg=None, **extra):
    return score_job(make_job(), BASE_CV, cfg or _enforcing(),
                     client=llm_client([_reply(score, **extra)]))


@pytest.mark.parametrize("flag", [{}, {"enforce_caps": False}])
def test_flag_off_leaves_prompt_and_schema_byte_identical(flag):
    cfg = _structured("native", {"score_caps": _CAPS, **flag})
    assert _candidate_rules(cfg) == _CAPS_RULES_TODAY
    fake = FakeAnthropic([_reply(70)])
    from src.llm import LLMClient

    job = make_job()
    score_job(job, BASE_CV, cfg, client=LLMClient("k", provider="anthropic", client=fake))
    request = fake.calls[0]
    assert request["output_config"]["format"]["schema"] == _SCHEMA_TODAY == RESPONSE_SCHEMA
    assert request["messages"][0]["content"] == build_prompt(
        job, BASE_CV, {}, rules=_CAPS_RULES_TODAY)
    assert "caps_fired" not in request["messages"][0]["content"]


def test_flag_on_names_each_cap_and_offers_caps_fired():
    rules = _candidate_rules(_enforcing())
    assert "- [german] If the posting requires German" in rules
    assert "- [cap-2] If the posting requires a PhD" in rules  # default id
    assert "`caps_fired`" in rules

    fake = FakeAnthropic([_reply(70)])
    from src.llm import LLMClient

    score_job(make_job(), BASE_CV, _enforcing(),
              client=LLMClient("k", provider="anthropic", client=fake))
    sent = fake.calls[0]["output_config"]["format"]["schema"]
    # Strict structured output (OpenAI's `strict: true`) rejects an optional
    # property and an array without `items`; "none fired" is null or [].
    assert sent["properties"]["caps_fired"] == {"type": ["array", "null"],
                                                "items": {"type": "string"}}
    assert sent["required"] == [*RESPONSE_KEYS, "caps_fired"]
    assert sent["properties"]["score"]["type"] == "integer"
    assert RESPONSE_SCHEMA == _SCHEMA_TODAY      # the flag-off schema is untouched


def test_flag_on_output_section_offers_caps_fired_too():
    """The caps block calls `caps_fired` a sixth key; the OUTPUT section
    must not then demand "exactly these five keys"."""
    from src.llm import LLMClient

    fake = FakeAnthropic([_reply(70)])
    score_job(make_job(), BASE_CV, _enforcing(),
              client=LLMClient("k", provider="anthropic", client=fake))
    output = fake.calls[0]["messages"][0]["content"].split("\nOUTPUT\n")[-1]
    assert "exactly these five keys" not in output
    assert "`caps_fired`" in output
    # Without caps to declare, nothing mentions it.
    plain = build_prompt(make_job(), BASE_CV, {}, rules=_candidate_rules(_enforcing(caps=[])))
    assert "caps_fired" not in plain


def test_a_declared_cap_holds_the_score_to_it():
    score = _capped(75, caps_fired=["german"])
    assert score.ok is True
    assert (score.value, score.raw_value, score.caps_applied) == (60, 75, ["german"])
    assert "cap german enforced: 75→60" in score.reasons


def test_two_declared_caps_hold_the_score_to_the_smaller():
    score = _capped(75, caps_fired=["german", "cap-2"])
    assert (score.value, score.raw_value, score.caps_applied) == (40, 75, ["cap-2"])


def test_a_cap_only_ever_lowers_a_score():
    score = _capped(50, caps_fired=["german"])
    assert (score.value, score.raw_value, score.caps_applied) == (50, None, [])
    assert not any("enforced" in reason for reason in score.reasons)


def test_an_unknown_cap_id_is_noted_not_fatal():
    score = _capped(75, caps_fired=["no-such-cap"])
    assert score.ok is True and score.value == 75
    assert any("unknown cap id" in r and "no-such-cap" in r for r in score.reasons)


def test_a_bare_string_caps_fired_is_one_cap():
    assert _capped(75, caps_fired="german").value == 60


@pytest.mark.parametrize("extra", [{}, {"caps_fired": None}, {"caps_fired": []},
                                   {"caps_fired": False}, {"caps_fired": ""}])
def test_an_omitted_or_empty_caps_fired_validates_and_enforces_nothing(extra):
    score = _capped(75, **extra)
    assert score.ok is True
    assert (score.value, score.caps_applied) == (75, [])


def test_a_true_caps_fired_names_no_cap_and_is_noted():
    score = _capped(75, caps_fired=True)
    assert score.ok is True
    assert (score.value, score.caps_applied) == (75, [])
    assert any("caps_fired was true" in reason for reason in score.reasons)


def test_an_error_score_is_never_capped():
    errored = Score(value=0, error="no numeric 'score'", reasons=["r"])
    assert _apply_caps(errored, ["german"], _score_caps(_enforcing())) == \
        Score(value=0, error="no numeric 'score'", reasons=["r"])
    via_job = _capped("excellent", caps_fired=["german"])
    assert via_job.ok is False and via_job.value == 0 and via_job.caps_applied == []


def test_stray_keys_are_still_rejected_with_caps_on():
    assert _capped(75, caps_fired=["german"], confidence=0.4).ok is False
    # ... and `caps_fired` itself is a stray key while the flag is off.
    assert _capped(75, _enforcing(on=False), caps_fired=["german"]).ok is False


def test_a_capped_score_is_classified_on_the_capped_value():
    cfg = _enforcing()
    cfg["scoring"]["threshold"] = 65
    scored = score_jobs([make_job()], BASE_CV, cfg,
                        client=llm_client([_reply(75, caps_fired=["german"])]))
    assert scored[0].status is ApplyStatus.SCORED_BELOW
    assert scored[0].score_value == 60
