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
    [(82, 82), (82.4, 82), (82.6, 83), ("82", 82), ("82.5", 82),
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
    assert scored[0].status is ApplyStatus.DIGEST
    assert "scorer failed" in scored[0].status_detail


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
    assert all(s.status is ApplyStatus.DIGEST for s in scored)
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
                          structured="auto"):
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
