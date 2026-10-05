# Local benchmark — 5 October 2026

**Measured model run; AI-proposed-label diagnostics only. Human labels: 0.** The coordinator ran ten frozen real jobs through `score_job` using complete retained/API descriptions: 10 valid scores, 0 errors, 10 HTTP requests, 412.17 seconds. This is not a demonstrated improvement over a baseline or a human-validated quality estimate.

- [Original report](2026-10-05-local-original.json): byte-for-byte preserved.
- [Original checkpoints](2026-10-05-local-checkpoints.jsonl): start, ten rows, completion; rows and telemetry match the original report exactly.
- [Derived report](2026-10-05-local-derived.json): same recorded rows, recomputed threshold metrics; **no additional generation or HTTP**.

Original SHA-256: `ce47ca3c0b97f43ac02b5a008ddbdbb02701c8ba36dca9f50f4152a884570005`.
Frozen cohort SHA-256: `6839b8f967163fb8ae559615aec464723877f33e0fc9902e6b896150cdf64ae8`.

## Scores

| ID | Employer | AI-proposed label | Score | Split |
|---|---|---|---:|---|
| real-01 | gravity9 | decline | 45 | tuning |
| real-03 | Monzo | pursue | 72 | heldout |
| real-04 | Predium | decline | 45 | tuning |
| real-05 | F H Bertling | pursue | 72 | heldout |
| real-06 | Nucs AI | decline | 35 | heldout |
| real-07 | Lemrock | unknown | 45 | tuning |
| real-08 | Cabify | decline | 45 | heldout |
| real-09 | HelloFresh | decline | 25 | heldout |
| real-10 | Stripe | decline | 45 | heldout |
| real-12 | Datadog | decline | 15 | heldout |

Monzo and F H Bertling are the two proposed pursue cases, both at 72; Monzo remains provisional because direct large-scale A/B ownership is not established in the CV evidence. Lemrock remains unknown and is excluded from label-dependent denominators. No frozen label was changed after seeing scores.

## Measured execution

Requested model: `qwen3.8:27b`. Response-reported served model: `qwen3.8:27b` on all ten requests; this is server attribution, not independent model-weight verification. Local endpoint: `http://127.0.0.1:11434/v1`; reasoning effort `none`, zero retries/fallbacks. All jobs used full private input, not repository excerpts; retained backlog descriptions remain completeness-unverified.

Reported usage: **35,624 input tokens; 4,731 output tokens**, with no missing usage records. **Cost unknown**, not zero. Row latencies ranged from 32.12 to 50.60 seconds; elapsed run time was 412.17 seconds. Limits were ten actual HTTP attempts, 1,500 output tokens/request, 120 seconds/request, 600 seconds/run, and stop after two consecutive errors. There were no unattempted selected IDs.

## AI-only derived metrics

At threshold 65, selected precision is **2/2** and recall **2/2**; false negatives are **0/2** against proposed pursue labels. One unknown and zero errors are excluded. Pairwise accuracy is **14/14** across the two proposed positives and seven negatives. These are small-sample agreement measurements against AI proposals, not population accuracy.

Precision at `min(10,n)` is **2/9** (22.2%); here it includes every assessed known label and equals cohort prevalence, so it is not evidence of ranking discrimination. On the selected heldout subset (seven known labels), threshold precision/recall are **2/2**, pairwise agreement **10/10**, and precision at k is **2/7**. Tuning contains two known declines and one unknown, so positive recall and pairwise accuracy are undefined; no tuning row crosses threshold.

Human-grounded precision/recall/pairwise metrics are **null** because no human labels exist. Cap, score-range and exclusion/high-score flags are empty; only one explicit research-cap case is present (Datadog, score 15, cap 60). This is not proof all candidate rules or production filters are correct.

## Provenance and limits

**Legacy hash limitation:** the original harness collected on-disk code hashes only after scoring, and that file changed during the run to add derived metrics/provenance. Its stored harness hash does not establish the exact imported revision. Preserve the original values; the derived report explicitly marks this limitation and fingerprints the metric implementation used for recomputation. The current harness captures import/start/end source snapshots and flags source changes, but cannot retroactively supply those snapshots for this run.

The ten-case subset was selected before output from the 12-job convenience cohort (3 tuning / 7 heldout in this run). Employer/dedup groups are disjoint, but the collection period is shared; there is no temporal holdout, blind human adjudication, repeated-run uncertainty estimate or baseline comparison. The cohort contains only two proposed positives, and omitted Wheely/Fin unknown cases were not scored. Availability and full pipeline application eligibility were not evaluated.

Results contain hashes, scores and telemetry, not CV/contact text, full job advertisements, raw prompts, completions or private reasoning. Complete source descriptions remain outside the repository results directory.

## Recompute without generation

```sh
python -m evals.real_job_fit.derive \
  --original evals/real_job_fit/results/2026-10-05-local-original.json \
  --checkpoints evals/real_job_fit/results/2026-10-05-local-checkpoints.jsonl \
  --output /tmp/real-job-derived.json
python -m pytest tests/test_real_job_fit.py -q
```
