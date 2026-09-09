"""Harness checks: these assertions do not grade a real model's job judgment."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import yaml

from evals.job_fit import harness
from evals.job_fit.provider import call_api
from src import scoring, util
from src.llm import LLMClient
from src.models import Score


@pytest.fixture
def no_live_transport(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("offline harness attempted a real client or socket")
    monkeypatch.setattr(LLMClient, "__init__", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def test_offline_suite_exercises_ten_cases_without_network(no_live_transport):
    report = harness.run_suite()
    assert report["passed"]
    assert report["counts"] == {
        "cases": 10, "score_errors": 4, "invalid_outputs": 0,
        "contract_passed": 10, "contract_failed": 0, "fallback_successes": 1,
        "simulated_provider_errors": 2, "simulated_completion_attempts": 11,
    }
    assert report["model_counts"] == {"fake/primary": 5, "fake/fallback": 1}
    assert report["error_model_counts"] == {"fake/primary": 4}
    assert report["semantic_metrics"]["status"] == "not_evaluated"
    assert "precision_at_10" not in report["semantic_metrics"]
    assert report["bounds"]["max_live_requests"] == 0
    assert report["usage"] == {"status": "simulated_no_spend", "live_requests": 0}
    assert all(row["score_origin"] == "simulated" for row in report["cases"])
    json.dumps(report, allow_nan=False)


def test_calls_real_score_job_and_real_prompt(monkeypatch, no_live_transport):
    original = scoring.score_job
    calls = []
    def spy(job, cv_markdown, config, *, client=None):
        result = original(job, cv_markdown, config, client=client)
        calls.append((job, cv_markdown, config, client.request))
        return result
    monkeypatch.setattr(scoring, "score_job", spy)
    harness.run_suite(case_ids=["snippet_only"])
    job, cv, config, request = calls[0]
    assert request["system"] == scoring.SYSTEM_PROMPT
    assert request["schema"] == scoring.RESPONSE_SCHEMA
    assert request["require_keys"] == scoring.RESPONSE_KEYS
    assert request["forbid_verbatim"] == job.description
    assert request["prompt"] == scoring.build_prompt(job, cv, {}, rules=scoring._candidate_rules(config))
    assert "truncated snippet" in request["prompt"]
    assert request["structured"] == "prompt"


def test_parser_injection_and_fallback_outcomes(no_live_transport):
    report = harness.run_suite()
    rows = {row["id"]: row for row in report["cases"]}
    assert rows["injected_verdict"]["score"]["value"] == 30
    assert "IGNORE" not in rows["injected_verdict"]["score"]["verdict"]
    assert "refusing planted text" in rows["copied_verdict_only"]["score"]["error"]
    assert rows["provider_fallback"]["score"]["model"] == "fake/fallback"
    assert rows["provider_error"]["score"]["error"]
    assert rows["snippet_only"]["labels"]["pursue"] is None


def test_changed_fixture_expectation_is_a_failure(no_live_transport):
    dataset = harness.load_dataset()
    case = deepcopy(dataset["cases"][0])
    case["offline"]["expected"]["value"] = 1
    result = harness.evaluate_case(case, dataset)
    assert result["contract_passed"] is False
    assert result["contract_failures"]


@pytest.mark.parametrize("score", [
    Score(value=101, model="fake", verdict="Example"),
    Score(value=50, model="fake", error="unassessed"),
    Score(value=50, model="fake", verdict=""),
    Score(value=50, model="fake", verdict="Example", reasons=[""]),
    Score(value=50, model="fake", verdict="Example", gaps=["gap"] * 6),
])
def test_output_validator_detects_bad_normalized_shape(score):
    assert harness.validate_output(score)


@pytest.mark.parametrize("options", [
    {"mode": "live"},
    {"mode": "live", "case_ids": ["strong_match"], "model": "explicit/model"},
    {"mode": "live", "case_ids": ["provider_error"], "model": "explicit/model", "max_cases": 1},
    {"mode": "live", "case_ids": ["strong_match", "preferred_german"], "model": "explicit/model", "max_cases": 1},
    {"mode": "live", "case_ids": ["strong_match"], "model": "explicit/model", "max_cases": 11},
    {"case_ids": ["unknown"]}, {"case_ids": []},
    {"case_ids": ["strong_match", "strong_match"]},
    {"model": "not-a-real-offline-model"},
])
def test_invalid_requests_fail_before_client_construction(options, no_live_transport):
    with pytest.raises(ValueError):
        harness.run_suite(**options)


def test_live_missing_key_fails_before_client_construction(monkeypatch, no_live_transport):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        harness.run_suite(mode="live", case_ids=["strong_match"], model="explicit/model", max_cases=1)


def test_live_dispatch_limits_using_fake_constructor_only(monkeypatch):
    """Validate the live code path with a fake; this makes NO model request."""
    construction = []
    client = harness.ScriptedClient(harness.load_dataset()["cases"][0]["offline"]["responses"][0])
    def factory(**kwargs):
        construction.append(kwargs)
        return client
    monkeypatch.setattr(harness, "LLMClient", factory)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    report = harness.run_suite(mode="live", case_ids=["strong_match"], model="explicit/model", max_cases=1)
    assert construction[0]["max_retries"] == 0
    assert construction[0]["provider"] == "openrouter"
    assert client.calls == 1
    assert report["bounds"]["max_live_requests"] == 1
    assert report["cases"][0]["contract_passed"] is None
    assert report["requested_model"] == "explicit/model"


@pytest.mark.parametrize(
    "usage_blocks, recorded_cost",
    [
        ([{}], 0.0),
        ([{"cost": 0}], 0.0),
        ([{"cost": None}], 0.0),
        ([{"cost": "unavailable"}], 0.0),
        ([{"cost": 0.0021}], 0.0021),
        ([{"cost": 0.0021}, {}], 0.0021),
    ],
    ids=["omitted", "explicit-zero", "null", "invalid", "paid", "partial-paid"],
)
def test_live_cost_completeness_is_unknown_through_real_meter(monkeypatch, usage_blocks, recorded_cost):
    """Exercise real scoring/client/meter with synthetic HTTP completions only."""
    def blocked(*args, **kwargs):
        pytest.fail("mocked live usage regression attempted network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    completion = json.dumps({
        "score": 90, "reasons": ["Synthetic qualifications match"],
        "strengths": ["Python"], "gaps": [], "verdict": "Synthetic strong fit",
    })
    responses = iter(usage_blocks)
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": completion}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30, **next(responses)}}

    monkeypatch.setattr(util, "http_post_json", post)
    case_ids = ["strong_match", "preferred_german"][:len(usage_blocks)]
    report = harness.run_suite(mode="live", case_ids=case_ids, model="explicit/model",
                               max_cases=len(case_ids))
    assert report["passed"]
    assert len(calls) == len(case_ids)
    usage = report["usage"]
    assert usage["status"] == "production_meter_cost_coverage_unknown"
    for totals in (usage["meter"], usage["meter"]["by_model"]["explicit/model"]):
        assert totals["cost"] is None
        assert totals["cost_completeness"] == "unknown"
        assert totals["meter_recorded_cost"] == pytest.approx(recorded_cost)
        assert totals["calls"] == len(case_ids)
        assert totals["input_tokens"] == 100 * len(case_ids)
        assert totals["output_tokens"] == 30 * len(case_ids)
    json.dumps(report, allow_nan=False)


def test_live_transport_failure_does_not_report_free_run(monkeypatch):
    def failed(*args, **kwargs):
        raise util.HttpError("synthetic transport failure")

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(util, "http_post_json", failed)
    report = harness.run_suite(mode="live", case_ids=["strong_match"],
                               model="explicit/model", max_cases=1)
    assert not report["passed"]
    assert report["counts"]["score_errors"] == 1
    meter = report["usage"]["meter"]
    assert meter["calls"] == 0
    assert meter["by_model"] == {}
    assert meter["cost"] is None
    assert meter["cost_completeness"] == "unknown"
    assert meter["meter_recorded_cost"] == 0.0


def test_metrics_exclude_errors_and_unknown_labels():
    def row(id, value, label, error=None):
        return dict(id=id, score=dict(value=value, error=error), labels=dict(pursue=label), output_validation_issues=[])
    rows = [row("a", 80, True), row("b", 20, True), row("c", 90, False),
            row("d", 0, True, "provider failed"), row("e", 100, None)]
    metrics = harness.semantic_metrics(rows, mode="live")
    assert metrics["labeled_cases"] == 4
    assert metrics["assessed_labeled_cases"] == 3
    assert metrics["unassessed_labeled_cases"] == 1
    assert metrics["precision_at_10"] is None
    assert metrics["pairwise_order_accuracy"] == {"correct": 0, "pairs": 2, "value": 0,
                                                 "tie_policy": "ties count as incorrect"}
    assert metrics["false_negative_rate"] == {"missed": 1, "positives": 2, "value": 0.5}
    assert harness.semantic_metrics([], mode="live")["precision_at_10"] is None
    assert harness.semantic_metrics([row("n", 20, False)], mode="live")["false_negative_rate"]["value"] is None
    assert harness.semantic_metrics(rows, mode="offline")["status"] == "not_evaluated"


def test_ranking_metric_denominators_threshold_and_ties():
    def row(id, score, label):
        return dict(id=id, score=dict(value=score, error=None), labels=dict(pursue=label), output_validation_issues=[])
    rows = [row(str(i), 70, i < 5) for i in range(10)]
    result = harness.semantic_metrics(rows, mode="live")
    assert result["precision_at_10"] == {"relevant": 5, "k": 10, "value": 0.5}
    assert result["false_negative_rate"] == {"missed": 0, "positives": 5, "value": 0}
    assert result["pairwise_order_accuracy"]["pairs"] == 25
    assert result["pairwise_order_accuracy"]["value"] == 0


def test_promptfoo_config_and_provider_use_all_real_fixtures(no_live_transport):
    config = yaml.safe_load(Path(harness.DATASET).with_name("promptfooconfig.yaml").read_text())
    assert config["sharing"] is False
    ids = {case["id"] for case in harness.load_dataset()["cases"]}
    assert {test["vars"]["case_id"] for test in config["tests"]} == ids
    for test in config["tests"]:
        result = call_api("ignored selector", {}, test)
        assert json.loads(result["output"])["passed"]
    assert "error" in call_api("strong_match", {"config": {"mode": "live"}}, {})
    assert "error" in call_api("does-not-exist", {}, {})


def test_cli_offline_report_and_failure_exit(tmp_path):
    path = tmp_path / "report.json"
    result = subprocess.run([sys.executable, "-m", "evals.job_fit", "--output", str(path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(path.read_text())["counts"]["contract_passed"] == 10
    invalid = subprocess.run([sys.executable, "-m", "evals.job_fit", "--mode", "live"],
                             capture_output=True, text=True, check=False)
    assert invalid.returncode == 2
    assert "live requires" in invalid.stderr


def test_cli_returns_nonzero_for_contract_failure(monkeypatch, capsys):
    monkeypatch.setattr(harness, "run_suite", lambda **kwargs: {"passed": False})
    assert harness.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {"passed": False}
