"""Exercise score_job; simulated completions are contract tests, never quality data."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import time
from typing import Any

from src import llm, scoring
from src.llm import LLMClient, LLMError, ModelChain, UsageMeter
from src.models import Job, Score

DATASET = Path(__file__).with_name("cases.json")
MAX_CASES = 10
PRIMARY = "fake/primary"
FALLBACK = "fake/fallback"


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_dataset() -> dict[str, Any]:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    if len(cases) != MAX_CASES or len(set(ids)) != len(ids):
        raise ValueError("dataset must contain exactly ten uniquely named synthetic cases")
    for case in cases:
        if case["labels"]["pursue"] not in (True, False, None):
            raise ValueError("pursue must be true, false, or null")
    return data


class ScriptedClient(LLMClient):
    """Replace only text generation; inherit the real complete_json parser/guards.

    No superclass constructor, SDK, HTTP session, key lookup, or transport exists.
    """

    def __init__(self, script: dict[str, Any]) -> None:
        self.provider = "openrouter"
        self.script = script
        self.calls = 0
        self.provider_errors = 0

    def complete(self, **kwargs: Any) -> str:
        self.calls += 1
        if "error" in self.script:
            self.provider_errors += 1
            raise LLMError(self.script["error"])
        return self.script["reply"]


class RecordingClient:
    """Capture the exact real-function request without rebuilding prompt literals."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.request: dict[str, Any] = {}

    @property
    def last_model(self) -> str | None:
        return getattr(self.delegate, "last_model", None)

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.request = kwargs
        return self.delegate.complete_json(**kwargs)


def validate_output(score: Score) -> list[str]:
    """Validate normalized Score shape, not truthfulness of the model's reasoning."""
    issues = []
    if type(score.value) is not int or not 0 <= score.value <= 100:
        issues.append("score must be an integer in [0, 100]")
    for field in ("reasons", "strengths", "gaps"):
        values = getattr(score, field)
        if not isinstance(values, list) or len(values) > 5 or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            issues.append(f"{field} must contain at most five nonempty strings")
    if not isinstance(score.model, str) or not score.model:
        issues.append("model attribution is missing")
    if score.error is not None:
        if not isinstance(score.error, str) or not score.error:
            issues.append("error must be a nonempty string or null")
        if score.value != 0:
            issues.append("an unassessed error must retain the zero sentinel")
    elif not isinstance(score.verdict, str) or not score.verdict.strip():
        issues.append("a successful score needs a verdict")
    return issues


def check_contract(score: Score, case: dict[str, Any]) -> list[str]:
    """Fixture assertions describe only the injected response's expected handling."""
    expected = case["offline"]["expected"]
    issues = []
    if bool(score.error) != expected["error"]:
        issues.append("unexpected success/error state")
    if score.value != expected["value"]:
        issues.append(f"expected normalized value {expected['value']}, got {score.value}")
    if score.model != expected.get("model", PRIMARY):
        issues.append("incorrect producing-model attribution")
    if expected.get("error_contains", "") not in (score.error or ""):
        issues.append("error does not identify the expected failure")
    for fragment in expected.get("gaps_contain", []):
        if fragment.lower() not in " ".join(score.gaps).lower():
            issues.append(f"fixture gap was lost: {fragment}")
    return issues


def evaluate_case(
    case: dict[str, Any], dataset: dict[str, Any], *, mode: str = "offline",
    model: str | None = None, live_client: Any = None,
) -> dict[str, Any]:
    if mode not in ("offline", "live"):
        raise ValueError("unknown evaluation mode")
    scripts = []
    if mode == "offline":
        scripts = [ScriptedClient(script) for script in case["offline"]["responses"]]
        entries = [(lambda target=target: target, PRIMARY if i == 0 else FALLBACK)
                   for i, target in enumerate(scripts)]
        delegate = ModelChain(entries)
        requested_model = PRIMARY
    else:
        if not case["live_eligible"] or not model or live_client is None:
            raise ValueError("live requires an eligible case, explicit model and client")
        delegate = live_client
        requested_model = model
    client = RecordingClient(delegate)
    config = {
        "applicant": dataset["applicant"],
        "scoring": {**dataset["candidate_rules"], "model": requested_model,
                    "max_tokens": 700, "temperature": 0, "fallback_models": []},
        # Prompt path prevents the native-format compatibility retry in live mode.
        "llm": {"structured_output": "prompt"},
    }
    job = Job(source="synthetic_eval", company="Fictional Example Lab",
              title=case["title"], url=f"https://example.invalid/jobs/{case['id']}",
              location="Remote, EU", country="PL", remote=True,
              description=case["description"], raw={"snippet_only": case["snippet_only"]})
    started = time.perf_counter()
    score = scoring.score_job(job, dataset["cv_markdown"], config, client=client)
    elapsed = time.perf_counter() - started
    validation = validate_output(score)
    contract = check_contract(score, case) if mode == "offline" else None
    return {
        "id": case["id"], "mode": mode, "score_origin": "simulated" if mode == "offline" else "model",
        "requested_model": requested_model, "score": asdict(score),
        "labels": case["labels"], "latency_seconds": round(elapsed, 6),
        "output_validation_issues": validation, "contract_failures": contract,
        "contract_passed": not validation and not contract if mode == "offline" else None,
        "simulated_completion_attempts": sum(target.calls for target in scripts),
        "simulated_provider_errors": sum(target.provider_errors for target in scripts),
        "fallback_used": score.error is None and score.model != requested_model,
        "fingerprints": {"request_sha256": fingerprint(client.request),
                         "cv_sha256": fingerprint(dataset["cv_markdown"]),
                         "job_sha256": fingerprint({**job.to_dict(), "raw": job.raw}),
                         "rules_sha256": fingerprint(dataset["candidate_rules"])},
    }


def semantic_metrics(rows: list[dict[str, Any]], *, mode: str, threshold: int = 70) -> dict[str, Any]:
    if mode != "live":
        return {"status": "not_evaluated", "reason": "simulated scores are not ranking-quality evidence"}
    labeled = [row for row in rows if row["labels"]["pursue"] is not None]
    assessed = [row for row in labeled if row["score"]["error"] is None
                and not row["output_validation_issues"]]
    result: dict[str, Any] = {
        "status": "synthetic_smoke_only", "label_source": "author-defined synthetic scenario labels",
        "labeled_cases": len(labeled), "assessed_labeled_cases": len(assessed),
        "unassessed_labeled_cases": len(labeled) - len(assessed), "threshold": threshold,
        "precision_at_10": None, "false_negative_rate": None, "pairwise_order_accuracy": None,
        "precision_at_10_note": "requires at least ten assessed, pursue-labeled cases",
    }
    if not assessed:
        return result
    ranked = sorted(assessed, key=lambda row: (-row["score"]["value"], row["id"]))
    positives = [row for row in assessed if row["labels"]["pursue"]]
    negatives = [row for row in assessed if not row["labels"]["pursue"]]
    missed = sum(row["score"]["value"] < threshold for row in positives)
    pairs = len(positives) * len(negatives)
    ordered = sum(positive["score"]["value"] > negative["score"]["value"]
                  for positive in positives for negative in negatives)
    if len(ranked) >= 10:
        relevant = sum(row["labels"]["pursue"] for row in ranked[:10])
        result["precision_at_10"] = {"relevant": relevant, "k": 10, "value": relevant / 10}
    result.update({"pairwise_order_accuracy": {"correct": ordered, "pairs": pairs,
                                               "value": ordered / pairs if pairs else None,
                                               "tie_policy": "ties count as incorrect"},
                   "false_negative_rate": {"missed": missed, "positives": len(positives),
                                           "value": missed / len(positives) if positives else None}})
    return result


def live_usage_report(meter: UsageMeter) -> dict[str, Any]:
    """Preserve meter diagnostics without presenting incomplete costs as totals."""
    snapshot = meter.snapshot()
    # The transport normalizes absent/invalid charges to zero before add().
    # Neither a zero nor a positive aggregate establishes reporting coverage.
    for totals in [snapshot, *snapshot["by_model"].values()]:
        totals["meter_recorded_cost"] = totals["cost"]
        totals["cost"] = None
        totals["cost_completeness"] = "unknown"
    return {
        "status": "production_meter_cost_coverage_unknown",
        "cost_limitations": (
            "The production meter cannot distinguish missing or invalid cost from explicit zero; "
            "positive amounts may cover only some responses. Meter calls and tokens include only "
            "recorded responses, so failed requests may be absent. Meter-recorded amounts are "
            "diagnostics, not complete spend or a billing guarantee."
        ),
        "meter": snapshot,
    }


def run_suite(*, mode: str = "offline", case_ids: list[str] | None = None,
              model: str | None = None, max_cases: int | None = None) -> dict[str, Any]:
    dataset = load_dataset()
    if mode not in ("offline", "live"):
        raise ValueError("mode must be offline or live")
    if case_ids is not None and (not case_ids or len(set(case_ids)) != len(case_ids)):
        raise ValueError("case selection must be nonempty and unique")
    by_id = {case["id"]: case for case in dataset["cases"]}
    if case_ids and set(case_ids) - by_id.keys():
        raise ValueError("unknown case id(s): " + ", ".join(sorted(set(case_ids) - by_id.keys())))
    selected = [by_id[key] for key in case_ids] if case_ids else dataset["cases"]
    if max_cases is not None and not 1 <= max_cases <= MAX_CASES:
        raise ValueError("max-cases must be between 1 and 10")
    if max_cases is not None and len(selected) > max_cases:
        raise ValueError("selected cases exceed max-cases; select fewer cases explicitly")
    client = None
    meter = UsageMeter()
    if mode == "live":
        if not case_ids or not model or not model.strip() or max_cases is None:
            raise ValueError("live requires --case, --model, and --max-cases")
        if any(not case["live_eligible"] for case in selected):
            raise ValueError("transport/parser-only fixtures cannot be selected for live evaluation")
        # Deliberately never load config.yaml, .env, a CV file, or configured fallback models.
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise ValueError("live requires OPENROUTER_API_KEY in the environment")
        client = LLMClient(api_key=key, provider="openrouter", max_retries=0, meter=meter)
    elif model is not None:
        raise ValueError("--model applies only to live mode; offline always reports fake models")
    rows = [evaluate_case(case, dataset, mode=mode, model=model, live_client=client) for case in selected]
    errors = sum(row["score"]["error"] is not None for row in rows)
    contract_failed = sum(row["contract_passed"] is False for row in rows)
    invalid = sum(bool(row["output_validation_issues"]) for row in rows)
    return {
        "report_version": 1, "dataset_version": dataset["version"], "mode": mode,
        "purpose": "deterministic_harness_validation" if mode == "offline" else "synthetic_semantic_smoke",
        "python_version": platform.python_version(), "requested_model": PRIMARY if mode == "offline" else model,
        "model_counts": dict(Counter(row["score"]["model"] for row in rows if row["score"]["error"] is None)),
        "error_model_counts": dict(Counter(row["score"]["model"] for row in rows if row["score"]["error"] is not None)),
        "counts": {"cases": len(rows), "score_errors": errors, "invalid_outputs": invalid,
                   "contract_passed": sum(row["contract_passed"] is True for row in rows),
                   "contract_failed": contract_failed, "fallback_successes": sum(row["fallback_used"] for row in rows),
                   "simulated_provider_errors": sum(row["simulated_provider_errors"] for row in rows),
                   "simulated_completion_attempts": sum(row["simulated_completion_attempts"] for row in rows)},
        "bounds": {"max_cases": max_cases or MAX_CASES, "max_live_requests": len(rows) if mode == "live" else 0,
                   "max_output_tokens_per_request": 700, "live_retries": 0, "live_fallbacks": 0},
        "usage": {"status": "simulated_no_spend", "live_requests": 0} if mode == "offline" else
                 live_usage_report(meter),
        "semantic_metrics": semantic_metrics(rows, mode=mode),
        "fingerprints": {"dataset_sha256": fingerprint(dataset),
                         "scoring_source_sha256": fingerprint(inspect.getsource(scoring)),
                         "llm_source_sha256": fingerprint(inspect.getsource(llm)),
                         "harness_source_sha256": fingerprint(Path(__file__).read_text(encoding="utf-8"))},
        "passed": contract_failed == 0 if mode == "offline" else errors == 0 and invalid == 0,
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--case", action="append", dest="case_ids", help="repeat to select explicit case IDs")
    parser.add_argument("--model", help="explicit OpenRouter model ID; live only")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--output", type=Path, help="JSON report path; defaults to stdout")
    args = parser.parse_args(argv)
    try:
        report = run_suite(mode=args.mode, case_ids=args.case_ids, model=args.model, max_cases=args.max_cases)
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["passed"] else 1
