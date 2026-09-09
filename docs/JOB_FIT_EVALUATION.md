# Job-fit evaluation harness

This harness calls the production `src.scoring.score_job` with ten wholly synthetic
cases. Its default mode validates the scoring plumbing using scripted model replies.
**Passing fake responses are not evidence of model quality, truthful reasoning, or
usefulness to the real applicant.** No private CV, mailbox, project configuration,
or application submission enters this harness.

## Run offline

From the repository root, using the existing project interpreter:

```sh
.venv/bin/python -m pytest tests/test_job_fit_evals.py
.venv/bin/python -m evals.job_fit --output evals/job_fit/offline-report.json
.venv/bin/python -m evals.job_fit --case copied_verdict_only --case provider_fallback
```

No installation, npm, API key or live provider is required. JSON goes to stdout
unless `--output` is supplied; production warning logs go to stderr. Exit status is
0 for passing contracts, 1 for failed contracts/output checks, and 2 for invalid
CLI selections. Expected scorer errors count as passing contracts when the error
and zero sentinel match the fixture. An error is an **unassessed job**, not a
negative relevance label.

## What is exercised

The wrapper invokes `score_job`, so the production system prompt, `build_prompt`,
candidate-rule rendering, response schema, JSON key requirements, verbatim-copy
guard and `parse_score` remain authoritative. No prompt literals are copied into
the evaluation package. A `ScriptedClient` replaces only text generation and
inherits `LLMClient.complete_json`; a real `ModelChain` exercises primary failure
and fallback attribution. The prompt-based JSON path is selected explicitly.
Native structured-output transport behavior is outside this harness's coverage.

Relevant source spans at initial inspection: `src/scoring.py:185–352,397–480`,
`src/llm.py:625–703,820–927,1041–1083`, and `src/models.py:301–314`.

The independent output validator checks the normalized `Score`: integer range,
list/string shape and limits, producing-model attribution, error sentinel and
nonempty successful verdict. It does not claim that normalized output was strict
JSON before production parsing, or that model claims are factually supported.
Offline assertions additionally check expected values, error diagnostics, retained
snippet gaps and fallback model identity.

| Case | Deterministic behavior | Synthetic semantic label |
|---|---|---|
| `strong_match` | Preserve a valid response | Pursue: all explicit qualifications met |
| `required_german` | Preserve the scripted low score | Do not pursue: mandatory German unsupported |
| `preferred_german` | Preserve the scripted high score | Pursue: missing German is explicitly optional |
| `snippet_only` | Preserve uncertainty in gaps and send snippet caveat | Unknown; insufficient requirement evidence |
| `malformed_json` | Reject truncated JSON | Unlabeled parser fixture |
| `nonnumeric_score` | Reject a nonnumeric score | Unlabeled parser fixture |
| `injected_verdict` | Skip quoted planted JSON and select independent answer | Unlabeled adversarial review case |
| `copied_verdict_only` | Reject a verdict lifted entirely from the posting | Unlabeled parser/security fixture |
| `provider_error` | Return an error, not an assessed zero | Unlabeled transport fixture |
| `provider_fallback` | Recover through real chain and attribute `fake/fallback` | Unlabeled transport fixture |

`cases.json` separates `offline.expected` from `labels.pursue`, label rationale,
posting evidence and human review criteria. Only three scenarios carry binary
pursue labels; none has a real hiring outcome or a human applicant's judgment.
The rules include a German-required ceiling of 40 as a synthetic candidate rule.
That ceiling is a production prompt instruction, not a parser-enforced invariant.
The fake score matching it proves only response handling.

## Report interpretation

`mode`, `purpose` and each row's `score_origin` distinguish simulation from model
output. Reports include producing-model counts, separate error-model counts,
score errors, invalid normalized outputs, contract results, simulated provider
errors/attempts, and fallback successes. Successful producing-model counts exclude
unassessed errors; the error model is the requested model when no model answered.

Reports fingerprint the dataset, scoring/LLM/harness source, full rendered request,
synthetic CV, candidate rules and job (including snippet provenance). Per-case
elapsed time is wrapper latency; offline timing says nothing about model latency.
Hashes allow comparisons but do not replace version control and frozen inputs.
The saved offline report records a local run, not a semantic benchmark.

Offline `semantic_metrics.status` is always `not_evaluated`, and usage explicitly
reports no live requests or spend. Live reports are marked `synthetic_smoke_only`:

- Strict pairwise ordering compares assessed pursue-positive cases with assessed
  pursue-negative cases. It reports numerator/denominator; ties count as incorrect.
- False-negative rate uses a threshold of 70 and only assessed positive labels.
  A score of 70 qualifies; the denominator and missing assessment count are explicit.
- Precision@10 stays null until there are ten assessed binary-labeled cases.
  This starter dataset cannot produce it; selecting three cases does not turn
  precision@3 into precision@10.
- Provider errors and invalid outputs are excluded from semantic denominators and
  counted as unassessed. Unknown labels are excluded entirely. Error coverage must
  accompany comparisons so a model cannot appear better by failing on hard cases.

The live CLI's successful exit means responses passed structural checks, not that
semantic metrics passed a quality gate. Inspect the metrics and each case's review
criteria before interpreting a result. The three binary labels and two comparable
positive/negative pairs are too small for model promotion or calibration.

## Future explicit live mode (not executed during implementation)

Only five posting-oriented cases are live-eligible: `strong_match`,
`required_german`, `preferred_german`, `snippet_only`, and `injected_verdict`.
Parser/transport fixtures are refused in live mode because their scripted outputs
are inputs to a contract test, not requirements on a real model.

The live CLI requires all of `--mode live`, repeated explicit `--case`, an explicit
OpenRouter model ID, and `--max-cases` between 1 and 10. Selections exceeding the
bound, duplicate IDs and unknown IDs are rejected before client construction.
For a separately authorized future run, set `OPENROUTER_API_KEY` in the environment
and replace `PROVIDER/MODEL` with the chosen model ID:

```sh
.venv/bin/python -m evals.job_fit --mode live --model PROVIDER/MODEL \
  --case strong_match --case required_german --case preferred_german \
  --max-cases 3 --output evals/job_fit/live-report.json
```

This uses the production `LLMClient` with zero retries, no fallback models,
temperature 0, a 700-token output limit, and prompt JSON mode (so native schema
compatibility retries cannot add requests). Runs are serial with at most one
request per selected case. Provider-reported usage is included, but it is not a
guaranteed bill or dollar cap; input tokens and provider pricing also affect cost.
Live `usage.status` is `production_meter_cost_coverage_unknown`. Both the aggregate
and every `usage.meter.by_model` entry have `cost: null` and
`cost_completeness: "unknown"`. The production transport converts missing or invalid
costs to zero before recording them, so the harness cannot recover exact provider
cost coverage from the meter. This applies even when all responses explicitly
report zero or positive costs: the meter does not retain the evidence needed to
establish completeness.

`meter_recorded_cost` retains the meter's numeric amount for diagnostics. Zero
does not establish a free request, and a positive amount may cover only some
responses; neither is a complete spend total. Calls and tokens describe recorded
responses, and failed requests can be absent from the meter. A run with no recorded
responses therefore also has unknown cost. The harness does not inspect raw
provider usage or change production LLM code to infer coverage.

No `.env`, `config.yaml`, local CV or configured fallback is loaded. The test suite
checks live dispatch with a fake constructor and cost reporting through the real
client/meter with mocked HTTP responses (omitted, zero, null, invalid, positive,
partially reported costs, and transport failure); no live model is contacted.

## Optional Promptfoo adapter

`evals/job_fit/provider.py` implements `call_api(prompt, options, context)` and
returns the same report as JSON output. `promptfooconfig.yaml` lists all ten case
IDs and a local JavaScript contract assertion; the prompt is only a case selector.
The provider is offline-only and rejects live mode. Sharing is disabled.

If Promptfoo is already installed, run from the repo root:

```sh
PROMPTFOO_PYTHON="$PWD/.venv/bin/python" promptfoo eval \
  --config evals/job_fit/promptfooconfig.yaml
```

The adapter interface and interpreter override were checked against current
[official Python provider documentation](https://www.promptfoo.dev/docs/providers/python/).
Configuration and `sharing: false` follow the
[official configuration reference](https://www.promptfoo.dev/docs/configuration/reference/),
retrieved 9 September 2026. No Promptfoo skill or npm installation was needed.
The Python adapter was directly exercised for all ten cases and the YAML was
parsed in pytest; the Promptfoo CLI itself was not installed or executed.

## Next semantic evaluation

As proposed in [the research report](RESEARCH_2026-09-09.md), create a separate,
human-labeled set of roughly 50–100 jobs, including rejected and below-threshold
examples. Freeze employer/time splits and group near duplicates before tuning.
Capture explicit requirement evidence and unknowns, especially required versus
preferred qualifications and incomplete descriptions. Review grounded reasoning,
unsupported candidate claims and prompt-injection behavior manually; lexical
matches and passing fake responses cannot establish those properties.

Use held-out useful-to-pursue labels for precision@10, threshold misses and model
comparisons, alongside assessment coverage, latency and spend. The starter CLI
deliberately accepts only its bundled synthetic dataset; adding private evaluation
inputs needs a separate data-handling and provenance design. Do not promote a model
or alter candidate facts from this ten-case contract suite.

## Local verification

Implementation validation used the existing `.venv/bin/python` (Python 3.9.6).
The project declares Python >=3.10; that pre-existing environment mismatch was not
changed. No dependencies or production files were edited, no graph rebuild was
run outside the owned scope, and no installs, paid calls, email/submission actions
or commits were performed.

Focused tests: **32 passed**. Offline demo: **10/10 contracts passed**, four expected
unassessed score errors, two simulated provider failures, one fallback recovery,
11 simulated completion attempts, zero invalid normalized outputs, and zero live
requests. These results demonstrate harness behavior only.

Graft reported approximately 550,144 tokens saved across this worker's queries
(overlapping whole-file baseline estimates, not unique measured savings).
