# Frozen real Europe job-fit evaluation

This package exercises the production `src.scoring.score_job` against 12 real postings. The frozen cohort contains **AI-proposed labels, not human gold**: two pursue, seven decline, and three unknown. No ranking-improvement claim follows from the offline checks. The coordinator owns the separately authorized local benchmark and its interpretation.

- [Completed local benchmark and limitations](results/README.md)
- [Cohort and provenance](dataset/cohort.json)
- [Human review packet](dataset/HUMAN_REVIEW.md)
- Runner: `python -m evals.real_job_fit`
- Offline tests: `python -m pytest tests/test_real_job_fit.py -q`

## Evidence and privacy

Seven descriptions came from the read-only `/tmp/job-reliability-oct05/tracker-snapshot.sqlite3` scoring backlog. Five came from selected Greenhouse single-job detail endpoints. Collection used **six HTTP requests total**: five successful public detail reads and one Grafana 404; no board scan, retries, or redirects. The failed detail is logged in the manifest and is not a labeled case. Retained jobs were not rechecked for current availability.

The distributed JSON holds short requirement excerpts, URLs, retrieval timestamps, publication-date provenance, job/description hashes, original tracker identities, employer/dedup/time groups, historical outcomes, proposed labels, and null human labels. An API update timestamp is never substituted for an unknown publication date. Backlog observation timestamps establish retained retrieval evidence, not independently verified employer publication dates.

Complete retained descriptions and public API responses stay **private/local** under `/tmp/job-followup-oct05/real-job-private/`. The scoring input file there is `full-inputs.json`; its complete job payloads and descriptions are hash-checked against the frozen manifest. The repository excerpts are marked `snippet_only` and `evaluation_excerpt`. An excerpt run must not be described as a full-ad evaluation. Raw CV text, applicant contacts, prompts and model reasoning are not copied into the dataset, review packet or ordinary result artifacts. Candidate evidence is summarized and source files are fingerprinted. Do not publish the private directory without checking rights and privacy.

## Selection and label limits

This is a purposive convenience cohort, not a representative sample of all European jobs or a recall benchmark. It includes marketplace/product/experimentation possibilities, requirements beyond documented experience, German-language friction, seniority exclusions, and a foundation-model research cap case. Historical tracker scores/statuses are observational metadata, not labels. No live model scores were used to choose the proposed labels.

Tuning has four jobs and heldout has eight. Employer, dedup and employer-month groups do not cross splits. Both sets share the collection period, so this is **not an out-of-time test**. Labels were frozen before model scoring, but the label author inspected the heldout descriptions; this is not blind human adjudication. Do not tune against heldout outputs and then describe them as untouched evaluation evidence.

The candidate facts come from `cv/base_cv*.md` and config. The config's approximate 1.5 years of DS experience is not silently extended using internships or thesis duration. Missing evidence remains unknown; advertised language alone is not a mandatory-language requirement. A positive proposal means worth human review, not proof every requirement is met. Monzo's large-scale A/B ownership remains a documented uncertainty.

Human review must be recorded separately, with `decision` (`pursue`, `decline`, or `unknown`), `reviewer`, `reviewed_at`, and `evidence` reflecting an actual response. Preserve the original proposed label and version/hash; do not manufacture human labels or mutate the frozen cohort during an active benchmark. A reviewed copy/new version can populate `human_label` later.

## Reproduce without network

From the repository root, using the existing environment (no installation required):

```sh
/Users/luisgimenez/job_search/.venv/bin/python -m pytest tests/test_real_job_fit.py -q
/Users/luisgimenez/job_search/.venv/bin/python -m evals.real_job_fit --mode offline --output /tmp/real-job-contract.json
/Users/luisgimenez/job_search/.venv/bin/python -m evals.real_job_fit --mode dry-run --output /tmp/real-job-dry-run.json
```

`offline` invokes real `score_job`, prompt construction, JSON extraction and schema checks using a scripted completion; it produces **contract evidence only** and suppresses ranking metrics. `dry-run` builds and fingerprints prompts without generation. The tests prohibit socket connections and cover malformed output, planted job-text answers, provider failure, snippet context, split integrity, input tampering and request limits. Current error status is `SCORING_PENDING`, including at threshold zero; the old skill reference to `DIGEST` is superseded.

To exercise the privately retained complete context offline:

```sh
/Users/luisgimenez/job_search/.venv/bin/python -m evals.real_job_fit --mode offline --full-inputs /tmp/job-followup-oct05/real-job-private/full-inputs.json --output /tmp/real-job-full-contract.json
```

## Opt-in bounded local execution

The command below documents the coordinator's approved, preselected ten-case run. **Do not rerun it as part of offline validation.** Explicit `--mode live --allow-local-live`, endpoint and model are required. No paid provider, production pipeline, tracker writes, applications or messaging are invoked by this package.

```sh
/Users/luisgimenez/job_search/.venv/bin/python -m evals.real_job_fit \
  --mode live --allow-local-live \
  --endpoint http://127.0.0.1:11434/v1 --model qwen3.8:27b \
  --max-requests 10 --max-tokens 1500 --timeout 120 --run-seconds 600 \
  --stop-after-errors 2 \
  --full-inputs /tmp/job-followup-oct05/real-job-private/full-inputs.json \
  --case-ids real-01 real-03 real-04 real-05 real-06 real-07 real-08 real-09 real-10 real-12 \
  --output /tmp/job-followup-oct05/real-job-live.json
```

The thin adapter inherits production JSON validation, sends one HTTP request per generation attempt, disables proxy environment use and redirects, and only accepts explicit HTTP loopback endpoints. `localhost` is converted to a loopback literal. It does not use API credentials, SDK retries, native-schema compatibility retries or remote fallbacks. `reasoning_effort` is `none`. Limits are 10 actual HTTP attempts for the documented run, 1,500 output tokens/request (15,000 requested maximum in aggregate), 64,000 UTF-8 input bytes/request, 120 seconds/request, 600 seconds for the run, and stop after two consecutive scoring errors. Input bytes are a separate bound, not a claimed exact input-token count. Server enforcement of its output limit is not assumed to prove actual usage; reported tokens are retained, missing usage remains unknown.

Per-request wall-clock deadlines use POSIX `SIGALRM` on the main thread (the intended macOS/Linux CLI environment). Use a new output filename per run. Live mode fsyncs an append-only `OUTPUT.jsonl` start record and each completed row, so interrupted runs retain partial evidence; `--checkpoint` overrides that path. A complete report is written to `--output`. A deadline/error stop can leave unattempted IDs; successful process exit alone does not mean all selected jobs were scored. Each case uses only its newly recorded request telemetry; a preflight rejection has zero case HTTP requests, unknown served model and no prior request telemetry. The served model is taken from the response, or unknown; `score_model_attribution` additionally records the scorer's requested-model fallback and must not be mistaken for verified served identity.

## Metrics and interpretation

Live reports separate `proposed_label_diagnostics` from `human_grounded_metrics`, each for all/tuning/heldout. Until actual human review exists, human precision and pairwise accuracy are null.

- Precision uses `k = min(10, n)` where `n` is the number of successfully scored, known-label cases. Unknown labels and failures are excluded from its denominator; their counts remain visible. This conditional metric can look favorable despite low coverage, so read the coverage fields with it.
- Selected-at-threshold precision divides known pursue cases scoring at least the threshold by all successfully scored known-label cases selected at that threshold. Recall divides those true positives by all successfully scored known pursue cases. Both exclude unknowns/errors and expose denominators. When `n <= 10`, precision at k is simply assessed cohort prevalence; pairwise and threshold metrics carry more discrimination information.
- False negatives are known pursue cases with a valid score below the unchanged configured threshold (65 at freeze). Errors are **unassessed**, listed separately, never negative labels or valid zero scores.
- Pairwise accuracy compares known pursue versus decline cases, giving ties half credit; unknowns and errors are excluded. Pair counts and ties are reported.
- Cap violations measure valid scores above an evidence-tagged configured cap. Caps are prompt instructions, not deterministic numeric enforcement. Exclusion/high-score diagnostics expose semantic/filter tension; a high scorer value alone does not establish application eligibility or prove a filter bug.
- Errors, unattempted IDs, actual requests, per-job/request latency, reported input/output tokens and unknown usage are retained. Cost remains unknown for local execution; absent billing data is not zero cost.
- Dataset, labels, CV, config, candidate rules, system prompt, per-job prompt/input and relevant code fingerprints are retained. In an isolated folder copy without `.git`, revision is explicitly unavailable and source hashes identify the available source. Import-time, run-start and run-end source snapshots expose edits during a long run through `source_changed_during_run` and `source_changed_since_import`; runtime monkeypatches are not fingerprinted. The already-running initial coordinator benchmark imported the earlier harness, whose hash was collected only after scoring: preserve its report and identify that harness hash as post-run/unverified for the exact loaded revision. Later changes affected derived metrics/provenance only, not scoring prompts or transport.

The harness scores exclusions intentionally to expose ranking behavior. It is not a production filtering/freshness, duplicate-application or submission-state replay. It neither changes thresholds nor compares a tuned candidate against a baseline. Claims of ranking improvement require model-backed comparisons on the same heldout inputs and actual human-grounded labels.
