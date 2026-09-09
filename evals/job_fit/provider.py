"""Optional Promptfoo adapter: intentionally offline-only, one fixture per call."""
from __future__ import annotations

import json
from pathlib import Path
import sys

# Promptfoo loads file providers independently of the repository working directory.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.job_fit.harness import run_suite


def call_api(prompt, options, context):
    """The prompt is a case selector, never a duplicate of the scoring prompt."""
    config = options.get("config", {})
    if config.get("mode", "offline") != "offline":
        return {"error": "This provider is offline-only; use the bounded Python CLI for live runs."}
    case_id = context.get("vars", {}).get("case_id") or str(prompt).strip()
    try:
        report = run_suite(case_ids=[case_id], max_cases=1)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"output": json.dumps(report)}
